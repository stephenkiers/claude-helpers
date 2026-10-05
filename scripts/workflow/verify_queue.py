"""
Deterministic verify-queue sync.

Scripted form of the "Sync: Discover and Queue Rulings" section of commands/verify-queue.md, so
/cleanup can enqueue a merged branch's pending rulings without an LLM interpreting prose.

Discovery is read-only over ~/.claude/reviews/<repo-key>/*/claude-action-plan.md; the only write is
an append-with-dedup to <worktree-parent>/verify-queue.jsonl.
"""

import fcntl
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import project, worktrees

QUEUE_FILENAME = "verify-queue.jsonl"
PLAN_FILENAME = "claude-action-plan.md"

_STATUS_RE = re.compile(r"^\s*-\s+\*\*STATUS\*\*:\s*(pending-decision|pending-measurement)\s*$")
_COMMAND_RE = re.compile(r"^\s*-\s+\*\*Command\*\*:\s*(.+?)\s*$")
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.*\S)\s*$")
_ITEM_NUMBER_RE = re.compile(r"^\d+\.\s*")
# Review dirs are "<branch-slug>-<short-sha>-<YYYYMMDDTHHMMSS>-<pid>".
_REVIEW_SUFFIX_RE = re.compile(r"-[0-9a-f]{7,}-\d{8}T\d{6}-\d+$")

_KIND_BY_STATUS = {"pending-decision": "your-call", "pending-measurement": "measurement"}


@dataclass
class Ruling:
    review_id: str
    slug: str
    kind: str
    summary: str
    command: str
    plan: str

    def row_id(self, repo_key: str) -> str:
        return f"{repo_key}/{self.review_id}::{self.slug}"


class ParsePlanResult(list):  # type: ignore[type-arg]
    """List of Ruling objects with skipped_headings count.

    Subclasses list for backward compatibility: iterating over this object
    yields Ruling objects, and the skipped_headings count is accessible as an attribute.
    """
    def __init__(self, rulings: List[Ruling], skipped_headings: int = 0):
        super().__init__(rulings)
        self.skipped_headings: int = skipped_headings


@dataclass
class SyncResult:
    queue: str
    repo_key: str
    scanned_plans: int = 0
    added: List[str] = field(default_factory=list)
    already_queued: List[str] = field(default_factory=list)
    open_total: int = 0
    skipped_headings: int = 0

    def to_dict(self) -> Dict[str, object]:
        return {
            "success": True,
            "queue": self.queue,
            "repo_key": self.repo_key,
            "scanned_plans": self.scanned_plans,
            "added": self.added,
            "already_queued": self.already_queued,
            "open_total": self.open_total,
            "skipped_headings": self.skipped_headings,
        }


def slugify(title: str) -> str:
    """Kebab-case slug of a finding title (alphanumerics only, no leading/trailing dashes)."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:80].strip("-") or "finding"


def branch_slug(branch: str) -> str:
    """Branch name as it appears in review dir names ('/' becomes '-')."""
    return branch.replace("/", "-")


def review_matches_branch(review_id: str, branch: str) -> bool:
    """True when review_id is exactly '<branch-slug>-<sha>-<ts>-<pid>' for this branch."""
    m = _REVIEW_SUFFIX_RE.search(review_id)
    return m is not None and review_id[: m.start()] == branch_slug(branch)


def parse_plan(text: str, review_id: str, plan_path: str) -> ParsePlanResult:
    """Extract rulings whose STATUS is still pending-* from one claude-action-plan.md.

    Returns a ParsePlanResult (list-like object containing Ruling objects) with
    a skipped_headings attribute counting headings that were parsed but didn't have
    both title and status.
    """
    rulings: List[Ruling] = []
    title: Optional[str] = None
    status: Optional[str] = None
    command = ""
    seen: Dict[str, int] = {}
    skipped_headings: int = 0
    heading_seen: bool = False

    def flush() -> None:
        nonlocal skipped_headings, heading_seen
        if not heading_seen:
            return
        if title is None or status is None:
            skipped_headings += 1
            return
        slug = slugify(title)
        seen[slug] = seen.get(slug, 0) + 1
        if seen[slug] > 1:
            slug = f"{slug}-{seen[slug]}"
        kind = _KIND_BY_STATUS[status]
        rulings.append(Ruling(
            review_id=review_id,
            slug=slug,
            kind=kind,
            summary=title,
            command=command if kind == "measurement" else "",
            plan=plan_path,
        ))

    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            title, status, command = None, None, ""
            heading_seen = False
            if len(heading.group(1)) == 3:
                title = _ITEM_NUMBER_RE.sub("", heading.group(2)).strip()
                heading_seen = True
            continue
        if title is None:
            continue
        s = _STATUS_RE.match(line)
        if s:
            status = s.group(1)
            continue
        c = _COMMAND_RE.match(line)
        if c and not command:
            command = c.group(1).strip().strip("`").strip()
    flush()
    return ParsePlanResult(rulings, skipped_headings)


def resolve_repo_key(cwd: Optional[Path] = None) -> Optional[str]:
    """owner-name from gh, falling back to the project root's directory name."""
    identity, _ = project.detect_repo_identity(cwd=cwd)
    if identity:
        return f"{identity[0]}-{identity[1]}"
    root = worktrees.detect_project_root(cwd=cwd)
    return Path(root).name if root else None


def resolve_queue_path(cwd: Optional[Path] = None) -> Optional[Path]:
    """<worktree-parent>/verify-queue.jsonl. Detached worktrees (merge-queue scratch) are ignored."""
    parent = worktrees.detect_worktree_parent(cwd=cwd)
    return Path(parent) / QUEUE_FILENAME if parent else None


def _read_rows(queue: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    if not queue.exists():
        return rows
    for line in queue.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def sync(
    cwd: Optional[Path] = None,
    branch: Optional[str] = None,
    reviews_root: Optional[Path] = None,
    queue: Optional[Path] = None,
    repo_key: Optional[str] = None,
) -> SyncResult:
    """
    Enqueue pending rulings (idempotent). With `branch`, only that branch's reviews are scanned;
    without it, every review for the repo is. Raises RuntimeError if the repo key or queue path
    cannot be resolved. Raises OSError if I/O operations fail (mkdir, file write, fsync).
    """
    key = repo_key or resolve_repo_key(cwd)
    if key is None:
        raise RuntimeError("could not determine repo key")
    queue_path = queue or resolve_queue_path(cwd)
    if queue_path is None:
        raise RuntimeError("could not determine verify-queue location")
    root = (reviews_root or Path.home() / ".claude" / "reviews") / key

    result = SyncResult(queue=str(queue_path), repo_key=key)
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    # One lock across read-dedup-append so concurrent syncs can't double-add a row.
    with open(queue_path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            existing = _read_rows(queue_path)
            known = {r.get("id") for r in existing}

            # Buffer rows to add in memory; write all at once after scanning completes.
            rows_to_add: List[Dict[str, object]] = []

            plans = sorted(root.glob(f"*/{PLAN_FILENAME}")) if root.is_dir() else []
            for plan in plans:
                review_id = plan.parent.name
                if branch and not review_matches_branch(review_id, branch):
                    continue
                result.scanned_plans += 1
                parse_result = parse_plan(plan.read_text(), review_id, str(plan))
                result.skipped_headings += parse_result.skipped_headings
                for ruling in parse_result:
                    rid = ruling.row_id(key)
                    if rid in known:
                        result.already_queued.append(rid)
                        continue
                    row: Dict[str, object] = {
                        "id": rid, "status": "open", "kind": ruling.kind,
                        "summary": ruling.summary, "command": ruling.command,
                        "plan": ruling.plan, "result": "",
                    }
                    rows_to_add.append(row)
                    known.add(rid)
                    result.added.append(rid)

            # Write all rows at once, then flush and fsync.
            for row in rows_to_add:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

            # Compute open_total from in-memory state (existing + newly added rows).
            # Only count rows with status == "open".
            result.open_total = sum(1 for r in existing if r.get("status") == "open") + len(rows_to_add)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

    return result
