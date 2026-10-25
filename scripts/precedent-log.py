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
  - Every no-log path prints one stderr line: "precedent-log: nothing logged (<reason>)". The
    reason starts with a stable token (trace-not-shown, bad-repo-key, row-build-failed, ...)
    followed by the path or question id and field involved.
  - Success: "precedent-log: logged N, skipped-duplicate M, unanswered K" (K = validated
    blocks with no answer in this run).
  - An unreadable --flow-answers round file is reported on stderr and skipped; the other
    rounds still count.
  - OSError on read/write of the log: "precedent-log: nothing logged (log-unwritable: <path>: <errno text>)",
    exit 0. An unreadable log is never treated as empty (that would silently disable dedup).
  - Never use bare except; OSError surfaces, no silent failures.

Row schema (PredictionRow TypedDict, PINNED_KEYS order):
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
from datetime import datetime, timezone
from typing import Any, Dict, Final, List, Literal, NoReturn, Optional, Tuple, TypedDict


Command = Literal["expert-plan", "expert-review"]
Confidence = Literal["high", "medium"]
ActualSource = Literal["option", "free_text", "accepted-on-continue"]
AnswerSource = Literal["answered", "overridden", "accepted-on-continue"]

# PINNED_KEYS defines the exact order of row fields (decision-level invariant).
PINNED_KEYS: Final = (
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


class PredictionRow(TypedDict):
    """One prediction log row. Total: every PINNED_KEYS entry is present."""
    ts: str
    repo_key: str
    command: Command
    run: str
    question_id: str
    header: str
    recommended: Optional[str]
    predicted: str
    confidence: Confidence
    actual: str
    hit: Optional[bool]
    citations: List[int]
    actual_source: ActualSource
    anchored: bool


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that reports errors on one stderr line and exits 2."""

    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        sys.stderr.write("precedent-log: %s\n" % message)
        raise SystemExit(2)


class _RowError(Exception):
    """A validated block could not become a row. Carries the offending field name."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def _stderr(message: str) -> None:
    """Print a message to stderr on one line."""
    sys.stderr.write(message + "\n")


def _skip(reason: str) -> NoReturn:
    """Print the single no-log stderr line and exit 0 (a data-side outcome)."""
    _stderr("precedent-log: nothing logged (%s)" % reason)
    sys.exit(0)


def _exc_text(exc: BaseException) -> str:
    """The errno text for an OSError when present, else the exception's own text."""
    strerror = getattr(exc, "strerror", None)
    return str(strerror) if strerror else str(exc)


def _fold(repo_key: str) -> str:
    """Normalize repo_key to lowercase (same fold as precedent-digest.py)."""
    return repo_key.lower()


def _log_path(log_root: str, repo_key: str) -> str:
    """The predictions.jsonl path for one repo under log_root."""
    return os.path.join(log_root, _fold(repo_key), "predictions.jsonl")


def _validate_repo_key(repo_key: str, log_root: str) -> bool:
    """Validate repo_key format and ensure resolved path is under log_root.

    repo_key should be hyphen-separated lowercase alphanumerics.
    Returns True if valid, False otherwise.
    """
    # Check format: alphanumeric and hyphens only, must contain hyphen
    if not repo_key or "-" not in repo_key:
        return False
    # Allowlist: lowercase alphanumeric, hyphen
    if not all(c.isalnum() or c == "-" for c in repo_key):
        return False

    # Check path safety: resolved path must be under log_root
    try:
        log_path = _log_path(log_root, repo_key)

        # Resolve both paths
        resolved_log_root = os.path.realpath(log_root)
        resolved_log_path = os.path.realpath(log_path) if os.path.exists(log_path) else os.path.normpath(log_path)

        # Check that log_path is under log_root
        if not resolved_log_path.startswith(resolved_log_root + os.sep) and resolved_log_path != resolved_log_root:
            return False
    except (OSError, ValueError):
        return False

    return True


def _load_validated(path: str) -> Optional[Dict[str, Any]]:
    """Load precedent-validated.json. None when missing, unparseable, not status 'ok', or without blocks."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") != "ok":
        return None
    blocks = data.get("blocks")
    if not isinstance(blocks, dict) or not blocks:
        return None
    return data


def _load_questions(path: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """Load precedent-questions.json as {id: question}. None when missing or without string ids."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    questions_list = data.get("questions", [])
    if not isinstance(questions_list, list):
        return None
    # Build a dict keyed by id for quick lookup
    questions: Dict[str, Dict[str, Any]] = {}
    for q in questions_list:
        if isinstance(q, dict):
            qid = q.get("id")
            if isinstance(qid, str):
                questions[qid] = q
    return questions if questions else None


def _load_normalized_answers(path: str) -> Optional[Dict[str, Tuple[str, AnswerSource]]]:
    """Load answers from normalized JSON file: {"answers": {"Q1": "...", ...}}

    Returns dict: {question_id: (value, source)} where source is "answered" for normalized answers.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    answers_raw = data.get("answers", {})
    if not isinstance(answers_raw, dict):
        return None
    # All normalized answers are sourced from "answered"
    result: Dict[str, Tuple[str, AnswerSource]] = {}
    for k, v in answers_raw.items():
        if isinstance(k, str) and isinstance(v, str):
            result[k] = (v, "answered")
    return result if result else None


def _load_flow_answers(paths: List[str]) -> Optional[Dict[str, Tuple[str, AnswerSource]]]:
    """Load and merge flow-mode answers from multiple files.

    Reads each file's top-level 'answers' or 'items' key.
    Merges with latest file winning per handle.
    Only includes handles with disposition 'answered', 'overridden', or 'accepted-on-continue'.
    An unreadable or malformed round file is reported on stderr and skipped; the other
    rounds still contribute. Returns dict: {question_id: (value, source)} where source is
    the disposition, or None when no usable answers remain.
    """
    merged: Dict[str, Tuple[str, AnswerSource]] = {}

    for path in paths:
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            _stderr("precedent-log: skipped unreadable round file %s: %s" % (path, _exc_text(exc)))
            continue

        if not isinstance(data, dict):
            _stderr("precedent-log: skipped round file %s: top level is not a JSON object" % path)
            continue

        # Try 'answers' key first (normal shape), then 'items' (documented fallback)
        answers_section = data.get("answers") or data.get("items")
        if not isinstance(answers_section, dict):
            answers_section = {}

        for handle, item in answers_section.items():
            if not isinstance(handle, str):
                continue
            # Handle both direct string values and item objects with disposition
            if isinstance(item, str):
                # Direct value (old shape); source is "answered"
                merged[handle] = (item, "answered")
            elif isinstance(item, dict):
                disposition = item.get("disposition")
                # Only include answered, overridden, or accepted-on-continue
                if disposition in ("answered", "overridden", "accepted-on-continue"):
                    value = item.get("value", "")
                    if isinstance(value, str):
                        merged[handle] = (value, disposition)
            # unresolved, accepted (D/W silence) give no row

    return merged if merged else None


def classify(value: str, options: List[str]) -> Tuple[str, Literal["option", "free_text"]]:
    """Classify an answer value as option or free_text.

    Returns (actual, actual_source) where:
      - actual: canonical label for option, or normalized text for free_text
      - actual_source: "option" or "free_text"
    """
    if not isinstance(value, str):
        # Treat non-string values as free_text, normalized to string
        normalized = " ".join(str(value).split())[:500]
        return (normalized, "free_text")

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
            if isinstance(opt, str) and opt and (opt[0].upper() == letter) and len(opt) > 1 and opt[1] in (".", " ")
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
    command: Command,
    run: str,
    question_id: str,
    question_block: Any,
    question_meta: Optional[Dict[str, Any]],
    actual: str,
    actual_source: ActualSource,
    anchored: bool,
) -> PredictionRow:
    """Build one prediction row from a validated block.

    Raises _RowError naming the first field that is invalid. Validates: hit <=>
    actual_source invariant, confidence in {high, medium}.
    """
    if not isinstance(question_block, dict):
        raise _RowError("block")

    # header and recommended come from questions metadata
    header = ""
    recommended: Optional[str] = None
    if question_meta:
        header_val = question_meta.get("header")
        if isinstance(header_val, str):
            header = header_val
        recommended = question_meta.get("recommended")

    predicted_val = question_block.get("prediction")
    if not isinstance(predicted_val, str):
        raise _RowError("prediction")
    predicted: str = predicted_val

    confidence = question_block.get("confidence")
    if confidence not in ("high", "medium"):
        raise _RowError("confidence")

    precedent_lines = question_block.get("precedent_lines", [])
    if not isinstance(precedent_lines, list):
        raise _RowError("precedent_lines")

    # Extract citations (ref integers from precedent_lines)
    citations: List[int] = []
    for line in precedent_lines:
        if not isinstance(line, dict):
            raise _RowError("precedent_lines")
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

    # Compute hit: predicted == actual when actual_source == "option", else None
    hit: Optional[bool] = None
    if actual_source == "option":
        hit = predicted == actual
    elif actual_source in ("free_text", "accepted-on-continue"):
        hit = None
    else:
        raise _RowError("actual_source")

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
        "citations": dedup_citations,
        "actual_source": actual_source,
        "anchored": anchored,
    }
    return row


def _read_existing_keys(log_path: str) -> set:
    """Read existing (run, question_id, command) keys from the log.

    A missing log has no keys. Any other OSError propagates: an unreadable log must not
    look empty, because that would silently disable dedup. The caller reports it as
    log-unwritable. Dedup as the guarantee: all logged rows are unique per this key.
    """
    keys: set = set()
    try:
        f = open(log_path)
    except FileNotFoundError:
        return keys
    with f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    # Skip non-object lines
                    continue
                run_val = row.get("run")
                qid_val = row.get("question_id")
                cmd_val = row.get("command")
                # Only add hashable (string) keys
                if isinstance(run_val, str) and isinstance(qid_val, str) and isinstance(cmd_val, str):
                    key = (run_val, qid_val, cmd_val)
                    keys.add(key)
            except (json.JSONDecodeError, TypeError):
                # Skip malformed lines
                pass
    return keys


def _append_rows(log_dir: str, repo_key: str, rows: List[PredictionRow]) -> Tuple[int, int]:
    """Append rows to the log, skipping duplicates.

    Returns (logged_count, skipped_count).
    Raises OSError on read/write failure.
    """
    log_subdir = os.path.join(log_dir, _fold(repo_key))
    log_path = os.path.join(log_subdir, "predictions.jsonl")

    # Read existing keys
    existing_keys = _read_existing_keys(log_path)

    # Filter out duplicates
    new_rows: List[PredictionRow] = []
    skipped_count = 0
    for row in rows:
        key = (row.get("run"), row.get("question_id"), row.get("command"))
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

    # Prefix newline if file lacks one (F13: torn trailing line)
    batch_text = "\n".join(lines) + "\n"
    if os.path.exists(log_path) and os.path.getsize(log_path) > 0:
        with open(log_path, "rb") as f:
            f.seek(-1, 2)  # Seek to last byte
            last_byte = f.read(1)
            if last_byte and last_byte != b"\n":
                batch_text = "\n" + batch_text

    fd = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        encoded = batch_text.encode("utf-8")
        written = 0
        while written < len(encoded):
            n = os.write(fd, encoded[written:])
            if n == 0:
                raise OSError("os.write returned 0")
            written += n
    finally:
        os.close(fd)

    return (len(new_rows), skipped_count)


def _build_parser() -> _Parser:
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
    return parser


def _check_answer_flags(parser: _Parser, args: argparse.Namespace) -> None:
    """Exactly one of --answers and --flow-answers must be given (argv error, exit 2)."""
    has_answers = args.answers is not None
    has_flow_answers = len(args.flow_answers) > 0
    if has_answers and has_flow_answers:
        parser.error("--answers and --flow-answers are mutually exclusive")
    if not has_answers and not has_flow_answers:
        parser.error("one of --answers or --flow-answers is required")


def _trace_gate(trace_file: str) -> None:
    """Proceed only when the trace's first line starts with 'shown'. Otherwise exit 0 with a reason."""
    try:
        with open(trace_file) as f:
            first_line = f.readline().rstrip("\n")
    except (OSError, ValueError) as exc:
        _skip("trace-file-missing: %s: %s" % (trace_file, _exc_text(exc)))
    if not first_line.startswith("shown"):
        _skip(
            "trace-not-shown: %s does not start with 'shown', so Pat did not speak "
            "and nothing is expected to be logged" % trace_file
        )


def _repo_key_gate(repo_key: str, log_root: str) -> None:
    """Proceed only for a hyphen-separated key that resolves under log_root. Otherwise exit 0."""
    if not _validate_repo_key(repo_key, log_root):
        _skip("bad-repo-key: %r must be hyphen-separated alphanumerics (owner-name) resolving under %s" % (repo_key, log_root))


def _load_inputs(args: argparse.Namespace) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Tuple[str, AnswerSource]]]:
    """Load validated blocks, questions and answers. Exits 0 with a reason on any invalid input."""
    validated = _load_validated(args.validated)
    if not validated:
        _skip("validated-invalid: %s is missing, unreadable, not status 'ok', or has no blocks" % args.validated)

    questions = _load_questions(args.questions)
    if not questions:
        _skip("questions-invalid: %s is missing, unreadable, or has no question objects with string ids" % args.questions)

    if args.answers is not None:
        answers = _load_normalized_answers(args.answers)
        answers_source = args.answers
    else:
        answers = _load_flow_answers(args.flow_answers)
        answers_source = ", ".join(args.flow_answers)
    if not answers:
        _skip("answers-invalid: no usable answers in %s" % answers_source)

    return validated, questions, answers


def _options_of(question_meta: Optional[Dict[str, Any]]) -> List[str]:
    """String option labels from the questions file (not from the validated block)."""
    if not question_meta:
        return []
    opts = question_meta.get("options", [])
    if not isinstance(opts, list):
        return []
    return [o for o in opts if isinstance(o, str)]


def _build_rows(
    validated: Dict[str, Any],
    questions: Dict[str, Dict[str, Any]],
    answers: Dict[str, Tuple[str, AnswerSource]],
    args: argparse.Namespace,
    ts: str,
    anchored: bool,
) -> Tuple[List[PredictionRow], int]:
    """One row per validated block that has an answer. Returns (rows, unanswered count).

    All-or-nothing: a block that cannot be built drops the whole run (exit 0, nothing
    logged). The stderr reason names the question id and the offending field.
    """
    rows: List[PredictionRow] = []
    unanswered = 0
    for question_id, question_block in validated["blocks"].items():
        if question_id not in answers:
            unanswered += 1
            continue

        answer_value, answer_source = answers[question_id]
        question_meta = questions.get(question_id)

        # If source is accepted-on-continue, use value and source directly
        if answer_source == "accepted-on-continue":
            actual: str = answer_value
            actual_source: ActualSource = "accepted-on-continue"
        else:
            # Otherwise, classify the answer against the question's own options
            actual, actual_source = classify(answer_value, _options_of(question_meta))

        try:
            row = _build_row(
                ts=ts,
                repo_key=args.repo_key,
                command=args.command,
                run=args.run,
                question_id=question_id,
                question_block=question_block,
                question_meta=question_meta,
                actual=actual,
                actual_source=actual_source,
                anchored=anchored,
            )
        except _RowError as exc:
            _skip("row-build-failed: question %s, field %s" % (question_id, exc.field))
        rows.append(row)

    return rows, unanswered


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.subcommand != "append":
        parser.error("only 'append' subcommand is supported")

    _check_answer_flags(parser, args)
    _trace_gate(args.trace_file)
    _repo_key_gate(args.repo_key, args.log_root)

    validated, questions, answers = _load_inputs(args)

    # At this point, trace_file starts with "shown", so anchored is True
    anchored = True
    # Generate timestamp once before the loop (F15)
    ts = datetime.now(timezone.utc).isoformat()

    rows, unanswered = _build_rows(validated, questions, answers, args, ts, anchored)
    if not rows:
        _skip("no-answered-questions: none of the %d validated blocks has an answer" % len(validated["blocks"]))

    try:
        logged_count, skipped_count = _append_rows(args.log_root, args.repo_key, rows)
    except OSError as exc:
        _skip("log-unwritable: %s: %s" % (_log_path(args.log_root, args.repo_key), _exc_text(exc)))

    _stderr("precedent-log: logged %d, skipped-duplicate %d, unanswered %d" % (logged_count, skipped_count, unanswered))
    sys.exit(0)


if __name__ == "__main__":
    main()
