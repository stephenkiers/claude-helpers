#!/usr/bin/env python3
"""Unit tests for scripts/test-cleanup-git-guard.py decide() (Phase 2 git guard, #463).

Run with: python3 tests/test_test_cleanup_git_guard.py
"""

import importlib.util
from _test_harness import REPO_ROOT, Harness

spec = importlib.util.spec_from_file_location("guard", REPO_ROOT / "scripts" / "test-cleanup-git-guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

if __name__ == "__main__":
    h = Harness("TEST-CLEANUP GIT GUARD TEST SUITE")
    on = "test-cleanup/20261110T000000Z"
    cases = [
        ("git switch -c test-cleanup/x", "main", True),
        ("git switch -c other", "main", False),
        ("git rm -- a.py", "main", False),
        ("git rm -- a.py", on, True),
        ("git revert --no-edit abc", on, True),
        ("git reset --hard HEAD", "main", False),
        ("git clean -fd -- a", on, True),
        ("git push origin x", on, False),
        ("git stash", on, False),
        ("git -c core.hooksPath=/dev/null commit -m x", on, True),
        ("git status && git rm -- a", "main", False),
        ("git status --porcelain", "main", True),
    ]
    for cmd, branch, allowed in cases:
        h.test_result(f"{cmd!r} on {branch}: {'allow' if allowed else 'deny'}",
                      (guard.decide(cmd, branch) is None) == allowed, str(guard.decide(cmd, branch)))
    h.summarize_and_exit()
