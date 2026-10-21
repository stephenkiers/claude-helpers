#!/usr/bin/env python3
"""
Flow redo utilities for /expert-flow.

Provides subcommands to sanitize step notes, archive step artifacts, and restore
previous attempts.

Exit codes:
  0: success
  1: data/contract violation (message on stderr, no writes)
  2: argparse usage error
  3: sanitize-only: result is empty/whitespace
  4: sanitize-only: HUMAN_TEXT_EOF or REASON_EOF found in text
"""

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import NoReturn


def error(msg: str) -> NoReturn:
    """Print error to stderr and exit with code 1."""
    print(f"flow-redo: {msg}", file=sys.stderr)
    sys.exit(1)


def usage_error(msg: str) -> NoReturn:
    """Print error to stderr and exit with code 2."""
    print(f"flow-redo: {msg}", file=sys.stderr)
    sys.exit(2)


def sanitize_error(msg: str, code: int) -> NoReturn:
    """Print error to stderr and exit with code for sanitize."""
    print(f"flow-redo: {msg}", file=sys.stderr)
    sys.exit(code)


def validate_nn(nn: str) -> None:
    """Validate NN is digits only."""
    if not nn or not nn.isdigit():
        error(f"invalid NN: must be digits only, got '{nn}'")


def validate_step(step: str) -> None:
    """Validate STEP matches [a-z][a-z0-9-]*."""
    if not step or not re.fullmatch(r"[a-z][a-z0-9-]*", step):
        error(f"invalid STEP: must match [a-z][a-z0-9-]*, got '{step}'")


def validate_attempt(attempt: str) -> int:
    """Validate and parse attempt as positive integer."""
    try:
        j = int(attempt)
        if j <= 0:
            error(f"invalid attempt: must be positive integer, got {j}")
        return j
    except ValueError:
        error(f"invalid attempt: must be positive integer, got '{attempt}'")


def cmd_sanitize() -> None:
    """Read note from stdin, write sanitized note to stdout.

    Order of sanitization:
    1. Normalize CRLF and lone CR to LF
    2. Remove control characters (keep \n and \t)
    3. Remove <<<NOTE and NOTE>>> repeatedly until fixed point
    4. Trim (strip)
    5. Truncate to 1000 characters
    6. Re-check for tokens and repeat loop until stable

    Exit codes:
      0: success
      3: result is empty/whitespace only
      4: HUMAN_TEXT_EOF or REASON_EOF found in raw or sanitised text
    """
    # Read stdin as bytes, decode with errors='replace'
    try:
        raw_bytes = sys.stdin.buffer.read()
    except AttributeError:
        # Fallback for non-binary mode
        raw_bytes = sys.stdin.read().encode("utf-8", errors="replace")

    raw_text = raw_bytes.decode("utf-8", errors="replace")
    original_len = len(raw_text)

    # Check for forbidden tokens in raw text (any line equal to sentinel)
    for line in raw_text.split('\n'):
        if line == "HUMAN_TEXT_EOF" or line == "REASON_EOF":
            sanitize_error("HUMAN_TEXT_EOF or REASON_EOF found as exact line", 4)

    # Sanitization loop
    text = raw_text
    while True:
        # 1. Normalize CRLF and lone CR to LF
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        # 2. Remove control characters (keep \n and \t)
        # Control chars are 0x00-0x08, 0x0B-0x0C, 0x0E-0x1F, 0x7F
        cleaned = []
        for ch in text:
            code = ord(ch)
            # Keep \n (0x0A) and \t (0x09)
            if ch == "\n" or ch == "\t":
                cleaned.append(ch)
            # Remove control chars
            elif code < 0x20 or code == 0x7F:
                pass
            else:
                cleaned.append(ch)
        text = "".join(cleaned)

        # 3. Remove <<<NOTE and NOTE>>> repeatedly until fixed point
        old_text = text
        while "<<<NOTE" in text or "NOTE>>>" in text:
            text = text.replace("<<<NOTE", "")
            text = text.replace("NOTE>>>", "")
        token_removed = text != old_text

        # 4. Trim (strip)
        text = text.strip()

        # 5. Truncate to 1000 characters
        text = text[:1000]

        # 6. Check if stable: if tokens were removed in this iteration,
        # we need to re-check because new tokens might form across truncation boundary
        if not token_removed or text == old_text:
            break

    # Check for forbidden tokens in sanitized text (any line equal to sentinel)
    for line in text.split('\n'):
        if line == "HUMAN_TEXT_EOF" or line == "REASON_EOF":
            sanitize_error("HUMAN_TEXT_EOF or REASON_EOF found as exact line", 4)

    # Check if result is empty/whitespace only
    if not text or not text.strip():
        sanitize_error("sanitized text is empty or whitespace only", 3)

    kept_len = len(text)
    print(f"original_len={original_len} kept_len={kept_len}", file=sys.stderr)
    print(text, end="")


def cmd_archive(flow_dir: Path, nn: str, step: str, attempt: int) -> None:
    """Archive step artifacts for a completed attempt.

    Copies:
      steps/NN-S.md -> steps/NN-S.aJ.md
      steps/NN-S.audit.md -> steps/NN-S.aJ.audit.md
      gates/NN-S.md -> gates/NN-S.aJ.md

    Validates before writing: receipt must have Decision: OK with <!-- step-end -->,
    audit and gate digest must exist and not be stubs.

    Refuses if archive files already exist with differing content.
    Succeeds if byte-identical archive already exists.
    """
    validate_nn(nn)
    validate_step(step)

    # Build source and archive file paths
    receipt_src = flow_dir / f"steps/{nn}-{step}.md"
    audit_src = flow_dir / f"steps/{nn}-{step}.audit.md"
    digest_src = flow_dir / f"gates/{nn}-{step}.md"

    archive_suffix = f".a{attempt}.md"
    receipt_archive = flow_dir / f"steps/{nn}-{step}{archive_suffix}"
    audit_archive = flow_dir / f"steps/{nn}-{step}.a{attempt}.audit.md"
    digest_archive = flow_dir / f"gates/{nn}-{step}{archive_suffix}"

    # Validate all source files exist
    if not receipt_src.exists():
        error(f"receipt not found: {receipt_src}")
    if not audit_src.exists():
        error(f"audit not found: {audit_src}")
    if not digest_src.exists():
        error(f"digest not found: {digest_src}")

    # Read and validate receipt
    try:
        receipt_content = receipt_src.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        error(f"cannot read receipt: {e}")

    # Check receipt has Decision: OK and <!-- step-end -->
    if "Decision: OK" not in receipt_content:
        error("receipt does not have Decision: OK")
    if "<!-- step-end -->" not in receipt_content:
        error("receipt does not have <!-- step-end -->")

    # Check audit is not a stub
    try:
        audit_content = audit_src.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        error(f"cannot read audit: {e}")
    if audit_content.startswith("STUB"):
        error("audit is a stub")

    # Check digest is not a stub
    try:
        digest_content = digest_src.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        error(f"cannot read digest: {e}")
    if digest_content.startswith("STUB"):
        error("digest is a stub")

    # Check for archive collisions (byte-identical is OK, different is error)
    for existing_path, content in [
        (audit_archive, audit_content),
        (digest_archive, digest_content),
        (receipt_archive, receipt_content),
    ]:
        if existing_path.exists():
            try:
                existing_content = existing_path.read_text(encoding="utf-8")
                if existing_content != content:
                    error(f"archive file exists with differing content: {existing_path}")
            except (OSError, UnicodeDecodeError) as e:
                error(f"cannot read existing archive: {e}")

    # All validation passed; now write via temp files
    # Order: audit, digest, receipt
    try:
        for archive_path, content in [
            (audit_archive, audit_content),
            (digest_archive, digest_content),
            (receipt_archive, receipt_content),
        ]:
            if archive_path.exists():
                # Byte-identical, skip
                existing = archive_path.read_text(encoding="utf-8")
                if existing == content:
                    continue

            # Write via temp file
            temp_fd, temp_path = tempfile.mkstemp(
                dir=archive_path.parent,
                prefix=f".{archive_path.name}-",
            )
            try:
                with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(temp_path, str(archive_path))
            except Exception:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise

    except (OSError, IOError) as e:
        error(f"write error: {e}")


def cmd_restore(flow_dir: Path, nn: str, step: str, attempt: int) -> None:
    """Restore step artifacts from archive.

    Restores:
      steps/NN-S.aJ.md -> steps/NN-S.md
      steps/NN-S.aJ.audit.md -> steps/NN-S.audit.md
      gates/NN-S.aJ.md -> gates/NN-S.md

    Validates all archive files exist and receipt has Decision: OK with <!-- step-end -->
    before writing anything.

    Restores in order: audit, digest, receipt (each atomic).

    Prints the restored receipt's FINAL_PLAN_PATH: or REVIEW_DIR: line to stdout.
    """
    validate_nn(nn)
    validate_step(step)

    # Build archive and target file paths
    archive_suffix = f".a{attempt}.md"
    receipt_archive = flow_dir / f"steps/{nn}-{step}{archive_suffix}"
    audit_archive = flow_dir / f"steps/{nn}-{step}.a{attempt}.audit.md"
    digest_archive = flow_dir / f"gates/{nn}-{step}{archive_suffix}"

    receipt_target = flow_dir / f"steps/{nn}-{step}.md"
    audit_target = flow_dir / f"steps/{nn}-{step}.audit.md"
    digest_target = flow_dir / f"gates/{nn}-{step}.md"

    # Validate all archive files exist
    if not receipt_archive.exists():
        error(f"archive not found: {receipt_archive}")
    if not audit_archive.exists():
        error(f"archive not found: {audit_archive}")
    if not digest_archive.exists():
        error(f"archive not found: {digest_archive}")

    # Read and validate receipt archive
    try:
        receipt_content = receipt_archive.read_text(encoding="utf-8")
        audit_content = audit_archive.read_text(encoding="utf-8")
        digest_content = digest_archive.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        error(f"cannot read archive: {e}")

    # Check receipt has Decision: OK and <!-- step-end -->
    if "Decision: OK" not in receipt_content:
        error("archive receipt does not have Decision: OK")
    if "<!-- step-end -->" not in receipt_content:
        error("archive receipt does not have <!-- step-end -->")

    # All validation passed; now restore via temp files
    # Order: audit, digest, receipt
    try:
        for target_path, content in [
            (audit_target, audit_content),
            (digest_target, digest_content),
            (receipt_target, receipt_content),
        ]:
            temp_fd, temp_path = tempfile.mkstemp(
                dir=target_path.parent,
                prefix=f".{target_path.name}-",
            )
            try:
                with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(temp_path, str(target_path))
            except Exception:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise

    except (OSError, IOError) as e:
        error(f"write error: {e}")

    # Print FINAL_PLAN_PATH or REVIEW_DIR from receipt
    for line in receipt_content.split("\n"):
        if line.startswith("FINAL_PLAN_PATH:") or line.startswith("REVIEW_DIR:"):
            print(line)


def main() -> None:
    """Parse arguments and dispatch to subcommand."""
    parser = argparse.ArgumentParser(
        description="Flow redo utilities for /expert-flow",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommand")

    # sanitize subcommand
    subparsers.add_parser(
        "sanitize",
        help="Sanitize note from stdin, write to stdout",
    )

    # archive subcommand
    archive_parser = subparsers.add_parser(
        "archive",
        help="Archive step artifacts",
    )
    archive_parser.add_argument("--flow-dir", required=True, help="Flow directory")
    archive_parser.add_argument("--nn", required=True, help="Step number (digits only)")
    archive_parser.add_argument("--step", required=True, help="Step name ([a-z][a-z0-9-]*)")
    archive_parser.add_argument("--attempt", required=True, help="Attempt number (positive integer)")

    # restore subcommand
    restore_parser = subparsers.add_parser(
        "restore",
        help="Restore step artifacts from archive",
    )
    restore_parser.add_argument("--flow-dir", required=True, help="Flow directory")
    restore_parser.add_argument("--nn", required=True, help="Step number (digits only)")
    restore_parser.add_argument("--step", required=True, help="Step name ([a-z][a-z0-9-]*)")
    restore_parser.add_argument("--attempt", required=True, help="Attempt number (positive integer)")

    args = parser.parse_args()

    try:
        if args.command == "sanitize":
            cmd_sanitize()

        elif args.command == "archive":
            flow_dir = Path(args.flow_dir).expanduser().resolve()
            if not flow_dir.is_dir():
                error(f"flow-dir is not a directory: {args.flow_dir}")

            attempt = validate_attempt(args.attempt)
            cmd_archive(flow_dir, args.nn, args.step, attempt)

        elif args.command == "restore":
            flow_dir = Path(args.flow_dir).expanduser().resolve()
            if not flow_dir.is_dir():
                error(f"flow-dir is not a directory: {args.flow_dir}")

            attempt = validate_attempt(args.attempt)
            cmd_restore(flow_dir, args.nn, args.step, attempt)

    except SystemExit:
        raise


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        error(f"unexpected error: {e}")
