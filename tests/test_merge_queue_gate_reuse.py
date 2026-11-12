#!/usr/bin/env python3
"""
Test suite for gate_reuse.py and the queue's gate reuse wiring (ADR-0021 amendment, #491).

When a passing full-mode check record covers the exact tree, command, and toolchain, the queue skips its
gate steps. Tests cover:
- decide_gate_reuse guards, one miss reason per test: disabled (kill switch, malformed value), steps-unmappable,
  command-mismatch, tracked-state (committed config and committed records), dirty-tree, tree-mismatch,
  stale-base, digest-uncomputable (patched), record-unparseable, minimum-record-only, digest-mismatch,
  no-record, and error (exception inside classification). Record-state tests write real record files.
- The real round trip: `checks validate` writes a record, and decide_gate_reuse then returns Reuse for it.
- classify_full_record and full_record_seen_for_tree on symlinked and empty records directories.
- The toolchain key: a lockfile change and a run-timeout change produce a different full digest.
- Config validation of the gate_reuse_disabled kill switch (non-bool values are rejected).
- Phase 7 (_phase_run_gate_and_assertions): a Reuse writes a gate_reused row and skips run_step; a failed audit
  write runs the gate; the gate steps receive no scratch environment.
- MergeResult provenance fields (gate_reuse grammar and the _gate_reuse_fields/_with_gate_fields result).
- The Merge-Gate trailer gate=reused marker (build, parse, and trailer_gate_reused).
- The status counters and the reason buckets (every MissReason is bucketed exactly once).

Run with: python3 tests/test_merge_queue_gate_reuse.py
"""

import sys
import os
import json
import tempfile
import subprocess
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness
from workflow.gate_reuse import (
    Miss,
    MissReason,
    Reuse,
    queue_gate_step,
    decide_gate_reuse,
)
from workflow.checks import (
    classify_full_record,
    full_record_seen_for_tree,
    ExpectedFullKey,
    expected_full_key,
    record_filename_hash,
    validate,
)
from workflow.merge_queue import (
    MergeQueueConfig,
    Step,
    MergeResult,
    MergeOutcome,
    build_trailer,
    parse_trailer,
    trailer_gate_reused,
    validate_config_data,
    _gate_reuse_fields,
    _gate_reuse_status_lines,
    _GUARD_REFUSALS,
    _UNEXPECTED_MISSES,
    _phase_run_gate_and_assertions,
    _with_gate_fields,
)
from workflow.models import CHECK_RECORD_SCHEMA_VERSION, CheckRecord, FullKey, MinimumKey

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


def _fixture_with_check(root, check_cmd):
    """
    A bare origin on main, cloned as `work`, one commit ahead on `feature`, with an untracked repo-cache.json
    whose check is `check_cmd`. Mirrors test_workflow_checks_execute._fixture_clone: .claude/.gitignore ignores
    check-records/ and repo-cache.json, so a record or cache written by validate leaves the tree clean and
    unchanged.
    """
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / ".claude").mkdir()
    (seed / ".claude" / ".gitignore").write_text("check-records/\nrepo-cache.json\n")
    (seed / "README.md").write_text("base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    origin = root / "origin.git"
    _git(root, "clone", "-q", "--bare", str(seed), str(origin))
    work = root / "work"
    _git(root, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "feature.txt").write_text("one\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "feature")
    (work / ".claude" / "repo-cache.json").write_text(
        json.dumps({"version": 1, "commands": {"check": check_cmd}}) + "\n"
    )
    return work


def _write_record_file(records_dir, key):
    """Write a real passing record for `key` at its canonical filename, as the cache would."""
    records_dir.mkdir(parents=True, exist_ok=True)
    record = CheckRecord(
        schema_version=CHECK_RECORD_SCHEMA_VERSION, key=key, passed=True, written_at=time.time(),
    )
    path = records_dir / f"{record_filename_hash(key)}.json"
    path.write_text(json.dumps(record.to_dict()) + "\n")
    return path


def _decide(config, work, tested_sha, base_sha):
    """decide_gate_reuse with the worktree as its own main worktree."""
    return decide_gate_reuse(
        config=config,
        worktree=work,
        main_worktree=work,
        tested_sha=tested_sha,
        base_sha=base_sha,
    )


def _run_phase7(cwd, state_dir, outcome, run_step_mock, append_mock, config):
    """
    Run Phase 7 with the gate decision and the side effects replaced. decide_gate_reuse is patched to
    return `outcome`; run_step, append_queue_log and ensure_state_dir are patched so nothing reaches the
    real queue state. Git and the tree checks run for real in `cwd`.
    """
    head = _git(cwd, "rev-parse", "HEAD")
    with ExitStack() as stack:
        stack.enter_context(patch("workflow.merge_queue.decide_gate_reuse", return_value=outcome))
        stack.enter_context(patch("workflow.merge_queue.run_step", run_step_mock))
        stack.enter_context(patch("workflow.merge_queue.append_queue_log", append_mock))
        stack.enter_context(patch("workflow.merge_queue.ensure_state_dir", return_value=state_dir))
        return _phase_run_gate_and_assertions(
            pr=1,
            branch="feature",
            worktree=str(cwd),
            orig_head=head,
            config=config,
            lock_fd_to_inherit=None,
            cwd=cwd,
            base_sha=_git(cwd, "rev-parse", "main"),
            main_worktree=cwd,
        )


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

print("[Test 11] MergeResult carries gate_reuse provenance from Phase 7 (reused and miss)")
ts = "2026-11-12T00:00:00+00:00"
reuse_outcome = Reuse(
    record_name="rec-name",
    written_at=1.0,
    expected=ExpectedFullKey(
        command_digest="c" * 64,  # placeholder digests: this test never recomputes them
        toolchain_digest="t" * 64,
        key=FullKey(tree="c" * 40, full_digest="d" * 64),
    ),
)
base_result = MergeResult(outcome=MergeOutcome.REFUSED, pr=42, branch="feature", worktree="wt")
reused_result = _with_gate_fields(base_result, _gate_reuse_fields(reuse_outcome, True, ts))
test_result(
    "MergeResult: reused gate sets gate_reuse, record, tree, digest and seen flag",
    reused_result.gate_reuse == "reused"
    and reused_result.gate_reuse_record == "rec-name"
    and reused_result.gate_reuse_tree == "c" * 40
    and reused_result.gate_reuse_digest == "d" * 64
    and reused_result.gate_reuse_full_record_seen is True
    and reused_result.gate_reuse_decided_at == ts,
    f"reused_result={reused_result}"
)
miss_result = _with_gate_fields(base_result, _gate_reuse_fields(Miss("no-record", "none"), False, ts))
test_result(
    "MergeResult: a miss sets miss:<reason> and clears the record fields",
    miss_result.gate_reuse == "miss:no-record"
    and miss_result.gate_reuse_record is None
    and miss_result.gate_reuse_tree is None
    and miss_result.gate_reuse_digest is None
    and miss_result.gate_reuse_full_record_seen is False,
    f"miss_result={miss_result}"
)

print("[Test 12] MergeResult gate_reuse fields default to None; grammar is enforced")
test_result(
    "MergeResult: gate_reuse defaults to None",
    base_result.gate_reuse is None,
    f"base_result.gate_reuse={base_result.gate_reuse}"
)
test_result(
    "MergeResult: gate_reuse_full_record_seen defaults to None",
    base_result.gate_reuse_full_record_seen is None,
    f"base_result.gate_reuse_full_record_seen={base_result.gate_reuse_full_record_seen}"
)
test_result(
    "MergeResult: no gate fields leaves the result unchanged",
    _with_gate_fields(base_result, {}) is base_result,
)
rejected = None
try:
    MergeResult(outcome=MergeOutcome.REFUSED, pr=44, branch="x", worktree="wt", gate_reuse="miss:made-up")
except ValueError as e:
    rejected = e
test_result(
    "MergeResult: a gate_reuse reason outside MissReason is rejected",
    rejected is not None,
    f"rejected={rejected}",
)

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

print("[Test 15] decide_gate_reuse: tracked-state (committed .claude/check-records/x.json) and dirty-tree (real)")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    (work / ".claude" / "check-records").mkdir(parents=True)
    (work / ".claude" / "check-records" / "x.json").write_text("{}\n")
    _git(work, "add", "-f", ".claude/check-records/x.json")
    _git(work, "commit", "-q", "-m", "committed record")
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")
    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
        result = decide_gate_reuse(
            config=config,
            worktree=work,
            main_worktree=work,
            tested_sha=tested_sha,
            base_sha=base_sha,
        )
        test_result(
            "decide_gate_reuse: committed check record is tracked-state",
            isinstance(result, Miss) and result.reason == "tracked-state",
            f"result={result}"
        )

with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")
    # Add an untracked non-ignored file; the real is_tree_clean sees it
    (work / "untracked.txt").write_text("untracked")

    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
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

print("[Test 17] decide_gate_reuse: stale-base (pinned base is not the fetched origin/main)")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    _git(work, "commit", "-q", "--allow-empty", "-m", "feature commit")
    tested_sha = _git(work, "rev-parse", "HEAD")
    # The pinned base is the feature commit, not origin/main, so the real fetch disagrees with it.
    with patch("workflow.gate_reuse.resolve_check_detail") as mock_resolve:
        mock_resolve.return_value = MagicMock(command="true")
        result = decide_gate_reuse(
            config=config,
            worktree=work,
            main_worktree=work,
            tested_sha=tested_sha,
            base_sha=tested_sha,
        )
        test_result(
            "decide_gate_reuse: stale-base",
            isinstance(result, Miss) and result.reason == "stale-base",
            f"result={result}"
        )

print("[Test 18] decide_gate_reuse: digest-uncomputable (expected_full_key returns None)")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    # Patched: the real None path needs an unreadable deps stamp, which this fixture does not create.
    with patch("workflow.gate_reuse.expected_full_key", return_value=None):
        result = _decide(config, work, tested_sha, base_sha)
        test_result(
            "decide_gate_reuse: digest-uncomputable",
            isinstance(result, Miss) and result.reason == "digest-uncomputable",
            f"result={result}"
        )

print("[Test 19] decide_gate_reuse: record-unparseable (real invalid record at the expected name)")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    expected = expected_full_key(work, work, timeout=300)
    records_dir = work / ".claude" / "check-records"
    records_dir.mkdir(parents=True, exist_ok=True)
    (records_dir / f"{record_filename_hash(expected.key)}.json").write_text("not valid json")

    result = _decide(config, work, tested_sha, base_sha)
    test_result(
        "decide_gate_reuse: record-unparseable",
        isinstance(result, Miss) and result.reason == "record-unparseable",
        f"result={result}"
    )

print("[Test 20] decide_gate_reuse: minimum-record-only (real minimum record, same tree and full digest)")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    expected = expected_full_key(work, work, timeout=300)
    min_key = MinimumKey(
        tree=expected.tree,
        full_digest=expected.key.full_digest,
        minimum_digest="a" * 64,  # placeholder: a minimum record's own digests are not checked here
        changed_files_hash="b" * 64,
    )
    _write_record_file(work / ".claude" / "check-records", min_key)

    result = _decide(config, work, tested_sha, base_sha)
    test_result(
        "decide_gate_reuse: minimum-record-only",
        isinstance(result, Miss) and result.reason == "minimum-record-only",
        f"result={result}"
    )

print("[Test 21] decide_gate_reuse: digest-mismatch (real full record, same tree, other digest)")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    expected = expected_full_key(work, work, timeout=300)
    other_key = FullKey(tree=expected.tree, full_digest="e" * 64)  # placeholder: a different toolchain or command
    _write_record_file(work / ".claude" / "check-records", other_key)

    result = _decide(config, work, tested_sha, base_sha)
    test_result(
        "decide_gate_reuse: digest-mismatch",
        isinstance(result, Miss) and result.reason == "digest-mismatch",
        f"result={result}"
    )

print("[Test 22] decide_gate_reuse: error (exception in classify_full_record)")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")

    with patch("workflow.gate_reuse.classify_full_record") as mock_classify:
        mock_classify.side_effect = RuntimeError("Test error")
        result = _decide(config, work, tested_sha, base_sha)
        test_result(
            "decide_gate_reuse: error (exception caught)",
            isinstance(result, Miss) and result.reason == "error",
            f"result={result}"
        )

print("[Test 23] decide_gate_reuse: no-record (real repo, no record for the expected key)")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")
    result = _decide(config, work, tested_sha, base_sha)
    test_result(
        "decide_gate_reuse: no-record",
        isinstance(result, Miss) and result.reason == "no-record",
        f"result={result}"
    )

print("[Test 24] decide_gate_reuse: real round trip, validate writes a record and the gate is reused")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")
    head_tree = _git(work, "rev-parse", "HEAD^{tree}")

    first = validate(work, mode="full", timeout=300)
    decision = _decide(config, work, tested_sha, base_sha)
    test_result(
        "round trip: validate passed and wrote a record",
        first.get("status") == "passed" and first.get("satisfied_by") is not None,
        f"first={first!r}",
    )
    test_result(
        "round trip: decide_gate_reuse returns Reuse for the record validate wrote",
        isinstance(decision, Reuse)
        and decision.record_name == first.get("satisfied_by")
        and decision.expected.tree == head_tree,
        f"decision={decision}",
    )

print("[Test 25] classify and key: a lockfile change and a run-timeout change alter the full digest")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    before = expected_full_key(work, work, timeout=300)
    (work / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    after_lockfile = expected_full_key(work, work, timeout=300)
    after_timeout = expected_full_key(work, work, timeout=900)
    test_result(
        "expected_full_key: a lockfile change changes the full digest",
        before is not None and after_lockfile is not None
        and before.key.full_digest != after_lockfile.key.full_digest,
        f"before={before} after={after_lockfile}",
    )
    test_result(
        "expected_full_key: the run timeout is part of the full digest",
        before is not None and after_timeout is not None
        and before.key.full_digest != after_timeout.key.full_digest,
        f"before={before} after={after_timeout}",
    )

print("[Test 26] validate_config_data: gate_reuse_disabled must be a boolean")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    rejected = None
    try:
        validate_config_data({"base": "main", "steps": ["true"], "gate_reuse_disabled": "yes"}, cwd=work)
    except ValueError as e:
        rejected = e
    test_result(
        "validate_config_data: a non-bool kill switch is rejected",
        rejected is not None and "gate_reuse_disabled must be a boolean" in str(rejected),
        f"rejected={rejected}",
    )
    accepted = validate_config_data({"base": "main", "steps": ["true"], "gate_reuse_disabled": True}, cwd=work)
    test_result(
        "validate_config_data: a boolean kill switch is accepted",
        accepted.gate_reuse_disabled is True,
        f"gate_reuse_disabled={accepted.gate_reuse_disabled!r}",
    )

print("[Test 27] Phase 7: a Reuse writes a gate_reused row and does not run the gate")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    state = Path(tmp) / "state"
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    head_tree = _git(work, "rev-parse", "HEAD^{tree}")
    head = _git(work, "rev-parse", "HEAD")
    reuse = Reuse(
        record_name="rec-x",
        written_at=1.0,
        expected=ExpectedFullKey(
            command_digest="c" * 64,  # placeholder digests: decide_gate_reuse is patched in Phase 7 tests
            toolchain_digest="t" * 64,
            key=FullKey(tree=head_tree, full_digest="d" * 64),
        ),
    )
    run_step = MagicMock(return_value=MagicMock(success=True))
    append = MagicMock()
    kick, tested, fields = _run_phase7(work, state, reuse, run_step, append, config)
    events = [c.args[0].get("event") for c in append.call_args_list]
    test_result(
        "Phase 7 reuse: the gate is not run",
        run_step.call_count == 0,
        f"run_step calls={run_step.call_count}",
    )
    test_result(
        "Phase 7 reuse: a gate_reused audit row is written",
        "gate_reused" in events and "gate_reuse_miss" not in events,
        f"events={events}",
    )
    test_result(
        "Phase 7 reuse: no kickback, tested_sha recorded, gate_reuse is reused",
        kick is None and tested == head and fields.get("gate_reuse") == "reused",
        f"kick={kick} tested={tested} fields={fields}",
    )

print("[Test 28] Phase 7: a failed audit write runs the gate instead of skipping it")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    state = Path(tmp) / "state"
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    head_tree = _git(work, "rev-parse", "HEAD^{tree}")
    reuse = Reuse(
        record_name="rec-x",
        written_at=1.0,
        expected=ExpectedFullKey(
            command_digest="c" * 64,  # placeholder digests: decide_gate_reuse is patched in Phase 7 tests
            toolchain_digest="t" * 64,
            key=FullKey(tree=head_tree, full_digest="d" * 64),
        ),
    )

    def _audit_write_fails(event):
        if event.get("event") == "gate_reused":
            raise OSError("queue.jsonl is not writable")

    run_step = MagicMock(return_value=MagicMock(success=True))
    append = MagicMock(side_effect=_audit_write_fails)
    kick, _tested, fields = _run_phase7(work, state, reuse, run_step, append, config)
    test_result(
        "Phase 7 audit failure: the gate runs once",
        run_step.call_count == 1,
        f"run_step calls={run_step.call_count}",
    )
    test_result(
        "Phase 7 audit failure: the decision is recorded as miss:audit-write-failed",
        kick is None and fields.get("gate_reuse") == "miss:audit-write-failed",
        f"kick={kick} fields={fields}",
    )

print("[Test 29] Phase 7: the gate steps get no scratch settings (no env passed to run_step)")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    state = Path(tmp) / "state"
    config = MergeQueueConfig(
        base="main",
        steps=[Step(cmd="true")],
        scratch_env={"SCRATCH_ONLY": "1"},  # must not reach the Phase 7 gate
    )
    miss = Miss("no-record", "placeholder: the gate must run")
    run_step = MagicMock(return_value=MagicMock(success=True))
    append = MagicMock()
    _run_phase7(work, state, miss, run_step, append, config)
    test_result(
        "Phase 7 no-scratch guard: run_step is called once",
        run_step.call_count == 1,
        f"run_step calls={run_step.call_count}",
    )
    for call_idx, call in enumerate(run_step.call_args_list):
        test_result(
            f"Phase 7 no-scratch guard: call {call_idx + 1} has no env",
            "env" not in call.kwargs and len(call.args) < 6,
            f"kwargs={sorted(call.kwargs)} args={len(call.args)}",
        )

print("[Test 30] Trailer: the default trailer is unchanged and gate=reused marks a reused gate")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    base = _git(work, "rev-parse", "HEAD")
    tested_head = "b" * 40  # placeholder: parse_trailer checks only that tested-base is the parent
    default_line = build_trailer(base, tested_head)
    reused_line = build_trailer(base, tested_head, reused=True)
    test_result(
        "build_trailer: the default trailer has no gate marker",
        default_line == f"Merge-Gate: merge-queue tested-base={base} tested-head={tested_head}",
        f"default_line={default_line}",
    )
    test_result(
        "build_trailer: reused=True appends ' gate=reused'",
        reused_line == default_line + " gate=reused",
        f"reused_line={reused_line}",
    )
    # parse_trailer requires tested-base to be the commit's first parent, so both merge commits sit directly on base.
    tree = _git(work, "rev-parse", "HEAD^{tree}")
    reused_commit = _git(work, "commit-tree", tree, "-p", base, "-m", f"Tested by merge-queue.\n\n{reused_line}")
    default_commit = _git(work, "commit-tree", tree, "-p", base, "-m", f"Tested by merge-queue.\n\n{default_line}")
    parsed = parse_trailer(reused_commit, cwd=work)
    test_result(
        "parse_trailer: a reused trailer still parses to (tested_base, tested_head)",
        parsed == (base, tested_head) and len(parsed) == 2,
        f"parsed={parsed}",
    )
    test_result(
        "trailer_gate_reused: True for a commit carrying the marker",
        trailer_gate_reused(reused_commit, cwd=work) is True,
    )
    test_result(
        "trailer_gate_reused: False for a commit with the default trailer",
        trailer_gate_reused(default_commit, cwd=work) is False,
    )
    test_result(
        "parse_trailer: the default trailer still parses",
        parse_trailer(default_commit, cwd=work) == (base, tested_head),
    )

print("[Test 31] Status: counters, opportunity lines, and the since filter")
with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "state"
    results = state / "results"
    results.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    rows = [
        {"gate_reuse": "reused", "gate_reuse_full_record_seen": True, "gate_reuse_decided_at": now.isoformat()},
        {"gate_reuse": "miss:dirty-tree", "gate_reuse_full_record_seen": False, "gate_reuse_decided_at": now.isoformat()},
        {"gate_reuse": "miss:no-record", "gate_reuse_full_record_seen": None, "gate_reuse_decided_at": now.isoformat()},
    ]
    for i, row in enumerate(rows):
        (results / f"r{i}.json").write_text(json.dumps(row))
    lines = _gate_reuse_status_lines(state, None)
    test_result(
        "status: reused count and miss reasons",
        lines[0] == "Gate reuse (last 3): reused 1/3; misses: dirty-tree 1, no-record 1",
        f"lines={lines}",
    )
    test_result(
        "status: opportunity counts the seen rows and the unknown rows",
        lines[1] == "Opportunity: 1/3 runs had a full record for the tested tree (unknown: 1)",
        f"lines={lines}",
    )
    test_result(
        "status: the seen rows split by reuse and guard refusals",
        lines[2] == "Of those 1: reused 1; refused by guard: none; unexpected: none",
        f"lines={lines}",
    )
    future = now + timedelta(days=1)
    filtered = _gate_reuse_status_lines(state, future)
    test_result(
        "status: a since bound after every decision excludes all rows",
        filtered[0] == "Gate reuse (last 0): reused 0/0; misses: none",
        f"filtered={filtered}",
    )

print("[Test 32] Status buckets: every MissReason is counted exactly once")
buckets = tuple(_GUARD_REFUSALS) + tuple(_UNEXPECTED_MISSES) + ("no-record", "minimum-record-only")
test_result(
    "status buckets: the buckets cover MissReason exactly",
    set(buckets) == set(get_args(MissReason)),
    f"missing={set(get_args(MissReason)) - set(buckets)} extra={set(buckets) - set(get_args(MissReason))}",
)
test_result(
    "status buckets: no reason is in two buckets",
    len(buckets) == len(set(buckets)),
    f"buckets={buckets}",
)

print()
h.summarize_and_exit()
