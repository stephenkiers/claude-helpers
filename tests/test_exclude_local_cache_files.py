#!/usr/bin/env python3
"""
Test that track-and-start's local .claude files are git-excluded so the
implement Final check's clean-tree rule holds.

Run with: python3 tests/test_exclude_local_cache_files.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.cache import exclude_local_cache_files, LOCAL_EXCLUDE_PATTERNS
from _test_harness import Harness
from _git_fixture import GitFixture


if __name__ == "__main__":
    h = Harness("EXCLUDE LOCAL CACHE FILES TEST SUITE")
    fixture = GitFixture()
    try:
        fixture.create_initial_commit()
        fixture.create_branch("feature/x")
        wt = fixture.create_worktree("feature/x")
        (wt / ".claude").mkdir(exist_ok=True)
        for name in ("github-cache.json", "repo-cache.json", "project.yaml"):
            (wt / ".claude" / name).write_text("x")

        h.test_result(
            "files untracked before exclude",
            ".claude/" in fixture._run_git(["status", "--porcelain"], cwd=wt),
        )
        h.test_result("exclude returns no error", exclude_local_cache_files(wt) is None)
        h.test_result(
            "worktree clean after exclude",
            fixture._run_git(["status", "--porcelain", "-uall"], cwd=wt) == "",
        )

        exclude_local_cache_files(wt)
        text = (fixture.repo_root / ".git" / "info" / "exclude").read_text()
        h.test_result(
            "idempotent: each pattern once",
            all(text.splitlines().count(p) == 1 for p in LOCAL_EXCLUDE_PATTERNS),
        )
    finally:
        fixture.cleanup()
    print()
    h.summarize_and_exit()
