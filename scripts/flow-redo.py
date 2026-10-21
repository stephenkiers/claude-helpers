#!/usr/bin/env python3
"""
Flow redo utilities for /expert-flow.

Subcommands:

  sanitize [--file PATH]
      Sanitise a redo note read from PATH, or from stdin when --file is omitted,
      and write the result to stdout. original_len and kept_len go to stderr.

  archive --flow-dir D --nn NN --step S --attempt J
      Copy steps/NN-S.md, steps/NN-S.audit.md and gates/NN-S.md to the attempt-J
      archive names steps/NN-S.aJ.md, steps/NN-S.aJ.audit.md and gates/NN-S.aJ.md.
      J must equal the receipt's ATTEMPT=<n> line. Prints J on stdout.
      The caller must persist J: restore needs it to find the archive.

  restore --flow-dir D --nn NN --step S --attempt J
      Restore the complete attempt-J archive over the live files. The archive is
      validated first (all three files present, receipt valid, receipt ATTEMPT == J).
      Prints J on stdout and nothing else. The restored receipt keeps its archived
      ATTEMPT line, so the caller must set attempts[STEP] = J after a restore.

  check --flow-dir D --step S --attempt J [--nn NN]
      Exit 0 and print J only if a complete, valid archive for exactly attempt J
      exists. Otherwise exit 5 with a one-line reason. Without --nn the NN is taken
      from the archive file names, which must identify exactly one step archive.

Exit codes:
  0: success
  1: refusal or invalid input (one-line message on stderr, nothing written)
  2: argparse usage error
  3: sanitize: result is empty or whitespace only
  4: sanitize: a line reads HUMAN_TEXT_EOF or REASON_EOF
  5: check: no complete archive for that attempt
"""

import argparse
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import List, NamedTuple, NoReturn, Optional, Tuple

MAX_NOTE_CHARS = 1000
NOTE_TOKENS = ("<<<NOTE", "NOTE>>>")
SENTINELS = ("HUMAN_TEXT_EOF", "REASON_EOF")
SENTINEL_MSG = (
    "note has a line reading HUMAN_TEXT_EOF or REASON_EOF, which would end the heredoc early; "
    "remove that line"
)
INVISIBLE_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")

NN_RE = re.compile(r"[0-9]+")
STEP_RE = re.compile(r"[a-z][a-z0-9-]*")
ATTEMPT_RE = re.compile(r"[1-9][0-9]*")
ATTEMPT_LINE_RE = re.compile(r"^ATTEMPT=([1-9][0-9]*)$", re.MULTILINE)
DECISION_OK_RE = re.compile(r"^Decision: OK$", re.MULTILINE)
STEP_END_RE = re.compile(r"^<!-- step-end -->$", re.MULTILINE)

EXIT_REFUSED = 1
EXIT_EMPTY = 3
EXIT_SENTINEL = 4
EXIT_NO_ARCHIVE = 5


class ArchiveProblem(Exception):
    """Plain-language reason an archive or receipt cannot be used."""


class Paths(NamedTuple):
    receipt: Path
    audit: Path
    digest: Path


class Archive(NamedTuple):
    attempt: int
    receipt: bytes
    audit: bytes
    digest: bytes


def error(msg: str, code: int = EXIT_REFUSED) -> NoReturn:
    """Print a one-line message to stderr and exit with the given code."""
    print(f"flow-redo: {msg}", file=sys.stderr)
    sys.exit(code)


def validate_nn(nn: str) -> str:
    """Return NN if it is digits only."""
    if not NN_RE.fullmatch(nn):
        error(f"invalid NN '{nn}': use digits only, for example 01")
    return nn


def validate_step(step: str) -> str:
    """Return STEP if it matches [a-z][a-z0-9-]*."""
    if not STEP_RE.fullmatch(step):
        error(f"invalid STEP '{step}': use lowercase letters, digits and hyphens, starting with a letter")
    return step


def validate_attempt(attempt: str) -> int:
    """Return ATTEMPT as an int if it is a positive integer with no leading zero."""
    if not ATTEMPT_RE.fullmatch(attempt):
        error(f"invalid attempt '{attempt}': must be a positive integer with no leading zero")
    return int(attempt)


# ---------------------------------------------------------------------------
# sanitize
# ---------------------------------------------------------------------------

def _normalize_newlines(text: str) -> str:
    """Convert CRLF and lone CR to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _drop_invisible(text: str) -> str:
    """Drop control, format and line/paragraph separator characters, keeping LF and TAB."""
    return "".join(
        ch for ch in text
        if ch in "\n\t" or unicodedata.category(ch) not in INVISIBLE_CATEGORIES
    )


def _remove_note_tokens(text: str) -> str:
    """Remove <<<NOTE and NOTE>>> until none remain, so nested tokens cannot re-form."""
    while any(token in text for token in NOTE_TOKENS):
        for token in NOTE_TOKENS:
            text = text.replace(token, "")
    return text


def _has_sentinel_line(text: str) -> bool:
    """True if any line is exactly a heredoc sentinel."""
    return any(line in SENTINELS for line in text.split("\n"))


def sanitize_text(raw: str) -> str:
    """Return the sanitised note. Refusals are decided by the caller.

    Single pass: normalise newlines, drop invisible characters, remove note tokens to a
    fixed point, truncate to MAX_NOTE_CHARS characters, then strip. Removing tokens
    before truncating means truncation cannot form a new token, and stripping last
    means the kept text has no leading or trailing whitespace from the cut.
    """
    text = _normalize_newlines(raw)
    text = _drop_invisible(text)
    text = _remove_note_tokens(text)
    return text[:MAX_NOTE_CHARS].strip()


def _read_note(file_path: Optional[str]) -> str:
    """Read the raw note bytes from a file, or from stdin when no file is given."""
    if file_path is None:
        data = sys.stdin.buffer.read()
    else:
        try:
            with open(file_path, "rb") as f:
                data = f.read()
        except OSError as e:
            error(f"cannot read note file {file_path}: {e.strerror or e}")
    return data.decode("utf-8", errors="replace")


def cmd_sanitize(file_path: Optional[str]) -> None:
    """Sanitise a note and print it to stdout.

    Exit codes: 0 success; 3 empty or whitespace-only result; 4 sentinel line found
    in the raw note or in the sanitised result.
    """
    raw = _read_note(file_path)
    if _has_sentinel_line(_normalize_newlines(raw)):
        error(SENTINEL_MSG, EXIT_SENTINEL)
    text = sanitize_text(raw)
    if _has_sentinel_line(text):
        error(SENTINEL_MSG, EXIT_SENTINEL)
    if not text:
        error("note is empty or whitespace only after sanitising; nothing to record", EXIT_EMPTY)
    print(f"original_len={len(raw)} kept_len={len(text)}", file=sys.stderr)
    print(text, end="")


# ---------------------------------------------------------------------------
# archive / restore / check shared helpers
# ---------------------------------------------------------------------------

def _live_paths(flow: Path, nn: str, step: str) -> Paths:
    return Paths(
        receipt=flow / f"steps/{nn}-{step}.md",
        audit=flow / f"steps/{nn}-{step}.audit.md",
        digest=flow / f"gates/{nn}-{step}.md",
    )


def _archive_paths(flow: Path, nn: str, step: str, attempt: int) -> Paths:
    return Paths(
        receipt=flow / f"steps/{nn}-{step}.a{attempt}.md",
        audit=flow / f"steps/{nn}-{step}.a{attempt}.audit.md",
        digest=flow / f"gates/{nn}-{step}.a{attempt}.md",
    )


def _receipt_problem(text: str) -> Optional[str]:
    """Return a plain-language reason the receipt is invalid, or None if it is valid.

    Whole-line anchors, so 'XATTEMPT=1' or '  Decision: OK' do not count. This is the
    only receipt validator: archive, restore and check all call it through _parse_receipt.
    """
    if len(ATTEMPT_LINE_RE.findall(text)) != 1:
        return "must contain exactly one ATTEMPT=<n> line"
    if not DECISION_OK_RE.search(text):
        return "has no 'Decision: OK' line"
    if not STEP_END_RE.search(text):
        return "has no '<!-- step-end -->' line"
    return None


def _receipt_attempt(text: str) -> int:
    """Return the ATTEMPT number of a receipt that _receipt_problem accepts."""
    match = ATTEMPT_LINE_RE.search(text)
    if match is None:
        raise ValueError("receipt has no ATTEMPT line; call _receipt_problem first")
    return int(match.group(1))


def _parse_receipt(path: Path, data: bytes) -> int:
    """Validate receipt bytes and return their ATTEMPT number. Raises ArchiveProblem."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ArchiveProblem(f"receipt {path} is not valid UTF-8")
    problem = _receipt_problem(text)
    if problem is not None:
        raise ArchiveProblem(f"receipt {path} {problem}")
    return _receipt_attempt(text)


def _is_stub(data: bytes) -> bool:
    return data.startswith(b"STUB")


def _existing_bytes(path: Path) -> Optional[bytes]:
    """Return the file's bytes, or None if it does not exist."""
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError as e:
        error(f"cannot read {path}: {e.strerror or e}")


def _load_archive(flow: Path, nn: str, step: str, attempt: int) -> Archive:
    """Load the complete attempt-J archive or raise ArchiveProblem with the reason."""
    arch = _archive_paths(flow, nn, step, attempt)
    missing = [str(p) for p in arch if not p.is_file()]
    if missing:
        raise ArchiveProblem(f"no complete archive for attempt {attempt}; missing {', '.join(missing)}")
    try:
        receipt = arch.receipt.read_bytes()
        audit = arch.audit.read_bytes()
        digest = arch.digest.read_bytes()
    except OSError as e:
        raise ArchiveProblem(f"cannot read archive for attempt {attempt}: {e.strerror or e}")
    found = _parse_receipt(arch.receipt, receipt)
    if found != attempt:
        raise ArchiveProblem(f"archive receipt {arch.receipt} says ATTEMPT={found}, not {attempt}")
    for path, data in ((arch.audit, audit), (arch.digest, digest)):
        if _is_stub(data):
            raise ArchiveProblem(f"archive file {path} is a stub")
    return Archive(attempt=attempt, receipt=receipt, audit=audit, digest=digest)


def _stage_temp(path: Path, data: bytes) -> str:
    """Write data to a new temp file beside path and return the temp path."""
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        _unlink_quiet(temp)
        raise
    return temp


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace path with data atomically: stage a temp file, then os.replace it."""
    temp = _stage_temp(path, data)
    try:
        os.replace(temp, str(path))
    except BaseException:
        _unlink_quiet(temp)
        raise


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------

def _read_source(path: Path, label: str) -> bytes:
    if not path.is_file():
        error(f"cannot archive: {label} not found: {path}")
    try:
        return path.read_bytes()
    except OSError as e:
        error(f"cannot read {label} {path}: {e.strerror or e}")


def cmd_archive(flow: Path, nn: str, step: str, attempt: int) -> None:
    """Archive the live step files as attempt J and print J on stdout.

    Refuses (writing nothing) unless the receipt is valid, its ATTEMPT line equals J,
    the audit and gate digest exist and are not stubs, and no existing archive file
    holds different bytes. Writes audit, digest, then receipt: the receipt archive is
    written last, so a complete archive is the only state where check passes.
    """
    src = _live_paths(flow, nn, step)
    dst = _archive_paths(flow, nn, step, attempt)

    receipt = _read_source(src.receipt, "receipt")
    audit = _read_source(src.audit, "audit")
    digest = _read_source(src.digest, "gate digest")

    try:
        found = _parse_receipt(src.receipt, receipt)
    except ArchiveProblem as e:
        error(f"cannot archive attempt {attempt}: {e}")
    if found != attempt:
        error(f"cannot archive attempt {attempt}: {src.receipt} says ATTEMPT={found}, not {attempt}")
    if _is_stub(audit):
        error(f"cannot archive attempt {attempt}: audit {src.audit} is a stub")
    if _is_stub(digest):
        error(f"cannot archive attempt {attempt}: gate digest {src.digest} is a stub")

    writes = [(dst.audit, audit), (dst.digest, digest), (dst.receipt, receipt)]
    for path, data in writes:
        existing = _existing_bytes(path)
        if existing is not None and existing != data:
            error(f"archive file exists with differing content: {path}; attempt {attempt} not overwritten")

    for path, data in writes:
        if _existing_bytes(path) == data:
            continue
        try:
            _atomic_write(path, data)
        except OSError as e:
            error(f"cannot write {path}: {e.strerror or e}")
    print(attempt)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def _roll_back(previous: List[Tuple[Path, Optional[bytes]]], count: int) -> str:
    """Put back the first `count` files to their previous bytes. Returns a status note."""
    if count == 0:
        return "nothing had been replaced"
    failures = []
    for path, before in previous[:count]:
        try:
            if before is None:
                _unlink_quiet(str(path))
            else:
                _atomic_write(path, before)
        except OSError as e:
            failures.append(f"{path} ({e.strerror or e})")
    if failures:
        return "rollback failed, check by hand: " + "; ".join(failures)
    return f"rolled back {count} already-replaced file(s)"


def cmd_restore(flow: Path, nn: str, step: str, attempt: int) -> None:
    """Restore the complete attempt-J archive and print J on stdout.

    Stages all three files, then replaces them in order audit, digest, receipt. If a
    replace fails part-way, the files already replaced are put back to their previous
    bytes, so the live files are never left half-restored.
    """
    try:
        arch = _load_archive(flow, nn, step, attempt)
    except ArchiveProblem as e:
        error(f"cannot restore: {e}")

    live = _live_paths(flow, nn, step)
    writes: List[Tuple[Path, bytes]] = [
        (live.audit, arch.audit),
        (live.digest, arch.digest),
        (live.receipt, arch.receipt),
    ]
    for path, _ in writes:
        if path.exists() and not path.is_file():
            error(f"cannot restore: {path} exists and is not a regular file")
        if not os.access(path.parent, os.W_OK):
            error(f"cannot restore: directory is not writable: {path.parent}")

    previous = [(path, _existing_bytes(path)) for path, _ in writes]
    temps: List[str] = []
    replaced = 0
    failing = live.audit
    try:
        for path, data in writes:
            failing = path
            temps.append(_stage_temp(path, data))
        for index, (path, _) in enumerate(writes):
            failing = path
            os.replace(temps[index], str(path))
            replaced += 1
    except OSError as e:
        note = _roll_back(previous, replaced)
        error(f"cannot restore {failing}: {e.strerror or e}; {note}")
    finally:
        for temp in temps:
            _unlink_quiet(temp)
    print(arch.attempt)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def _discover_nn(flow: Path, step: str, attempt: int) -> str:
    """Find the single NN that has a steps/NN-STEP.aJ.md receipt archive."""
    pattern = re.compile(rf"([0-9]+)-{re.escape(step)}\.a{attempt}\.md")
    try:
        names = os.listdir(flow / "steps")
    except OSError:
        names = []
    found = sorted({m.group(1) for m in (pattern.fullmatch(name) for name in names) if m})
    if not found:
        error(f"no complete archive for attempt {attempt}; no steps/NN-{step}.a{attempt}.md found",
              EXIT_NO_ARCHIVE)
    if len(found) > 1:
        error(f"ambiguous: {step} attempt {attempt} is archived under NN {', '.join(found)}; pass --nn",
              EXIT_NO_ARCHIVE)
    return found[0]


def cmd_check(flow: Path, step: str, attempt: int, nn: Optional[str]) -> None:
    """Exit 0 and print J only if a complete, valid attempt-J archive exists."""
    resolved_nn = nn if nn is not None else _discover_nn(flow, step, attempt)
    try:
        arch = _load_archive(flow, resolved_nn, step, attempt)
    except ArchiveProblem as e:
        error(str(e), EXIT_NO_ARCHIVE)
    print(arch.attempt)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _resolve_flow_dir(raw: str) -> Path:
    flow = Path(raw).expanduser().resolve()
    if not flow.is_dir():
        error(f"flow dir does not exist or is not a directory: {raw}")
    return flow


def main() -> None:
    """Parse arguments, validate every input once, and dispatch to a subcommand."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommand")

    sanitize_parser = subparsers.add_parser("sanitize", help="Sanitise a note to stdout")
    sanitize_parser.add_argument("--file", metavar="PATH", help="read the note from PATH instead of stdin")

    archive_parser = subparsers.add_parser("archive", help="Archive step artifacts as attempt J")
    archive_parser.add_argument("--flow-dir", required=True, help="Flow directory")
    archive_parser.add_argument("--nn", required=True, help="Step number (digits only)")
    archive_parser.add_argument("--step", required=True, help="Step name ([a-z][a-z0-9-]*)")
    archive_parser.add_argument("--attempt", required=True, help="Attempt number J (positive integer)")

    restore_parser = subparsers.add_parser("restore", help="Restore attempt J from archive")
    restore_parser.add_argument("--flow-dir", required=True, help="Flow directory")
    restore_parser.add_argument("--nn", required=True, help="Step number (digits only)")
    restore_parser.add_argument("--step", required=True, help="Step name ([a-z][a-z0-9-]*)")
    restore_parser.add_argument("--attempt", required=True, help="Attempt number J (positive integer)")

    check_parser = subparsers.add_parser("check", help="Exit 0 only if a complete attempt-J archive exists")
    check_parser.add_argument("--flow-dir", required=True, help="Flow directory")
    check_parser.add_argument("--step", required=True, help="Step name ([a-z][a-z0-9-]*)")
    check_parser.add_argument("--attempt", required=True, help="Attempt number J (positive integer)")
    check_parser.add_argument("--nn", default=None, help="Step number; optional if unambiguous")

    args = parser.parse_args()

    if args.command == "sanitize":
        cmd_sanitize(args.file)
        return

    flow = _resolve_flow_dir(args.flow_dir)
    step = validate_step(args.step)
    attempt = validate_attempt(args.attempt)

    if args.command == "check":
        nn = validate_nn(args.nn) if args.nn is not None else None
        cmd_check(flow, step, attempt, nn)
    elif args.command == "archive":
        cmd_archive(flow, validate_nn(args.nn), step, attempt)
    else:
        cmd_restore(flow, validate_nn(args.nn), step, attempt)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        error(f"unexpected error: {e}")
