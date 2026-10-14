#!/usr/bin/env python3
"""North Star preflight: fail loudly when North Star Nick's inputs are missing (#273).

Defines, once, the North Star required-input contract, the blind vocabulary and the
`NORTH STAR BLIND:` line grammar. `/expert-plan` and the shared review panel call it; nothing else
re-implements the predicate. The script is side-effect free: it reads, prints and exits. It never
writes files (the orchestrator owns the per-run token file).

Subcommands:
    files    [--claude-dir DIR] [--allow-degraded]
        Required installed files (`scripts/in-flight-snapshot.py`, `prompts/north-star-scout.md`
        under the claude dir) are hard: each must exist, resolve to a regular file, and be non-empty.
        `gh auth status` is advisory only: a failed, missing or timed-out check emits a `gh-auth`
        notice (BLIND, exit 0). Host-global gh auth must not hard-block a working user; the
        `snapshot` phase is the authoritative gate on real fetch outcomes. A missing `gh` binary
        counts as a `gh-auth` notice.
    snapshot --dir DIR [--allow-degraded]
        Classifies the snapshot the orchestrator just wrote into DIR (`in-flight.md`,
        `in-flight.err`, `in-flight-issues.md`).
          hard:    `in-flight.md` missing or empty -> snapshot-empty
                   `in-flight.err` has both `issues:` and `prs:` keys -> snapshot-unavailable
          notice:  exactly one of `issues:` / `prs:`, an `epics:` key, or a missing or empty
                   `in-flight-issues.md` -> snapshot-partial=<issues|prs|epics|issue-bodies>
    blind-line --tokens-file PATH [--nick-file PATH] [token ...]
        Prints the canonical blind line for the de-duplicated union (first-seen order) of the
        tokens in PATH (one token per line; a missing or undecodable file is empty) and any extra
        tokens. Prints nothing and exits 0 when the union is empty. An invalid token never drops the
        line: it is replaced by `nick=unparseable` (de-duplicated like any token), a warning goes to
        stderr, and the exit code is 0. Exit 2 is reserved for argparse usage errors.
        --nick-file adds the `nick=` tokens from Nick's output: the first line that starts with the
        blind prefix within the first few lines (so a Pass 1 heading above it does not hide it).
        Any malformed piece collapses the whole line to a single `nick=unparseable`; never an error.

Blind grammar: one line, exact prefix `NORTH STAR BLIND: `, then comma-space-separated tokens from
the closed vocabulary: snapshot-script, scout-prompt, gh-auth, snapshot-empty,
snapshot-unavailable, snapshot-partial=<issues|prs|epics|issue-bodies>,
scout-failed=<adrs|docs|in-flight|all>, nick=<free text, no commas or newlines>, and the
fallback nick=unparseable. `nick=unparseable` is a reserved sentinel: a free-text nick that is
literally "unparseable" is treated as the sentinel.

Output and exit codes (files, snapshot): stdout carries exactly one line: `OK`, `BLIND <tokens>`
or `FAIL <tokens>`. Tokens are space-separated, so callers consuming this stdout whitespace-split
it into tokens. `BLIND` lists the hard tokens first, then the notice tokens (hard tokens appear
only under --allow-degraded). `FAIL` lists hard tokens only: notice tokens are omitted. stderr
carries a human remedy.
    0  OK, or BLIND (notice only, or a hard failure downgraded by --allow-degraded)
    3  hard failure without --allow-degraded
    2  argparse usage error only
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

BLIND_PREFIX = "NORTH STAR BLIND: "
BLIND_SCAN_LINES = 5
NICK_MAX_CHARS = 200
UNPARSEABLE_TOKEN = "nick=unparseable"

REQUIRED_FILES = (
    ("snapshot-script", "scripts/in-flight-snapshot.py"),
    ("scout-prompt", "prompts/north-star-scout.md"),
)
FILE_TOKENS = tuple(token for token, _ in REQUIRED_FILES)
PARTIAL_SECTIONS = ("issues", "prs", "epics", "issue-bodies")
SCOUT_LENSES = ("adrs", "docs", "in-flight", "all")
FIXED_TOKENS = FILE_TOKENS + ("gh-auth", "snapshot-empty", "snapshot-unavailable")

# Producer: scripts/in-flight-snapshot.py writes one line-initial key per failure to in-flight.err.
# tests/test_north_star_preflight.py pins this contract; keep the two in sync.
_ERR_KEY = re.compile(r"^(issues|prs|epics):")

FILES_REMEDY = ("North Star inputs are not installed in ~/.claude: run /setup-local "
                "(or ./install.sh) in claude-helpers. Retry will not help until fixed.")
GH_REMEDY = {
    "timeout": "gh auth status timed out; check network, retry may help.",
    "missing": "gh is not installed: install the GitHub CLI, then run `gh auth login`. "
               "Retry will not help until fixed.",
    "unauth": "run `gh auth login` for GitHub issue and PR context. Retry will not help until fixed.",
}
CONTEXT_REMEDY = ("North Star Nick needs GitHub issue and PR context: run `gh auth login` "
                  "(and check the repo's Issues/PRs are reachable), or pass --allow-degraded "
                  "to run blind on purpose. Retry will not help until fixed.")
EMPTY_REMEDY = ("in-flight.md was empty: the snapshot script produced no output; check in-flight.err "
                "in the run dir for the reason. Retry may help.")


def valid_token(token: str) -> bool:
    if token in FIXED_TOKENS:
        return True
    if token.startswith("snapshot-partial="):
        return token.split("=", 1)[1] in PARTIAL_SECTIONS
    if token.startswith("scout-failed="):
        return token.split("=", 1)[1] in SCOUT_LENSES
    if token.startswith("nick="):
        # Guards for free-text nick: blank, over-long, unprintable or grammar-breaking text is
        # invalid. blind-line and parse_nick_file map every failure to nick=unparseable.
        # The literal "unparseable" passes here as ordinary text (it is the reserved sentinel).
        text = token[len("nick="):]
        if not text.strip() or len(text) > NICK_MAX_CHARS or not text.isprintable():
            return False
        return "," not in text and "\n" not in text and "\r" not in text
    return False


def _usable_file(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        return resolved.is_file() and resolved.stat().st_size > 0
    except (OSError, RuntimeError):
        return False


def _gh_ok() -> str:
    """Status of `gh auth status`: "ok", "missing" (no gh binary), "timeout" or "unauth"."""
    try:
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return "missing"
    except subprocess.TimeoutExpired:
        return "timeout"
    except (OSError, subprocess.SubprocessError):
        return "unauth"
    return "ok" if result.returncode == 0 else "unauth"


def _remedy(tokens: Sequence[str], gh_status: str = "unauth") -> str:
    lines = []
    if any(t in FILE_TOKENS for t in tokens):
        lines.append(FILES_REMEDY)
    if "gh-auth" in tokens:
        lines.append(GH_REMEDY.get(gh_status, GH_REMEDY["unauth"]))
    # Only the GitHub-context tokens get the gh auth login remedy. FILE_TOKENS never do.
    if "snapshot-unavailable" in tokens or any(t.startswith("snapshot-partial=") for t in tokens):
        lines.append(CONTEXT_REMEDY)
    if "snapshot-empty" in tokens:
        lines.append(EMPTY_REMEDY)
    return "\n".join(lines)


def _emit(hard: List[str], notice: List[str], allow_degraded: bool, gh_status: str = "unauth") -> int:
    remedy = _remedy(list(hard) + list(notice), gh_status)
    if hard and not allow_degraded:
        print("FAIL " + " ".join(hard))
        sys.stderr.write("north-star-preflight: FAIL " + " ".join(hard) + "\n" + remedy + "\n")
        return 3
    tokens = hard + notice
    if tokens:
        print("BLIND " + " ".join(tokens))
        if hard:
            sys.stderr.write("north-star-preflight: continuing blind (--allow-degraded): "
                             + " ".join(hard) + "\n")
        if remedy:
            sys.stderr.write(remedy + "\n")
        return 0
    print("OK")
    return 0


def phase_files(claude_dir: Path, allow_degraded: bool) -> int:
    hard = [token for token, rel in REQUIRED_FILES if not _usable_file(claude_dir / rel)]
    gh_status = _gh_ok()
    notice = [] if gh_status == "ok" else ["gh-auth"]
    return _emit(hard, notice, allow_degraded, gh_status)


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
    """`nick=` tokens from Nick's output, read from the first few lines.

    The blind line is the first line within BLIND_SCAN_LINES that starts with BLIND_PREFIX, so a
    Pass 1 heading above it does not hide it. If any comma-separated piece is not a valid `nick=`
    token, the whole line collapses to a single `nick=unparseable` (a comma inside nick text splits
    it into a malformed piece, so that case also yields one sentinel).
    """
    line: Optional[str] = None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(BLIND_SCAN_LINES):
                raw = handle.readline()
                if not raw:
                    break
                if raw.startswith(BLIND_PREFIX):
                    line = raw.rstrip("\r\n")
                    break
    except OSError:
        return []
    if line is None:
        return []
    pieces = [piece.strip() for piece in line[len(BLIND_PREFIX):].split(",")]
    # Guard: any malformed piece (including an empty one) collapses the line to the sentinel.
    if not all(piece.startswith("nick=") and valid_token(piece) for piece in pieces):
        return [UNPARSEABLE_TOKEN]
    return pieces


def phase_blind_line(tokens_file: Path, extra: Sequence[str], nick_file: Optional[Path] = None) -> int:
    if nick_file is not None:
        extra = list(extra) + parse_nick_file(nick_file)
    try:
        file_tokens = tokens_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        file_tokens = []
    tokens: List[str] = []
    for raw in list(file_tokens) + list(extra):
        token = raw.strip()
        if not token:
            continue
        if not valid_token(token):
            # Guard: a bad token never drops the line. It is shown as the visible fallback instead.
            sys.stderr.write("north-star-preflight: invalid blind token {!r} replaced with {}\n".format(
                token, UNPARSEABLE_TOKEN))
            token = UNPARSEABLE_TOKEN
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
