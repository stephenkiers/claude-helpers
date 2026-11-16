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
    effective_gate_timeout,
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
    _merge_pr,
    MergePROutcome,
    read_pr_result,
    write_result_json,
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

def _round_trip_result(result, state, cwd):
    """write_result_json into a temp state dir, then read result-<pr>.json back with read_pr_result."""
    (state / "results").mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(patch("workflow.merge_queue.ensure_state_dir", return_value=state))
        stack.enter_context(patch("workflow.merge_queue.get_state_dir", return_value=state))
        stack.enter_context(patch("workflow.merge_queue.prune_old_results"))
        write_result_json(result)
        return read_pr_result(result.pr, cwd=cwd)


print("[Test 11] MergeResult gate_reuse provenance from Phase 7 (reused and miss) survives a result-file round trip")
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
with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "state"
    reused_read = _round_trip_result(
        _with_gate_fields(base_result, _gate_reuse_fields(reuse_outcome, True, ts)), state, Path(tmp),
    )
    test_result(
        "MergeResult: reused gate is read back from result-<pr>.json with record, tree, digest, seen flag and decided_at",
        reused_read is not None
        and reused_read[0].gate_reuse == "reused"
        and reused_read[0].gate_reuse_record == "rec-name"
        and reused_read[0].gate_reuse_tree == "c" * 40
        and reused_read[0].gate_reuse_digest == "d" * 64
        and reused_read[0].gate_reuse_full_record_seen is True
        and reused_read[0].gate_reuse_decided_at == ts,
        f"read={reused_read}",
    )
    miss_read = _round_trip_result(
        _with_gate_fields(base_result, _gate_reuse_fields(Miss("no-record", "none"), False, ts)), state, Path(tmp),
    )
    test_result(
        "MergeResult: a miss is read back as miss:<reason> with the record fields cleared",
        miss_read is not None
        and miss_read[0].gate_reuse == "miss:no-record"
        and miss_read[0].gate_reuse_record is None
        and miss_read[0].gate_reuse_tree is None
        and miss_read[0].gate_reuse_digest is None
        and miss_read[0].gate_reuse_full_record_seen is False,
        f"read={miss_read}",
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
with tempfile.TemporaryDirectory() as tmp:
    default_read = _round_trip_result(base_result, Path(tmp) / "state", Path(tmp))
test_result(
    "MergeResult: default gate fields are read back from the result file as None",
    default_read is not None
    and default_read[0].gate_reuse is None
    and default_read[0].gate_reuse_full_record_seen is None,
    f"read={default_read}",
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

print("[Test 25b] effective_gate_timeout: an unset step timeout resolves like checks validate for the check command")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    cache_path = work / ".claude" / "repo-cache.json"
    cache = json.loads(cache_path.read_text())
    cache["checkTimeoutSecs"] = 777
    cache_path.write_text(json.dumps(cache))
    qconfig = MergeQueueConfig(base="main", steps=[Step(cmd="true")], mutation_timeout_secs=300)
    explicit = MergeQueueConfig(base="main", steps=[Step(cmd="true", timeout_secs=450)], mutation_timeout_secs=300)
    other = MergeQueueConfig(base="main", steps=[Step(cmd="echo other")], mutation_timeout_secs=300)
    got = effective_gate_timeout(qconfig, qconfig.steps[0], work, work)
    test_result("effective_gate_timeout: unset step timeout on the check command uses checkTimeoutSecs", got == 777, f"got={got}")
    got = effective_gate_timeout(explicit, explicit.steps[0], work, work)
    test_result("effective_gate_timeout: an explicit step timeout wins", got == 450, f"got={got}")
    got = effective_gate_timeout(other, other.steps[0], work, work)
    test_result("effective_gate_timeout: a non-check step keeps mutation_timeout_secs", got == 300, f"got={got}")

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
            call.kwargs.get("env") is None and call.kwargs.get("settings") is None and len(call.args) <= 5,
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

print("[Test 33] validate_config_data: int and string kill switch values are rejected; False and absent are accepted")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    rejected_values = []
    for bad in (1, 0, "true", "false"):
        try:
            validate_config_data({"base": "main", "steps": ["true"], "gate_reuse_disabled": bad}, cwd=work)
        except ValueError:
            rejected_values.append(bad)
    test_result(
        "validate_config_data: non-bool kill switch values (1, 0, 'true', 'false') are all rejected",
        rejected_values == [1, 0, "true", "false"],
        f"rejected={rejected_values}",
    )
    off = validate_config_data({"base": "main", "steps": ["true"], "gate_reuse_disabled": False}, cwd=work)
    test_result(
        "validate_config_data: an explicit False kill switch is accepted",
        off.gate_reuse_disabled is False,
        f"gate_reuse_disabled={off.gate_reuse_disabled!r}",
    )
    absent = validate_config_data({"base": "main", "steps": ["true"]}, cwd=work)
    test_result(
        "validate_config_data: an absent kill switch defaults to gate reuse enabled",
        absent.gate_reuse_disabled is False,
        f"gate_reuse_disabled={absent.gate_reuse_disabled!r}",
    )

print("[Test 34] decide_gate_reuse: kill switch False is not a disable; a malformed value is named in the detail")
with tempfile.TemporaryDirectory() as tmp:
    worktree = Path(tmp) / "wt"
    worktree.mkdir()
    off_work = _fixture_with_check(Path(tmp), "true")
    off_tested = _git(off_work, "rev-parse", "HEAD")
    off_base = _git(off_work, "rev-parse", "main")
    off_result = _decide(
        MergeQueueConfig(base="main", steps=[Step(cmd="true")], gate_reuse_disabled=False),
        off_work,
        off_tested,
        off_base,
    )
    test_result(
        "decide_gate_reuse: kill switch False reaches the record lookup (a real no-record miss)",
        isinstance(off_result, Miss) and off_result.reason == "no-record",
        f"result={off_result}",
    )
    malformed_result = decide_gate_reuse(
        config=MergeQueueConfig(base="main", steps=[Step(cmd="true")], gate_reuse_disabled="yes"),
        worktree=worktree,
        main_worktree=worktree,
        tested_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        base_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    test_result(
        "decide_gate_reuse: a malformed kill switch detail carries repr('yes')",
        isinstance(malformed_result, Miss)
        and malformed_result.reason == "disabled"
        and "'yes'" in repr(malformed_result),
        f"result={malformed_result!r}",
    )

print("[Test 35] expected_full_key: a check command change and a new commit each change the key")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    before = expected_full_key(work, work, timeout=300)
    cache = work / ".claude" / "repo-cache.json"
    cache.write_text(json.dumps({"version": 1, "commands": {"check": "echo changed"}}) + "\n")
    after_command = expected_full_key(work, work, timeout=300)
    test_result(
        "expected_full_key: a check command change changes the full digest",
        before is not None and after_command is not None
        and before.key.full_digest != after_command.key.full_digest,
        f"before={before} after={after_command}",
    )
    cache.write_text(json.dumps({"version": 1, "commands": {"check": "true"}}) + "\n")
    (work / "second.txt").write_text("two\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "second")
    new_tree = _git(work, "rev-parse", "HEAD^{tree}")
    after_commit = expected_full_key(work, work, timeout=300)
    test_result(
        "expected_full_key: a new commit moves the key tree to the new HEAD tree",
        before is not None and after_commit is not None
        and after_commit.key.tree == new_tree and after_commit.key.tree != before.key.tree,
        f"before={before} after={after_commit} new_tree={new_tree}",
    )
    test_result(
        "record_filename_hash: different full digests map to different record file names",
        before is not None and after_command is not None
        and record_filename_hash(before.key) != record_filename_hash(after_command.key),
        f"before={before} after={after_command}",
    )

print("[Test 36] Phase 7: a Miss runs the gate once, writes no gate_reused row, and records miss:<reason>")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    state = Path(tmp) / "state"
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    run_step = MagicMock(return_value=MagicMock(success=True))
    append = MagicMock()
    _kick, _tested, fields = _run_phase7(
        work, state, Miss("digest-mismatch", "placeholder: the gate must run"), run_step, append, config,
    )
    events = [c.args[0].get("event") for c in append.call_args_list]
    test_result(
        "Phase 7 miss: the gate runs exactly once",
        run_step.call_count == 1,
        f"run_step calls={run_step.call_count}",
    )
    test_result(
        "Phase 7 miss: no gate_reused row is written",
        "gate_reused" not in events,
        f"events={events}",
    )
    test_result(
        "Phase 7 miss: the result records gate_reuse as miss:digest-mismatch",
        fields.get("gate_reuse") == "miss:digest-mismatch",
        f"fields={fields}",
    )

print("[Test 37] MergeResult.gate_reuse grammar: every MissReason and 'reused' are accepted; other forms are rejected")
accepted_failures = []
for accepted in ["reused"] + [f"miss:{reason}" for reason in get_args(MissReason)]:
    try:
        MergeResult(outcome=MergeOutcome.REFUSED, pr=46, branch="x", worktree="wt", gate_reuse=accepted)
    except ValueError:
        accepted_failures.append(accepted)
test_result(
    "MergeResult: 'reused' and miss:<every MissReason> are accepted",
    accepted_failures == [],
    f"rejected={accepted_failures}",
)
rejected_forms = []
bad_forms = ["miss", "miss:", "reuse", "reused:", "miss:Dirty-Tree", "miss:made-up"]
for bad in bad_forms:
    try:
        MergeResult(outcome=MergeOutcome.REFUSED, pr=47, branch="x", worktree="wt", gate_reuse=bad)
    except ValueError:
        rejected_forms.append(bad)
test_result(
    "MergeResult: malformed gate_reuse values are all rejected",
    rejected_forms == bad_forms,
    f"rejected={rejected_forms}",
)

print("[Test 38] Trailer: reused=False is the default trailer, and a commit with no trailer has no gate marker")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    base = _git(work, "rev-parse", "HEAD")
    tested_head = "b" * 40
    test_result(
        "build_trailer: reused=False produces the same line as the default",
        build_trailer(base, tested_head, reused=False) == build_trailer(base, tested_head),
    )
    tree = _git(work, "rev-parse", "HEAD^{tree}")
    plain_commit = _git(work, "commit-tree", tree, "-p", base, "-m", "plain commit without a trailer")
    test_result(
        "trailer_gate_reused: False for a commit with no Merge-Gate trailer",
        trailer_gate_reused(plain_commit, cwd=work) is False,
    )

# 39 | overlaps: 8, 10 (lookup-level only; this adds the decide_gate_reuse mapping)
print("[Test 39] decide_gate_reuse: records-unsafe (the records directory is a symlink, real fixture)")
with tempfile.TemporaryDirectory() as tmp:
    work = _fixture_with_check(Path(tmp), "true")
    # The fixture's .claude/.gitignore lists check-records/, which is directory-only and does not match a
    # symlink. Exclude the name locally so the symlink leaves the tree clean and the decision reaches the lookup.
    exclude = work / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as f:
        f.write("check-records\n")
    fake_target = Path(tmp) / "fake"
    fake_target.mkdir()
    (work / ".claude" / "check-records").symlink_to(fake_target)
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")
    result = _decide(config, work, tested_sha, base_sha)
    test_result(
        "decide_gate_reuse: records-unsafe",
        isinstance(result, Miss) and result.reason == "records-unsafe",
        f"result={result}",
    )

# 40 | overlaps: 30, 38 (builder and reader only; this adds the _merge_pr call)
print("[Test 40] _merge_pr: the body it sends carries gate=reused only for a reused gate, read back from a commit")
with tempfile.TemporaryDirectory() as tmp:
    work = _setup_git_repo(Path(tmp))
    tested_sha = _git(work, "rev-parse", "HEAD")
    base_sha = _git(work, "rev-parse", "main")
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])

    def _pr_view(pr, fields, cwd=None):
        if "state" in fields:
            return {"state": "MERGED"}
        return {"headRefOid": tested_sha, "mergeable": "MERGEABLE", "title": "Feature"}

    for flag in (True, False):
        run_gh = MagicMock(return_value="")
        with ExitStack() as stack:
            stack.enter_context(patch("workflow.merge_queue.git.pr_view_json", side_effect=_pr_view))
            stack.enter_context(patch("workflow.merge_queue.Runner.run_gh", run_gh))
            outcome = _merge_pr(42, tested_sha, base_sha, config, cwd=work, gate_reused=flag)
        args = run_gh.call_args.args[0]
        body = args[args.index("--body") + 1]
        commit = _git(work, "commit-tree", _git(work, "rev-parse", "HEAD^{tree}"), "-p", base_sha, "-m", body)
        expected_last = f"Merge-Gate: merge-queue tested-base={base_sha} tested-head={tested_sha}"
        if flag:
            expected_last += " gate=reused"
        test_result(
            f"_merge_pr gate_reused={flag}: the merge succeeds and the sent body ends with the expected trailer",
            outcome == MergePROutcome.SUCCESS and body.splitlines()[-1] == expected_last,
            f"outcome={outcome} last={body.splitlines()[-1]!r}",
        )
        test_result(
            f"_merge_pr gate_reused={flag}: the commit reads back as reused={flag}",
            trailer_gate_reused(commit, cwd=work) is flag
            and parse_trailer(commit, cwd=work) == (base_sha, tested_sha),
            f"reused={trailer_gate_reused(commit, cwd=work)}",
        )

print()
h.summarize_and_exit()
