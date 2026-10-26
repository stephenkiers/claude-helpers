"""
Shared fixture for tests that drive an OK implement/fix receipt through flow-verify-receipt.py
against this repo's own HEAD (#338): the verifier now requires CHECK lines on every OK receipt.

`with_check_lines` resolves the check against a throwaway worktree (via --check-worktree) that holds
a repo-cache with a known command, so the result does not depend on this machine's .claude state.
"""

import json
import subprocess
from pathlib import Path
from typing import List, Tuple

CHECK_COMMAND = "just check"


def with_check_lines(receipt_text: str, repo_root: Path, tmp: Path) -> Tuple[str, List[str]]:
    """Return (receipt text with passing CHECK lines prepended when it is an OK receipt, extra argv)."""
    cache = tmp / "check-worktree" / ".claude" / "repo-cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"commands": {"check": CHECK_COMMAND}}))
    extra = ["--check-worktree", str(tmp / "check-worktree")]
    if "Decision: OK" not in receipt_text:
        return receipt_text, extra
    head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    lines = (
        f"CHECK: {CHECK_COMMAND}\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\nCHECK_SHA: {head}\n"
    )
    return lines + receipt_text, extra
