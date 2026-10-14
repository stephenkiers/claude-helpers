#!/usr/bin/env python3
"""Verify step receipts against on-disk artifacts for /expert-flow.

Exit codes:
  0: artifacts OK
  1: receipt admits deviation, artifact missing, or a plan/review checkpoint was not fully relayed
     (fewer questions in <flow-dir>/questions/<step>-*.json than decision briefs / Needs-you items)
  2: bad arguments or internal error (message on stderr)
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, NoReturn, Optional

# Constants for timeouts and mtime skew tolerance
MTIME_SKEW_SECS = 5
GIT_TIMEOUT_SECS = 5

# Contribution failure reasons
CONTRIB_ABSENT = "absent"
CONTRIB_UNTERMINATED = "unterminated"
CONTRIB_STANDIN = "stand-in"

# A decision brief heading in a plan session's decisions.md (`#### Q1. …`, `#### Q2 — …`).
BRIEF_HEADING_RE = re.compile(r"^####\s+Q\d+\b", re.MULTILINE)
# A *Needs you* item heading in claude-action-plan.md (`### 1. Title`).
NEEDS_YOU_ITEM_RE = re.compile(r"^###\s+\d+\.", re.MULTILINE)
NEEDS_YOU_HEADING_RE = re.compile(r"^##\s+Needs you\b.*$", re.MULTILINE)


def error(msg: str) -> NoReturn:
    """Print error to stderr and exit with code 2."""
    print(msg, file=sys.stderr)
    sys.exit(2)


def read_file(path: Path) -> str:
    """Read file contents; error on missing/unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        error(f"Could not read {path}: {e}")
    except (OSError, UnicodeDecodeError) as e:
        error(f"Could not read {path}: {e}")


def canonicalize_path(path: str) -> Path:
    """Canonicalize a path (symlinks and .. resolved). Containment is checked by callers."""
    return Path(path).expanduser().resolve()


def scan_for_deviation(content: str) -> bool:
    """Scan for Deviation: or NOT dispatched outside fenced blocks, blockquotes, backticks.

    Returns True if deviation found (should reject).

    Accepts list/bold prefixes before Deviation: (e.g. "- Deviation:", "**Deviation:**").
    Tracks code fences by marker character and length (``` vs ~~~). Treats an unterminated
    fence as malformed (flags deviation).
    """
    lines = content.split("\n")
    in_fence = False
    fence_marker = ""
    fence_length = 0

    i = 0
    while i < len(lines):
        line = lines[i]

        # Handle fenced code blocks (triple backticks or tildes)
        # Match opening fence: at least 3 backticks or tildes, possibly with language tag
        fence_match = re.match(r"^(\s*)(```+|~~~+)", line)
        if fence_match:
            marker = line[fence_match.start(2):fence_match.end(2)]
            marker_char = marker[0]
            marker_len = len(marker)

            if not in_fence:
                in_fence = True
                fence_marker = marker_char
                fence_length = marker_len
            elif fence_marker == marker_char and marker_len >= fence_length:
                in_fence = False
                fence_marker = ""
                fence_length = 0
            i += 1
            continue

        # If inside fence, skip
        if in_fence:
            i += 1
            continue

        # If unterminated fence at EOF, treat as malformed
        if i == len(lines) - 1 and in_fence:
            return True

        # Check for blockquote line (starts with >)
        if line.lstrip().startswith(">"):
            i += 1
            continue

        # Check for Deviation: at line start, possibly with list/bold prefix
        # Patterns: "Deviation:", "- Deviation:", "* Deviation:", "**Deviation:**"
        if re.match(r"^(\s*[-*]\s+)?(\*\*)?Deviation:", line):
            return True

        # Check for NOT dispatched outside backticks
        # Remove text inside single-backtick spans
        cleaned_line = re.sub(r"`[^`]*`", "", line)
        if "NOT dispatched" in cleaned_line:
            return True

        i += 1

    # Check if fence was left unterminated at end
    if in_fence:
        return True

    return False


def check_plan(receipt_file: Path, since: float, home: Path, flow_dir: Optional[Path]) -> Optional[str]:
    """Check plan artifacts. Return error reason or None."""
    receipt = read_file(receipt_file)

    # Get FINAL_PLAN_PATH from last matching line
    plan_path_str = None
    for line in receipt.split("\n"):
        if line.startswith("FINAL_PLAN_PATH:"):
            plan_path_str = line.split(":", 1)[1].strip()

    if not plan_path_str:
        return "plan artifacts missing (FINAL_PLAN_PATH not under plans/ or no invocation id)"

    plan_path = canonicalize_path(plan_path_str)
    plans_dir = (home / ".claude" / "plans").resolve()

    # Check path is under plans/
    try:
        plan_path.relative_to(plans_dir)
    except ValueError:
        return "plan artifacts missing (FINAL_PLAN_PATH not under plans/ or no invocation id)"

    # Extract invocation ID from basename
    basename = plan_path.name
    # Pattern: {slug}-{YYYYMMDDTHHMMSS}-{NNNNN}.md
    match = re.search(r"-(\d{8}T\d{6}-\d{5})\.md$", basename)
    if not match:
        return "plan artifacts missing (FINAL_PLAN_PATH not under plans/ or no invocation id)"

    invocation_id = match.group(1)

    # Find session dir: glob ~/.claude/plan-sessions/*/*-{invocation_id}
    session_root = home / ".claude" / "plan-sessions"
    matches = []
    if session_root.exists():
        for dir_path in session_root.glob(f"*/*-{invocation_id}"):
            if dir_path.is_dir():
                matches.append(dir_path)

    if len(matches) != 1:
        return f"plan artifacts missing (session dir: {len(matches)} matches for {invocation_id})"

    session_dir = matches[0]

    # Check context.md mtime
    context_file = session_dir / "context.md"
    if not context_file.exists():
        return "plan artifacts missing (session dir older than this attempt)"

    try:
        context_mtime = context_file.stat().st_mtime
        if context_mtime < since - MTIME_SKEW_SECS:
            return "plan artifacts missing (session dir older than this attempt)"
    except OSError:
        return "plan artifacts missing (session dir older than this attempt)"

    # Check selected-experts.md
    roster_file = session_dir / "selected-experts.md"
    if not roster_file.exists():
        return "plan artifacts missing (roster)"

    roster = read_file(roster_file)

    # Parse roster: find first table with header's first cell = "Expert"
    roster_rows = _parse_roster_table(roster)
    if not roster_rows:
        return "plan artifacts missing (roster)"

    # Load reviewer index
    index_file = home / ".claude" / "reviewers" / "index.yaml"
    reviewer_map = _load_reviewer_index(index_file) if index_file.exists() else {}

    # Check contributions
    for row in roster_rows:
        # Skip non-contributors
        if row.lower().startswith("auditor") or row.lower().startswith("synthesis"):
            continue

        # Strip trailing (annotation)
        name = re.sub(r"\s*\([^)]*\)$", "", row).strip()

        # Resolve name to file stem
        file_stem = reviewer_map.get(name.lower())
        if not file_stem:
            # Fallback: name lowercased, non-alphanumerics -> '-'
            file_stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

        # Try {stem}-contribution.md or kebab-name form
        kebab = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        contrib_file = session_dir / f"{file_stem}-contribution.md"
        if not contrib_file.exists():
            contrib_file = session_dir / f"{kebab}-contribution.md"
        if not contrib_file.exists():
            return f"plan artifacts missing (contribution: {name} {CONTRIB_ABSENT})"

        contrib = read_file(contrib_file)

        # Check sentinel: last non-empty line
        lines = [ln for ln in contrib.split("\n") if ln.strip()]
        if not lines or lines[-1] != "<!-- contribution-end -->":
            return f"plan artifacts missing (contribution: {name} {CONTRIB_UNTERMINATED})"

        # Check for stand-in
        if _is_standin(contrib):
            return f"plan artifacts missing (contribution: {name} {CONTRIB_STANDIN})"

    # Checkpoint completeness: every decision brief in decisions.md must have been put to the
    # human through the relay. decisions.md is written before the first Ask (even with zero
    # briefs), so its absence means the checkpoint never ran.
    decisions_file = session_dir / "decisions.md"
    if not decisions_file.exists():
        return "plan artifacts missing (decisions.md)"
    briefs = count_briefs(read_file(decisions_file))
    asked = count_relayed_questions(flow_dir, "plan")
    if asked < briefs:
        return f"plan checkpoint incomplete (briefs: {briefs}, asked: {asked})"

    return None


def check_review(receipt_file: Path, since: float, home: Path, flow_dir: Optional[Path]) -> Optional[str]:
    """Check review artifacts. Return error reason or None."""
    receipt = read_file(receipt_file)

    # Get REVIEW_DIR from last matching line
    review_dir_str = None
    for line in receipt.split("\n"):
        if line.startswith("REVIEW_DIR:"):
            review_dir_str = line.split(":", 1)[1].strip()

    if not review_dir_str:
        return "review artifacts missing (REVIEW_DIR not under reviews/)"

    review_dir = canonicalize_path(review_dir_str)
    reviews_root = (home / ".claude" / "reviews").resolve()

    # Check path is under reviews/
    try:
        review_dir.relative_to(reviews_root)
    except ValueError:
        return "review artifacts missing (REVIEW_DIR not under reviews/)"

    # Check required files
    for filename in ["claude-action-plan.md", "final-report.md", "findings.json"]:
        if not (review_dir / filename).exists():
            return f"review artifacts missing ({filename})"

    # Check claude-action-plan.md mtime
    action_plan = review_dir / "claude-action-plan.md"
    try:
        mtime = action_plan.stat().st_mtime
        if mtime < since - MTIME_SKEW_SECS:
            return "review artifacts missing (action plan older than this attempt)"
    except OSError:
        return "review artifacts missing (action plan older than this attempt)"

    # Check for reviewer checkpoints: >=2 *-pass1.md or *-pod.md
    checkpoints = list(review_dir.glob("*-pass1.md")) + list(review_dir.glob("*-pod.md"))
    if len(checkpoints) < 2:
        return f"review artifacts missing (reviewer checkpoints: {len(checkpoints)})"

    # Rulings completeness: every *Needs you* item must have been put to the human through the relay.
    needs_you = count_needs_you_items(read_file(action_plan))
    asked = count_relayed_questions(flow_dir, "review")
    if asked < needs_you:
        return f"review rulings incomplete (needs-you: {needs_you}, asked: {asked})"

    return None


def count_briefs(decisions: str) -> int:
    """Count `#### Q<n>` decision-brief headings in a plan session's decisions.md."""
    return len(BRIEF_HEADING_RE.findall(decisions))


def count_needs_you_items(action_plan: str) -> int:
    """Count `### N.` items under the `## Needs you` heading of claude-action-plan.md.

    The section ends at the next `## ` heading. No section means zero items.
    """
    m = NEEDS_YOU_HEADING_RE.search(action_plan)
    if not m:
        return 0
    rest = action_plan[m.end():]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    section = rest[: nxt.start()] if nxt else rest
    return len(NEEDS_YOU_ITEM_RE.findall(section))


def count_relayed_questions(flow_dir: Optional[Path], step: str) -> int:
    """Sum `questions[]` across `<flow_dir>/questions/<step>-*.json`.

    An unknown flow dir, a missing questions/ dir, or an unparseable file counts as zero asked:
    the check fails closed when briefs or items exist and nothing provably reached the human.
    """
    if flow_dir is None:
        return 0
    total = 0
    for qfile in sorted((flow_dir / "questions").glob(f"{step}-*.json")):
        try:
            data = json.loads(qfile.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        questions = data.get("questions") if isinstance(data, dict) else None
        if isinstance(questions, list):
            total += len(questions)
    return total


def resolve_flow_dir(explicit: Optional[str], receipt_file: Path) -> Optional[Path]:
    """`--flow-dir` when given; else the receipt's grandparent when the receipt sits in `steps/`."""
    if explicit:
        return canonicalize_path(explicit)
    receipt = receipt_file.resolve()
    if receipt.parent.name == "steps":
        return receipt.parent.parent
    return None


def _count_commits(repo: str, base: str, branch: str) -> int:
    """Count commits from base to branch. Return commit count; error on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", repo, "rev-list", "--count", "--end-of-options", base + ".." + branch],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            error(f"Could not count commits: {result.stderr.strip()}")

        return int(result.stdout.strip())
    except subprocess.TimeoutExpired as e:
        error(f"Git operation failed: {e}")
    except ValueError as e:
        error(f"Git operation failed: {e}")


def check_implement(
    receipt_file: Path,
    repo: str,
    branch: str,
    base: str,
) -> Optional[str]:
    """Check implement artifacts. Return error reason or None."""
    count = _count_commits(repo, base, branch)
    if count < 1:
        return f"implement artifacts missing (no new commits on {branch} since step began)"

    return None


def check_fix(
    receipt_file: Path,
    repo: str,
    branch: str,
    base: str,
    review_dir: str,
    home: Path,
) -> Optional[str]:
    """Check fix artifacts with zero-commit carve-out. Return error reason or None."""
    count = _count_commits(repo, base, branch)

    # If zero commits, check for zero "Doing it" items in action plan
    if count == 0:
        action_plan_path = canonicalize_path(review_dir)
        action_plan_path = action_plan_path / "claude-action-plan.md"
        reviews_root = (home / ".claude" / "reviews").resolve()

        # Check path is under reviews/
        try:
            action_plan_path.relative_to(reviews_root)
        except ValueError:
            return f"fix artifacts missing (no new commits on {branch} since step began)"

        if not action_plan_path.exists():
            return f"fix artifacts missing (no new commits on {branch} since step began)"

        action_plan = read_file(action_plan_path)
        doing_it_count = _count_doing_it_items(action_plan)

        if doing_it_count >= 1:
            return f"fix artifacts missing (no new commits on {branch} since step began)"

    return None


def _parse_iso8601(iso_str: str) -> float:
    """Parse ISO-8601 UTC timestamp to float (epoch seconds).

    Handles formats like "2026-10-14T22:20:13Z" and "2026-10-14T22:20:13+00:00"
    with or without timezone offsets.
    """
    try:
        # Normalize trailing Z to +00:00 for fromisoformat
        normalized = iso_str.rstrip("Z")
        if normalized == iso_str:
            # Z was not present, so no normalization needed
            dt = datetime.fromisoformat(iso_str)
        else:
            # Z was present, add +00:00
            normalized = normalized + "+00:00"
            dt = datetime.fromisoformat(normalized)

        # Ensure timezone-aware, defaulting to UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.timestamp()
    except ValueError:
        raise ValueError(f"Could not parse ISO timestamp: {iso_str}")


def _parse_roster_table(content: str) -> List[str]:
    """Parse markdown roster table and return list of Expert names."""
    rows: List[str] = []
    header_seen = False

    for line in content.split("\n"):
        # Table lines start with |
        if not line.strip().startswith("|"):
            if rows:  # Found table, return it
                return rows
            continue

        # Parse table row
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if not cells:
            continue

        if not header_seen:
            # Check if first cell is "Expert"
            if cells[0].lower() == "expert":
                header_seen = True
            continue

        # Skip the separator row, collect the rest
        if cells[0] and set(cells[0]) <= set("-: "):
            continue
        if header_seen and cells:
            rows.append(cells[0])

    return rows if rows else []


def _load_reviewer_index(index_file: Path) -> Dict[str, str]:
    """Load reviewer index YAML and return {name.lower(): file_stem} mapping.

    Parses simple YAML format with "- name:" and "file:" pairs. On parse errors
    or missing file, returns an empty dict (fallback behavior).
    """
    result: Dict[str, str] = {}
    current_name: Optional[str] = None
    try:
        content = index_file.read_text(encoding="utf-8")
        for line in content.split("\n"):
            # Parse lines like: "- name: Danielle the Designer"
            name_match = re.search(r'^\s*-\s+name:\s*(.+)$', line)
            if name_match:
                current_name = name_match.group(1).strip()
                continue

            # Parse lines like: "file: danielle-designer.yaml"
            file_match = re.search(r'^\s+file:\s*(.+?)\.yaml\s*$', line)
            if file_match and current_name is not None:
                file_stem = file_match.group(1).strip()
                result[current_name.lower()] = file_stem
                current_name = None
    except OSError:
        pass

    return result


def _is_standin(content: str) -> bool:
    """Check if contribution is a stand-in (## Decision / FAILED or Decision: FAILED)."""
    lines = [ln.strip() for ln in content.split("\n")]

    # Check for ## Decision followed by FAILED
    for i, line in enumerate(lines):
        if line == "## Decision":
            # Look for next non-empty line
            for j in range(i + 1, len(lines)):
                if lines[j]:
                    if lines[j] == "FAILED":
                        return True
                    break

    # Check for inline Decision: FAILED
    for line in lines:
        if line == "Decision: FAILED":
            return True

    return False


def _count_doing_it_items(action_plan: str) -> int:
    """Count items in the 'Doing it' section by parsing table rows.

    Parses the "## Doing it: N" section, bounded by the next ## heading.
    Counts data rows in the table (excluding header and separator rows).
    Returns 0 if section not found (fail closed).

    Use with --from verify to check against the real claude-action-plan.md template.
    """
    lines = action_plan.split("\n")
    count = 0
    in_doing_it = False
    table_started = False

    i = 0
    while i < len(lines):
        line = lines[i]

        # Match "## Doing it:" heading (could have :N at the end)
        if re.match(r"^##\s+Doing it:", line):
            in_doing_it = True
            table_started = False
            i += 1
            continue

        # Stop at next ## heading (same or higher level)
        if in_doing_it and re.match(r"^##\s+", line):
            break

        if in_doing_it:
            stripped = line.strip()

            # Check for table start (lines starting with |)
            if stripped.startswith("|") and not table_started:
                table_started = True
                i += 1
                continue

            # In table: count data rows, skip header and separator
            if table_started and stripped.startswith("|"):
                # Skip separator rows (all dashes and pipes)
                if not re.match(r"^\|\s*-+\s*(\|\s*-+\s*)*\|$", stripped):
                    count += 1
            elif table_started and not stripped.startswith("|"):
                # Table ended
                table_started = False

        i += 1

    # If section never found, fail closed
    if not in_doing_it:
        return 0

    return count


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Verify step receipts against on-disk artifacts"
    )
    parser.add_argument(
        "--step",
        required=True,
        choices=["plan", "review", "implement", "fix", "track", "verify", "ship", "merge"],
        help="Step name",
    )
    parser.add_argument("--receipt", required=True, help="Path to receipt file")
    parser.add_argument("--since", required=True, help="ISO-8601 UTC timestamp")
    parser.add_argument("--repo", help="Repo directory (for implement/fix)")
    parser.add_argument("--branch", help="Branch name (for implement/fix)")
    parser.add_argument("--base", help="Base SHA (for implement/fix)")
    parser.add_argument("--review-dir", help="Review directory (for fix)")
    parser.add_argument(
        "--flow-dir",
        help="Flow directory holding questions/ (plan/review); default: the receipt's grandparent when it sits in steps/",
    )

    args = parser.parse_args()

    # Parse --since once; bad value exits with code 2
    try:
        since_ts = _parse_iso8601(args.since)
    except ValueError as e:
        error(str(e))

    # Deviation scan applies to all steps
    receipt_file = Path(args.receipt)
    if not receipt_file.exists():
        error(f"Receipt file not found: {args.receipt}")

    receipt = read_file(receipt_file)
    if scan_for_deviation(receipt):
        print("Reason: receipt admits deviation")
        sys.exit(1)

    # Home directory for artifact roots
    home = Path.home().resolve()

    # Step-specific checks
    error_reason: Optional[str] = None

    flow_dir = resolve_flow_dir(args.flow_dir, receipt_file)

    if args.step == "plan":
        error_reason = check_plan(receipt_file, since_ts, home, flow_dir)
    elif args.step == "review":
        error_reason = check_review(receipt_file, since_ts, home, flow_dir)
    elif args.step == "implement":
        if not all([args.repo, args.branch, args.base]):
            error("--repo, --branch, --base required for implement")
        # Validate base as hex SHA (7-40 chars)
        if not re.match(r"^[0-9a-f]{7,40}$", args.base):
            error(f"Invalid base SHA: {args.base}")
        error_reason = check_implement(receipt_file, args.repo, args.branch, args.base)
    elif args.step == "fix":
        if not all([args.repo, args.branch, args.base, args.review_dir]):
            error("--repo, --branch, --base, --review-dir required for fix")
        # Validate base as hex SHA (7-40 chars)
        if not re.match(r"^[0-9a-f]{7,40}$", args.base):
            error(f"Invalid base SHA: {args.base}")
        error_reason = check_fix(
            receipt_file, args.repo, args.branch, args.base, args.review_dir, home
        )
    elif args.step in ["track", "verify", "ship", "merge"]:
        # No artifact checks
        error_reason = None

    if error_reason:
        print(f"Reason: {error_reason}")
        sys.exit(1)

    print("ARTIFACTS: ok")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        error(f"Unexpected error: {e}")
