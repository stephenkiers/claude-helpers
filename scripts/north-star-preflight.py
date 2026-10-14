#!/usr/bin/env python3
"""North Star preflight: fail loudly when North Star Nick's inputs are missing (#273).

Defines, once, the North Star required-input contract, the blind vocabulary and the
`NORTH STAR BLIND:` line grammar. `/expert-plan` and the shared review panel call it; nothing else
re-implements the predicate. The script is side-effect free: it reads, prints and exits. It never
writes files (the orchestrator owns the per-run token file).

Subcommands:
    files    [--claude-dir DIR] [--allow-degraded]
        Required installed files (`scripts/in-flight-snapshot.py`, `prompts/north-star-scout.md`
        under the claude dir; each must exist, resolve to a regular file, and be non-empty) and
        `gh auth status`. A missing `gh` binary counts as a `gh-auth` failure.
    snapshot --dir DIR [--allow-degraded]
        Classifies the snapshot the orchestrator just wrote into DIR (`in-flight.md`,
        `in-flight.err`, `in-flight-issues.md`).
          hard:    `in-flight.md` missing or empty -> snapshot-empty
                   `in-flight.err` has both `issues:` and `prs:` keys -> snapshot-unavailable
          notice:  exactly one of `issues:` / `prs:`, an `epics:` key, or a missing or empty
                   `in-flight-issues.md` -> snapshot-partial=<issues|prs|epics|issue-bodies>
    blind-line --tokens-file PATH [--nick-file PATH] [token ...]
        Prints the canonical blind line for the de-duplicated union (first-seen order) of the
        tokens in PATH (one per line; a missing file is empty) and any extra tokens. Prints nothing
        and exits 0 when the union is empty. Exit 2 on any token outside the vocabulary.
        --nick-file adds the `nick=` tokens from the first line of Nick's output (only when that
        line starts with the blind prefix; a malformed piece becomes `nick=unparseable`, never an error).

Blind grammar: one line, exact prefix `NORTH STAR BLIND: `, then comma-space-separated tokens from
the closed vocabulary: snapshot-script, scout-prompt, gh-auth, snapshot-empty,
snapshot-unavailable, snapshot-partial=<issues|prs|epics|issue-bodies>,
scout-failed=<adrs|docs|in-flight|all>, nick=<free text, no commas or newlines>, and the
fallback nick=unparseable.

Output and exit codes (files, snapshot): stdout carries exactly one line: `OK`, `BLIND <tokens>`
(notice only) or `FAIL <tokens>`; tokens are space-separated. stderr carries a human remedy.
    0  OK, or notice-only BLIND, or a hard failure downgraded by --allow-degraded
    3  hard failure without --allow-degraded
    2  usage error or invalid token
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

BLIND_PREFIX = "NORTH STAR BLIND: "

FILE_TOKENS = ("snapshot-script", "scout-prompt")
REQUIRED_FILES = (
    ("snapshot-script", "scripts/in-flight-snapshot.py"),
    ("scout-prompt", "prompts/north-star-scout.md"),
)
PARTIAL_SECTIONS = ("issues", "prs", "epics", "issue-bodies")
SCOUT_LENSES = ("adrs", "docs", "in-flight", "all")
FIXED_TOKENS = ("snapshot-script", "scout-prompt", "gh-auth", "snapshot-empty", "snapshot-unavailable")

_ERR_KEY = re.compile(r"^(issues|prs|epics):")


def valid_token(token: str) -> bool:
    if token in FIXED_TOKENS:
        return True
    if token.startswith("snapshot-partial="):
        return token.split("=", 1)[1] in PARTIAL_SECTIONS
    if token.startswith("scout-failed="):
        return token.split("=", 1)[1] in SCOUT_LENSES
    if token.startswith("nick="):
        text = token[len("nick="):]
        return bool(text.strip()) and "," not in text and "\n" not in text and "\r" not in text
    return False


def _usable_file(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        return resolved.is_file() and resolved.stat().st_size > 0
    except (OSError, RuntimeError):
        return False


def _gh_ok() -> bool:
    try:
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _remedy(tokens: Sequence[str]) -> str:
    lines = []
    if any(t in FILE_TOKENS for t in tokens):
        lines.append("North Star inputs are not installed in ~/.claude: run /setup-local "
                     "(or ./install.sh) in claude-helpers. Retry will not help until fixed.")
    if "gh-auth" in tokens or any(t.startswith("snapshot-") for t in tokens):
        lines.append("North Star Nick needs GitHub issue and PR context: run `gh auth login` "
                     "(and check the repo's Issues/PRs are reachable), or pass --allow-degraded "
                     "to run blind on purpose. Retry will not help until fixed.")
    return "\n".join(lines)


def _emit(hard: List[str], notice: List[str], allow_degraded: bool) -> int:
    if hard and not allow_degraded:
        print("FAIL " + " ".join(hard))
        sys.stderr.write("north-star-preflight: FAIL " + " ".join(hard) + "\n" + _remedy(hard) + "\n")
        return 3
    tokens = hard + notice
    if tokens:
        print("BLIND " + " ".join(tokens))
        if hard:
            sys.stderr.write("north-star-preflight: continuing blind (--allow-degraded): "
                             + " ".join(hard) + "\n")
        return 0
    print("OK")
    return 0


def phase_files(claude_dir: Path, allow_degraded: bool) -> int:
    hard = [token for token, rel in REQUIRED_FILES if not _usable_file(claude_dir / rel)]
    if not _gh_ok():
        hard.append("gh-auth")
    return _emit(hard, [], allow_degraded)


def _read_err_keys(err: Path) -> List[str]:
    try:
        text = err.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    keys = []
    for line in text.splitlines():
        match = _ERR_KEY.match(line)
        if match and match.group(1) not in keys:
            keys.append(match.group(1))
    return keys


def phase_snapshot(directory: Path, allow_degraded: bool) -> int:
    hard: List[str] = []
    notice: List[str] = []
    md = directory / "in-flight.md"
    try:
        md_nonempty = md.is_file() and md.stat().st_size > 0
    except OSError:
        md_nonempty = False
    keys = _read_err_keys(directory / "in-flight.err")
    if not md_nonempty:
        hard.append("snapshot-empty")
    if "issues" in keys and "prs" in keys:
        hard.append("snapshot-unavailable")
    else:
        for key in ("issues", "prs"):
            if key in keys:
                notice.append("snapshot-partial=" + key)
    if "epics" in keys:
        notice.append("snapshot-partial=epics")
    bodies = directory / "in-flight-issues.md"
    try:
        if md_nonempty and (not bodies.is_file() or bodies.stat().st_size == 0):
            notice.append("snapshot-partial=issue-bodies")
    except OSError:
        pass
    return _emit(hard, notice, allow_degraded)


def parse_nick_file(path: Path) -> List[str]:
    """`nick=` tokens from the first line of Nick's output; `nick=unparseable` if that line breaks the grammar."""
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline().rstrip("\r\n")
    except OSError:
        return []
    if not first.startswith(BLIND_PREFIX):
        return []
    found: List[str] = []
    for piece in first[len(BLIND_PREFIX):].split(", "):
        piece = piece.strip()
        if valid_token(piece) and piece.startswith("nick="):
            found.append(piece)
        elif piece:
            found.append("nick=unparseable")
    return found


def phase_blind_line(tokens_file: Path, extra: Sequence[str], nick_file: Optional[Path] = None) -> int:
    if nick_file is not None:
        extra = list(extra) + parse_nick_file(nick_file)
    tokens: List[str] = []
    try:
        file_tokens = tokens_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        file_tokens = []
    for raw in list(file_tokens) + list(extra):
        token = raw.strip()
        if not token:
            continue
        if not valid_token(token):
            sys.stderr.write("north-star-preflight: invalid blind token: {!r}\n".format(token))
            return 2
        if token not in tokens:
            tokens.append(token)
    if tokens:
        print(BLIND_PREFIX + ", ".join(tokens))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="north-star-preflight.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="phase", required=True)
    p_files = sub.add_parser("files")
    p_files.add_argument("--claude-dir", default=os.path.join(os.path.expanduser("~"), ".claude"))
    p_files.add_argument("--allow-degraded", action="store_true")
    p_snap = sub.add_parser("snapshot")
    p_snap.add_argument("--dir", required=True)
    p_snap.add_argument("--allow-degraded", action="store_true")
    p_line = sub.add_parser("blind-line")
    p_line.add_argument("--tokens-file", required=True)
    p_line.add_argument("--nick-file", default=None)
    p_line.add_argument("tokens", nargs="*")
    args = parser.parse_args(argv)
    if args.phase == "files":
        return phase_files(Path(args.claude_dir), args.allow_degraded)
    if args.phase == "snapshot":
        return phase_snapshot(Path(args.dir), args.allow_degraded)
    return phase_blind_line(Path(args.tokens_file), args.tokens, Path(args.nick_file) if args.nick_file else None)


if __name__ == "__main__":
    sys.exit(main())
