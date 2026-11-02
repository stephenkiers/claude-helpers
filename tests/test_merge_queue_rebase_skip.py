"""
Phase 6 rebase skip (#420): real-git pair.

Case 1: the branch already merged origin/main, so _phase_rebase skips the rebase.
Case 2: origin/main has a commit the branch lacks, so _phase_rebase still rebases.

Uses the real git.is_ancestor (no mock). Runner.run_git is wrapped with a recording
spy that delegates to the real implementation.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness
from _git_fixture import GitFixture

from workflow import merge_queue
from workflow.merge_queue import MergeQueueConfig, Runner, _phase_rebase

h = Harness("MERGE QUEUE REBASE SKIP TEST SUITE")
test_result = h.test_result


def _write_and_commit(fixture, cwd, filename, message):
    (cwd / filename).write_text(f"{message}\n")
    fixture._run_git(["add", filename], cwd=cwd)
    fixture._run_git(["commit", "-m", message], cwd=cwd)
    return fixture._run_git(["rev-parse", "HEAD"], cwd=cwd)


def _setup_repo():
    """Return (fixture, branch_worktree, base_sha_A) with hermetic repo config."""
    fixture = GitFixture()
    fixture._run_git(["config", "commit.gpgsign", "false"], cwd=fixture.repo_root)
    fixture._run_git(["config", "core.hooksPath", "/dev/null"], cwd=fixture.repo_root)
    sha_a = fixture.create_initial_commit("A")
    fixture.create_branch("feature")
    wt = fixture.create_worktree("feature")
    return fixture, wt, sha_a


def _run_phase_rebase(wt, config):
    """Run _phase_rebase with a recording spy on Runner.run_git. Returns (result, argvs, events)."""
    argvs = []
    events = []
    original = Runner.run_git

    def spy(args, cwd=None, check=True, timeout=None):
        argvs.append(list(args))
        return original(args, cwd=cwd, check=check, timeout=timeout)

    with patch.object(Runner, "run_git", staticmethod(spy)), \
            patch.object(merge_queue, "append_queue_log", side_effect=events.append):
        result = _phase_rebase(1, "feature", str(wt), "unused", config, wt)
    return result, argvs, events


config = MergeQueueConfig(base="main", steps=[], mutation_timeout_secs=60)

# Case 1: branch contains a merge of origin/main -> skip, no rebase
fixture, wt, sha_a = _setup_repo()
try:
    _write_and_commit(fixture, wt, "b.txt", "b")
    sha_c = _write_and_commit(fixture, fixture.repo_root, "c.txt", "C")
    fixture._run_git(["update-ref", "refs/remotes/origin/main", sha_c], cwd=fixture.repo_root)
    fixture._run_git(["merge", "--no-edit", "main"], cwd=wt)
    _write_and_commit(fixture, wt, "d.txt", "d")
    head_before = fixture.get_head_sha(cwd=wt)

    result, argvs, events = _run_phase_rebase(wt, config)
    head_after = fixture.get_head_sha(cwd=wt)

    test_result("(1) Case 1 returns None", result is None, f"result={result}")
    test_result(
        "(1) Case 1 issues no rebase argv",
        not any(a and a[0] == "rebase" for a in argvs),
        f"argvs={argvs}",
    )
    test_result("(1) Case 1 HEAD unchanged", head_after == head_before, f"before={head_before} after={head_after}")
    test_result(
        "(1) Case 1 records rebase_skipped event",
        any(e.get("event") == "rebase_skipped" for e in events),
        f"events={events}",
    )
finally:
    fixture.cleanup()

# Case 2: origin/main has a commit the branch lacks -> rebase still runs
fixture, wt, sha_a = _setup_repo()
try:
    _write_and_commit(fixture, wt, "b.txt", "b")
    sha_c = _write_and_commit(fixture, fixture.repo_root, "c.txt", "C")
    fixture._run_git(["update-ref", "refs/remotes/origin/main", sha_c], cwd=fixture.repo_root)

    result, argvs, events = _run_phase_rebase(wt, config)
    head_after = fixture.get_head_sha(cwd=wt)
    rebase_argvs = [a for a in argvs if a and a[0] == "rebase"]
    origin_is_ancestor = merge_queue.git.is_ancestor("origin/main", "HEAD", cwd=wt)

    test_result("(2) Case 2 returns None", result is None, f"result={result}")
    test_result(
        "(2) Case 2 issues exactly one rebase origin/main",
        rebase_argvs == [["rebase", "origin/main"]],
        f"rebase_argvs={rebase_argvs}",
    )
    test_result(
        "(2) Case 2 origin/main is ancestor of new HEAD",
        origin_is_ancestor and head_after != sha_c,
        f"head_after={head_after}",
    )
    test_result(
        "(2) Case 2 records no rebase_skipped event",
        not any(e.get("event") == "rebase_skipped" for e in events),
        f"events={events}",
    )
finally:
    fixture.cleanup()

print()
h.summarize_and_exit()
