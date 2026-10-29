#!/usr/bin/env python3
"""Verify step receipts against on-disk artifacts for /expert-flow.

The attempt-start bound for --since-from-log is derived from the flow's log.md by code (scripts/flow_log.py)
and is never copied by the model. Where it is used: `implement-ledger.py locate --flow-dir --step` in the
expert-flow Fidelity audit, and `--since-from-log` in the expert-flow resume path's rule-8 validation.
Why it matters: a bound that is too early or wrong lets a stale run pass the audit (a false pass that
masks a skipped step), and a bound that is too late or missing gives a false `ledger unavailable`.
Design: ADR-0013 (deterministic logic lives in scripts, not prose) and ADR-0023 (the receipt is a claim
that the Fidelity audit exists to check).

Exit codes:
  0: artifacts OK
  1: receipt admits deviation, artifact missing, or a plan/review checkpoint was not fully relayed
     (counting is disk-authoritative and scoped by attempt: questions in files matching
     questions/<step>-NN.json whose attempt equals the receipt's ATTEMPT, or a
     reused-answers marker names, counted only when their answers/<step>-NN.json exists).
     With --since-from-log, a missing or malformed begin/retry line in log.md is exit 1.
  2: bad arguments or internal error (message on stderr). With --since-from-log, an absent or
     invalid flow dir is exit 2.
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, NamedTuple, NoReturn, Optional, Set, Tuple

# Import shared schema module
try:
    import plan_checkpoint_schema as schema
except ImportError:
    # If not in path, add scripts directory
    sys.path.insert(0, str(Path(__file__).parent))
    import plan_checkpoint_schema as schema

# Shared leaf module (log.md attempt-start parsing and ISO-8601); same pattern as plan_checkpoint_schema.
try:
    import flow_log
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    import flow_log

# Constants for timeouts and mtime skew tolerance
MTIME_SKEW_SECS = 5
GIT_TIMEOUT_SECS = 5

# Implement/fix check outcome (#338): the receipt's CHECK value for an explicit opt-out. The resolver
# source that must back it is OPT_OUT_SOURCE, imported from scripts/workflow/checks.py in the gate.
CHECK_OPTED_OUT = "opted-out"

# Contribution failure reasons
CONTRIB_ABSENT = "absent"
CONTRIB_UNTERMINATED = "unterminated"
CONTRIB_STANDIN = "stand-in"

# Question file pattern
QUESTION_FILE_RE = r"^(?P<step>[a-z0-9-]+)-(?P<seq>\d{2})\.json$"

# Checkpoint incompleteness reason strings (printed with parenthesized context)
REASON_PLAN_INCOMPLETE = "plan checkpoint incomplete"
REASON_REVIEW_INCOMPLETE = "review rulings incomplete"

# Artifact freshness reason strings (used when mtime checks fail)
REASON_PLAN_OLDER_FIRST = "plan artifacts missing (session dir older than this step's first attempt)"
REASON_PLAN_OLDER_ATTEMPT = "plan artifacts missing (session dir older than this attempt)"
REASON_REVIEW_OLDER_FIRST = "review artifacts missing (action plan older than this step's first attempt)"
REASON_REVIEW_OLDER_ATTEMPT = "review artifacts missing (action plan older than this attempt)"

# Suffixes for relay incompleteness
SUFFIX_NO_ANSWERED_FMT = ", no answered {step}-* question files for attempt {attempt}"
SUFFIX_FLOW_DIR_UNRESOLVED = ", flow dir unresolved"
SUFFIX_UNANSWERED = ", unanswered: "
SUFFIX_MALFORMED = ", malformed: "
SUFFIX_OUT_OF_ATTEMPT = ", out-of-attempt: "


class RelayCount(NamedTuple):
    """Result of counting relayed questions from disk, scoped by attempt.

    Relayed questions are counted from disk: a `questions/<step>-NN.json` counts when its
    `attempt` equals the receipt's `ATTEMPT` (or a `reused-answers` marker names it) and its
    `answers/<step>-NN.json` exists; a question file without `attempt` does not count, and an
    `awaiting-answers` marker whose answers file is missing is reduced.

    Fields:
    - asked: sum of len(questions[]) across selected files with answers/ existing
    - files: list of selected question file basenames (the files that were scoped and counted)
    - unanswered: basenames of selected files without an answers/ file
    - malformed: list of "<basename> (<problem>)" strings for schema-invalid files (including
      files that fail to read or parse). Gating: any entry fails the check closed.
    - out_of_attempt: basenames named by awaiting-answers markers but from a different attempt.
      Gating: any entry fails the check closed.
    - markers: count of distinct marker-named basenames (diagnostic only)
    """
    asked: int
    files: List[str]
    unanswered: List[str]
    malformed: List[str]
    out_of_attempt: List[str]
    markers: int


class FoldRecord(NamedTuple):
    """A single entry in the fold of report answers files.

    disposition: the answer disposition (e.g., "answered", "accepted", "unresolved")
    item_data: the full item dict from the answers file
    source_file: basename of the answers file (e.g., "plan-02.json")
    round_num: the round number from the answers file
    loop_end: the loop_end value from the answers file (e.g., "all-answered", "continue")
    """
    disposition: Optional[str]
    item_data: Dict
    source_file: str
    round_num: int
    loop_end: Optional[str]


class OutstandingItem(NamedTuple):
    """A parsed OUTSTANDING: line item.

    Attributes:
        unit_id: The unit identifier (e.g., 'Step 1', 'D1')
        reason: The reason for outstanding status
        provenance: Either 'self-report' or 'both' (Literal type)
    """
    unit_id: str
    reason: str
    provenance: Literal["self-report", "both"]


# A decision brief heading in a plan session's decisions.md (`#### Q1. …`, `#### Q2 — …`).
# Captures the Q number as group 1 to allow deduplication.
BRIEF_HEADING_RE = re.compile(r"^####\s+Q(\d+)\b", re.MULTILINE)
# A *Needs you* item heading in claude-action-plan.md (`### 1. Title`).
NEEDS_YOU_ITEM_RE = re.compile(r"^###\s+\d+\.", re.MULTILINE)
NEEDS_YOU_HEADING_RE = re.compile(r"^##\s+Needs you\b.*$", re.MULTILINE)
# OUTSTANDING: line format: <unit id> (<reason>) [provenance]
OUTSTANDING_ITEM_RE = re.compile(r"^(.+?)\s+\((.+?)\)\s+\[([^\]]+)\]$")
# ROUND 1 section header in an implement/fix receipt (#341, item 5; producer contract in
# commands/implement-with-haiku.md). Tolerant of the real header shapes: `ROUND 1 — Implementer
# (parallel)`, `ROUND 1 - ...`, `ROUND 1: units ...`, `ROUND 1+2 complete ...`, a bare `ROUND 1`, and
# markdown-marked `**Round 1** — ...` / `## ROUND 1: ...`. The line must START with the header (optional
# indent and markdown markers), and `1` must be followed by a separator (`—` `–` `:` `+` `-`) or end of
# line (`(` also counts when a digit follows, as in `ROUND 1 (3 units: ...)`). The marker group
# `(?:[#>-][ \t]*|\*[ \t]+)*` avoids ambiguous asterisk distribution: a `*` marker must be followed by
# whitespace, so bare `*` characters are unambiguously part of the following `\**`, preventing quadratic
# backtracking. So prose that merely contains "round 1", and status-table lines such as `Round 1 join: ...` or
# `Round 1 (implement): ...`, do not match. A line that matches is a section header only; whether round 1
# actually ran is receipt rule 7's job. CRLF line endings are handled by the explicit `\r` in the trailing
# character class `[ \t\r]*`.
ROUND1_HEADER_PATTERN = r"(?im)^[ \t]*(?:[#>-][ \t]*|\*[ \t]+)*\**ROUND[ \t]*1\**[ \t\r]*(?:[—–:+-]|\((?=\d)|$)"
ROUND1_HEADER_RE = re.compile(ROUND1_HEADER_PATTERN)
# Markdown table separator row (allow :?-+:? per cell)
MARKDOWN_SEPARATOR_RE = re.compile(r"^\|\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|$")


def _is_markdown_separator_row(line: str) -> bool:
    """Check if a line is a markdown table separator row (e.g., | --- | --- |)."""
    return bool(MARKDOWN_SEPARATOR_RE.match(line))


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


def validate_question_file(path: Path, step: str) -> Tuple[Optional[dict], Optional[str]]:
    """Validate a question file's schema. Return (data, None) if valid, or (None, problem).

    Problems (short, fixed strings):
    - "bad name": filename doesn't match <step>-NN.json
    - "not json": file is not valid JSON
    - "step mismatch": JSON's "step" field doesn't equal the step argument
    - "no attempt field": non-report file missing integer "attempt" >= 1
    - "bad questions": "questions" field is not a non-empty list (for non-report files), or a
      question entry is malformed (not a dict, missing id/question, or id not of the form qN)
    - "bad round": a "kind": "report" file whose "round" is not an int >= 1
    - "duplicate id": question ids are not unique

    For "kind": "report" files, validates "attempt" and "round" as ints >= 1.
    For non-report files, validates "attempt" as int >= 1 and "questions" as non-empty list.
    """
    # Check filename
    if not re.match(QUESTION_FILE_RE, path.name):
        return (None, "bad name")

    # Parse JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return (None, "not json")

    # Must be a dict
    if not isinstance(data, dict):
        return (None, "not json")

    # Check step field
    if data.get("step") != step:
        return (None, "step mismatch")

    # Check if report kind
    kind = data.get("kind")
    if kind == "report":
        # Report files: validate attempt and round as ints >= 1
        attempt = data.get("attempt")
        round_num = data.get("round")
        if not isinstance(attempt, int) or attempt < 1:
            return (None, "no attempt field")
        if not isinstance(round_num, int) or round_num < 1:
            return (None, "bad round")
        return (data, None)

    # Non-report files: validate attempt and questions
    attempt = data.get("attempt")
    if not isinstance(attempt, int) or attempt < 1:
        return (None, "no attempt field")

    questions = data.get("questions")
    if not isinstance(questions, list) or len(questions) == 0:
        return (None, "bad questions")

    # Check that all questions have id and question field, and ids are unique
    seen_ids = set()
    for q in questions:
        if not isinstance(q, dict):
            return (None, "bad questions")
        if "id" not in q or "question" not in q:
            return (None, "bad questions")
        q_id = q.get("id")
        if not isinstance(q_id, str) or not re.match(r"^q\d+$", q_id):
            return (None, "bad questions")
        if q_id in seen_ids:
            return (None, "duplicate id")
        seen_ids.add(q_id)

    return (data, None)


def _receipt_attempt(receipt: str) -> Optional[int]:
    """Extract the attempt from the receipt's `ATTEMPT:` header lines.

    Only lines that start with `ATTEMPT:` count, and the LAST such line wins. Returns None when
    no line is present, or when the winning line is not an integer >= 1 (callers treat an invalid
    value as an error, never as attempt 1).
    """
    found = False
    attempt: Optional[int] = None
    for line in receipt.split("\n"):
        if line.startswith("ATTEMPT:"):
            found = True
            attempt_str = line.split(":", 1)[1].strip()
            try:
                parsed = int(attempt_str)
                attempt = parsed if parsed >= 1 else None
            except ValueError:
                attempt = None
    return attempt if found else None


def _receipt_attempt_error(receipt: str) -> bool:
    """True when the receipt has an ATTEMPT: line whose winning value is not an integer >= 1."""
    has_line = any(ln.startswith("ATTEMPT:") for ln in receipt.split("\n"))
    return has_line and _receipt_attempt(receipt) is None


def _resolve_attempt(receipt: str, flow_dir: Optional[Path], step: str) -> Tuple[Optional[int], Optional[str]]:
    """Resolve the attempt to count against. Returns (attempt, error_reason).

    An explicit, valid `ATTEMPT:` line is used as-is. An invalid one is an error. A missing one
    defaults to 1 only when no `<step>-*.json` question file on disk carries attempt > 1;
    otherwise the default would silently count the wrong attempt, so it is an error.
    """
    if _receipt_attempt_error(receipt):
        return (None, "invalid ATTEMPT line in receipt (must be an integer >= 1)")
    explicit = _receipt_attempt(receipt)
    if explicit is not None:
        return (explicit, None)
    if flow_dir is not None:
        qdir = flow_dir / "questions"
        if qdir.is_dir():
            for qfile in qdir.glob(f"{step}-*.json"):
                try:
                    data = json.loads(qfile.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, ValueError):
                    continue
                if isinstance(data, dict):
                    att = data.get("attempt")
                    if isinstance(att, int) and not isinstance(att, bool) and att > 1:
                        return (None, f"receipt has no ATTEMPT: line but {qfile.name} is attempt {att}")
    return (1, None)


def _step_floor(flow_dir: Optional[Path], step: str, since: float) -> Tuple[float, bool]:
    """Get the first-attempt identity floor for a step.

    Reads flow_dir / "log.md" and finds the newest line matching "^(\\S+) <step> begin$" or
    "^(\\S+) <step> redo( .*)?$", parsing the timestamp with _parse_iso8601. A redo line starts a
    new identity for the step (the human asked for a fresh attempt), so once one exists it
    supersedes the first-attempt `begin` line as the floor; `retry` lines never move the floor.
    Returns (min(floor_ts, since), True) if found.

    Fallback (no flow dir, no log.md, no matching line, or parse error): returns (since, False),
    which is exactly today's rule and never more permissive.

    Returns (floor_timestamp, from_log_md).
    """
    if flow_dir is None:
        return (since, False)

    log_file = flow_dir / "log.md"
    if not log_file.exists():
        return (since, False)

    try:
        log_content = log_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return (since, False)

    # Find the newest line matching "^(\S+) <step> begin$"
    newest_begin_ts = None
    for line in log_content.split("\n"):
        line = line.rstrip()
        # Pattern: timestamp (ISO-8601) followed by step and "begin"
        match = re.match(rf"^(\S+) {re.escape(step)} (?:begin|redo(?: .*)?)$", line)
        if match:
            iso_str = match.group(1)
            try:
                ts = _parse_iso8601(iso_str)
                if newest_begin_ts is None or ts > newest_begin_ts:
                    newest_begin_ts = ts
            except ValueError:
                # Unparseable timestamp, skip
                pass

    if newest_begin_ts is None:
        return (since, False)

    return (min(newest_begin_ts, since), True)


def _load_answers_file(path: Path) -> Optional[Dict]:
    """Load and parse an answers JSON file. Return None on error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _load_reaction_file(path: Path) -> Optional[Dict]:
    """Load and parse a reaction JSON file. Return None on error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return None
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _validate_reply_excerpt(excerpt: str, reply_file: Path) -> bool:
    """Check if excerpt is present in reply file (whitespace-normalized).

    Args:
        excerpt: The excerpt to validate
        reply_file: Path to the .reply-<round>.md file

    Returns:
        True if excerpt is found in file, False otherwise
    """
    if not reply_file.exists():
        return False
    try:
        reply_text = reply_file.read_text(encoding="utf-8")
        normalized_excerpt = schema.normalize_whitespace(excerpt)
        normalized_reply = schema.normalize_whitespace(reply_text)
        return normalized_excerpt in normalized_reply
    except (OSError, UnicodeDecodeError):
        return False


def _fold_report_answers(
    adir: Path, attempt: int
) -> Tuple[Dict[str, FoldRecord], Optional[str]]:
    r"""Fold report answers files by SEQ order into a handle disposition dict.

    Parses and validates all answers files in a single pass with fail-closed policy:
    - Rejects files not matching plan-\d+\.json pattern (F1)
    - Rejects unparseable or duplicate SEQ numbers (F1)
    - Rejects unreadable or non-dict files (F2)
    - Validates shape for all kind == "report" files (F3)
    - Rejects falsy dispositions (F12)

    Returns (fold_dict, error_msg). fold_dict maps handle -> FoldRecord.
    error_msg is set on any unreadable, shape, or validation error.
    """
    fold: Dict[str, FoldRecord] = {}

    if not adir.exists():
        return fold, None

    # F1: Filter glob to plan-\d+\.json, reject unparseable or duplicate SEQ
    pattern = re.compile(r'^plan-(\d+)\.json$')
    answer_files_raw = list(adir.glob("plan-*.json"))

    # Validate and extract SEQ numbers for matching files only
    seq_map: Dict[int, Path] = {}
    answer_files_filtered = []
    for answer_file in answer_files_raw:
        match = pattern.match(answer_file.name)
        if not match:
            # F1: Skip non-matching filenames silently (they're not plan-\d+.json format)
            continue

        try:
            seq = int(match.group(1))
        except ValueError:
            return {}, f"plan checkpoint incomplete (invalid answer file SEQ: {answer_file.name})"

        if seq in seq_map:
            return {}, f"plan checkpoint incomplete (duplicate answer file SEQ {seq}: {answer_file.name}, {seq_map[seq].name})"
        seq_map[seq] = answer_file
        answer_files_filtered.append(answer_file)

    # Sort by SEQ order (numeric sort)
    answer_files = sorted(answer_files_filtered, key=lambda f: schema.answer_file_sort_key(f.name))

    for answer_file in answer_files:
        try:
            answers_data = json.loads(answer_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            # F2: Fail closed on unreadable files
            return {}, f"plan checkpoint incomplete (unreadable answers file: {answer_file.name})"

        if not isinstance(answers_data, dict):
            # F2: Fail closed on non-dict files
            return {}, f"plan checkpoint incomplete (unreadable answers file: {answer_file.name})"

        kind = answers_data.get("kind")
        file_attempt = answers_data.get("attempt")

        # Only process files for this attempt
        if file_attempt != attempt:
            continue

        if kind == "report":
            # F3: Validate shape for all report-kind files
            shape_err = schema.validate_answers_file_shape(answers_data, "report")
            if shape_err:
                return {}, f"plan checkpoint incomplete ({shape_err}: {answer_file.name})"

            items = answers_data.get("items", {})
            if not isinstance(items, dict):
                return {}, f"plan checkpoint incomplete (malformed answers file: {answer_file.name})"

            round_num = answers_data.get("round")
            loop_end = answers_data.get("loop_end")

            for handle, item in items.items():
                if isinstance(item, dict):
                    disposition = item.get("disposition")
                    # F12: Reject items with falsy disposition
                    if not disposition:
                        return {}, f"plan checkpoint incomplete (answer file item missing disposition: {handle} in {answer_file.name})"
                    # Fold: later file overwrites earlier one for same handle
                    fold[handle] = FoldRecord(disposition, item, answer_file.name, round_num, loop_end)

    return fold, None


def _check_excerpt_validity(
    handle: str, fold_record: FoldRecord, adir: Path
) -> Optional[str]:
    """Check excerpt validity for any handle using the fold record.

    fold_record is a FoldRecord(disposition, item_data, source_file, round_num, loop_end).
    Returns error reason or None.

    Implements W1 and Q2: branches on source, fails closed for prose.
    F5: Checks reply-file and round validity for all non-form fold records (not just must-answer).
    F8: Whitelists source to {"form", "prose"}, rejects unknown values.
    """
    disposition, item_data, source_file, round_num, loop_end = fold_record

    if not isinstance(item_data, dict):
        return None

    reply_excerpt = item_data.get("reply_excerpt")
    source = item_data.get("source")

    # Common: empty or missing excerpt is an error
    if not reply_excerpt:
        return f"plan checkpoint incomplete (must-answer without quote: {handle})"

    # F8: Whitelist source values to {"form", "prose"}; missing source defaults to prose
    if source is None:
        source = "prose"  # Default to prose for backward compatibility with legacy answers
    elif source not in {"form", "prose"}:
        return f"plan checkpoint incomplete (unknown source value '{source}': {handle})"

    # Branch on source
    if source == "form":
        # W1: form answers don't need reply-file check, only non-empty excerpt check
        return None
    else:
        # Q2 and default: prose check - strict excerpt binding
        # Extract SEQ from source_file (e.g., "plan-02.json" -> "02")
        seq_match = re.match(r'plan-(\d+)', source_file)
        if not seq_match:
            return None

        seq = seq_match.group(1)
        reply_filename = f"plan-{seq}.reply-{round_num}.md"
        reply_file = adir / reply_filename

        # F5: Round must be a positive int (restored for all non-form records)
        if not (isinstance(round_num, int) and not isinstance(round_num, bool) and round_num > 0):
            return f"plan checkpoint incomplete (answers file round not a positive integer: {source_file})"

        # F5: Reply file must exist (restored for all non-form records)
        if not reply_file.exists():
            return f"plan checkpoint incomplete (reply file missing: {reply_filename})"

        # Excerpt must be in the reply file
        if not _validate_reply_excerpt(reply_excerpt, reply_file):
            return f"plan checkpoint incomplete (reply excerpt not found in {reply_filename})"

    return None


# Post-tier ledger handle: a D/W/Q letter followed by digits (D1, W1, Q1, ...).
_LEDGER_HANDLE_RE = re.compile(r"[DWQ]\d+")


def _ledger_handles(ledger: dict) -> Dict[str, dict]:
    """Return {handle: data} for every post-tier handle in the checkpoint ledger.

    A key is a handle only if it is a string matching `_LEDGER_HANDLE_RE` and its value is a
    dict. Empty or non-matching keys are simply not handles (never an error).
    """
    return {
        key: data
        for key, data in ledger.items()
        if isinstance(key, str) and _LEDGER_HANDLE_RE.fullmatch(key) and isinstance(data, dict)
    }


def _ledger_handle_keys(ledger: dict) -> set:
    """Return the set of post-tier handle keys, regardless of value type.

    Used by the legacy fail-closed in check_plan: any D/W/Q-shaped key fails closed, even when
    its value is malformed. The dict requirement in `_ledger_handles` applies only where values
    are validated (the report branch).
    """
    return {key for key in ledger if isinstance(key, str) and _LEDGER_HANDLE_RE.fullmatch(key)}


def _relay_anomaly_suffix(relay_count: RelayCount) -> str:
    """Reason suffix naming malformed and out-of-attempt question files; "" when there are none.

    Both anomalies are gating: a question file that is schema-invalid, or answered but from a
    different attempt than the receipt's, may be a question that never reached the human, so a
    check that finds either fails closed instead of counting the remaining valid files.
    """
    out = ""
    if relay_count.malformed:
        out += f"{SUFFIX_MALFORMED}{', '.join(relay_count.malformed)}"
    if relay_count.out_of_attempt:
        out += f"{SUFFIX_OUT_OF_ATTEMPT}{', '.join(relay_count.out_of_attempt)}"
    return out


def _relay_shortfall_suffix(
    relay_count: RelayCount, flow_dir: Optional[Path], step: str, attempt: int, has_items: bool
) -> str:
    """Reason suffix for a relay shortfall, after the `asked: N` count and before the closing paren.

    `has_items` is whether the checkpoint expects at least one relayed item (briefs for plan,
    needs-you items for review). The flow-dir-unresolved note only applies when there are items.
    """
    suffix = ""
    if flow_dir is None and has_items:
        suffix += SUFFIX_FLOW_DIR_UNRESOLVED
    elif len(relay_count.files) == 0:
        suffix += SUFFIX_NO_ANSWERED_FMT.format(step=step, attempt=attempt)
    if relay_count.unanswered:
        suffix += f"{SUFFIX_UNANSWERED}{', '.join(relay_count.unanswered)}"
    suffix += _relay_anomaly_suffix(relay_count)
    return suffix


def _last_header_value(receipt: str, header: str) -> Optional[str]:
    """Value of the last `<header>:` line in the receipt, stripped; None when no such line."""
    value = None
    for line in receipt.split("\n"):
        if line.startswith(f"{header}:"):
            value = line.split(":", 1)[1].strip()
    return value or None


def _is_under_root(path: Path, root: Path) -> bool:
    """True when the resolved path sits under the resolved root."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sorted_plan_answer_files(adir: Path) -> List[Path]:
    """plan-*.json files in an answers dir, in SEQ order."""
    return sorted(adir.glob("plan-*.json"), key=lambda f: schema.answer_file_sort_key(f.name))


def check_report_checkpoint(
    session_dir: Path, flow_dir: Path, decisions_text: str, attempt: int = 1, relay_count: Optional[RelayCount] = None
) -> Optional[str]:
    """Check report-kind plan checkpoint. Return error reason or None.

    Implements rules 1-10 from Step 5.

    Args:
        session_dir: Path to the plan session directory
        flow_dir: Path to the flow directory
        decisions_text: Content of decisions.md from the session
        attempt: The attempt number (from receipt)
        relay_count: Optional RelayCount from the receipt (for scoping verification)
    """
    # Load the checkpoint ledger (F6: use schema module)
    ledger = schema.extract_ledger_json(decisions_text)
    if not ledger:
        return "plan checkpoint incomplete (no checkpoint ledger in decisions.md)"

    # Validate ledger shape
    ledger_err = schema.validate_ledger_shape(ledger)
    if ledger_err:
        return f"plan checkpoint incomplete ({ledger_err})"

    seated = ledger.get("seated", [])
    expected_reactions = ledger.get("expected_reactions", [])
    briefs = ledger.get("briefs", {})

    if not isinstance(seated, list):
        seated = []
    if not isinstance(expected_reactions, list):
        expected_reactions = []

    # Rule 7a: Check that contrarian-carl is in seated list
    if "contrarian-carl" not in seated:
        return "plan checkpoint incomplete (missing contrarian-carl from seated list)"

    # Fail with a named cause before the handle/report comparison below
    nested_reason = schema.nested_handle_map_reason(ledger)
    if nested_reason:
        return f"plan checkpoint incomplete ({nested_reason})"
    entry_reason = schema.validate_handle_entries(ledger)
    if entry_reason:
        return f"plan checkpoint incomplete ({entry_reason})"

    # Get all handles from ledger (post-tier handles: D1, W1, Q1, ...)
    ledger_handles_and_tiers = _ledger_handles(ledger)
    ledger_handles = set(ledger_handles_and_tiers.keys())

    # Determine which question files to check: from relay_count.unanswered and answered
    qdir = flow_dir / "questions"
    adir = flow_dir / "answers"

    # NOTE: the marker/ledger handling in this section (and the ledger-handle fail-closed in
    # check_plan below) is slated for rework under #310 and #308. Keep the fail-closed
    # behaviour and the existing reason prefixes stable until those issues land.

    # Fail closed: a marker-named report whose answers file is absent (or whose question file
    # is unreadable) never provably reached the human, wherever a decoy copy may sit.
    if relay_count and relay_count.unanswered:
        return (
            "plan checkpoint incomplete (question files without an answers file: "
            f"{', '.join(sorted(relay_count.unanswered))})"
        )
    anomaly = _relay_anomaly_suffix(relay_count) if relay_count else ""
    if anomaly:
        return f"plan checkpoint incomplete ({anomaly.lstrip(', ')})"

    # Rule 1: Verify we have plan-* question files
    # (reaching this point means relay_count has no unanswered files or anomalies)
    marker_files = set()
    unreadable_answers: List[str] = []
    if relay_count:
        # Also need to check answered files
        if adir.exists():
            answer_files = _sorted_plan_answer_files(adir)
            for answer_file in answer_files:
                # Check if this file has an attempt matching our attempt
                try:
                    adata = json.loads(answer_file.read_text(encoding="utf-8"))
                    if isinstance(adata, dict) and adata.get("attempt") == attempt:
                        marker_files.add(answer_file.name)
                except (OSError, UnicodeDecodeError, ValueError):
                    unreadable_answers.append(answer_file.name)
    else:
        # Without relay_count, fall back to finding all plan-*.json files for this attempt
        if qdir.exists():
            qfiles = sorted(qdir.glob("plan-*.json"), key=lambda f: schema.answer_file_sort_key(f.name))
            for qfile in qfiles:
                try:
                    qdata = json.loads(qfile.read_text(encoding="utf-8"))
                    if isinstance(qdata, dict) and qdata.get("attempt") == attempt:
                        marker_files.add(qfile.name)
                except (OSError, UnicodeDecodeError, ValueError):
                    pass

    if not qdir.exists():
        return "plan checkpoint incomplete (questions directory missing)"

    # Find all report question files for this attempt
    report_files = {}
    for marker in marker_files:
        qfile = qdir / marker
        if not qfile.exists():
            continue
        try:
            data = json.loads(qfile.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("kind") == "report" and data.get("attempt") == attempt:
                report_files[marker] = data
        except (OSError, UnicodeDecodeError, ValueError):
            pass

    if not report_files:
        # An answers file that cannot be read never provably reached the human: name it.
        if unreadable_answers:
            return f"plan checkpoint incomplete (marker files without a readable answered report: {', '.join(sorted(unreadable_answers))})"
        return "plan checkpoint incomplete (no report question files for this attempt)"

    # Rule 2: Check that round-1 report's handle set matches ledger handles
    # (Round-1 is the first report file by numeric SEQ order; every ledger handle must appear in round-1).
    # Union coverage across an attempt (every ledger handle appears in at least one file, and no file
    # holds an unknown handle) is checked separately below.
    round1_basename = None
    if report_files:
        sorted_basenames = sorted(report_files.keys(), key=lambda f: schema.answer_file_sort_key(f))
        round1_basename = sorted_basenames[0]

    if round1_basename:
        round1_data = report_files[round1_basename]
        if not isinstance(round1_data.get("items"), list):
            return f"plan checkpoint incomplete (malformed report file: {round1_basename})"
        report_handles = set()
        for item in round1_data["items"]:
            if isinstance(item, dict) and "handle" in item:
                report_handles.add(item["handle"])

        if report_handles != ledger_handles:
            missing = ledger_handles - report_handles
            extra = report_handles - ledger_handles
            reason = "plan checkpoint incomplete (round-1 handles mismatch"
            if missing:
                reason += f": missing {', '.join(sorted(missing))}"
            if extra:
                reason += f": extra {', '.join(sorted(extra))}"
            reason += ")"
            return reason

    # Step 3: Per-file same-SEQ cause lookup for wrong-shape answers files
    # Run before the fold to catch shape errors early
    if adir.exists():
        answer_files = _sorted_plan_answer_files(adir)
        for answer_file in answer_files:
            # Find same-SEQ question file
            qfile = qdir / answer_file.name
            if not qfile.exists():
                # F2: Fail closed on missing question file
                continue

            try:
                qdata = json.loads(qfile.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                # F2: Fail closed on unreadable question file
                return f"plan checkpoint incomplete (unreadable question file: {answer_file.name})"

            if not isinstance(qdata, dict):
                # F2: Fail closed on non-dict question file
                return f"plan checkpoint incomplete (unreadable question file: {answer_file.name})"

            # Gate: these checks run only when a same-SEQ question file exists (else `continue`
            # above); a missing question file is caught by the coverage checks below.
            # If the question file is a report for this attempt,
            # the answers file must also be a report for this attempt
            if qdata.get("kind") == "report" and qdata.get("attempt") == attempt:
                try:
                    adata = json.loads(answer_file.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, ValueError):
                    # Already handled by _fold_report_answers, but double-check here
                    return f"plan checkpoint incomplete (unreadable answers file: {answer_file.name})"

                if not isinstance(adata, dict):
                    # Already handled by _fold_report_answers
                    return f"plan checkpoint incomplete (unreadable answers file: {answer_file.name})"

                # Step 3: Check required keys (kind first, then attempt), then their values
                if adata.get("kind") is None:
                    # No kind key - might be legacy {"answers": {...}} shape
                    # Validate as report shape to get the specific error
                    shape_err = schema.validate_answers_file_shape(adata, "report")
                    if shape_err:
                        return f"plan checkpoint incomplete ({shape_err}: {answer_file.name})"

                # Wrong values fail closed too: _fold_report_answers silently skips a
                # file with the wrong kind or attempt, which would certify the answer
                # the human replaced.
                file_kind = adata.get("kind")
                if file_kind != "report":
                    return f"plan checkpoint incomplete (report answers file has kind {file_kind!r}, expected 'report': {answer_file.name})"

                if adata.get("attempt") is None:
                    return f"plan checkpoint incomplete (report answers file missing 'attempt' key: {answer_file.name})"

                file_attempt = adata.get("attempt")
                if isinstance(file_attempt, bool) or not isinstance(file_attempt, int) or file_attempt != attempt:
                    return f"plan checkpoint incomplete (report answers file attempt {file_attempt!r} does not match receipt attempt {attempt}: {answer_file.name})"

    # Step 4: Fold pre-pass and coverage checks (F10: parse once, no dead parameters)
    fold, fold_err = _fold_report_answers(adir, attempt)
    if fold_err:
        return fold_err

    # Extract dispositions from fold
    final_dispositions: Dict[str, str] = {}
    for handle, fold_record in fold.items():
        final_dispositions[handle] = fold_record.disposition

    # Rule 3, replaced: Union coverage and per-round must-answer coverage
    # First, check that the first report file (round-1) contains every ledger handle
    if report_files and adir.exists():
        answer_files = _sorted_plan_answer_files(adir)
        for answer_file in answer_files:
            try:
                adata = json.loads(answer_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                # Already validated by _fold_report_answers
                continue

            if not isinstance(adata, dict) or adata.get("attempt") != attempt:
                continue

            if adata.get("kind") == "report":
                items = adata.get("items", {})
                if not isinstance(items, dict):
                    continue

                # Check which question file corresponds to this answer file (same basename)
                qfile = qdir / answer_file.name
                if not qfile.exists():
                    # F2: Fail closed on missing question file
                    return f"plan checkpoint incomplete (unreadable question file: {answer_file.name})"

                try:
                    qdata = json.loads(qfile.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, ValueError):
                    # F2: Fail closed on unreadable question file
                    return f"plan checkpoint incomplete (unreadable question file: {answer_file.name})"

                if not isinstance(qdata, dict) or qdata.get("attempt") != attempt:
                    continue

                # This is a report file. Check round match
                afile_round = adata.get("round")
                qfile_round = qdata.get("round")
                if afile_round != qfile_round:
                    return f"plan checkpoint incomplete (answers file round mismatch: {answer_file.name} round {afile_round}, question file round {qfile_round})"

                # Get must-answer items from this round's question file
                q_items = qdata.get("items", [])
                if isinstance(q_items, list):
                    for q_item in q_items:
                        if not isinstance(q_item, dict):
                            continue
                        handle = q_item.get("handle")
                        must_answer = q_item.get("must_answer", False)
                        reason_must_answer = q_item.get("reason_must_answer")

                        # If this item is must-answer in this round's question file,
                        # it must appear in this round's answer file
                        if (must_answer or reason_must_answer) and handle not in items:
                            return f"plan checkpoint incomplete (answer file missing handle: {handle} in {answer_file.name})"

    # Check that all ledger handles appear in at least one file (union coverage)
    for handle in ledger_handles:
        if handle not in fold:
            return f"plan checkpoint incomplete (answer file missing handle: {handle})"

    # Check that no file holds an unknown handle (subset of ledger)
    for handle in fold.keys():
        if handle not in ledger_handles:
            # We know which file holds this unknown handle from the fold
            fold_record = fold[handle]
            return f"plan checkpoint incomplete (unknown handle in {fold_record.source_file}: {handle})"

    # Rule 4: Check for unresolved dispositions
    for handle, disposition in final_dispositions.items():
        if disposition == "unresolved":
            return f"plan checkpoint incomplete (unresolved: {handle})"

    # Rebuild must-answer info from report files (union across all rounds)
    must_answer_items: Dict[str, bool] = {}
    must_answer_reasons: Dict[str, str] = {}

    for basename, report_data in report_files.items():
        items = report_data.get("items", [])
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            handle = item.get("handle")
            if not handle:
                continue

            must_answer = item.get("must_answer", False)
            reason_must_answer = item.get("reason_must_answer")

            if must_answer or reason_must_answer:
                must_answer_items[handle] = True
                if reason_must_answer:
                    must_answer_reasons[handle] = reason_must_answer

    # Rule 5: Check must-answer items have proper dispositions (use fold)
    for handle in must_answer_items:
        disposition = final_dispositions.get(handle)
        if not disposition:
            return f"plan checkpoint incomplete (must-answer without quote: {handle})"

        if disposition == "accepted":
            return f"plan checkpoint incomplete (must-answer without quote: {handle})"

        if disposition in ["answered", "overridden"]:
            fold_record = fold.get(handle)
            if fold_record:
                excerpt_err = _check_excerpt_validity(handle, fold_record, adir)
                if excerpt_err:
                    return excerpt_err

        if disposition == "accepted-on-continue":
            fold_record = fold.get(handle)
            if not fold_record:
                return f"plan checkpoint incomplete (accepted-on-continue not found: {handle})"

            if fold_record.loop_end != "continue":
                return f"plan checkpoint incomplete (accepted-on-continue without loop_end: {handle})"

            # Check excerpt validity
            excerpt_err = _check_excerpt_validity(handle, fold_record, adir)
            if excerpt_err:
                return excerpt_err

    # F5: Check excerpt validity for ALL non-form records in fold (not just must-answer)
    # Only check records with dispositions that require excerpts: answered, overridden, accepted-on-continue
    for handle, fold_record in fold.items():
        item_data = fold_record.item_data
        source = item_data.get("source")
        disposition = fold_record.disposition

        # Skip form source (they don't need reply file check)
        if source == "form":
            continue

        # Only check dispositions that require excerpts
        if disposition not in ["answered", "overridden", "accepted-on-continue"]:
            continue

        # For all non-form records with excerpt-requiring dispositions, check validity
        excerpt_err = _check_excerpt_validity(handle, fold_record, adir)
        if excerpt_err:
            return excerpt_err

    # Rule 6: Check accepted items are only D or W
    for handle, disposition in final_dispositions.items():
        if disposition == "accepted":
            handle_entry = ledger_handles_and_tiers.get(handle)
            if handle_entry is None:
                # Handle not in ledger at all
                return f"plan checkpoint incomplete (accepted handle not in checkpoint ledger: {handle})"
            tier = handle_entry.get("tier")
            if tier is None:
                # Defense-in-depth secondary check (distinct from schema's early validation);
                # catches tier field missing on accepted items even if early checks missed it.
                return f"plan checkpoint incomplete (checkpoint ledger entry {handle} has no tier)"
            if tier not in schema.ACCEPTABLE_TIERS:
                # Tier exists but is not D or W
                return f"plan checkpoint incomplete (accepted non-D/W: {handle})"

    # Rule 7: Check reaction files exist and are complete
    missing_experts: List[str] = []
    for expert in seated:
        # Check for pre-show round-1
        found = False
        for entry in expected_reactions:
            if (
                isinstance(entry, dict)
                and entry.get("slug") == expert
                and entry.get("mode") == "pre-show"
                and entry.get("round") == 1
            ):
                found = True
                # Check file exists and has "complete": true sentinel
                path_str = entry.get("path")
                if path_str:
                    # F25: Confine path to session dir
                    confine_err = schema.confine_path_to_dir(path_str, session_dir)
                    if confine_err:
                        return f"plan checkpoint incomplete ({confine_err})"

                    path = session_dir / path_str
                    data = _load_reaction_file(path)
                    if not data or not data.get("complete"):
                        return f"plan checkpoint incomplete (reaction file missing or incomplete: {path_str})"
                break

        if not found:
            missing_experts.append(expert)

    if missing_experts:
        return f"plan checkpoint incomplete (missing pre-show reactions: {', '.join(missing_experts)})"

    # Rule 8, 8b: Check for protested D/W items and disagreement D/W items
    # Build evidence from pre-show reactions only
    evidence: Dict[str, Dict[str, str]] = {}

    for entry in expected_reactions:
        if not isinstance(entry, dict):
            continue
        if entry.get("mode") != "pre-show":
            continue

        slug = entry.get("slug")
        path_str = entry.get("path")
        if not slug or not path_str:
            continue

        path = session_dir / path_str
        data = _load_reaction_file(path)
        if not data:
            continue

        # Process each brief_id in the reaction file
        for brief_id, reaction_data in data.items():
            if brief_id == "complete" or not isinstance(reaction_data, dict):
                continue

            # Get the handle for this brief_id from ledger.briefs
            # (we'll check this in Rule 8b)
            brief_type = briefs.get(brief_id, {}).get("brief_type")

            # Classify the reaction using schema module
            classification = schema.classify_reaction(reaction_data, slug)

            # Find the handle(s) for this brief_id (from post-tier ledger)
            for handle, handle_data in ledger_handles_and_tiers.items():
                if handle_data.get("brief_id") == brief_id:
                    if handle not in evidence:
                        evidence[handle] = {}
                    evidence[handle][slug] = classification

    for handle, tier_data in ledger_handles_and_tiers.items():
        tier = tier_data.get("tier")
        brief_id = tier_data.get("brief_id")
        brief_type = tier_data.get("brief_type")

        if tier in ["decided", "worth-a-look"]:
            # Rule 8b: Check that tier is not "disagreement"
            if brief_type == "disagreement":
                return f"plan checkpoint tier mismatch ({handle} is a disagreement)"

            # Rule 8: Check that no reaction says "protested"
            handle_evidence = evidence.get(handle, {})
            for expert, reaction in handle_evidence.items():
                if reaction == "protested":
                    return f"plan checkpoint tier mismatch ({handle} protested by {expert})"

    # Rule 9: Raw-reply files existence is now checked in _check_excerpt_validity (Step 5)

    # Rule 10: Check for repeated pushbacks on same handle by same expert
    pushback_tracker: Dict[tuple, int] = {}  # (expert, handle) -> count

    # F9: First, check that all expected reply-mode reaction files exist and are complete
    for entry in expected_reactions:
        if not isinstance(entry, dict):
            continue
        if entry.get("mode") != "reply":
            continue

        slug = entry.get("slug")
        path_str = entry.get("path")
        if not slug or not path_str:
            continue

        path = session_dir / path_str
        data = _load_reaction_file(path)
        if not data or not data.get("complete"):
            return f"plan checkpoint incomplete (reaction file missing or incomplete: {path_str})"

    # Now process pushback reactions
    for entry in expected_reactions:
        if not isinstance(entry, dict):
            continue
        if entry.get("mode") != "reply":
            continue

        slug = entry.get("slug")
        path_str = entry.get("path")
        if not slug or not path_str:
            continue

        path = session_dir / path_str
        data = _load_reaction_file(path)
        if not data:
            continue

        for handle, reaction_data in data.items():
            if handle == "complete" or not isinstance(reaction_data, dict):
                continue
            if reaction_data.get("pushback") is True:
                key = (slug, handle)
                pushback_tracker[key] = pushback_tracker.get(key, 0) + 1

                if pushback_tracker[key] > 1:
                    return f"plan checkpoint repeated pushback ({slug} on {handle})"

    return None


def check_plan(receipt_file: Path, since: float, home: Path, flow_dir: Optional[Path]) -> Optional[str]:
    """Check plan artifacts. Return error reason or None."""
    receipt = read_file(receipt_file)

    # Get FINAL_PLAN_PATH from last matching line
    plan_path_str = _last_header_value(receipt, "FINAL_PLAN_PATH")

    if not plan_path_str:
        return "plan artifacts missing (FINAL_PLAN_PATH not under plans/ or no invocation id)"

    plan_path = canonicalize_path(plan_path_str)
    plans_dir = (home / ".claude" / "plans").resolve()

    # Check path is under plans/
    if not _is_under_root(plan_path, plans_dir):
        return "plan artifacts missing (FINAL_PLAN_PATH not under plans/ or no invocation id)"

    # Check that FINAL_PLAN_PATH file exists
    if not plan_path.is_file():
        return "plan artifacts missing (FINAL_PLAN_PATH file does not exist)"

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

    # Get the first-attempt identity floor for plan
    floor, from_log = _step_floor(flow_dir, "plan", since)
    floor_reason = REASON_PLAN_OLDER_FIRST if from_log else REASON_PLAN_OLDER_ATTEMPT

    # Check context.md mtime against the floor
    context_file = session_dir / "context.md"
    if not context_file.exists():
        return floor_reason

    try:
        context_mtime = context_file.stat().st_mtime
        if context_mtime < floor - MTIME_SKEW_SECS:
            return floor_reason
    except OSError:
        return floor_reason

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

    # Check that plan.md exists in the session directory
    plan_session_file = session_dir / "plan.md"
    if not plan_session_file.is_file():
        return "plan artifacts missing (plan.md)"

    # Checkpoint completeness: every decision brief in decisions.md must have been put to the
    # human through the relay. decisions.md is written before the first Ask (even with zero
    # briefs), so its absence means the checkpoint never ran.
    decisions_file = session_dir / "decisions.md"
    if not decisions_file.exists():
        return "plan artifacts missing (decisions.md)"

    decisions_text = read_file(decisions_file)

    # Get attempt from receipt
    attempt, attempt_err = _resolve_attempt(receipt, flow_dir, "plan")
    if attempt_err or attempt is None:
        return f"{REASON_PLAN_INCOMPLETE} ({attempt_err})"

    # Count relayed questions, scoped by attempt
    relay_count = count_relayed_questions(flow_dir, "plan", receipt, attempt)

    # Check for report-kind checkpoint: any questions/plan-NN.json with kind: "report" and attempt == ATTEMPT
    is_report_checkpoint = False
    if flow_dir is not None:
        qdir = flow_dir / "questions"
        if qdir.exists():
            for qfile in qdir.glob("plan-*.json"):
                if qfile.is_file():
                    data, _ = validate_question_file(qfile, "plan")
                    if data is not None and data.get("kind") == "report" and data.get("attempt") == attempt:
                        is_report_checkpoint = True
                        break

    if is_report_checkpoint:
        return check_report_checkpoint(session_dir, flow_dir, decisions_text, attempt, relay_count)

    # Fail closed: if ledger carries D/W/Q handles but we didn't take the report branch,
    # or if unreadable is non-empty in the legacy branch.
    # NOTE: this ledger-handle fail-closed is slated for rework under #310 and #308; keep the
    # "plan checkpoint incomplete (" reason prefix stable until those issues land.
    ledger = schema.extract_ledger_json(decisions_text)
    if ledger:
        # Check if ledger has D/W/Q handles (post-tier handles)
        ledger_handles = _ledger_handle_keys(ledger)
        if ledger_handles:
            # Ledger has handles but we didn't validate via report checkpoint, fail closed.
            # Say which situation applies so the reason is actionable, using the same suffix
            # info as the legacy path below.
            handles_txt = ", ".join(sorted(ledger_handles))
            detail_parts = []
            if relay_count.markers == 0:
                detail_parts.append("no relayed report files")
            elif flow_dir is None:
                detail_parts.append("flow dir unresolved")
            else:
                detail_parts.append("question files exist but none is a current-attempt kind: report checkpoint")
            if relay_count.unanswered:
                detail_parts.append(f"unanswered: {', '.join(relay_count.unanswered)}")
            if relay_count.malformed:
                detail_parts.append(f"malformed: {', '.join(relay_count.malformed)}")
            detail = ", ".join(detail_parts)
            return (
                f"plan checkpoint incomplete (ledger has D/W/Q handles but no report file was validated: "
                f"{detail}; handles: {handles_txt})"
            )
        else:
            # No top-level handles: legacy path defense in depth check for nested handles
            nested_reason = schema.nested_handle_map_reason(ledger)
            if nested_reason:
                return f"plan checkpoint incomplete ({nested_reason})"

    # Legacy per-question rule (fails closed on unanswered, malformed and out-of-attempt files)
    briefs = count_briefs(decisions_text)
    if relay_count.asked < briefs or relay_count.unanswered or _relay_anomaly_suffix(relay_count):
        reason = f"{REASON_PLAN_INCOMPLETE} (briefs: {briefs}, asked: {relay_count.asked}"
        reason += _relay_shortfall_suffix(relay_count, flow_dir, "plan", attempt, briefs > 0)
        reason += ")"
        return reason

    return None


def check_review(receipt_file: Path, since: float, home: Path, flow_dir: Optional[Path]) -> Optional[str]:
    """Check review artifacts. Return error reason or None."""
    receipt = read_file(receipt_file)

    # Get REVIEW_DIR from last matching line
    review_dir_str = _last_header_value(receipt, "REVIEW_DIR")

    if not review_dir_str:
        return "review artifacts missing (REVIEW_DIR not under reviews/)"

    review_dir = canonicalize_path(review_dir_str)
    reviews_root = (home / ".claude" / "reviews").resolve()

    # Check path is under reviews/
    if not _is_under_root(review_dir, reviews_root):
        return "review artifacts missing (REVIEW_DIR not under reviews/)"

    # Check required files
    for filename in ["claude-action-plan.md", "final-report.md", "findings.json"]:
        if not (review_dir / filename).exists():
            return f"review artifacts missing ({filename})"

    # Get the first-attempt identity floor for review
    floor, from_log = _step_floor(flow_dir, "review", since)
    floor_reason = REASON_REVIEW_OLDER_FIRST if from_log else REASON_REVIEW_OLDER_ATTEMPT

    # Check claude-action-plan.md mtime against the floor
    action_plan = review_dir / "claude-action-plan.md"
    try:
        mtime = action_plan.stat().st_mtime
        if mtime < floor - MTIME_SKEW_SECS:
            return floor_reason
    except OSError:
        return floor_reason

    # Check for reviewer checkpoints: >=2 *-pass1.md or *-pod.md
    checkpoints = list(review_dir.glob("*-pass1.md")) + list(review_dir.glob("*-pod.md"))
    if len(checkpoints) < 2:
        return f"review artifacts missing (reviewer checkpoints: {len(checkpoints)})"

    # Get attempt from receipt
    attempt, attempt_err = _resolve_attempt(receipt, flow_dir, "review")
    if attempt_err or attempt is None:
        return f"{REASON_REVIEW_INCOMPLETE} ({attempt_err})"

    # Rulings completeness: every *Needs you* item must have been put to the human through the relay.
    needs_you = count_needs_you_items(read_file(action_plan))
    relay_count = count_relayed_questions(flow_dir, "review", receipt, attempt)
    if relay_count.asked < needs_you or relay_count.unanswered or _relay_anomaly_suffix(relay_count):
        reason = f"{REASON_REVIEW_INCOMPLETE} (needs-you: {needs_you}, asked: {relay_count.asked}"
        reason += _relay_shortfall_suffix(relay_count, flow_dir, "review", attempt, needs_you > 0)
        reason += ")"
        return reason

    return None


def count_briefs(decisions: str) -> int:
    """Count `#### Q<n>` decision-brief headings in a plan session's decisions.md.

    Uses the distinct Q numbers to handle repeated headings (e.g., in checkpoint results).
    """
    q_numbers = BRIEF_HEADING_RE.findall(decisions)
    return len(set(q_numbers))


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


def count_relayed_questions(
    flow_dir: Optional[Path], step: str, receipt_text: str, attempt: int
) -> RelayCount:
    r"""Count relayed questions from disk, scoped by attempt.

    Relayed questions are counted from disk: a `questions/<step>-NN.json` counts when its
    `attempt` equals the receipt's `ATTEMPT` (or a `reused-answers` marker names it) and its
    `answers/<step>-NN.json` exists; a question file without `attempt` does not count, and an
    `awaiting-answers` marker whose answers file is missing is reduced.

    Selection rule. Candidates are the `questions/<step>-NN.json` files. A file is selected when:
    - it is non-report, passes the schema check, and its `attempt` equals the receipt's `ATTEMPT`; or
    - a `<!-- reused-answers: questions/<step>-NN.json -->` marker in the receipt names it and it
      passes the schema check.

    Classification:
    - A selected file with an answers file adds len(questions) to asked.
    - A selected file without one is unanswered.
    - An awaiting-answers marker naming a file with no answers file is also unanswered.
    - An awaiting-answers marker naming a question file that is answered but out of this attempt
      (its attempt differs, and no reused-answers marker names it) fails closed and is listed in
      out_of_attempt. It is never counted.
    - A current-attempt report-kind file without an answers file is unanswered.
    - A schema-invalid candidate is listed in malformed and never counted. Callers treat malformed
      and out_of_attempt entries as failures (fail closed), not merely as diagnostics.

    Returns RelayCount with: asked, files (selected filenames), unanswered,
    malformed, out_of_attempt, and markers (diagnostic).
    """
    # Find marker-named files from receipt text
    awaiting_re = re.compile(r"<!-- awaiting-answers: (questions/[^ ]+\.json) -->")
    reused_re = re.compile(r"<!-- reused-answers: (questions/[^ ]+\.json) -->")

    awaiting_paths = awaiting_re.findall(receipt_text)
    reused_paths = reused_re.findall(receipt_text)

    # Extract basenames and keep only those matching <step>-*.json
    awaiting_basenames = set()
    reused_basenames = set()

    for path_str in awaiting_paths:
        basename = Path(path_str).name
        if basename.startswith(f"{step}-"):
            awaiting_basenames.add(basename)

    for path_str in reused_paths:
        basename = Path(path_str).name
        if basename.startswith(f"{step}-"):
            reused_basenames.add(basename)

    marker_count = len(awaiting_basenames | reused_basenames)

    # If no flow dir, return zeros
    if flow_dir is None:
        return RelayCount(
            asked=0,
            files=[],
            unanswered=[],
            malformed=[],
            out_of_attempt=[],
            markers=marker_count,
        )

    qdir = flow_dir / "questions"
    adir = flow_dir / "answers"

    # Candidates: all <step>-NN.json files in questions/
    candidates = set()
    if qdir.exists():
        for qfile in qdir.glob(f"{step}-*.json"):
            if qfile.is_file():
                candidates.add(qfile.name)

    # Add marker-named files even if they don't exist on disk yet
    candidates.update(awaiting_basenames)
    candidates.update(reused_basenames)

    # Select files according to the selection rule
    selected = []
    unanswered = []
    malformed = []
    out_of_attempt = []
    total = 0

    for basename in sorted(candidates):
        qfile = qdir / basename if qdir.exists() else None

        # Check if file exists and is readable
        if qfile is None or not qfile.is_file():
            # File doesn't exist on disk
            if basename in awaiting_basenames:
                # awaiting-answers marker names a non-existent file: unanswered
                unanswered.append(basename)
            continue

        # Validate the file's schema
        data, problem = validate_question_file(qfile, step)

        if problem is not None:
            # Schema invalid
            malformed.append(f"{basename} ({problem})")
            # Also check if this file has no answers and is named by awaiting-answers marker
            answers_file = adir / basename if adir.exists() else None
            if basename in awaiting_basenames and (answers_file is None or not answers_file.is_file()):
                unanswered.append(basename)
            continue

        # Extract attempt field
        file_attempt = data.get("attempt")

        # Selection rule:
        # 1. Non-report, attempt matches receipt attempt: select
        # 2. reused-answers marker names it: select
        # Otherwise: out of scope
        is_report = data.get("kind") == "report"
        is_reused = basename in reused_basenames

        if not is_report and file_attempt == attempt:
            # Current-attempt file: select it
            pass
        elif is_reused:
            # reused-answers marker: select it
            pass
        else:
            # Out of scope: not a selected non-report file.
            answers_file = adir / basename if adir.exists() else None
            has_answers = answers_file is not None and answers_file.is_file()
            if is_report and file_attempt == attempt:
                # Current-attempt report-kind file: validated by the report checkpoint, not counted
                # here. Without an answers file it never reached the human.
                if not has_answers:
                    unanswered.append(basename)
            elif basename in awaiting_basenames:
                if has_answers:
                    # Answered but from a different attempt, and no reused-answers marker
                    out_of_attempt.append(basename)
                else:
                    # No answers: unanswered regardless of attempt
                    unanswered.append(basename)
            continue

        # File is selected. Check for answers file.
        selected.append(basename)
        answers_file = adir / basename if adir.exists() else None

        if answers_file is not None and answers_file.is_file():
            # Has answers: count questions
            questions = data.get("questions", [])
            if isinstance(questions, list):
                total += len(questions)
        else:
            # No answers file
            unanswered.append(basename)

    return RelayCount(
        asked=total,
        files=selected,
        unanswered=unanswered,
        malformed=malformed,
        out_of_attempt=out_of_attempt,
        markers=marker_count,
    )


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


def _git_head(repo: str, branch: Optional[str]) -> str:
    """The step's final HEAD: the branch tip when --branch is given, else HEAD. Error on git failure."""
    ref = branch or "HEAD"
    try:
        result = subprocess.run(
            ["git", "-C", repo, "rev-parse", ref],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            error(f"Could not read step HEAD: {result.stderr.strip()}")
        return result.stdout.strip()
    except subprocess.TimeoutExpired as e:
        error(f"Git operation failed: {e}")


@dataclass(frozen=True)
class CheckOutcome:
    """The CHECK lines of an OK implement/fix receipt (#338), parsed once. An absent line is None."""

    check: Optional[str]
    exit_code: Optional[str]
    source: Optional[str]
    sha: Optional[str]


def _parse_check_outcome(receipt: str) -> CheckOutcome:
    """Read the four CHECK lines from a receipt header."""
    return CheckOutcome(
        check=parse_receipt_field(receipt, "CHECK"),
        exit_code=parse_receipt_field(receipt, "CHECK_EXIT"),
        source=parse_receipt_field(receipt, "CHECK_SOURCE"),
        sha=parse_receipt_field(receipt, "CHECK_SHA"),
    )


def _check_gate_reason(
    receipt: str,
    repo: str,
    branch: Optional[str],
    main_worktree: Optional[str],
    check_worktree: Optional[str] = None,
) -> Optional[str]:
    """Validate an OK receipt's check lines against the resolver and the step's final HEAD (#338).

    The resolver is `checks.resolve_check_command` run in-process, so the receipt cannot vouch for
    its own command or an opt-out. Return the named rejection reason, or None when the lines hold.

    Trust scope (documented, not enforced): the resolver reads repo-cache.json and project.yaml from
    the step's own worktree first, and that worktree is agent-writable. An agent that edits its own
    worktree config can make the resolver report a command or an opt-out it chose, and this gate
    would then agree with it. The gate catches honest mistakes (a stale, substituted or unrun check,
    or an opt-out the config does not carry); it does not defend against a deliberately falsified
    worktree config. Preferring the main worktree's config would close that gap but changes the
    resolution order in scripts/workflow/checks.py, so it is out of scope here.
    """
    # scripts/workflow is a package under the repo root, the parent of this script's directory.
    # Imported lazily so the plan, review and other modes never depend on it.
    repo_root = str(Path(__file__).resolve().parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from scripts.workflow import checks
    from scripts.workflow.checks import OPT_OUT_SOURCE

    outcome = _parse_check_outcome(receipt)
    if not outcome.check:
        return "check outcome missing from receipt"
    check_exit = outcome.exit_code or "none"

    main_path = Path(main_worktree) if main_worktree else None
    # Resolve against the step's own worktree (where the Final check resolved and ran); fall back to --repo.
    resolve_root = Path(check_worktree) if check_worktree else Path(repo)
    try:
        command, source, failure_reason = checks.resolve_check_command(resolve_root, main_path)
    except Exception as exc:  # a resolver crash means the check could not run, not that none is configured
        return f"check-not-run: resolver unavailable ({type(exc).__name__}: {exc})"

    # Contradictory resolver output (a command and an opt-out at once) is never a valid basis for a pass.
    if command is not None and source == OPT_OUT_SOURCE:
        return f"resolver reported both a command ('{command}') and an opt-out (contradictory)"

    if outcome.check == CHECK_OPTED_OUT:
        # Rule (d): an opt-out is accepted only when the resolver itself reported one, with no command.
        if command is not None or source != OPT_OUT_SOURCE:
            return (
                "check opted out in receipt, but the resolver did not report an opt-out "
                f"(resolver source '{source or 'none'}')"
            )
        if outcome.source != OPT_OUT_SOURCE:
            return f"check source mismatch: opt-out receipt names CHECK_SOURCE '{outcome.source or 'none'}'"
    elif command is None:
        # Rule (e): nothing resolves and there is no opt-out, so an OK receipt cannot vouch for a check.
        if source == OPT_OUT_SOURCE:
            return f"check substituted: receipt ran '{outcome.check}', resolved '{source}'"
        return (
            f"check not configured: receipt says OK with '{outcome.check}' but no check command resolves "
            f"({' '.join((failure_reason or 'no reason reported by the resolver').split())})"
        )
    else:
        # Rule (b): the receipt must name the resolved command verbatim.
        if outcome.check != command:
            return f"check substituted: receipt ran '{outcome.check}', resolved '{command}'"
        # Rule (c): a resolved check must have passed; CHECK_EXIT none or missing is not a pass.
        if check_exit != "0":
            return f"check did not pass: CHECK_EXIT '{check_exit}'"
        # The receipt's CHECK_SOURCE must match where the resolver found the command.
        if outcome.source != source:
            return (
                f"check source mismatch: receipt CHECK_SOURCE '{outcome.source or 'none'}', "
                f"resolver source '{source or 'none'}'"
            )

    # Rule (f): the check must have run on the step's final HEAD, not on an earlier tree.
    head = _git_head(repo, branch)
    if outcome.sha != head:
        return f"check ran on {outcome.sha or 'none'}, step ends at {head}"

    return None


def _undecided_reason(receipt: str) -> Optional[str]:
    """Fail closed (#338): an implement/fix receipt whose Decision is not OK or FAILED in the header block.

    parse_receipt_field stops at the first blank line, so a Decision (or CHECK lines) that sits
    below a blank line reads as absent. Treating that as "not OK" would skip the check gate, so it
    is rejected by name instead.
    """
    if parse_receipt_field(receipt, "Decision") in ("OK", "FAILED"):
        return None
    return (
        "receipt Decision missing or outside the header block "
        "(no blank line may precede the CHECK, ATTEMPT and Decision lines)"
    )


def check_implement(
    receipt_file: Path,
    repo: str,
    branch: str,
    base: str,
    main_worktree: Optional[str] = None,
    check_worktree: Optional[str] = None,
) -> Optional[str]:
    """Check implement artifacts. Return error reason or None."""
    count = _count_commits(repo, base, branch)
    if count < 1:
        return f"implement artifacts missing (no new commits on {branch} since step began)"

    # Check discarded files (parse from receipt and validate against branch)
    receipt = read_file(receipt_file)
    discarded_files = parse_discarded_files_block(receipt)
    if discarded_files:
        discard_error = check_discarded_files(repo, branch, discarded_files)
        if discard_error:
            return discard_error

    # An OK receipt must carry a check outcome that matches the resolver and the final HEAD.
    undecided = _undecided_reason(receipt)
    if undecided:
        return undecided
    if parse_receipt_field(receipt, "Decision") == "OK":
        return _check_gate_reason(receipt, repo, branch, main_worktree, check_worktree)

    return None


def check_fix(
    receipt_file: Path,
    repo: str,
    branch: str,
    base: str,
    review_dir: str,
    home: Path,
    main_worktree: Optional[str] = None,
    check_worktree: Optional[str] = None,
) -> Optional[str]:
    """Check fix artifacts (zero-commit carve-out), then the OK receipt's check lines (#338)."""
    artifact_reason = _check_fix_artifacts(repo, branch, base, review_dir, home)
    if artifact_reason:
        return artifact_reason

    receipt = read_file(receipt_file)
    undecided = _undecided_reason(receipt)
    if undecided:
        return undecided
    if parse_receipt_field(receipt, "Decision") == "OK":
        return _check_gate_reason(receipt, repo, branch, main_worktree, check_worktree)

    return None


def _check_fix_artifacts(
    repo: str,
    branch: str,
    base: str,
    review_dir: str,
    home: Path,
) -> Optional[str]:
    """Fix artifact check with zero-commit carve-out. Return error reason or None."""
    count = _count_commits(repo, base, branch)

    # If zero commits, check for zero "Doing it" items in action plan
    if count == 0:
        action_plan_path = canonicalize_path(review_dir)
        action_plan_path = action_plan_path / "claude-action-plan.md"
        reviews_root = (home / ".claude" / "reviews").resolve()

        # Check path is under reviews/
        if not _is_under_root(action_plan_path, reviews_root):
            return f"fix artifacts missing (no new commits on {branch} since step began)"

        if not action_plan_path.exists():
            return f"fix artifacts missing (no new commits on {branch} since step began)"

        action_plan = read_file(action_plan_path)
        doing_it_count = _count_doing_it_items(action_plan)

        if doing_it_count >= 1:
            return f"fix artifacts missing (no new commits on {branch} since step began)"

    return None


# Alias kept so existing call sites and tests keep working; the implementation lives in flow_log.
_parse_iso8601 = flow_log.parse_iso8601


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
                # Skip separator rows: cells with optional colons and dashes
                if not _is_markdown_separator_row(stripped):
                    count += 1
            elif table_started and not stripped.startswith("|"):
                # Table ended
                table_started = False

        i += 1

    # If section never found, fail closed
    if not in_doing_it:
        return 0

    return count


def parse_receipt_field(receipt: str, field_name: str) -> Optional[str]:
    """Parse a single receipt field (e.g., IMPLEMENTER_MODEL, FAILURE_CLASS).

    Searches for field_name in the header block (fields before the first blank line
    or <!-- step-end --> marker). Returns the value of the last matching line, or None
    if not found.
    """
    result = None
    for line in receipt.split("\n"):
        # Stop at blank line or step-end marker
        if not line.strip() or "<!-- step-end -->" in line:
            break
        if line.startswith(f"{field_name}:"):
            result = line.split(":", 1)[1].strip()
    return result


def parse_discarded_files_block(receipt: str) -> List[str]:
    """Parse DISCARDED_ADDED_FILES block from receipt.

    Returns a list of file paths from indented lines under the block.
    """
    files: List[str] = []
    in_block = False
    for line in receipt.split("\n"):
        if line.startswith("DISCARDED_ADDED_FILES:"):
            in_block = True
            continue
        if in_block:
            # Check if line is indented (part of the block)
            if line and line[0] == " ":
                # Strip leading whitespace and extract the path
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith("<!--"):
                    # Handle both "- path" and plain "path" formats
                    if stripped.startswith("- "):
                        files.append(stripped[2:])
                    else:
                        files.append(stripped)
            elif line and line[0] != " ":
                # End of block (non-indented line)
                break
    return files


def check_discarded_files(repo: str, branch: str, discarded_files: List[str]) -> Optional[str]:
    """Check that discarded files are absent from the branch via git ls-tree.

    Requires repo and branch to be non-empty. Rejects absolute paths and .. components.
    Returns error reason if any files are present or if validation fails, None if all are absent.
    """
    if not discarded_files:
        return None

    # Validate that repo and branch are provided
    if not repo or not branch:
        return "discarded files check requires --repo and --branch"

    # Validate each file path
    for file_path in discarded_files:
        # Reject absolute paths
        if file_path.startswith("/"):
            return f"discarded files check: absolute path not allowed ({file_path})"
        # Reject .. components
        if ".." in file_path:
            return f"discarded files check: .. component not allowed ({file_path})"

    present_files = []
    for file_path in discarded_files:
        try:
            result = subprocess.run(
                ["git", "-C", repo, "ls-tree", "-r", branch, "--", file_path],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECS,
            )
            # If exit code is 0 and there's output, the file exists
            if result.returncode == 0 and result.stdout.strip():
                present_files.append(file_path)
            # On non-zero exit or other git errors, fail closed
            elif result.returncode != 0 and result.returncode != 128:
                # 128 typically means "not found", which is OK
                return f"git operation failed checking {file_path}: {result.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return f"git operation timed out checking {file_path}"
        except Exception as e:
            # Fail closed: any other error is treated as a check failure
            return f"git operation error checking {file_path}: {e}"

    if present_files:
        return f"discarded files still present ({', '.join(present_files)})"
    return None


def iter_outstanding_lines(receipt: str) -> tuple:
    """Iterate OUTSTANDING: lines from receipt until the step-end marker.

    Blank lines before the first OUTSTANDING: line are skipped; once the block starts,
    a blank line ends it as an error (see Raises).

    Returns (valid_items, has_none, has_items) where:
    - valid_items: list of OutstandingItem objects
    - has_none: True if any line was "OUTSTANDING: none"
    - has_items: True if any item was parsed (non-none lines matching format)

    Raises ValueError if "none" is mixed with other items (in same receipt block) or if
    blank lines appear within the OUTSTANDING block.
    """
    valid_items: List[OutstandingItem] = []
    has_none = False
    has_items = False
    in_outstanding_block = False

    for line in receipt.split("\n"):
        # Check for step-end marker - stops the block
        if "<!-- step-end -->" in line:
            break

        # If we've started an OUTSTANDING block and hit a blank line, that's an error
        if in_outstanding_block and not line.strip():
            raise ValueError("blank line in OUTSTANDING block")

        # Blank lines before the block are skipped, not terminal: a receipt with a blank
        # line above OUTSTANDING: is still a valid receipt (#322 side finding)
        if not line.strip() and not in_outstanding_block:
            continue

        if line.startswith("OUTSTANDING:"):
            in_outstanding_block = True
            # Parse: OUTSTANDING: <unit id> (<reason>) [provenance] or OUTSTANDING: none
            rest = line[len("OUTSTANDING:"):].strip()
            if rest == "none":
                has_none = True
            else:
                # Validate format with regex
                match = OUTSTANDING_ITEM_RE.match(rest)
                if match:
                    unit_id = match.group(1)
                    reason = match.group(2)
                    provenance = match.group(3)
                    # Validate provenance is one of the allowed values
                    if provenance in ["self-report", "both"]:
                        valid_items.append(OutstandingItem(unit_id, reason, provenance))  # type: ignore
                        has_items = True

    # Reject mixing "none" with items
    if has_none and has_items:
        raise ValueError("OUTSTANDING block mixes 'none' with items")

    return (valid_items, has_none, has_items)


def parse_outstanding(receipt: str) -> List[OutstandingItem]:
    """Parse OUTSTANDING: lines from receipt.

    Returns a list of OutstandingItem objects. Returns empty list if no OUTSTANDING:
    lines found or if only "OUTSTANDING: none" is present.
    """
    try:
        valid_items, has_none, has_items = iter_outstanding_lines(receipt)
        return valid_items
    except ValueError:
        # If validation fails, return empty list (fail closed)
        return []


def _enumerate_step_headings(plan_text: str) -> List[str]:
    """Enumerate ### Step N headings from plan text.

    Returns list of unit ids like ['Step 1', 'Step 2', ...] in order.
    """
    units: List[str] = []
    for line in plan_text.split("\n"):
        match = re.match(r"^###\s+Step\s+(\d+)\b", line)
        if match:
            units.append(f"Step {match.group(1)}")
    return units


def _enumerate_doing_it_items(action_plan_text: str) -> List[str]:
    """Enumerate D<n> directives from 'Doing it' section of action plan.

    Returns list of unit ids like ['D1', 'D2', ...] in order.
    """
    units: List[str] = []
    in_doing_it = False
    table_started = False
    doing_it_count = 0

    for line in action_plan_text.split("\n"):
        # Match "## Doing it:" heading
        if re.match(r"^##\s+Doing it:", line):
            in_doing_it = True
            table_started = False
            continue

        # Stop at next ## heading
        if in_doing_it and re.match(r"^##\s+", line):
            break

        if in_doing_it:
            stripped = line.strip()

            # Check for table start (first line starting with |)
            if stripped.startswith("|") and not table_started:
                table_started = True
                continue

            # In table: count data rows, skip header and separator
            if table_started and stripped.startswith("|"):
                # Skip separator rows: cells with optional colons and dashes
                if not _is_markdown_separator_row(stripped):
                    doing_it_count += 1
                    units.append(f"D{doing_it_count}")
            elif table_started and not stripped.startswith("|"):
                # Table ended
                table_started = False

    return units


def _get_file_names_from_plan_section(plan_text: str, unit_id: str) -> List[str]:
    """Extract file names mentioned in a plan unit's section.

    Looks for backtick-quoted tokens that look like file paths in the section
    from unit_id heading to the next ### heading.
    Returns list of file paths found.

    Note: 'covered' is a file-touch heuristic. Files are extracted as-is but normalized:
    - Leading ./ is stripped
    - Trailing :digit+ (line references) are stripped
    - Suffix matching is used for comparison
    """
    # Find the section from the unit heading to the next ###
    lines = plan_text.split("\n")
    start_idx = None
    end_idx = len(lines)

    for i, line in enumerate(lines):
        # Use anchored match with word boundary to avoid substring matches
        if re.match(r"^#+.*\b(?!\d)" + re.escape(unit_id) + r"\b", line):
            start_idx = i
        elif start_idx is not None and line.strip().startswith("###"):
            end_idx = i
            break

    if start_idx is None:
        return []

    section_text = "\n".join(lines[start_idx:end_idx])

    # Find all backtick-quoted tokens that look like paths
    # Match `path/to/file` patterns
    files: List[str] = []
    for match in re.finditer(r"`([^`]+(?:/[^`]+)*)`", section_text):
        path = match.group(1)
        # Normalize: strip ./ prefix and :\d+ suffix
        path = re.sub(r"^\.\/", "", path)
        path = re.sub(r":\d+$", "", path)
        # Only include if it looks like a repo path (contains / or has extension)
        if "/" in path or path.endswith((".py", ".js", ".ts", ".rs", ".md", ".yaml", ".json")):
            files.append(path)

    return files


def _git_diff_names(repo: str, base: str, branch: str) -> Optional[Set[str]]:
    """Get set of file names changed from base to branch.

    Returns set of file paths from git diff --name-only.
    Returns None on git failure (timeout, command error, etc.) to signal 'unknown' state.
    """
    try:
        # Validate base and branch do not start with '-' (F13)
        if base.startswith("-") or branch.startswith("-"):
            return None

        result = subprocess.run(
            ["git", "-C", repo, "diff", "--name-only", "--end-of-options", f"{base}..{branch}"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            return None
        return set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
    except (subprocess.TimeoutExpired, Exception):
        return None


def coverage_mode(
    step: str,
    plan_path: str,
    receipt_path: str,
    repo: str,
    branch: str,
    base: str,
) -> None:
    """Run coverage mode: enumerate units and classify by coverage.

    Prints to stdout:
      UNITS: <n> (or UNITS: 0 (no step headings) / UNITS: 0 if empty)
      <id> | <status> | <evidence>

    Status values: covered, declared, uncovered, unknown
    - covered: files in the unit appear in the diff
    - declared: unit is in OUTSTANDING section
    - uncovered: unit is not in diff and not declared
    - unknown: no extractable evidence (git failure, fix with no files, etc.)

    Exits with 0 unless plan is unreadable (exit 2).
    """
    # Read plan file
    plan_file = Path(plan_path)
    if not plan_file.exists():
        error(f"Plan file not found: {plan_path}")

    plan_text = read_file(plan_file)

    # Read receipt file
    receipt_file = Path(receipt_path)
    receipt_text = ""
    if receipt_file.exists():
        receipt_text = read_file(receipt_file)

    # Enumerate units based on step
    units: List[str] = []
    if step == "implement":
        units = _enumerate_step_headings(plan_text)
    elif step == "fix":
        # For fix, read the action plan from the receipt's path or infer it
        # The receipt is named in --receipt, but we need claude-action-plan.md
        # which is typically in the same review directory
        action_plan_path = plan_file  # The --plan argument is the action plan for fix
        if action_plan_path.exists():
            action_plan_text = read_file(action_plan_path)
            units = _enumerate_doing_it_items(action_plan_text)

    # Get the changed files from git diff (None = unknown state)
    changed_files = _git_diff_names(repo, base, branch)

    # Parse outstanding items from receipt (validate format first)
    outstanding_dict: Dict[str, OutstandingItem] = {}
    try:
        valid_items, has_none, has_items = iter_outstanding_lines(receipt_text)
        for item in valid_items:
            outstanding_dict[item.unit_id] = item
    except ValueError as e:
        error(f"OUTSTANDING validation error: {e}")

    # Classify each unit
    results: List[tuple] = []
    unknown_declared: List[str] = []

    for unit_id in units:
        # Check if declared in OUTSTANDING
        is_declared = unit_id in outstanding_dict

        # Classify
        if changed_files is None:
            # Git failure: no extractable evidence, mark as unknown
            status = "unknown"
            evidence = "no extractable evidence (git failure)"
        elif is_declared:
            item = outstanding_dict[unit_id]
            status = "declared"
            evidence = f"{item.provenance}: {item.reason}"
        else:
            # Check if covered: files in the unit appear in the diff
            unit_files = _get_file_names_from_plan_section(plan_text, unit_id)
            if step == "fix" and not unit_files:
                # For fix mode, no files from action plan table row = unknown (F1)
                status = "unknown"
                evidence = "no extractable evidence (no files in table row)"
            elif unit_files and any(f in changed_files for f in unit_files):
                status = "covered"
                evidence = f"files in diff: {', '.join(unit_files)}"
            elif unit_files:
                # Has files but none in diff
                status = "uncovered"
                evidence = f"not in diff: {', '.join(unit_files)}"
            else:
                # No files found at all
                status = "uncovered"
                evidence = "not in diff, not in OUTSTANDING"

        results.append((unit_id, status, evidence))

    # Check for unknown declared items (in OUTSTANDING but not in plan)
    for declared_item in outstanding_dict.values():
        if declared_item.unit_id not in units:
            unknown_declared.append(declared_item.unit_id)

    # Print output
    print(f"UNITS: {len(units)}" + (" (no step headings)" if len(units) == 0 else ""))
    for unit_id, status, evidence in results:
        print(f"{unit_id} | {status} | {evidence}")

    for unknown_id in unknown_declared:
        print(f"unknown-declared: {unknown_id}")

    sys.exit(0)


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
    since_group = parser.add_mutually_exclusive_group(required=False)
    since_group.add_argument("--since", help="ISO-8601 UTC timestamp")
    since_group.add_argument(
        "--since-from-log",
        action="store_true",
        dest="since_from_log",
        help="derive the bound from the newest --step begin|retry line in the flow's log.md",
    )
    parser.add_argument("--repo", help="Repo directory (for implement/fix)")
    parser.add_argument("--branch", help="Branch name (for implement/fix)")
    parser.add_argument("--base", help="Base SHA (for implement/fix)")
    parser.add_argument("--review-dir", help="Review directory (for fix)")
    parser.add_argument(
        "--main-worktree",
        help="Main worktree (for implement/fix): the check resolver falls back to it; absent = worktree only",
    )
    parser.add_argument(
        "--check-worktree",
        help="Step worktree (for implement/fix): the check resolves against it; absent = --repo",
    )
    parser.add_argument(
        "--flow-dir",
        help="Flow directory holding questions/ (plan/review); default: the receipt's grandparent when it sits in steps/",
    )
    parser.add_argument(
        "--escalation",
        action="store_true",
        help="Escalation mode: print ESCALATE line and exit 0",
    )
    parser.add_argument(
        "--stop-source",
        choices=["receipt", "validation", "audit"],
        help="Stop source for escalation mode",
    )
    parser.add_argument(
        "--escalations",
        type=int,
        help="Number of escalations so far",
    )
    parser.add_argument(
        "--failures",
        type=int,
        help="Number of failures so far",
    )
    parser.add_argument(
        "--implementer-model-state",
        choices=["unset", "sonnet"],
        help="Current implementer model state",
    )
    parser.add_argument(
        "--expect-model",
        help="Expected implementer model for OK receipts",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Coverage mode: enumerate and classify plan units by coverage",
    )
    parser.add_argument(
        "--plan",
        help="Plan file path (for coverage mode)",
    )

    args = parser.parse_args()

    # Read receipt file
    receipt_file = Path(args.receipt)
    if not receipt_file.exists():
        error(f"Receipt file not found: {args.receipt}")

    receipt = read_file(receipt_file)

    # Handle --coverage mode (read-only, no --since required)
    if args.coverage:
        if not args.plan:
            error("--coverage requires --plan")
        if args.step not in ["implement", "fix"]:
            error("--coverage only supported for implement/fix steps")
        if not all([args.repo, args.branch, args.base]):
            error("--coverage requires --repo, --branch, --base")
        coverage_mode(args.step, args.plan, args.receipt, args.repo, args.branch, args.base)

    # Handle --escalation mode (check before any --since requirement)
    if args.escalation:
        # Check if receipt has a decision of OK
        decision = parse_receipt_field(receipt, "Decision")
        if decision == "OK":
            print("ESCALATE: none receipt-ok")
            sys.exit(0)

        # For non-implement/fix steps, print none and exit 0
        if args.step not in ["implement", "fix"]:
            print(f"ESCALATE: none step-{args.step}")
            sys.exit(0)

        # For implement/fix steps, validate required arguments
        if not args.stop_source:
            error("--escalation requires --stop-source")
        if args.escalations is None:
            error("--escalation requires --escalations")
        if args.failures is None:
            error("--escalation requires --failures")

        # Validate stop_source value
        if args.stop_source not in ["receipt", "validation", "audit"]:
            error(f"unknown --stop-source value: {args.stop_source}")

        # Default --implementer-model-state to 'unset' if missing
        implementer_model_state = args.implementer_model_state or "unset"

        # Parse receipt fields
        failure_class = parse_receipt_field(receipt, "FAILURE_CLASS")
        turns_exhausted = parse_receipt_field(receipt, "TURNS_EXHAUSTED")
        # Treat missing IMPLEMENTER_MODEL as haiku (pre-feature attempts ran Haiku)
        implementer_model = parse_receipt_field(receipt, "IMPLEMENTER_MODEL") or "haiku"

        # Determine if it's a failure
        if not failure_class:
            print("ESCALATE: none not-failed")
            sys.exit(0)

        # cwd-drift is never escalation-eligible
        if failure_class == "cwd-drift":
            print("ESCALATE: none cwd-drift")
            sys.exit(0)

        # Check auto path eligibility (all 7 conditions)
        auto_eligible = (
            args.stop_source == "receipt"
            and failure_class == "implementer-shortfall"
            and turns_exhausted == "no"
            and implementer_model == "haiku"
            and implementer_model_state == "unset"
            and args.escalations == 0
            and args.failures < 2
        )

        if auto_eligible:
            print("ESCALATE: auto sonnet")
            sys.exit(0)

        # Check manual path eligibility (once-per-step: escalations must be 0)
        if args.escalations == 0 and args.failures < 2 and implementer_model == "haiku":
            # Manual escalation applicable, determine reason
            if args.stop_source != "receipt":
                print(f"ESCALATE: manual sonnet stop-source {args.stop_source}")
                sys.exit(0)
            elif failure_class != "implementer-shortfall":
                print("ESCALATE: manual sonnet class-other")
                sys.exit(0)
            elif turns_exhausted != "no":
                # Determine reason for turns being exhausted/unknown/absent
                if turns_exhausted == "yes":
                    reason = "turns-yes"
                elif turns_exhausted == "unknown":
                    reason = "turns-unknown"
                else:
                    reason = "turns-absent"
                print(f"ESCALATE: manual sonnet {reason}")
                sys.exit(0)
            elif implementer_model_state != "unset" or args.escalations > 0:
                # Go to none path instead
                pass
            else:
                print("ESCALATE: manual sonnet")
                sys.exit(0)

        # None path
        none_reason: Optional[str] = None
        if args.escalations is not None and args.escalations >= 1:
            none_reason = "escalations-spent"
        elif args.failures is not None and args.failures >= 2:
            none_reason = "failures-at-cap"
        elif implementer_model not in [None, "haiku"]:
            none_reason = "model-not-haiku"

        if none_reason:
            print(f"ESCALATE: none {none_reason}")
        else:
            print("ESCALATE: none")
        sys.exit(0)

    # Handle --expect-model mode (does not require --since)
    if args.expect_model:
        # Expect-model mode for implement/fix on OK receipts
        if args.step not in ["implement", "fix"]:
            error("--expect-model only supported for implement/fix steps")

        # Check for deviation
        if scan_for_deviation(receipt):
            print("Reason: receipt admits deviation")
            sys.exit(1)

        # Parse receipt fields
        receipt_impl_model = parse_receipt_field(receipt, "IMPLEMENTER_MODEL")

        # Rule 8: For escalated receipts (escalations > 0), validate model match is required
        if args.escalations is not None and args.escalations > 0:
            if receipt_impl_model != args.expect_model:
                print(f"implementer model mismatch: escalated receipt expected {args.expect_model}, got {receipt_impl_model}")
                sys.exit(1)
        else:
            # Check model match (standard case)
            if receipt_impl_model != args.expect_model:
                print(f"implementer model mismatch (expected {args.expect_model}, receipt {receipt_impl_model})")
                sys.exit(1)

        # Check discarded files (requires both repo and branch)
        if args.repo and args.branch:
            discarded_files = parse_discarded_files_block(receipt)
            if discarded_files:
                discarded_error = check_discarded_files(args.repo, args.branch, discarded_files)
                if discarded_error:
                    print(discarded_error)
                    sys.exit(1)

        # For expect-model mode: fall through to OUTSTANDING and ROUND 1 checks (NY1)
        # These checks are run after expect-model validation, same as standard verification
        decision = parse_receipt_field(receipt, "Decision")
        if args.step in ["implement", "fix"] and decision != "FAILED":
            undecided = _undecided_reason(receipt)
            if undecided:
                print(f"Reason: {undecided}")
                sys.exit(1)
        if decision == "OK" and args.step in ["implement", "fix"]:
            # Check for OUTSTANDING: line using shared helper (F2)
            try:
                valid_items, has_none, has_items = iter_outstanding_lines(receipt)
                if not (has_none or has_items):
                    # No OUTSTANDING: line found at all
                    print("Reason: receipt missing OUTSTANDING")
                    sys.exit(1)
            except ValueError:
                # iter_outstanding_lines raises on malformed blocks
                print("Reason: receipt missing OUTSTANDING (malformed)")
                sys.exit(1)
            # ROUND 1 section header (#341, item 5). Only OK receipts: a FAILED receipt keeps its real stop reason.
            if not ROUND1_HEADER_RE.search(receipt):
                print("Reason: receipt missing ROUND 1 section header")
                sys.exit(1)
            # The check gate (#338) applies to escalated re-runs too. It cannot run without the repo and
            # branch it resolves and compares against, so a missing pair is a usage error, not a skip.
            if not (args.repo and args.branch):
                error("escalated re-run check gate requires --repo and --branch")
            gate_reason = _check_gate_reason(
                receipt, args.repo, args.branch, args.main_worktree, args.check_worktree
            )
            if gate_reason:
                print(f"Reason: {gate_reason}")
                sys.exit(1)

        print("ARTIFACTS: ok")
        sys.exit(0)

    # Standard artifact verification mode
    if args.since is None and not args.since_from_log:
        error("--since or --since-from-log required for standard verification")

    if args.since_from_log:
        # Bound from code, never from the model: exit 2 on a bad flow dir, exit 1 on a missing or
        # malformed begin/retry line.
        flow_dir_arg = resolve_flow_dir(args.flow_dir, Path(args.receipt))
        if flow_dir_arg is None:
            error("--since-from-log needs --flow-dir (or a receipt under steps/)")
        try:
            flow_dir_real = flow_log.validate_flow_dir(str(flow_dir_arg))
        except flow_log.FlowLogError as e:
            error(f"--since-from-log: {e}")
        # Reason lines go to stdout as the verdict; error() (stderr, exit 2) is reserved for bad arguments.
        try:
            log_ts, window_cut = flow_log.newest_attempt_start_detail(flow_dir_real / "log.md", args.step)
        except flow_log.MalformedLineError as e:
            print(f"Reason: malformed begin or retry line for {args.step} ({e})")
            sys.exit(1)
        except flow_log.FlowLogError as e:
            print(f"Reason: cannot read begin or retry line for {args.step} ({e})")
            sys.exit(1)
        if log_ts is None and window_cut:
            print(f"Reason: no begin or retry line for {args.step} in the readable tail of log.md "
                  f"(only the last {flow_log.LOG_READ_CAP_BYTES} bytes of log.md were read; an older line may exist)")
            sys.exit(1)
        if log_ts is None:
            print(f"Reason: no begin or retry line for {args.step}")
            sys.exit(1)
        since_ts = flow_log.parse_iso8601(log_ts)
    else:
        # Parse --since once; bad value exits with code 2
        try:
            since_ts = _parse_iso8601(args.since)
        except ValueError as e:
            error(str(e))

    if scan_for_deviation(receipt):
        print("Reason: receipt admits deviation")
        sys.exit(1)

    # Check for FAILED decision and return its reason
    decision = parse_receipt_field(receipt, "Decision")
    if decision == "FAILED":
        # Try "REASON:" (uppercase) first, then "Reason:" (title case)
        reason = parse_receipt_field(receipt, "REASON") or parse_receipt_field(receipt, "Reason")
        if reason:
            print(f"Reason: {reason}")
        else:
            print("Reason: receipt is FAILED but has no reason")
        sys.exit(1)

    # An implement/fix receipt whose Decision is not readable in the header block is rejected, never skipped (#338).
    if args.step in ["implement", "fix"]:
        undecided = _undecided_reason(receipt)
        if undecided:
            print(f"Reason: {undecided}")
            sys.exit(1)

    # Validate OUTSTANDING: line and ROUND 1 header for implement/fix OK receipts
    if decision == "OK" and args.step in ["implement", "fix"]:
        # Check for OUTSTANDING: line using shared helper (F2)
        try:
            valid_items, has_none, has_items = iter_outstanding_lines(receipt)
            if not (has_none or has_items):
                # No OUTSTANDING: line found at all
                print("Reason: receipt missing OUTSTANDING")
                sys.exit(1)
        except ValueError:
            # iter_outstanding_lines raises on malformed blocks
            print("Reason: receipt missing OUTSTANDING (malformed)")
            sys.exit(1)
        # ROUND 1 section header (#341, item 5). Only OK receipts: a FAILED receipt keeps its real stop reason.
        if not ROUND1_HEADER_RE.search(receipt):
            print("Reason: receipt missing ROUND 1 section header")
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
        error_reason = check_implement(
            receipt_file, args.repo, args.branch, args.base, args.main_worktree,
            args.check_worktree,
        )
    elif args.step == "fix":
        if not all([args.repo, args.branch, args.base, args.review_dir]):
            error("--repo, --branch, --base, --review-dir required for fix")
        # Validate base as hex SHA (7-40 chars)
        if not re.match(r"^[0-9a-f]{7,40}$", args.base):
            error(f"Invalid base SHA: {args.base}")
        error_reason = check_fix(
            receipt_file, args.repo, args.branch, args.base, args.review_dir, home,
            args.main_worktree, args.check_worktree,
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
