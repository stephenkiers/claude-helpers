"""
Principles and the decision log (PLAN.md section 6).

Principles are plain markdown, read whole by the models that need them. The decision log
(`decisions.jsonl` under the repo's state directory) records every resolved question and is what
"what we decided before" on a question card and `principles suggest` are drawn from.
"""

import fcntl
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from scripts.flow3.paths import repo_dir

PERSONAL_SCOPE = "personal"
PROJECT_SCOPE = "project"
SEED_SCOPE = "seed"

# How many equal human answers on one theme make a candidate principle.
REPEAT_THRESHOLD = 3


@dataclass(frozen=True)
class Principle:
    id: str
    title: str
    statement: str
    spirit: str
    revisit_if: str
    scope: str


# -- parsing ---------------------------------------------------------------------------------

_GOAL_HEADING = re.compile(r"^#\s+goal\s*$", re.IGNORECASE)
_PRINCIPLE_HEADING = re.compile(r"^##\s+(.*?)\s*$")
_OTHER_HEADING = re.compile(r"^#{1,6}\s+")
_FIELDS = (
    ("statement", re.compile(r"^\s*statement\s*:\s*(.*)$", re.IGNORECASE)),
    ("spirit", re.compile(r"^\s*spirit\s*:\s*(.*)$", re.IGNORECASE)),
    ("revisit_if", re.compile(r"^\s*revisit\s+if\s*:\s*(.*)$", re.IGNORECASE)),
)


def _split_heading(text: str) -> Tuple[str, str]:
    if ":" in text:
        head, _, title = text.partition(":")
        return head.strip(), title.strip()
    return text.strip(), ""


def parse(text: str, scope: str) -> Tuple[str, List[Principle]]:
    """Parse a principles file into (goal, principles). Missing fields become ""."""
    goal_lines: List[str] = []
    entries: List[Dict[str, Any]] = []
    in_goal = False
    current: Optional[Dict[str, Any]] = None
    field: Optional[str] = None
    for line in text.splitlines():
        if _GOAL_HEADING.match(line):
            in_goal, current, field = True, None, None
            continue
        heading = _PRINCIPLE_HEADING.match(line)
        if heading and not line.startswith("###"):
            in_goal, field = False, None
            pid, title = _split_heading(heading.group(1))
            current = {"id": pid, "title": title, "statement": [], "spirit": [], "revisit_if": []}
            entries.append(current)
            continue
        if _OTHER_HEADING.match(line):
            in_goal, current, field = False, None, None
            continue
        if in_goal:
            goal_lines.append(line)
            continue
        if current is None:
            continue
        matched = False
        for name, pattern in _FIELDS:
            m = pattern.match(line)
            if m:
                field = name
                current[name].append(m.group(1).strip())
                matched = True
                break
        if not matched and field is not None:
            current[field].append(line.strip())
    goal = "\n".join(goal_lines).strip()
    out: List[Principle] = []
    for e in entries:
        if not e["id"]:
            continue
        out.append(Principle(
            id=e["id"], title=e["title"],
            statement=" ".join(p for p in e["statement"] if p).strip(),
            spirit=" ".join(p for p in e["spirit"] if p).strip(),
            revisit_if=" ".join(p for p in e["revisit_if"] if p).strip(),
            scope=scope))
    return goal, out


def _read_optional(path: Optional[Path]) -> str:
    if path is None:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def load(project_file: Optional[Path], personal_file: Optional[Path]) -> Tuple[str, List[Principle]]:
    """Both files are optional. The project wins on an id clash, and its goal wins when non-empty."""
    p_goal, p_list = parse(_read_optional(project_file), PROJECT_SCOPE)
    u_goal, u_list = parse(_read_optional(personal_file), PERSONAL_SCOPE)
    seen = {p.id for p in p_list}
    merged = list(p_list) + [p for p in u_list if p.id not in seen]
    return (p_goal or u_goal), merged


def load_for(ctx: Any) -> Tuple[str, List[Principle]]:
    """
    The principles that apply to a flow: the repo's file and the personal one. The repo's file is the
    one in the repo when it exists, else the one kept outside it (repoconfig.external_principles_file).
    """
    from scripts.flow3 import repoconfig
    project = Path(ctx.repo_root) / ctx.cfg.principles
    if not project.is_file():
        project = repoconfig.external_principles_file(Path(ctx.repo_root))
    personal = Path.home() / ".claude" / "principles.md"
    return load(project, personal)


def as_text(goal: str, principles: List[Principle]) -> str:
    """Render in the file format (what the resolver model reads, and what `starter` writes)."""
    parts: List[str] = []
    if goal.strip():
        parts.append("# Goal\n" + goal.strip() + "\n")
    for p in principles:
        block = ["## %s: %s" % (p.id, p.title)]
        block.append("Statement: %s" % p.statement)
        block.append("Spirit: %s" % p.spirit)
        block.append("Revisit if: %s" % p.revisit_if)
        parts.append("\n".join(block) + "\n")
    return "\n".join(parts)


# -- seed themes -----------------------------------------------------------------------------

SEED_THEMES: List[Principle] = [
    Principle(
        "P-docs-follow-code", "Docs follow code",
        "When code and an ADR or doc disagree, fix the doc in the same change unless that changes direction.",
        "Documentation that contradicts the code is worse than none; keep them moving together.",
        "The disagreement means the code is wrong, or fixing the doc would change the project's direction.",
        SEED_SCOPE),
    Principle(
        "P-smallest-change", "Smallest change that meets the ticket",
        "Do what the ticket asks and nothing else; everything else becomes a filed follow-up, never silently included.",
        "A small change is easy to review, easy to revert, and easy to trust.",
        "The ticket cannot be met without the wider change.",
        SEED_SCOPE),
    Principle(
        "P-fail-closed", "Fail closed",
        "Model absent as absent, not as empty. Validate strictly at boundaries.",
        "An unknown or missing input should stop the work, not pass as a harmless default.",
        "Failing closed would block a user for a case that is provably harmless.",
        SEED_SCOPE),
    Principle(
        "P-extend-what-exists", "Extend what exists",
        "Reuse the shared helper or module; enforce invariants at the lowest layer (the type or the "
        "datastore); keep one source for a constant.",
        "One place to read and one place to fix keeps the codebase coherent.",
        "The existing helper does not fit and bending it would make it worse for its current users.",
        SEED_SCOPE),
    Principle(
        "P-tests-prove-behaviour", "Tests prove behaviour",
        "Tests are strengthened, never weakened; prefer real evidence over throwaway proofs.",
        "A test is only worth having if it fails when the behaviour breaks.",
        "The test pins an implementation detail the ticket is deliberately changing.",
        SEED_SCOPE),
    Principle(
        "P-bounded-budgets", "Bounded budgets",
        "Use explicit timeouts and limits, and say what is not safe to cancel.",
        "Unbounded work eventually becomes the incident.",
        "The work is inherently unbounded and the bound would lose data.",
        SEED_SCOPE),
]


def starter(goal: str, chosen: List[Principle]) -> str:
    """The text of a starter principles file that `parse` reads back."""
    return as_text(goal, chosen)


# -- the decision log ------------------------------------------------------------------------

def _log_path(repo: str) -> Path:
    return repo_dir(repo) / "decisions.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_decision(repo: str, record: Dict[str, Any]) -> None:
    """Append one record to the repo's decision log, under an exclusive lock."""
    row: Dict[str, Any] = {"overruled": False}
    row.update(record)
    row.setdefault("ts", _now())
    line = json.dumps(row, sort_keys=True) + "\n"
    with open(_log_path(repo), "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_decisions(repo: str) -> List[Dict[str, Any]]:
    """Every record, in the order written. Unreadable lines are skipped."""
    path = _log_path(repo)
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _option_label(record: Dict[str, Any], option_id: str) -> str:
    for opt in record.get("options") or []:
        if isinstance(opt, dict) and opt.get("id") == option_id:
            return str(opt.get("label") or "")
    return ""


def mark_overruled(repo: str, flow_name: str, question_id: str, new_answer: str, reason: str) -> None:
    """Record that the human overruled an earlier answer (the merge gate uses this)."""
    original: Optional[Dict[str, Any]] = None
    for row in read_decisions(repo):
        if row.get("flow") == flow_name and row.get("id") == question_id and not row.get("overruled"):
            original = row
    base: Dict[str, Any] = dict(original) if original else {"flow": flow_name, "id": question_id}
    label = _option_label(base, new_answer)
    base.update({
        "ts": _now(), "answer": new_answer, "answer_text": label or new_answer,
        "source": "human", "reason": reason, "verbatim": "", "overruled": True,
    })
    append_decision(repo, base)


def _superseded(rows: List[Dict[str, Any]]) -> Set[Tuple[Any, Any]]:
    """(flow, id) pairs that a later overrule record replaced."""
    return {(r.get("flow"), r.get("id")) for r in rows if r.get("overruled")}


def prior_decisions(repo: str, themes: List[str], limit: int = 5) -> List[Dict[str, Any]]:
    """Most recent first: records sharing any theme tag (case-insensitive)."""
    wanted = {str(t).strip().lower() for t in themes if str(t).strip()}
    if not wanted:
        return []
    rows = read_decisions(repo)
    replaced = _superseded(rows)
    out: List[Dict[str, Any]] = []
    for row in reversed(rows):
        if not row.get("overruled") and (row.get("flow"), row.get("id")) in replaced:
            continue
        tags = {str(t).strip().lower() for t in row.get("themes") or []}
        if tags & wanted:
            out.append(row)
            if len(out) >= limit:
                break
    return out


# -- suggestions -----------------------------------------------------------------------------

def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _answer_key(row: Dict[str, Any]) -> str:
    label = _option_label(row, str(row.get("answer") or ""))
    return _norm(label or str(row.get("answer_text") or ""))


def _themes_of(row: Dict[str, Any]) -> List[str]:
    tags = [str(t).strip().lower() for t in row.get("themes") or [] if str(t).strip()]
    return tags


def suggest_candidates(decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deterministic candidate principles, no model. A candidate when the human gave the same answer on
    one theme at least three times, or overruled an assumed or principle answer. The human accepts,
    edits, or drops each one; nothing here becomes a principle by itself.
    """
    out: List[Dict[str, Any]] = []

    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in decisions:
        if row.get("source") != "human":
            continue
        key = _answer_key(row)
        if not key:
            continue
        for theme in _themes_of(row):
            groups.setdefault((theme, key), []).append(row)
    for (theme, _key), rows in sorted(groups.items()):
        if len(rows) >= REPEAT_THRESHOLD:
            text = str(rows[-1].get("answer_text") or "")
            out.append({
                "theme": theme,
                "proposed_statement": "When a question touches '%s', choose: %s." % (theme, text),
                "evidence": list(rows),
                "why": "answered %d times" % len(rows),
            })

    for row in decisions:
        if not row.get("overruled"):
            continue
        original: Optional[Dict[str, Any]] = None
        for other in decisions:
            if (other is not row and not other.get("overruled") and other.get("flow") == row.get("flow")
                    and other.get("id") == row.get("id")):
                original = other
        if original is None:
            continue
        src = str(original.get("source") or "")
        if src != "assumed" and not src.startswith("principle:"):
            continue
        tags = _themes_of(original) or _themes_of(row)
        theme = tags[0] if tags else str(original.get("kind") or "general")
        out.append({
            "theme": theme,
            "proposed_statement": "When a question touches '%s', choose: %s (the %s answer was overruled)."
                                  % (theme, row.get("answer_text") or "", src),
            "evidence": [original, row],
            "why": "overruled",
        })
    return out
