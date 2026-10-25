#!/usr/bin/env python3
"""precedent-log.py: append predictions to a prediction log after Step 5 answers.

Usage:
    precedent-log.py append --validated PATH --questions PATH (--answers PATH | --flow-answers PATH [--flow-answers PATH ...]) --repo-key OWNER-NAME --command {expert-plan,expert-review} --run NAME --trace-file PATH [--log-root DIR]

The append subcommand logs one JSONL row per answered question that Pat spoke on
(has a block in validated JSON). Rows record the predicted answer, actual answer,
confidence, and whether Pat was right (hit).

Inputs:
  --validated PATH           Path to precedent-validated.json (the output of precedent-render.py validate).
  --questions PATH           Path to precedent-questions.json, with schema {"questions":[{id, header, options, recommended}]}.
  --answers PATH             Normalized answers: {"answers": {"Q1": "chosen label or typed text", ...}}.
                             Mutually exclusive with --flow-answers; one required.
  --flow-answers PATH        Flow-mode answers file in round order. Repeatable.
                             Merges across files, latest round wins per handle.
                             Reads top-level 'answers' or 'items' key.
  --repo-key OWNER-NAME      Repository key (lowercased, hyphen-separated). Bad keys (no hyphen) log nothing, exit 0.
  --command {expert-plan,expert-review}  The originating command.
  --run NAME                 Run identifier (e.g., directory basename).
  --trace-file PATH          Path to precedent-trace.txt. Read first line to check if it starts with "shown".
                             Only when it does: append to log (exit 0 on data-side failures, 2 on argv).
  --log-root DIR             Log directory root (default ~/.claude/precedent). Appends to {log_root}/{repo_key}/predictions.jsonl.

Exit and stderr contract:
  - Exit 0 on every data-side outcome: missing/invalid input, bad repo-key, nothing logged.
  - Exit 2 on invocation errors: bad arguments, both/neither --answers and --flow-answers.
  - Every no-log path prints one stderr line: "precedent-log: nothing logged (<reason>)".
  - Success: "precedent-log: logged N, skipped-duplicate M".
  - OSError on read/write: "precedent-log: nothing logged (log-unwritable: <errno text>)", exit 0.
  - Never use bare except; OSError surfaces, no silent failures.

Row schema (TypedDict, PINNED_KEYS order):
  ts, repo_key, command, run, question_id, header, recommended, predicted, confidence,
  actual, hit, citations, actual_source, anchored

Never-read rule: Pat, triage, reviewers, the digest --match path, and the Amalgamator
never read the prediction log. The log is measurement only. Its sole sanctioned reader
is scripts/precedent-digest.py --stats (project-scoped only).
"""
import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple, TypedDict


# PINNED_KEYS defines the exact order of row fields (decision-level invariant).
PINNED_KEYS = (
    "ts",
    "repo_key",
    "command",
    "run",
    "question_id",
    "header",
    "recommended",
    "predicted",
    "confidence",
    "actual",
    "hit",
    "citations",
    "actual_source",
    "anchored",
)


class PredictionRow(TypedDict, total=False):
    """Schema for one prediction log row."""
    ts: str
    repo_key: str
    command: str
    run: str
    question_id: str
    header: str
    recommended: Optional[str]
    predicted: str
    confidence: str
    actual: str
    hit: Optional[bool]
    citations: List[int]
    actual_source: str
    anchored: bool


class _Usage(Exception):
    """An invocation error: exit 2, one stderr line, no traceback."""


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that reports errors on one stderr line and exits 2."""

    def error(self, message: str) -> str:  # type: ignore[override]
        sys.stderr.write("precedent-log: %s\n" % message)
        raise SystemExit(2)


def _stderr(message: str) -> None:
    """Print a message to stderr without a trailing newline."""
    sys.stderr.write(message + "\n")


def _fold(repo_key: str) -> str:
    """Normalize repo_key to lowercase (same fold as precedent-digest.py)."""
    return repo_key.lower()


def _load_validated(path: str) -> Optional[Dict[str, Any]]:
    """Load and validate the precedent-validated.json file."""
    try:
        with open(path) as f:
            data = json.load(f)
        if data.get("status") != "ok":
            return None
        if not data.get("blocks"):
            return None
        return data
    except (IOError, json.JSONDecodeError):
        return None


def _load_questions(path: str) -> Optional[Dict[str, Any]]:
    """Load the precedent-questions.json file."""
    try:
        with open(path) as f:
            data = json.load(f)
        questions_list = data.get("questions", [])
        # Build a dict keyed by id for quick lookup
        questions = {q.get("id"): q for q in questions_list}
        return questions
    except (IOError, json.JSONDecodeError):
        return None


def _load_normalized_answers(path: str) -> Optional[Dict[str, str]]:
    """Load answers from normalized JSON file: {"answers": {"Q1": "...", ...}}"""
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("answers", {})
    except (IOError, json.JSONDecodeError):
        return None


def _load_flow_answers(paths: List[str]) -> Optional[Dict[str, str]]:
    """Load and merge flow-mode answers from multiple files.

    Reads each file's top-level 'answers' or 'items' key.
    Merges with latest file winning per handle.
    Only includes handles with disposition 'answered', 'overridden', or 'accepted-on-continue'.
    """
    merged: Dict[str, str] = {}

    for path in paths:
        try:
            with open(path) as f:
                data = json.load(f)
        except (IOError, json.JSONDecodeError):
            return None

        # Try 'answers' key first (normal shape), then 'items' (documented fallback)
        answers_section = data.get("answers") or data.get("items") or {}

        for handle, item in answers_section.items():
            # Handle both direct string values and item objects with disposition
            if isinstance(item, str):
                # Direct value (old shape)
                merged[handle] = item
            elif isinstance(item, dict):
                disposition = item.get("disposition")
                # Only include answered, overridden, or accepted-on-continue
                if disposition in ("answered", "overridden", "accepted-on-continue"):
                    merged[handle] = item.get("value", "")
            # unresolved, accepted (D/W silence) give no row

    return merged if merged else None


def classify(value: str, options: List[str]) -> Tuple[str, Literal["option", "free_text", "accepted-on-continue"]]:
    """Classify an answer value as option or free_text.

    Returns (actual, actual_source) where:
      - actual: canonical label for option, or normalized text for free_text
      - actual_source: "option" or "free_text" or "accepted-on-continue"
    """
    if value is None:
        return ("", "accepted-on-continue")

    # Strip trailing " (Recommended)" marker
    stripped = value.rstrip()
    if stripped.endswith("(Recommended)"):
        stripped = stripped[: stripped.rfind("(Recommended)")].rstrip()

    # Exact match to an option label
    if stripped in options:
        return (stripped, "option")

    # Letter match: bare letter (A–Z, case-insensitive, optional trailing .)
    # that matches exactly one option starting with "<letter>." or "<letter> "
    letter_part = stripped.rstrip(".").strip()
    if len(letter_part) == 1 and letter_part.isalpha():
        letter = letter_part.upper()
        matching_options = [
            opt for opt in options
            if opt and (opt[0].upper() == letter) and len(opt) > 1 and opt[1] in (".", " ")
        ]
        if len(matching_options) == 1:
            return (matching_options[0], "option")

    # Everything else is free_text
    # Collapse whitespace (including newlines) and cap at 500 chars
    normalized = " ".join(stripped.split())[:500]
    return (normalized, "free_text")


def _build_row(
    ts: str,
    repo_key: str,
    command: str,
    run: str,
    question_block: Dict[str, Any],
    actual: str,
    actual_source: str,
    anchored: bool,
) -> Optional[PredictionRow]:
    """Build a prediction row.

    Returns None if any required field is invalid.
    Validates: hit ⇔ actual_source invariant, confidence in {high, medium}.
    """
    question_id_val: Any = question_block.get("id")
    if not isinstance(question_id_val, str):
        return None
    question_id: str = question_id_val

    header = question_block.get("header", "")
    recommended = question_block.get("recommended")

    predicted_val: Any = question_block.get("prediction")
    if not isinstance(predicted_val, str):
        return None
    predicted: str = predicted_val

    confidence = question_block.get("confidence")
    precedent_lines = question_block.get("precedent_lines", [])

    # Validate confidence
    if confidence not in ("high", "medium"):
        return None

    # Extract citations (ref integers from precedent_lines)
    citations = []
    for line in precedent_lines:
        ref = line.get("ref")
        if isinstance(ref, int):
            citations.append(ref)
    # De-duplicate while keeping order
    seen: set = set()
    dedup_citations: List[int] = []
    for c in citations:
        if c not in seen:
            seen.add(c)
            dedup_citations.append(c)
    citations = dedup_citations

    # Compute hit: predicted == actual when actual_source == "option", else None
    hit: Optional[bool] = None
    if actual_source == "option":
        hit = predicted == actual
    elif actual_source in ("free_text", "accepted-on-continue"):
        hit = None
    else:
        return None  # Invalid actual_source

    row: PredictionRow = {
        "ts": ts,
        "repo_key": repo_key,
        "command": command,
        "run": run,
        "question_id": question_id,
        "header": header,
        "recommended": recommended,
        "predicted": predicted,
        "confidence": confidence,
        "actual": actual,
        "hit": hit,
        "citations": citations,
        "actual_source": actual_source,
        "anchored": anchored,
    }
    return row


def _read_existing_keys(log_path: str) -> set:
    """Read existing (run, question_id) keys from the log."""
    keys: set = set()
    if not os.path.exists(log_path):
        return keys
    try:
        with open(log_path) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    key = (row.get("run"), row.get("question_id"))
                    keys.add(key)
                except json.JSONDecodeError:
                    # Skip malformed lines
                    pass
    except IOError:
        pass
    return keys


def _append_rows(log_dir: str, repo_key: str, rows: List[PredictionRow]) -> Tuple[int, int]:
    """Append rows to the log, skipping duplicates.

    Returns (logged_count, skipped_count).
    Raises OSError on read/write failure.
    """
    # Normalize repo_key
    repo_key_normalized = _fold(repo_key)
    log_subdir = os.path.join(log_dir, repo_key_normalized)
    log_path = os.path.join(log_subdir, "predictions.jsonl")

    # Read existing keys
    existing_keys = _read_existing_keys(log_path)

    # Filter out duplicates
    new_rows = []
    skipped_count = 0
    for row in rows:
        key = (row.get("run"), row.get("question_id"))
        if key in existing_keys:
            skipped_count += 1
        else:
            new_rows.append(row)

    if not new_rows:
        return (0, skipped_count)

    # Create directory
    os.makedirs(log_subdir, exist_ok=True)

    # Write all new rows in one O_APPEND write
    lines = []
    for row in new_rows:
        row_dict: Dict[str, Any] = dict(row)  # type: ignore[arg-type]
        ordered = {k: row_dict[k] for k in PINNED_KEYS}
        lines.append(json.dumps(ordered))
    batch_text = "\n".join(lines) + "\n"

    fd = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, batch_text.encode("utf-8"))
    finally:
        os.close(fd)

    return (len(new_rows), skipped_count)


def main() -> None:
    parser = _Parser(description="Log predictions after Step 5 answers.")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    append_parser = subparsers.add_parser("append", help="Append prediction rows to the log")
    append_parser.add_argument("--validated", required=True, help="Path to precedent-validated.json")
    append_parser.add_argument("--questions", required=True, help="Path to precedent-questions.json")
    append_parser.add_argument("--answers", help="Path to normalized answers JSON")
    append_parser.add_argument("--flow-answers", action="append", default=[], help="Flow-mode answers files (repeatable)")
    append_parser.add_argument("--repo-key", required=True, help="Repository key (owner-name)")
    append_parser.add_argument("--command", required=True, choices=["expert-plan", "expert-review"], help="Originating command")
    append_parser.add_argument("--run", required=True, help="Run identifier")
    append_parser.add_argument("--trace-file", required=True, help="Path to precedent-trace.txt")
    append_parser.add_argument("--log-root", default=os.path.expanduser("~/.claude/precedent"), help="Log root directory")

    args = parser.parse_args()

    if args.subcommand != "append":
        parser.error("only 'append' subcommand is supported")

    # Validate mutually exclusive --answers and --flow-answers
    has_answers = args.answers is not None
    has_flow_answers = len(args.flow_answers) > 0
    if has_answers and has_flow_answers:
        parser.error("--answers and --flow-answers are mutually exclusive")
    if not has_answers and not has_flow_answers:
        parser.error("one of --answers or --flow-answers is required")

    # Check trace file: only proceed if it starts with "shown"
    precedent_trace = ""
    try:
        with open(args.trace_file) as f:
            first_line = f.readline().rstrip("\n")
            precedent_trace = first_line
    except IOError:
        # Missing trace file: treat as no-proceed
        _stderr("precedent-log: nothing logged (trace-file-missing)")
        sys.exit(0)

    if not precedent_trace.startswith("shown"):
        _stderr("precedent-log: nothing logged (trace-not-shown)")
        sys.exit(0)

    # Validate repo_key (must have hyphen: owner-name)
    if "-" not in args.repo_key:
        _stderr("precedent-log: nothing logged (bad-repo-key)")
        sys.exit(0)

    # Load validated JSON
    validated = _load_validated(args.validated)
    if not validated:
        _stderr("precedent-log: nothing logged (validated-invalid)")
        sys.exit(0)

    # Load questions
    questions = _load_questions(args.questions)
    if not questions:
        _stderr("precedent-log: nothing logged (questions-invalid)")
        sys.exit(0)

    # Load answers (either normalized or flow-based)
    answers_dict: Optional[Dict[str, str]] = None
    if has_answers:
        answers_dict = _load_normalized_answers(args.answers)
    else:
        answers_dict = _load_flow_answers(args.flow_answers)

    if not answers_dict:
        _stderr("precedent-log: nothing logged (answers-invalid)")
        sys.exit(0)

    # Check if trace file indicates anchoring
    anchored = precedent_trace.startswith("shown")

    # Build rows for all spoken-on questions that have answers
    rows: List[PredictionRow] = []
    blocks = validated.get("blocks", {})

    for question_id, question_block in blocks.items():
        # Only process questions that have answers
        if question_id not in answers_dict:
            continue

        answer_value = answers_dict[question_id]
        options = question_block.get("options", [])

        # Classify the answer
        actual, actual_source = classify(answer_value, options)

        # Handle accepted-on-continue specially
        if actual_source == "accepted-on-continue":
            actual = answer_value

        # Build the row
        ts = datetime.utcnow().isoformat()
        row = _build_row(
            ts=ts,
            repo_key=args.repo_key,
            command=args.command,
            run=args.run,
            question_block=question_block,
            actual=actual,
            actual_source=actual_source,
            anchored=anchored,
        )

        if row is None:
            _stderr("precedent-log: nothing logged (row-build-failed)")
            sys.exit(0)

        rows.append(row)

    # If no rows, nothing to log
    if not rows:
        _stderr("precedent-log: nothing logged (no-answered-questions)")
        sys.exit(0)

    # Append to log
    try:
        logged_count, skipped_count = _append_rows(args.log_root, args.repo_key, rows)
    except OSError as e:
        _stderr("precedent-log: nothing logged (log-unwritable: %s)" % e.strerror)
        sys.exit(0)

    _stderr("precedent-log: logged %d, skipped-duplicate %d" % (logged_count, skipped_count))
    sys.exit(0)


if __name__ == "__main__":
    main()
