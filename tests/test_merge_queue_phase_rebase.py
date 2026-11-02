"""
Phase 6 (_phase_rebase) skips `git rebase` when origin/<base> is already an ancestor of HEAD.

Real-git tests: ancestry is decided by git itself (no mocked is_ancestor). Runner.run_git is wrapped
with a recording spy that delegates to the real implementation, so argv is captured while real git runs.
"""

import contextlib
import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness
from _git_fixture import GitFixture
from workflow.git import is_ancestor
from workflow.merge_queue import MergeQueueConfig, Runner, _phase_rebase


h = Harness("MERGE QUEUE PHASE 6 REBASE SKIP TEST SUITE")
test_result = h.test_result


def _make_repo():
    """Hermetic fixture repo: no gpg signing, no hooks, initial commit on main. Returns (fixture, A_sha)."""
    fixture = GitFixture()
    fixture._run_git(["config", "commit.gpgsign", "false"], cwd=fixture.repo_root)
    fixture._run_git(["config", "core.hooksPath", "/dev/null"], cwd=fixture.repo_root)
    base_sha = fixture.create_initial_commit("A")
    return fixture, base_sha


def _commit_file(fixture, cwd, name, message):
    """Write and commit a new file at cwd. Returns the new HEAD sha."""
    (cwd / name).write_text(f"{message}\n")
    fixture._run_git(["add", name], cwd=cwd)
    fixture._run_git(["commit", "-m", message], cwd=cwd)
    return fixture.get_head_sha(cwd=cwd)


def _set_origin_main(fixture, sha):
    """Point refs/remotes/origin/main at sha, the ref Phase 6 compares against."""
    fixture._run_git(["update-ref", "refs/remotes/origin/main", sha], cwd=fixture.repo_root)


def _run_phase_rebase(fixture, wt_path, branch):
    """
    Call the real _phase_rebase with a recording spy on Runner.run_git (delegating to the real call).
    Returns (result, argv_list, stderr_text, queue_events, head_before, head_after).
    """
    orig_head = fixture.get_head_sha(cwd=wt_path)
    recorded = []
    real_run_git = Runner.run_git

    def spy(args, *rest, **kw):
        recorded.append(list(args))
        return real_run_git(args, *rest, **kw)

    config = MergeQueueConfig(base="main", steps=[])
    stderr = io.StringIO()
    with patch("workflow.merge_queue.Runner.run_git", new=spy), \
            patch("workflow.merge_queue.append_queue_log") as log_mock, \
            contextlib.redirect_stderr(stderr):
        result = _phase_rebase(
            pr=101,
            branch=branch,
            worktree=str(wt_path),
            orig_head=orig_head,
            config=config,
            cwd=wt_path,
        )
    head_after = fixture.get_head_sha(cwd=wt_path)
    events = [c.args[0] for c in log_mock.call_args_list if c.args]
    return result, recorded, stderr.getvalue(), events, orig_head, head_after


def _rebase_argvs(recorded):
    return [a for a in recorded if a and a[0] == "rebase"]


# ---------------------------------------------------------------------------
# Case 1: branch contains a merge of current origin/main -> skip
# ---------------------------------------------------------------------------
fixture, a_sha = _make_repo()
try:
    fixture.create_branch("feature/merged")
    wt = fixture.create_worktree("feature/merged")
    _commit_file(fixture, wt, "b.txt", "b")
    c_sha = _commit_file(fixture, fixture.repo_root, "c.txt", "C")  # main advances
    _set_origin_main(fixture, c_sha)
    fixture._run_git(["merge", "--no-edit", "main"], cwd=wt)  # branch now contains C via a merge
    _commit_file(fixture, wt, "d.txt", "d")

    test_result(
        "case1 setup: origin/main is an ancestor of branch HEAD (real git)",
        is_ancestor("origin/main", "HEAD", cwd=wt),
    )
    merge_line = fixture._run_git(["rev-list", "--parents", "-n1", "--merges", "HEAD"], cwd=wt).split()
    test_result(
        "case1 setup: branch contains a two-parent merge",
        len(merge_line) == 3,
        f"rev-list --parents --merges={merge_line}",
    )
    result, recorded, _err, _, head_before, head_after = _run_phase_rebase(fixture, wt, "feature/merged")

    test_result(
        "case1 (merge of base) _phase_rebase returns None",
        result is None,
        f"result={result!r}",
    )
    test_result(
        "case1 (merge of base) no git rebase argv issued",
        _rebase_argvs(recorded) == [],
        f"rebase argvs={_rebase_argvs(recorded)!r} all argvs={recorded!r}",
    )
    test_result(
        "case1 (merge of base) HEAD unchanged",
        head_after == head_before,
        f"before={head_before} after={head_after}",
    )
finally:
    fixture.cleanup()


# ---------------------------------------------------------------------------
# Case 2: branch is linear and already contains origin/main -> skip
# ---------------------------------------------------------------------------
fixture, a_sha = _make_repo()
try:
    c_sha = _commit_file(fixture, fixture.repo_root, "c.txt", "C")  # main at C before branching
    _set_origin_main(fixture, c_sha)
    fixture.create_branch("feature/linear")  # branch created at C
    wt = fixture.create_worktree("feature/linear")
    _commit_file(fixture, wt, "b.txt", "b")  # linear commit on top of origin/main

    result, recorded, err, _, head_before, head_after = _run_phase_rebase(fixture, wt, "feature/linear")

    test_result(
        "case2 (linear, contains origin/main) returns None",
        result is None,
        f"result={result!r}",
    )
    test_result(
        "case2 (linear, contains origin/main) no git rebase argv issued",
        _rebase_argvs(recorded) == [],
        f"rebase argvs={_rebase_argvs(recorded)!r}",
    )
    test_result(
        "case2 (linear, contains origin/main) HEAD unchanged",
        head_after == head_before,
        f"before={head_before} after={head_after}",
    )
    test_result(
        "case2 skip writes 'rebase skipped' to stderr",
        "rebase skipped" in err,
        f"stderr={err!r}",
    )
finally:
    fixture.cleanup()


# ---------------------------------------------------------------------------
# Case 3: HEAD == origin/main exactly -> skip (empty PR)
# ---------------------------------------------------------------------------
fixture, a_sha = _make_repo()
try:
    _set_origin_main(fixture, a_sha)
    fixture.create_branch("feature/empty")  # branch at A == origin/main
    wt = fixture.create_worktree("feature/empty")

    result, recorded, err, _, head_before, head_after = _run_phase_rebase(fixture, wt, "feature/empty")

    test_result(
        "case3 (HEAD == origin/main) returns None",
        result is None,
        f"result={result!r}",
    )
    test_result(
        "case3 (HEAD == origin/main) no git rebase argv issued",
        _rebase_argvs(recorded) == [],
        f"rebase argvs={_rebase_argvs(recorded)!r}",
    )
    test_result(
        "case3 (HEAD == origin/main) HEAD unchanged",
        head_after == head_before == a_sha,
        f"before={head_before} after={head_after} expected={a_sha}",
    )
finally:
    fixture.cleanup()


# ---------------------------------------------------------------------------
# Case 4: skip appends a durable rebase_skipped queue event for this PR
# ---------------------------------------------------------------------------
fixture, a_sha = _make_repo()
try:
    fixture.create_branch("feature/logged")
    wt = fixture.create_worktree("feature/logged")
    _commit_file(fixture, wt, "b.txt", "b")
    c_sha = _commit_file(fixture, fixture.repo_root, "c.txt", "C")
    _set_origin_main(fixture, c_sha)
    fixture._run_git(["merge", "--no-edit", "main"], cwd=wt)

    result, recorded, err, events, head_before, head_after = _run_phase_rebase(fixture, wt, "feature/logged")

    skip_events = [e for e in events if "rebase_skipped" in e.values()]
    test_result(
        "case4 skip appends one rebase_skipped queue event",
        len(skip_events) == 1,
        f"events={events!r}",
    )
    test_result(
        "case4 rebase_skipped event references PR 101",
        any(v == 101 for e in skip_events for v in e.values()),
        f"skip_events={skip_events!r}",
    )
finally:
    fixture.cleanup()


# ---------------------------------------------------------------------------
# Case 5 (negative pair): origin/main is ahead and not contained -> rebase still runs
# ---------------------------------------------------------------------------
fixture, a_sha = _make_repo()
try:
    fixture.create_branch("feature/behind")
    wt = fixture.create_worktree("feature/behind")
    _commit_file(fixture, wt, "b.txt", "b")
    c_sha = _commit_file(fixture, fixture.repo_root, "c.txt", "C")  # non-conflicting, branch lacks C
    _set_origin_main(fixture, c_sha)

    result, recorded, err, events, head_before, head_after = _run_phase_rebase(fixture, wt, "feature/behind")

    test_result(
        "case5 (not contained) returns None",
        result is None,
        f"result={result!r}",
    )
    test_result(
        "case5 (not contained) exactly one rebase origin/main argv issued",
        _rebase_argvs(recorded) == [["rebase", "origin/main"]],
        f"rebase argvs={_rebase_argvs(recorded)!r}",
    )
    test_result(
        "case5 (not contained) origin/main is an ancestor of new HEAD (real git)",
        is_ancestor("origin/main", "HEAD", cwd=wt) and head_after != head_before,
        f"before={head_before} after={head_after}",
    )
    test_result(
        "case5 (not contained) no 'rebase skipped' on stderr",
        "rebase skipped" not in err,
        f"stderr={err!r}",
    )
    test_result(
        "case5 (not contained) no rebase_skipped queue event",
        not any("rebase_skipped" in e.values() for e in events),
        f"events={events!r}",
    )
finally:
    fixture.cleanup()


# ---------------------------------------------------------------------------
# Case 6: same-file conflicting topology -> skip (the scenario #420 was filed for)
# Branch and base both edit shared.txt; the conflict was resolved in a merge of base into the
# branch. A replaying `git rebase origin/main` would linearize the branch and re-hit that conflict.
# ---------------------------------------------------------------------------
fixture, a_sha = _make_repo()
try:
    _commit_file(fixture, fixture.repo_root, "shared.txt", "base")
    fixture.create_branch("feature/conflicting")
    wt = fixture.create_worktree("feature/conflicting")
    _commit_file(fixture, wt, "shared.txt", "branch edit")
    c_sha = _commit_file(fixture, fixture.repo_root, "shared.txt", "main edit")  # same file, conflicting
    _set_origin_main(fixture, c_sha)
    fixture._run_git(["merge", "--no-edit", "-X", "ours", "main"], cwd=wt)  # conflict resolved in the merge

    test_result(
        "case6 setup: origin/main is an ancestor of branch HEAD (real git)",
        is_ancestor("origin/main", "HEAD", cwd=wt),
    )
    result, recorded, err, events, head_before, head_after = _run_phase_rebase(
        fixture, wt, "feature/conflicting"
    )

    test_result(
        "case6 (same-file conflict) _phase_rebase returns None, no conflict kickback",
        result is None,
        f"result={result!r}",
    )
    test_result(
        "case6 (same-file conflict) no git rebase argv issued",
        _rebase_argvs(recorded) == [],
        f"rebase argvs={_rebase_argvs(recorded)!r}",
    )
    test_result(
        "case6 (same-file conflict) HEAD unchanged",
        head_after == head_before,
        f"before={head_before} after={head_after}",
    )
    test_result(
        "case6 (same-file conflict) rebase_skipped queue event recorded",
        any(isinstance(e, dict) and e.get("event") == "rebase_skipped" for e in events),
        f"events={events!r}",
    )
finally:
    fixture.cleanup()


print()
h.summarize_and_exit()
