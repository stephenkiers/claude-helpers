#!/usr/bin/env python3
"""Fail if any tracked file contains a forbidden term from a local, uncommitted list.

The list lives outside the repo (default ~/.claude/pseudonyms.txt, override with
CLAUDE_PSEUDONYMS_FILE), one case-insensitive term per line; blank lines and '#' comments are
ignored. A missing file prints a one-line skip and exits 0, so forks are unaffected. Matches are
reported as file:line only, never echoing the matched text.
"""
import os
import subprocess
import sys
from pathlib import Path
from typing import List


def load_terms(path: Path) -> List[str]:
    terms = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line.lower())
    return terms


def main() -> int:
    env = os.environ.get("CLAUDE_PSEUDONYMS_FILE")
    path = Path(env) if env else Path.home() / ".claude" / "pseudonyms.txt"
    if not path.is_file():
        print("check-pseudonyms: skipped (no pseudonyms file)")
        return 0
    terms = load_terms(path)
    if not terms:
        print("check-pseudonyms: skipped (no terms)")
        return 0
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
    names = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, check=True).stdout.decode().split("\0")
    hits = []
    for name in filter(None, names):
        p = Path(root) / name
        if not p.is_file() or p.is_symlink():
            continue
        try:
            text = p.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            if any(t in low for t in terms):
                hits.append(f"{name}:{i}")
    if hits:
        print("check-pseudonyms: forbidden term found at:")
        for h in hits:
            print(f"  {h}")
        return 1
    print("check-pseudonyms: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
