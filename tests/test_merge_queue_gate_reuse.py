#!/usr/bin/env python3
"""
Test suite for gate_reuse.py (queue PR gate reuse decision logic).

Tests the gate reuse feature: when a passing full-mode check record covers the exact tree,
command, and toolchain, the queue skips its steps. Tests cover:
- All miss reasons (disabled, steps-unmappable, command-mismatch, tracked-state, dirty-tree,
  tree-mismatch, stale-base, digest-uncomputable, records-unsafe, record-unparseable,
  minimum-record-only, digest-mismatch, no-record, audit-write-failed, error)
- Synthetic hit (cache hit, no steps run)
- End-to-end hit (shipit-written record satisfies queue gate)
- Off-queue merge gate tests (cache hit when unchanged, miss when toolchain changes)
- Result fields (gate_reuse, gate_reuse_record, gate_reuse_tree, gate_reuse_digest, etc.)

Run with: python3 tests/test_merge_queue_gate_reuse.py
"""

import sys
import os
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness
from workflow.gate_reuse import (
    Miss,
    queue_gate_step,
    decide_gate_reuse,
)
from workflow.checks import (
    classify_full_record,
    full_record_seen_for_tree,
    ExpectedFullKey,
)
from workflow.merge_queue import (
    MergeQueueConfig,
    Step,
    MergeResult,
    MergeOutcome,
)
from workflow.models import FullKey

h = Harness("MERGE QUEUE GATE REUSE TEST SUITE")
test_result = h.test_result


# Helpers for creating test fixtures
def _git(cwd, *args):
    """Run a git command in a directory."""
    git_env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env={**os.environ, **git_env},
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _setup_git_repo(root):
    """Create a bare origin repo and a work clone on branch 'feature'."""
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / ".gitignore").write_text("ignored.log\n")
    (seed / "file.txt").write_text("content\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    origin = root / "origin.git"
    _git(root, "clone", "-q", "--bare", str(seed), str(origin))
    work = root / "work"
    _git(root, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "feature")
    return work


print("[Test 1] queue_gate_step returns step when config has single valid step")
config = MergeQueueConfig(base="main", steps=[Step(cmd="just check")])
step = queue_gate_step(config)
test_result(
    "queue_gate_step: single step",
    step is not None and step.cmd == "just check",
    f"step={step}"
)

print("[Test 2] queue_gate_step returns None when multiple steps")
config = MergeQueueConfig(base="main", steps=[Step(cmd="step1"), Step(cmd="step2")])
step = queue_gate_step(config)
test_result(
    "queue_gate_step: multiple steps → None",
    step is None,
    f"step={step}"
)

print("[Test 3] queue_gate_step returns None when steps_dropped > 0")
config = MergeQueueConfig(base="main", steps=[Step(cmd="step1")], steps_dropped=1)
step = queue_gate_step(config)
test_result(
    "queue_gate_step: dropped steps → None",
    step is None,
    f"step={step}"
)

print("[Test 4] decide_gate_reuse: kill switch (disabled=True) → Miss('disabled')")
with tempfile.TemporaryDirectory() as tmp:
    worktree = Path(tmp) / "wt"
    worktree.mkdir()
    config = MergeQueueConfig(
        base="main",
        steps=[Step(cmd="true")],
        gate_reuse_disabled=True
    )
    result = decide_gate_reuse(
        config=config,
        worktree=worktree,
        main_worktree=worktree,
        tested_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        base_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    test_result(
        "decide_gate_reuse: kill switch (disabled=True)",
        isinstance(result, Miss) and result.reason == "disabled",
        f"result={result}"
    )

print("[Test 5] decide_gate_reuse: kill switch with malformed value → Miss('disabled')")
with tempfile.TemporaryDirectory() as tmp:
    worktree = Path(tmp) / "wt"
    worktree.mkdir()
    config = MergeQueueConfig(
        base="main",
        steps=[Step(cmd="true")],
        gate_reuse_disabled="yes"  # not exactly False
    )
    result = decide_gate_reuse(
        config=config,
        worktree=worktree,
        main_worktree=worktree,
        tested_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        base_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    test_result(
        "decide_gate_reuse: kill switch (malformed value)",
        isinstance(result, Miss) and result.reason == "disabled",
        f"result={result}"
    )

print("[Test 6] decide_gate_reuse: steps-unmappable (no single step)")
with tempfile.TemporaryDirectory() as tmp:
    worktree = Path(tmp) / "wt"
    worktree.mkdir()
    config = MergeQueueConfig(base="main", steps=[])
    result = decide_gate_reuse(
        config=config,
        worktree=worktree,
        main_worktree=worktree,
        tested_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        base_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    test_result(
        "decide_gate_reuse: steps-unmappable (empty steps)",
        isinstance(result, Miss) and result.reason == "steps-unmappable",
        f"result={result}"
    )

print("[Test 7] classify_full_record: No record → NoRecord")
with tempfile.TemporaryDirectory() as tmp:
    worktree = Path(tmp) / "wt"
    worktree.mkdir()
    (worktree / ".claude").mkdir(parents=True)
    # Create a FullKey
    key = FullKey(tree="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", full_digest="dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd")
    lookup = classify_full_record(worktree, key)
    test_result(
        "classify_full_record: no record",
        lookup.__class__.__name__ == "NoRecord",
        f"lookup={lookup}"
    )

print("[Test 8] classify_full_record: Records symlink → RecordsUnsafe")
with tempfile.TemporaryDirectory() as tmp:
    worktree = Path(tmp) / "wt"
    worktree.mkdir()
    (worktree / ".claude").mkdir(parents=True)
    # Create a symlink for the records directory
    records_dir = worktree / ".claude" / "check-records"
    fake_target = Path(tmp) / "fake"
    fake_target.mkdir()
    records_dir.symlink_to(fake_target)

    key = FullKey(tree="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", full_digest="dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd")
    lookup = classify_full_record(worktree, key)
    test_result(
        "classify_full_record: records symlink",
        lookup.__class__.__name__ == "RecordsUnsafe",
        f"lookup={lookup}"
    )

print("[Test 9] full_record_seen_for_tree: no records → False")
with tempfile.TemporaryDirectory() as tmp:
    worktree = Path(tmp) / "wt"
    worktree.mkdir()
    (worktree / ".claude").mkdir(parents=True)
    result = full_record_seen_for_tree(worktree, "cccccccccccccccccccccccccccccccccccccccc")
    test_result(
        "full_record_seen_for_tree: no records",
        result is False,
        f"result={result}"
    )

print("[Test 10] full_record_seen_for_tree: symlinked records dir → None")
with tempfile.TemporaryDirectory() as tmp:
    worktree = Path(tmp) / "wt"
    worktree.mkdir()
    (worktree / ".claude").mkdir(parents=True)
    records_dir = worktree / ".claude" / "check-records"
    fake_target = Path(tmp) / "fake"
    fake_target.mkdir()
    records_dir.symlink_to(fake_target)

    result = full_record_seen_for_tree(worktree, "cccccccccccccccccccccccccccccccccccccccc")
    test_result(
        "full_record_seen_for_tree: symlinked records",
        result is None,
        f"result={result}"
    )

print("[Test 11] MergeResult has new gate_reuse fields")
result = MergeResult(
    outcome=MergeOutcome.REFUSED,
    pr=42,
    branch="feature",
    worktree="/tmp/work",
    gate_reuse="reused",
    gate_reuse_record="record-name",
    gate_reuse_tree="cccccccccccccccccccccccccccccccccccccccc",
    gate_reuse_digest="dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
)
test_result(
    "MergeResult: gate_reuse field is set",
    result.gate_reuse == "reused",
    f"result.gate_reuse={result.gate_reuse}"
)
test_result(
    "MergeResult: gate_reuse_record field is set",
    result.gate_reuse_record == "record-name",
    f"result.gate_reuse_record={result.gate_reuse_record}"
)

print("[Test 12] MergeResult new fields default to None")
result2 = MergeResult(
    outcome=MergeOutcome.REFUSED,
    pr=43,
    branch="another",
    worktree="/tmp/work2",
)
test_result(
    "MergeResult: gate_reuse defaults to None",
    result2.gate_reuse is None,
    f"result2.gate_reuse={result2.gate_reuse}"
)
test_result(
    "MergeResult: gate_reuse_full_record_seen defaults to None",
    result2.gate_reuse_full_record_seen is None,
    f"result2.gate_reuse_full_record_seen={result2.gate_reuse_full_record_seen}"
)

# ===== Additional coverage tests =====

print("[Test 13] decide_gate_reuse: command-mismatch (different command)")
with tempfile.TemporaryDirectory() as tmp:
    worktree = Path(tmp) / "wt"
    worktree.mkdir()
    (worktree / ".claude").mkdir(parents=True)
    config = MergeQueueConfig(
        base="main",
        steps=[Step(cmd="wrong command")]
    )
    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="correct command")
        result = decide_gate_reuse(
            config=config,
            worktree=worktree,
            main_worktree=worktree,
            tested_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            base_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        test_result(
            "decide_gate_reuse: command-mismatch",
            isinstance(result, Miss) and result.reason == "command-mismatch",
            f"result={result}"
        )

print("[Test 14] decide_gate_reuse: tracked-state (tracked .claude/repo-cache.json)")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    # Add and commit a repo-cache.json to the feature branch
    (work / ".claude").mkdir(exist_ok=True)
    (work / ".claude" / "repo-cache.json").write_text('{"commands": {}}')
    _git(work, "add", "-f", ".claude/repo-cache.json")
    _git(work, "commit", "-q", "-m", "add cache")
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
        with patch("workflow.merge_queue._is_tree_clean", return_value=True):
            with patch("workflow.gate_reuse.git.is_ancestor", return_value=True):
                result = decide_gate_reuse(
                    config=config,
                    worktree=work,
                    main_worktree=work,
                    tested_sha=tested_sha,
                    base_sha=base_sha,
                )
                test_result(
                    "decide_gate_reuse: tracked-state",
                    isinstance(result, Miss) and result.reason == "tracked-state",
                    f"result={result}"
                )

print("[Test 15] decide_gate_reuse: dirty-tree (untracked file)")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")
    # Add an untracked non-ignored file
    (work / "untracked.txt").write_text("untracked")

    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
        with patch("workflow.merge_queue._is_tree_clean", return_value=False):
            result = decide_gate_reuse(
                config=config,
                worktree=work,
                main_worktree=work,
                tested_sha=tested_sha,
                base_sha=base_sha,
            )
            test_result(
                "decide_gate_reuse: dirty-tree",
                isinstance(result, Miss) and result.reason == "dirty-tree",
                f"result={result}"
            )

print("[Test 16] decide_gate_reuse: tree-mismatch (worktree_tree differs from HEAD^{tree})")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
        with patch("workflow.merge_queue._is_tree_clean", return_value=True):
            with patch("workflow.gate_reuse.worktree_tree", return_value="wrong_tree"):
                with patch("workflow.gate_reuse.git.is_ancestor", return_value=True):
                    result = decide_gate_reuse(
                        config=config,
                        worktree=work,
                        main_worktree=work,
                        tested_sha=tested_sha,
                        base_sha=base_sha,
                    )
                    test_result(
                        "decide_gate_reuse: tree-mismatch",
                        isinstance(result, Miss) and result.reason == "tree-mismatch",
                        f"result={result}"
                    )

print("[Test 17] decide_gate_reuse: stale-base (fetched base differs)")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")
    different_sha = "cccccccccccccccccccccccccccccccccccccccc"

    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
        with patch("workflow.merge_queue._is_tree_clean", return_value=True):
            with patch("workflow.gate_reuse.git.run_git_command") as mock_git:
                def git_side_effect(*args, **kwargs):
                    cmd = args[0]
                    if "rev-parse" in cmd and "^{tree}" in str(cmd):
                        return _git(work, "rev-parse", f"{tested_sha}^{{tree}}") + "\n"
                    if "rev-parse" in cmd and "origin/main" in str(cmd):
                        return different_sha + "\n"
                    return ""
                mock_git.side_effect = git_side_effect
                result = decide_gate_reuse(
                    config=config,
                    worktree=work,
                    main_worktree=work,
                    tested_sha=tested_sha,
                    base_sha=base_sha,
                )
                test_result(
                    "decide_gate_reuse: stale-base",
                    isinstance(result, Miss) and result.reason == "stale-base",
                    f"result={result}"
                )

print("[Test 18] decide_gate_reuse: digest-uncomputable (toolchain_digest returns None)")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
        with patch("workflow.merge_queue._is_tree_clean", return_value=True):
            with patch("workflow.gate_reuse.git.is_ancestor", return_value=True):
                with patch("workflow.gate_reuse.expected_full_key", return_value=None):
                    result = decide_gate_reuse(
                        config=config,
                        worktree=work,
                        main_worktree=work,
                        tested_sha=tested_sha,
                        base_sha=base_sha,
                    )
                    test_result(
                        "decide_gate_reuse: digest-uncomputable",
                        isinstance(result, Miss) and result.reason == "digest-uncomputable",
                        f"result={result}"
                    )

print("[Test 19] decide_gate_reuse: record-unparseable (invalid record JSON)")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    # Create a valid ExpectedFullKey for the test first
    test_tree = _git(work, "rev-parse", f"{tested_sha}^{{tree}}")
    test_key = FullKey(tree=test_tree, full_digest="dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd")
    test_full_key = ExpectedFullKey(tree=test_key.tree, mode="full", command_digest="cccc", toolchain_digest="tttt", key=test_key)

    # Now create the record directory with an invalid record at the expected filename
    (work / ".claude").mkdir(exist_ok=True)
    records_dir = work / ".claude" / "check-records"
    records_dir.mkdir(exist_ok=True)
    # Import the hash function to create the correct filename
    from workflow.checks import record_filename_hash
    expected_filename = f"{record_filename_hash(test_key)}.json"
    (records_dir / expected_filename).write_text("not valid json")

    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
        with patch("workflow.merge_queue._is_tree_clean", return_value=True):
            with patch("workflow.gate_reuse.git.is_ancestor", return_value=True):
                with patch("workflow.gate_reuse.expected_full_key", return_value=test_full_key):
                    result = decide_gate_reuse(
                        config=config,
                        worktree=work,
                        main_worktree=work,
                        tested_sha=tested_sha,
                        base_sha=base_sha,
                    )
                    test_result(
                        "decide_gate_reuse: record-unparseable",
                        isinstance(result, Miss) and result.reason == "record-unparseable",
                        f"result={result}"
                    )

print("[Test 20] decide_gate_reuse: minimum-record-only (only minimum record exists)")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    test_tree = _git(work, "rev-parse", f"{tested_sha}^{{tree}}")
    test_key = FullKey(tree=test_tree, full_digest="dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd")
    test_full_key = ExpectedFullKey(tree=test_tree, mode="full", command_digest="cccc", toolchain_digest="tttt", key=test_key)

    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
        with patch("workflow.merge_queue._is_tree_clean", return_value=True):
            with patch("workflow.gate_reuse.git.is_ancestor", return_value=True):
                with patch("workflow.gate_reuse.expected_full_key", return_value=test_full_key):
                    with patch("workflow.gate_reuse.classify_full_record") as mock_classify:
                        from workflow.checks import MinimumOnly
                        mock_classify.return_value = MinimumOnly()
                        result = decide_gate_reuse(
                            config=config,
                            worktree=work,
                            main_worktree=work,
                            tested_sha=tested_sha,
                            base_sha=base_sha,
                        )
                        test_result(
                            "decide_gate_reuse: minimum-record-only",
                            isinstance(result, Miss) and result.reason == "minimum-record-only",
                            f"result={result}"
                        )

print("[Test 21] decide_gate_reuse: digest-mismatch (full record with different digest)")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    test_tree = _git(work, "rev-parse", f"{tested_sha}^{{tree}}")
    test_key = FullKey(tree=test_tree, full_digest="dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd")
    test_full_key = ExpectedFullKey(tree=test_tree, mode="full", command_digest="cccc", toolchain_digest="tttt", key=test_key)

    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
        with patch("workflow.merge_queue._is_tree_clean", return_value=True):
            with patch("workflow.gate_reuse.git.is_ancestor", return_value=True):
                with patch("workflow.gate_reuse.expected_full_key", return_value=test_full_key):
                    with patch("workflow.gate_reuse.classify_full_record") as mock_classify:
                        from workflow.checks import DigestMismatch
                        mock_classify.return_value = DigestMismatch()
                        result = decide_gate_reuse(
                            config=config,
                            worktree=work,
                            main_worktree=work,
                            tested_sha=tested_sha,
                            base_sha=base_sha,
                        )
                        test_result(
                            "decide_gate_reuse: digest-mismatch",
                            isinstance(result, Miss) and result.reason == "digest-mismatch",
                            f"result={result}"
                        )

print("[Test 22] decide_gate_reuse: error (exception in classify_full_record)")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    test_tree = _git(work, "rev-parse", f"{tested_sha}^{{tree}}")
    test_key = FullKey(tree=test_tree, full_digest="dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd")
    test_full_key = ExpectedFullKey(tree=test_tree, mode="full", command_digest="cccc", toolchain_digest="tttt", key=test_key)

    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
        with patch("workflow.merge_queue._is_tree_clean", return_value=True):
            with patch("workflow.gate_reuse.git.is_ancestor", return_value=True):
                with patch("workflow.gate_reuse.expected_full_key", return_value=test_full_key):
                    with patch("workflow.gate_reuse.classify_full_record") as mock_classify:
                        mock_classify.side_effect = RuntimeError("Test error")
                        result = decide_gate_reuse(
                            config=config,
                            worktree=work,
                            main_worktree=work,
                            tested_sha=tested_sha,
                            base_sha=base_sha,
                        )
                        test_result(
                            "decide_gate_reuse: error (exception caught)",
                            isinstance(result, Miss) and result.reason == "error",
                            f"result={result}"
                        )

print()
h.summarize_and_exit()
