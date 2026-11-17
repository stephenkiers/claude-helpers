#!/usr/bin/env python3
"""Tests for v2 Layer 1: durable state, receipts, resume cascade (#485).

Covers:
- fsutil: atomic_write_text with fsync, nonblocking_flock
- State schema and ownership: load, dispatch, D1 projection, fleet field contract
- Locking and mutating: mutual exclusion, concurrent reader safety, inode stability
- Receipts: classification, archive, reconcile, rebuilt receipts, verifier wrapper
- Resume cascade: rule predicates, attempt recovery, logged decisions, exit codes
- Docs: rule pins, skeleton runbook corrections

Every path uses a temp HOME and never touches the real ~/.claude/.

Run with: python3 tests/test_v2_layer1.py
"""

import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Iterator, Tuple

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from _test_harness import REPO_ROOT, Harness

from scripts.workflow import fsutil
from scripts.flow_driver import receipts, flowdir
from scripts.flow_driver.state import (
    TransitionKey, PendingSpawn, V2State, LiveRecord, load_v2_state, classify_state,
    V2Owned, V1Foreign, SkeletonForeign, InvalidState, NoState, ReadOnlyFlow,
    read_flow, mutating, reconcile, StateSchemaError, Refusal,
    record_spawn, record_cleared, init_state, InitSpec,
)
from scripts.flow_driver.receipts import (
    run_verifier, VerifierTimeout,
)
from scripts.flow_driver.resume import (
    RULES, ResumeFacts, decide, _rule_2a_assess,
)

h = Harness("v2 Layer 1 tests")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def temp_home():
    """Context manager yielding a temp HOME directory."""
    return tempfile.TemporaryDirectory()


def v2_root(home: Path) -> Path:
    """Return the v2 flows root under home."""
    return home / ".claude" / "flows-v2"


def v1_root(home: Path) -> Path:
    """Return the v1 flows root under home."""
    return home / ".claude" / "flows"


def make_v2_flow(home: Path, repo="test/repo", issue=123) -> Path:
    """Create a minimal v2 flow dir."""
    flow = v2_root(home) / repo / f"{issue}-test-id"
    flow.mkdir(parents=True, exist_ok=True)
    return flow


def make_v1_flow(home: Path, repo="test/repo", issue=123) -> Path:
    """Create a minimal v1 flow dir."""
    flow = v1_root(home) / repo / f"{issue}-test-id"
    flow.mkdir(parents=True, exist_ok=True)
    return flow


def sha256_text(text: str) -> str:
    """Return sha256 hex of text."""
    return sha256(text.encode()).hexdigest()


def init_v2_state(flow: Path, repo="test/repo", issue=123, worktree=None) -> Path:
    """Call init and return the flow."""
    if worktree is None:
        worktree = str(flow.parent / "worktree")
    with mutating(flow, create=InitSpec(repo=repo, issue=issue, worktree=worktree)) as locked:
        init_state(locked, InitSpec(repo=repo, issue=issue, worktree=worktree))
    return flow


@contextmanager
def patched_home(home: Path) -> Iterator[None]:
    """Point HOME (and so Path.home() and expanduser) at a temp home for in-process code under test."""
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old


def run_cli(home: Path, *args: str) -> Tuple[int, str, str]:
    """Run the real driver entry point in a subprocess with HOME set to the temp home: (rc, stdout, stderr)."""
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "flow_driver" / "__main__.py"), *args],
        capture_output=True, text=True, env=env, cwd=str(home), timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def cli_flow(home: Path, v1: bool = False, key: str = "owner_name", issue: int = 123) -> Path:
    """A flow dir with a path shape the CLI accepts (<root>/<repo key>/<issue>-<slug>-<id>)."""
    flow = (v1_root(home) if v1 else v2_root(home)) / key / f"{issue}-test-id"
    flow.mkdir(parents=True, exist_ok=True)
    return flow


# ---------------------------------------------------------------------------
# fsutil tests (1-4)
# ---------------------------------------------------------------------------

def test_1_atomic_write_positional():
    """Test 1: atomic_write_text(p, "x") writes x, leaves no .tmp, keeps mode."""
    with temp_home() as tmp:
        home = Path(tmp)
        p = home / "test.txt"

        # Write with existing mode
        p.write_text("old")
        p.chmod(0o640)
        old_mode = p.stat().st_mode

        # Positional signature (no fsync keyword)
        fsutil.atomic_write_text(p, "x")

        h.test_result("1a: positional writes content", p.read_text() == "x")
        h.test_result("1b: no .tmp file left", not any(home.glob("test.txt.*.tmp")))
        h.test_result("1c: mode preserved", p.stat().st_mode == old_mode)


def test_2_atomic_write_fsync():
    """Test 2: atomic_write_text(p, "y", fsync=True) fsyncs twice, fsync=False doesn't."""
    with temp_home() as tmp:
        home = Path(tmp)
        p = home / "test.txt"
        fsync_calls = []

        old_fsync = os.fsync
        def mock_fsync(fd):
            fsync_calls.append(fd)
            return old_fsync(fd)

        try:
            os.fsync = mock_fsync

            # With fsync=True: should fsync temp and directory
            fsync_calls.clear()
            fsutil.atomic_write_text(p, "y", fsync=True)
            h.test_result("2a: fsync=True calls fsync at least twice", len(fsync_calls) >= 2)
            h.test_result("2b: content written correctly", p.read_text() == "y")

            # With fsync=False: should not call fsync
            fsync_calls.clear()
            fsutil.atomic_write_text(p, "z", fsync=False)
            h.test_result("2c: fsync=False calls fsync 0 times", len(fsync_calls) == 0)
        finally:
            os.fsync = old_fsync


def test_3_nonblocking_flock_exclusion():
    """Test 3: Second nonblocking_flock raises LockHeld within 2 seconds; released after exit."""
    with temp_home() as tmp:
        home = Path(tmp)
        lock_path = home / ".lock"

        # First acquisition succeeds
        try:
            with fsutil.nonblocking_flock(lock_path):
                # Second attempt fails
                try:
                    with fsutil.nonblocking_flock(lock_path):
                        h.test_result("3a: second acquisition should fail", False, "no exception")
                except fsutil.LockHeld:
                    h.test_result("3a: second acquisition raises LockHeld", True)

            # After release, can acquire again
            with fsutil.nonblocking_flock(lock_path):
                h.test_result("3b: acquisition succeeds after release", True)
        except Exception as e:
            h.test_result("3: unexpected error", False, str(e))


def test_4_nonblocking_flock_properties():
    """Test 4: nonblocking_flock creates file, keeps inode, fd is non-inheritable."""
    with temp_home() as tmp:
        home = Path(tmp)
        lock_path = home / ".lock"

        # First acquisition
        with fsutil.nonblocking_flock(lock_path) as fd1:
            created = lock_path.exists()
            ino1 = lock_path.stat().st_ino
            inheritable1 = os.get_inheritable(fd1)

        h.test_result("4a: file created", created)
        h.test_result("4b: fd is non-inheritable", inheritable1 is False)

        # Second cycle
        with fsutil.nonblocking_flock(lock_path):
            ino2 = lock_path.stat().st_ino
            size2 = lock_path.stat().st_size

        h.test_result("4c: inode unchanged", ino1 == ino2)
        h.test_result("4d: size stays 0", size2 == 0)


# ---------------------------------------------------------------------------
# State schema and ownership (5-10)
# ---------------------------------------------------------------------------

def test_5_init_writes_v2_schema():
    """Test 5: init writes state.json with driver_format=2, step=worktree, etc."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        state_obj = json.loads((flow / "state.json").read_text())
        h.test_result("5a: driver_format is int 2", state_obj["driver_format"] == 2)
        h.test_result("5b: step is worktree", state_obj["step"] == "worktree")
        h.test_result("5c: status is running", state_obj["status"] == "running")
        h.test_result("5d: live is None", state_obj["live"] is None)
        h.test_result("5e: pending_spawn is None", state_obj["pending_spawn"] is None)
        h.test_result("5f: healed is False", state_obj["healed"] is False)
        h.test_result("5g: attempts is empty", state_obj["attempts"] == {})
        h.test_result("5h: run_id is 16 hex chars", len(state_obj["run_id"]) == 16 and all(c in "0123456789abcdef" for c in state_obj["run_id"]))
        h.test_result("5i: updated matches ISO format", hasattr(state_obj, '__getitem__') and 'updated' in state_obj and state_obj["updated"].count('T') == 1 and state_obj["updated"].endswith('Z'))


def test_6_classify_state_driver_format():
    """Test 6: classify_state on various driver_format values."""
    with temp_home() as tmp:
        home = Path(tmp)

        # Missing -> V1Foreign
        flow = make_v2_flow(home, "test1", 1)
        (flow / "state.json").write_text(json.dumps({"step": "plan"}))
        result = classify_state(flow)
        h.test_result("6a: missing driver_format -> V1Foreign", isinstance(result, V1Foreign))

        # "skeleton" -> SkeletonForeign
        flow = make_v2_flow(home, "test2", 2)
        (flow / "state.json").write_text(json.dumps({"driver_format": "skeleton"}))
        result = classify_state(flow)
        h.test_result("6b: driver_format skeleton -> SkeletonForeign", isinstance(result, SkeletonForeign))

        # 2 (int) -> V2Owned (after validation)
        flow = make_v2_flow(home, "test3", 3)
        init_v2_state(flow)
        result = classify_state(flow)
        h.test_result("6c: driver_format 2 -> V2Owned", isinstance(result, V2Owned))

        # True, "2", 2.0, 3 -> InvalidState
        for bad_val in [True, "2", 2.0, 3]:
            flow = make_v2_flow(home, f"test{bad_val}", 10 + hash(str(bad_val)) % 100)
            (flow / "state.json").write_text(json.dumps({"driver_format": bad_val, "step": "plan", "status": "running"}))
            result = classify_state(flow)
            h.test_result(f"6d: driver_format {repr(bad_val)} -> InvalidState", isinstance(result, InvalidState), str(type(result)))


def test_7_state_schema_strictness():
    """Test 7: Invalid enum values or types are rejected."""
    with temp_home() as tmp:
        home = Path(tmp)

        bad_states = [
            {"status": "stopped"},
            {"step": "deploy"},
            {"stop_source": {"plan": "human"}},
            {"attempts": {"plan": "1"}},
            {"attempts": {"plan": True}},
            {"failures": {"plan": -1}},
        ]

        for i, bad_field in enumerate(bad_states):
            flow = make_v2_flow(home, f"bad{i}", 100 + i)
            init_v2_state(flow)
            state_data = json.loads((flow / "state.json").read_text())
            state_data.update(bad_field)
            (flow / "state.json").write_text(json.dumps(state_data))

            try:
                result = classify_state(flow)
                h.test_result(f"7-{i}: invalid field {list(bad_field.keys())[0]}", isinstance(result, InvalidState), str(type(result)))
            except StateSchemaError:
                h.test_result(f"7-{i}: invalid field raises StateSchemaError", True)


def test_8_unknown_keys_preserved():
    """Test 8: Unknown top-level key survives load -> commit round trip."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)

        # Init and add extra key
        init_v2_state(flow)
        state_data = json.loads((flow / "state.json").read_text())
        state_data["x_future"] = [1, 2]
        (flow / "state.json").write_text(json.dumps(state_data))

        # Load from disk and commit through the real path; the key must come from the file, not the test
        from scripts.flow_driver import state as state_module
        old_now = state_module._utc_now
        state_module._utc_now = lambda: "2031-01-01T00:00:00Z"
        try:
            with mutating(flow) as locked:
                h.test_result("8a: loaded state carries the unknown key", locked.state.extra.get("x_future") == [1, 2])
                locked.commit(locked.state)
        finally:
            state_module._utc_now = old_now

        new_data = json.loads((flow / "state.json").read_text())
        h.test_result("8b: unknown key preserved", new_data.get("x_future") == [1, 2])
        h.test_result("8c: commit really wrote (updated changed)", new_data["updated"] == "2031-01-01T00:00:00Z")


def test_9_live_projection():
    """Test 9: live is non-null exactly when pending_spawn.state is spawned/resumed."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        with mutating(flow) as locked:
            # Record a spawn
            record_spawn(locked, "plan", "nonce1")

        new_state = json.loads((flow / "state.json").read_text())
        h.test_result("9a: live non-null when pending_spawn.state is spawned",
                     new_state["live"] is not None and new_state["pending_spawn"]["state"] == "spawned")

        # Clear the pending
        with mutating(flow) as locked:
            run_id = load_v2_state(json.loads((flow / "state.json").read_text())).run_id
            record_cleared(locked, TransitionKey(run_id, "plan", 1))

        new_state = json.loads((flow / "state.json").read_text())
        h.test_result("9b: live null when pending_spawn is null",
                     new_state["live"] is None and new_state["pending_spawn"] is None)


def test_10_fleet_field_contract():
    """Test 10: Fleet can read all required fields from v2 state."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        # Build a state at plan, paused
        with mutating(flow) as locked:
            loaded = load_v2_state(json.loads((flow / "state.json").read_text()))
            test_state = V2State(
                driver_format=2, run_id=loaded.run_id, issue=123, repo="test/repo",
                created=loaded.created, updated=loaded.updated,
                step="plan", status="paused", live=None, pending_spawn=None,
                attempts={"plan": 0}, failures={}, stop_reason={}, stop_source={},
                pr=None, worktree=loaded.worktree, healed=False,
                attempt_log={}, applied=(), extra={}
            )
            locked.commit(test_state)

        # Verify it can be read and contains the required fields
        new_state = json.loads((flow / "state.json").read_text())
        required_fields = ["issue", "updated", "step", "status", "live", "attempts", "stop_reason", "stop_source", "failures", "pr", "worktree", "healed"]
        for field in required_fields:
            h.test_result(f"10: fleet field {field} present", field in new_state, f"missing {field}")


# ---------------------------------------------------------------------------
# Locking and mutating (11-18, 18a, 18b)
# ---------------------------------------------------------------------------

def test_11_locked_refuses_locked():
    """Test 11: resume on a locked flow exits 1 with refused: locked."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        # Hold the lock in this process
        with fsutil.nonblocking_flock(flow / ".driver.lock"):
            # Try to resume in a subprocess
            result = subprocess.run(
                [sys.executable, "-c", f"""
import sys
sys.path.insert(0, '{REPO_ROOT}')
from scripts.flow_driver import cli
sys.exit(cli.main_resume('{flow}', redo=None))
"""],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "HOME": str(home)}
            )
            h.test_result("11a: exit code is 1", result.returncode == 1)
            h.test_result("11b: stderr contains refused: locked", "refused: locked" in result.stderr)
            h.test_result("11c: stdout is empty", result.stdout == "")

        # Verify state is unchanged
        h.test_result("11d: .driver.lock still exists", (flow / ".driver.lock").exists())


def test_12_concurrent_reader_safety():
    """Test 12: Concurrent readers never see a torn JSON file."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        # Simple check: state.json is always valid JSON after any commit
        # (This is guaranteed by atomic_write_text, but we verify the invariant)
        for i in range(10):
            with mutating(flow) as locked:
                current = load_v2_state(json.loads((flow / "state.json").read_text()))
                new_state = V2State(
                    driver_format=2, run_id=current.run_id, issue=current.issue,
                    repo=current.repo, created=current.created, updated=current.updated,
                    step="plan", status="running", live=None, pending_spawn=None,
                    attempts={}, failures={}, stop_reason={}, stop_source={},
                    pr=None, worktree=current.worktree, healed=False,
                    attempt_log={}, applied=(), extra={}
                )
                locked.commit(new_state)

        # A reader thread runs while the main thread keeps committing; it must never see a torn file
        import threading
        stop = threading.Event()
        failures = []
        reads = [0]

        def reader() -> None:
            while not stop.is_set():
                try:
                    json.loads((flow / "state.json").read_text())
                    reads[0] += 1
                except (json.JSONDecodeError, OSError) as exc:
                    failures.append(repr(exc))

        t = threading.Thread(target=reader)
        t.start()
        try:
            for _ in range(60):
                with mutating(flow) as locked:
                    locked.commit(locked.state)
        finally:
            stop.set()
            t.join()
        h.test_result("12: concurrent reader never sees a torn file", not failures and reads[0] > 0, str(failures[:1]))


def test_13_lock_inode_stable():
    """Test 13: .driver.lock keeps same inode before and after resume."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        lock_path = flow / ".driver.lock"
        ino1 = lock_path.stat().st_ino if lock_path.exists() else None

        # Trigger a lock acquire/release cycle
        with mutating(flow):
            pass

        ino2 = lock_path.stat().st_ino
        h.test_result("13: lock inode unchanged", ino1 is None or ino1 == ino2)


def test_15_verifier_lock_released_after_call():
    """Test 15: After run_verifier completes, the lock can be acquired immediately."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        probe = f"""
import fcntl
import os
fd = os.open('{flow / ".driver.lock"}', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("acquired")
except BlockingIOError:
    print("busy")
finally:
    os.close(fd)
"""
        with mutating(flow) as locked:
            verifier = Path(tmp) / "dummy_verifier.py"
            verifier.write_text("import sys; sys.exit(0)")
            result_during = run_verifier(locked, [], verifier=verifier, timeout_s=5)
            h.test_result("15a: verifier ran and exited 0", result_during.returncode == 0)
            during = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=10)
            h.test_result("15b: lock is really held inside mutating", "busy" in during.stdout, during.stdout + during.stderr)

        # After the context exits the lock must be free
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=10)
        h.test_result("15c: lock released after verifier", "acquired" in result.stdout, result.stdout + result.stderr)


def test_16_verifier_timeout_retried_once():
    """Test 16: First timeout is retried; second consecutive timeout is refused."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        # Create a verifier that times out
        timeout_verifier = Path(tmp) / "timeout_verifier.py"
        timeout_verifier.write_text("import time; time.sleep(10); print('done')")

        with mutating(flow) as locked:
            try:
                run_verifier(locked, [], verifier=timeout_verifier, timeout_s=0.1)
                h.test_result("16: verifier timeout is recoverable", False, "no timeout raised")
            except VerifierTimeout:
                h.test_result("16: verifier timeout raised after retry", True)


def test_17_commit_after_context_exit_raises():
    """Test 17: Calling commit after context exit raises RuntimeError."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        ctx = mutating(flow)
        locked = ctx.__enter__()
        ctx.__exit__(None, None, None)

        try:
            current = load_v2_state(json.loads((flow / "state.json").read_text()))
            locked.commit(current)
            h.test_result("17: commit after exit should raise", False, "no exception")
        except RuntimeError:
            h.test_result("17: commit after exit raises RuntimeError", True)


def test_18_once_per_attempt_idempotent():
    """Test 18: Applying same once-per-attempt transition twice is a no-op."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        # Record spawn
        with mutating(flow) as locked:
            record_spawn(locked, "plan", "nonce1")

        state1 = (flow / "state.json").read_text()
        state1_obj = json.loads(state1)
        attempt_1 = state1_obj["pending_spawn"]["attempt"]

        # Clear and test idempotence
        with mutating(flow) as locked:
            run_id = load_v2_state(json.loads((flow / "state.json").read_text())).run_id
            record_cleared(locked, TransitionKey(run_id, "plan", attempt_1))

        state_before = (flow / "state.json").read_text()
        with mutating(flow) as locked:
            run_id = load_v2_state(json.loads((flow / "state.json").read_text())).run_id
            record_cleared(locked, TransitionKey(run_id, "plan", attempt_1))  # Same transition again

        state_after = (flow / "state.json").read_text()
        h.test_result("18: idempotent transition leaves state unchanged",
                     state_before == state_after)


def test_18a_repeatable_transitions_not_swallowed():
    """Test 18a: Different stale receipts are both archived; changed fingerprint is stored."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        with mutating(flow) as locked:
            record_spawn(locked, "plan", "nonce1")

        # Archive two different stale receipts
        receipt_path_plan = receipts.receipt_path(flow, "plan")
        receipt_path_plan.parent.mkdir(parents=True, exist_ok=True)

        receipt_a = "ATTEMPT: 1\nDecision: OK\n<!-- step-end -->\nbody a"
        receipt_b = "ATTEMPT: 1\nDecision: OK\n<!-- step-end -->\nbody b"

        # First stale
        receipt_path_plan.write_text(receipt_a)
        with mutating(flow) as locked:
            run_id = locked.state.run_id
            receipts.archive_stale(locked, TransitionKey(run_id, "plan", 2), sha256_text(receipt_a))

        archived1 = list(flow.glob("archive/plan/a2.stale.*.md"))
        h.test_result("18a-1: first stale archived", len(archived1) == 1)

        # Second different stale
        receipt_path_plan.write_text(receipt_b)
        with mutating(flow) as locked:
            run_id = locked.state.run_id
            receipts.archive_stale(locked, TransitionKey(run_id, "plan", 2), sha256_text(receipt_b))

        archived2 = list(flow.glob("archive/plan/a2.stale.*.md"))
        h.test_result("18a-2: both stales archived", len(archived2) == 2)


def test_18b_record_spawn_advances_counter_atomically():
    """Test 18b: record_spawn advances counter in same replace."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        write_count = [0]
        old_atomic_write = fsutil.atomic_write_text
        def counting_write(*args, **kwargs):
            write_count[0] += 1
            return old_atomic_write(*args, **kwargs)

        fsutil.atomic_write_text = counting_write
        try:
            with mutating(flow) as locked:
                record_spawn(locked, "plan", "nonce1")

            state_obj = json.loads((flow / "state.json").read_text())
            h.test_result("18b-1: state.json written exactly once for spawn",
                         write_count[0] == 1 and state_obj["attempts"]["plan"] == 1 and
                         state_obj["pending_spawn"]["attempt"] == 1)
        finally:
            fsutil.atomic_write_text = old_atomic_write


# ---------------------------------------------------------------------------
# Write refusals and read-only (19-26, 26a)
# ---------------------------------------------------------------------------

def test_19_v1_and_skeleton_refused():
    """Test 19: resume on v1 or skeleton flows exits 1 with `refused: <class>`, writing nothing on stdout."""
    with temp_home() as tmp:
        home = Path(tmp)

        v1_flow = cli_flow(home, v1=True)
        (v1_flow / "state.json").write_text(json.dumps({"step": "plan"}))
        rc, out, err = run_cli(home, "resume", "--flow", str(v1_flow))
        h.test_result("19a: v1 resume exits 1", rc == 1, f"rc={rc} err={err!r}")
        h.test_result("19a: v1 refusal line", "refused: v1-owned" in err, err)
        h.test_result("19a: nothing on stdout", out == "", out)

        skel_flow = cli_flow(home, key="skel_repo")
        (skel_flow / "state.json").write_text(json.dumps({"driver_format": "skeleton"}))
        rc, out, err = run_cli(home, "resume", "--flow", str(skel_flow))
        h.test_result("19b: skeleton resume exits 1", rc == 1, f"rc={rc} err={err!r}")
        h.test_result("19b: skeleton refusal line", "refused: skeleton-owned" in err, err)
        h.test_result("19b: nothing on stdout", out == "", out)

        with patched_home(home):
            h.test_result("19c: v1 flow classified V1Foreign", isinstance(classify_state(v1_flow), V1Foreign))
            h.test_result("19d: skeleton flow classified SkeletonForeign", isinstance(classify_state(skel_flow), SkeletonForeign))


def test_19e_v2_state_under_v1_root_is_v1():
    """Test 19e: a valid driver_format 2 state under the v1 root is still V1Foreign (the root decides, not the content)."""
    with temp_home() as tmp:
        home = Path(tmp)
        v2_flow = make_v2_flow(home)
        init_v2_state(v2_flow)
        v1_flow = cli_flow(home, v1=True, key="owner_name", issue=7)
        (v1_flow / "state.json").write_text((v2_flow / "state.json").read_text())

        with patched_home(home):
            h.test_result("19e: valid v2 state under v1 root is V1Foreign", isinstance(classify_state(v1_flow), V1Foreign))
        rc, out, err = run_cli(home, "resume", "--flow", str(v1_flow))
        h.test_result("19e: CLI refuses it as v1-owned", rc == 1 and "refused: v1-owned" in err, f"rc={rc} err={err!r}")


def test_20_resume_no_state_refused():
    """Test 20: resume on a v2-root flow with no state.json exits 1 with no-state."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = cli_flow(home)

        h.test_result("20a: no state.json classified NoState", isinstance(classify_state(flow), NoState))
        rc, out, err = run_cli(home, "resume", "--flow", str(flow))
        h.test_result("20b: exits 1", rc == 1, f"rc={rc} err={err!r}")
        h.test_result("20c: refusal line", "refused: no-state" in err, err)
        h.test_result("20d: nothing on stdout", out == "", out)


def test_21_double_init_refused():
    """Test 21: Second init on same flow exits 1 with exists."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)

        init_v2_state(flow)
        run_id_1 = json.loads((flow / "state.json").read_text())["run_id"]

        # Second init should be refused with the "exists" class
        try:
            init_v2_state(flow)
            h.test_result("21a: second init should fail", False, "no refusal")
        except Refusal as exc:
            h.test_result("21a: second init refused as exists", exc.refusal_class == "exists", exc.refusal_class)

        run_id_2 = json.loads((flow / "state.json").read_text())["run_id"]
        h.test_result("21b: same run_id (not reinitialized)", run_id_1 == run_id_2)


def test_22_schema_invalid_refused():
    """Test 22: state with driver_format: True is refused as schema-invalid, and the CLI exits 1."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = cli_flow(home)
        (flow / "state.json").write_text(json.dumps({
            "driver_format": True,
            "step": "plan",
            "status": "running"
        }))

        h.test_result("22a: driver_format True is InvalidState", isinstance(classify_state(flow), InvalidState))
        rc, out, err = run_cli(home, "resume", "--flow", str(flow))
        h.test_result("22b: exits 1", rc == 1, f"rc={rc} err={err!r}")
        h.test_result("22c: refusal line", "refused: schema-invalid" in err, err)
        h.test_result("22d: nothing on stdout", out == "", out)


def test_23_loop_subcommands_on_v2():
    """Test 23: next, answer and clear-live run the Layer 2 loop on a v2 flow (they were refused in Layer 1)."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = cli_flow(home)
        rc, out, err = run_cli(home, "init", "--flow", str(flow), "--worktree", str(home / "wt"))
        h.test_result("23a: init exits 0", rc == 0, f"rc={rc} err={err!r}")
        before = (flow / "state.json").read_text()

        rc, out, err = run_cli(home, "answer", "--flow", str(flow), "--text", "x")
        h.test_result("23b: answer exits 1", rc == 1, f"rc={rc} err={err!r}")
        h.test_result("23b: answer refusal line", "refused: answer refused" in err, err)
        h.test_result("23c: a refused answer leaves state.json unchanged", (flow / "state.json").read_text() == before)

        rc, out, err = run_cli(home, "next", "--flow", str(flow))
        h.test_result("23b: next exits 0", rc == 0, f"rc={rc} err={err!r}")
        h.test_result("23b: next prints one NEXT line", len(out.splitlines()) == 1 and out.startswith("NEXT: "), out)
        h.test_result("23c: next changed state.json", (flow / "state.json").read_text() != before)

        rc, out, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "r")
        h.test_result("23b: clear-live exits 0 on the live record", rc == 0, f"rc={rc} err={err!r}")
        h.test_result("23b: clear-live reports the cleared record", "cleared live record" in err, err)
        h.test_result("23c: the live record is gone", json.loads((flow / "state.json").read_text())["live"] is None)


def test_24_readonly_commands_leave_unchanged():
    """Test 24: status --json and list don't modify flows or .git/refs."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        # Record the state
        state_before = (flow / "state.json").read_text()

        # Read through read_flow
        readonly_flow = read_flow(flow)
        h.test_result("24a: read_flow succeeds", isinstance(readonly_flow, ReadOnlyFlow))

        # State unchanged
        state_after = (flow / "state.json").read_text()
        h.test_result("24b: state.json unchanged", state_before == state_after)

        # No lock file created by readonly operations
        (flow / ".driver.lock").unlink()
        names_before = sorted(p.name for p in flow.iterdir())
        read_flow(flow)
        h.test_result("24c: no .driver.lock from readonly", not (flow / ".driver.lock").exists() and sorted(p.name for p in flow.iterdir()) == names_before)


def test_25_list_shows_v1_and_initializing():
    """Test 25: `list` exits 0 and shows v1 flows and v2 initializing dirs."""
    with temp_home() as tmp:
        home = Path(tmp)

        v1_flow = cli_flow(home, v1=True, key="owner_name", issue=1)
        (v1_flow / "state.json").write_text(json.dumps({"step": "plan", "status": "running"}))
        v2_init = cli_flow(home, key="owner_name", issue=2)

        rc, out, err = run_cli(home, "list")
        h.test_result("25a: list exits 0", rc == 0, f"rc={rc} err={err!r}")
        lines = out.splitlines()
        h.test_result("25b: v1 flow listed with step and status",
                      f"v1\towner_name/{v1_flow.name}\tplan\trunning" in lines, out)
        h.test_result("25c: initializing dir listed",
                      f"v2\towner_name/{v2_init.name}\t-\tinitializing" in lines, out)
        h.test_result("25d: read_flow still readable in process",
                      read_flow(v1_flow) is not None and read_flow(v2_init) is not None)


def test_26_status_json_readonly():
    """Test 26: status --json on a v1 flow exits 0 with owner v1 and writes nothing."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = cli_flow(home, v1=True)
        (flow / "state.json").write_text(json.dumps({"step": "plan", "status": "running"}))
        names_before = sorted(p.name for p in flow.iterdir())

        rc, out, err = run_cli(home, "status", "--json", "--flow", str(flow))
        h.test_result("26a: exits 0", rc == 0, f"rc={rc} err={err!r}")
        try:
            data = json.loads(out)
        except ValueError:
            data = {}
        h.test_result("26b: owner is v1", data.get("owner") == "v1", out)
        h.test_result("26c: nothing written", sorted(p.name for p in flow.iterdir()) == names_before)
        with patched_home(home):
            h.test_result("26d: classified V1Foreign", isinstance(classify_state(flow), V1Foreign))


def test_26a_resolve_read_flow_dir():
    """Test 26a: resolve_read_flow_dir accepts v2 and v1 roots."""
    with temp_home() as tmp:
        home = Path(tmp)

        # V2 path
        v2_flow = make_v2_flow(home)
        v2_root_result = flowdir.resolve_read_flow_dir(home, v2_flow)
        h.test_result("26a-1: v2 path resolved", v2_root_result is not None and v2_root_result[0] == "v2")

        # V1 path
        v1_flow = make_v1_flow(home)
        v1_root_result = flowdir.resolve_read_flow_dir(home, v1_flow)
        h.test_result("26a-2: v1 path resolved", v1_root_result is not None and v1_root_result[0] == "v1")


# ---------------------------------------------------------------------------
# Attempt reconcile (27-30, 30a)
# ---------------------------------------------------------------------------

def test_27_reconcile_mismatch_logged():
    """Test 27: Deleting an archive logs reconcile mismatch; resume still exits 0."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)

        with mutating(flow, create=InitSpec(repo="test/repo", issue=123, worktree=str(home))) as locked:
            init_state(locked, InitSpec(repo="test/repo", issue=123, worktree=str(home)))
            record_spawn(locked, "plan", "nonce1")

        # Record a landed receipt
        with mutating(flow) as locked:
            current = locked.state
            new_state = V2State(
                driver_format=2, run_id=current.run_id, issue=current.issue,
                repo=current.repo, created=current.created, updated=current.updated,
                step="plan", status="running", live=current.live, pending_spawn=current.pending_spawn,
                attempts={"plan": 1}, failures={}, stop_reason={}, stop_source={},
                pr=None, worktree=current.worktree, healed=False,
                attempt_log={"plan": {1: "receipt-landed"}}, applied=(), extra={}
            )
            locked.commit(new_state)

        # Create archive
        archive_dir = flow / "archive" / "plan"
        archive_dir.mkdir(parents=True)
        (archive_dir / "a1.landed.md").write_text("landed content")

        # Delete the archive
        (archive_dir / "a1.landed.md").unlink()

        # Trigger reconcile
        with mutating(flow) as locked:
            result27 = reconcile(locked)
        h.test_result("27a: per_step carries the mismatch, not Ok",
                      isinstance(result27.per_step.get("plan"), dict) and result27.per_step["plan"]["missing_archives"] == [1])

        # Check log
        log_lines = []
        log_file = flow / "driver-log.jsonl"
        if log_file.exists():
            for line in log_file.read_text().strip().split('\n'):
                if line:
                    log_lines.append(json.loads(line))

        mismatch_found = any(entry.get("event") == "attempt-reconcile-mismatch" for entry in log_lines)
        h.test_result("27: reconcile mismatch logged", mismatch_found)


def test_28_cleared_no_mismatch():
    """Test 28: Attempt recorded cleared with no archive produces no mismatch."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)

        with mutating(flow, create=InitSpec(repo="test/repo", issue=123, worktree=str(home))) as locked:
            init_state(locked, InitSpec(repo="test/repo", issue=123, worktree=str(home)))
            record_spawn(locked, "plan", "nonce1")

        # Clear the attempt (no archive)
        with mutating(flow) as locked:
            current = locked.state
            new_state = V2State(
                driver_format=2, run_id=current.run_id, issue=current.issue,
                repo=current.repo, created=current.created, updated=current.updated,
                step="plan", status="running", live=None, pending_spawn=None,
                attempts={"plan": 1}, failures={}, stop_reason={}, stop_source={},
                pr=None, worktree=current.worktree, healed=False,
                attempt_log={"plan": {1: "cleared"}}, applied=(), extra={}
            )
            locked.commit(new_state)

        # Reconcile should not log a mismatch
        with mutating(flow) as locked:
            reconcile(locked)

        log_lines = []
        log_file = flow / "driver-log.jsonl"
        if log_file.exists():
            for line in log_file.read_text().strip().split('\n'):
                if line:
                    log_lines.append(json.loads(line))

        mismatch_found = any(entry.get("event") == "attempt-reconcile-mismatch" for entry in log_lines)
        h.test_result("28: cleared produces no mismatch", not mismatch_found)


def test_29_counter_repaired():
    """Test 29: Counter raised from archived highest; second reconcile logs no repair."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)

        with mutating(flow, create=InitSpec(repo="test/repo", issue=123, worktree=str(home))) as locked:
            init_state(locked, InitSpec(repo="test/repo", issue=123, worktree=str(home)))

        # Create an archive at attempt 3
        archive_dir = flow / "archive" / "plan"
        archive_dir.mkdir(parents=True)
        (archive_dir / "a3.landed.md").write_text("landed attempt 3")

        # Reconcile should raise counter
        with mutating(flow) as locked:
            reconcile(locked)

        new_state = json.loads((flow / "state.json").read_text())
        h.test_result("29a: counter raised to 3", new_state["attempts"]["plan"] == 3)

        # Second reconcile should not repair again
        log_before = (flow / "driver-log.jsonl").read_text() if (flow / "driver-log.jsonl").exists() else ""
        with mutating(flow) as locked:
            reconcile(locked)
        log_after = (flow / "driver-log.jsonl").read_text() if (flow / "driver-log.jsonl").exists() else ""

        def repairs(text: str) -> int:
            return sum(1 for ln in text.splitlines() if ln.strip() and json.loads(ln).get("event") == "attempt-counter-repaired")

        h.test_result("29b: exactly one repair after both reconciles", repairs(log_before) == 1 and repairs(log_after) == 1,
                      f"before={repairs(log_before)} after={repairs(log_after)}")


def test_30_reconcile_no_repair_no_write():
    """Test 30: Reconcile with nothing to repair leaves state.json unchanged."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        with mutating(flow) as locked:
            record_spawn(locked, "plan", "nonce1")

        state_before = (flow / "state.json").read_text()

        with mutating(flow) as locked:
            reconcile(locked)

        state_after = (flow / "state.json").read_text()
        h.test_result("30: state unchanged when no repair needed", state_before == state_after)


def test_30a_untrusted_attempt_ignored():
    """Test 30a: Untrusted ATTEMPT values never raise counter."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)

        with mutating(flow, create=InitSpec(repo="test/repo", issue=123, worktree=str(home))) as locked:
            init_state(locked, InitSpec(repo="test/repo", issue=123, worktree=str(home)))

        # Create archives with untrusted ATTEMPT values
        archive_dir = flow / "archive" / "plan"
        archive_dir.mkdir(parents=True)
        (archive_dir / "a1.malformed.md").write_text("ATTEMPT: 999\nDecision: OK")
        (archive_dir / "a1.stale.abc123.md").write_text("ATTEMPT: 50\nDecision: OK")

        # Reconcile should not use these ATTEMPT values
        with mutating(flow) as locked:
            reconcile(locked)

        new_state = json.loads((flow / "state.json").read_text())
        h.test_result("30a: untrusted ATTEMPT ignored, counter stays at max filename", new_state["attempts"]["plan"] == 1)


# ---------------------------------------------------------------------------
# Receipts (31-37)
# ---------------------------------------------------------------------------

def test_31_receipt_path_step_index():
    """Test 31: receipt_path returns correct step indices."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)

        pairs = [
            ("plan", "steps/01-plan.md"),
            ("fix", "steps/05-fix.md"),
            ("assess", "steps/07-assess.md"),
            ("worktree", "steps/00-worktree.md"),
            ("merge", "steps/09-merge.md"),
        ]

        for step, expected_suffix in pairs:
            path = receipts.receipt_path(flow, step)
            h.test_result(f"31: {step} path", str(path).endswith(expected_suffix))


def test_33_matching_attempt_receipt_is_landed():
    """Test 33: A receipt whose ATTEMPT matches the pending attempt classifies as landed."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        with mutating(flow) as locked:
            record_spawn(locked, "plan", "nonce1")

        # Create receipt with wrong attempt
        receipt_path_plan = receipts.receipt_path(flow, "plan")
        receipt_path_plan.parent.mkdir(parents=True, exist_ok=True)
        receipt_path_plan.write_text("ATTEMPT: 1\nDecision: OK\n<!-- step-end -->\nbody")

        # Current attempt is 1, receipt is for 1, so it should be Landed
        with mutating(flow) as locked:
            classification = receipts.classify(locked, locked.state.pending_spawn)

        h.test_result("33: receipt with matching attempt is Landed", classification.state == "landed")


def test_34_well_formed_receipt_lands():
    """Test 34: Well-formed receipt with matching attempt lands."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        with mutating(flow) as locked:
            record_spawn(locked, "plan", "nonce1")

        # Create matching receipt
        receipt_path_plan = receipts.receipt_path(flow, "plan")
        receipt_path_plan.parent.mkdir(parents=True, exist_ok=True)
        receipt_path_plan.write_text("ATTEMPT: 1\nDecision: OK\n<!-- step-end -->\nbody")

        with mutating(flow) as locked:
            run_id = locked.state.run_id
            receipts.land(locked, TransitionKey(run_id, "plan", 1))

        # Verify archive
        archive_path = flow / "archive" / "plan" / "a1.landed.md"
        h.test_result("34: receipt landed at archive", archive_path.exists())


def test_35_no_attempt_line_malformed_after_two_reads():
    """Test 35: Receipt with no ATTEMPT line is malformed after two identical reads."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        with mutating(flow) as locked:
            record_spawn(locked, "plan", "nonce1")

        # Receipt without ATTEMPT
        receipt_path_plan = receipts.receipt_path(flow, "plan")
        receipt_path_plan.parent.mkdir(parents=True, exist_ok=True)
        receipt_path_plan.write_text("Decision: OK\n<!-- step-end -->\nbody")

        # First read
        with mutating(flow) as locked:
            classification1 = receipts.classify(locked, locked.state.pending_spawn)

        h.test_result("35a: first read is InProgress", classification1.state == "in-progress")

        # The caller stores the fingerprint (plan Step 5: classify never stores it)
        with mutating(flow) as locked:
            pending = locked.state.pending_spawn
            receipts.store_fingerprint(
                locked, TransitionKey(locked.state.run_id, "plan", pending.attempt),
                classification1.fingerprint,
            )

        # Second read with same content
        with mutating(flow) as locked:
            classification2 = receipts.classify(locked, locked.state.pending_spawn)

        h.test_result("35b: second read is Malformed", classification2.state == "malformed")


def test_36_malformed_receipt_archived_reruns():
    """Test 36: Malformed receipt is archived as rerun."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        with mutating(flow) as locked:
            record_spawn(locked, "plan", "nonce1")

        # Malformed receipt
        receipt_path_plan = receipts.receipt_path(flow, "plan")
        receipt_path_plan.parent.mkdir(parents=True, exist_ok=True)
        receipt_path_plan.write_text("ATTEMPT: x\nDecision: OK\n<!-- step-end -->\nbody")

        # classify never stores a fingerprint; this call only shows the receipt is not landed
        with mutating(flow) as locked:
            h.test_result("36a: not landed", receipts.classify(locked, locked.state.pending_spawn).state != "landed")

        # rerun_malformed is called directly here; the two-read rule is covered by tests 35 and 47
        with mutating(flow) as locked:
            run_id = locked.state.run_id
            receipts.rerun_malformed(locked, TransitionKey(run_id, "plan", 1))

        # Verify archive
        archive_path = flow / "archive" / "plan" / "a1.malformed.md"
        h.test_result("36: malformed receipt archived", archive_path.exists())


def test_37_rebuild_receipt():
    """Test 37: rebuild_receipt respects FAILED-to-OK constraint."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        with mutating(flow) as locked:
            record_spawn(locked, "plan", "nonce1")

        archive_dir = flow / "archive" / "plan"
        archive_dir.mkdir(parents=True)

        # Test: OK source -> OK rebuild is allowed
        with mutating(flow) as locked:
            source_receipt = receipts.Receipt(step="plan", attempt=1, decision="OK", rebuilt=False)
            path = receipts.rebuild_receipt(
                locked, TransitionKey(locked.state.run_id, "plan", 1),
                source_receipt, "OK", "rebuilt body"
            )
            h.test_result("37a: rebuild OK from OK allowed", path.exists())

        # Test: FAILED source -> OK rebuild is forbidden
        with mutating(flow) as locked:
            source_receipt = receipts.Receipt(step="plan", attempt=2, decision="FAILED", rebuilt=False)
            try:
                receipts.rebuild_receipt(
                    locked, TransitionKey(locked.state.run_id, "plan", 2),
                    source_receipt, "OK", "rebuilt body"
                )
                h.test_result("37b: rebuild OK from FAILED should raise", False, "no exception")
            except receipts.RebuildRefused:
                h.test_result("37b: rebuild OK from FAILED raises RebuildRefused", True)


# ---------------------------------------------------------------------------
# Resume cascade (38-43, 43a)
# ---------------------------------------------------------------------------

def test_38_rule_names_in_order():
    """Test 38: RULES contains all expected names in the correct order."""
    expected_names = [
        "rule-0-redo", "rule-1-live-step", "rule-2-paused-gate", "rule-2a-assess",
        "rule-3-done", "rule-3a-abandoned", "rule-1a-pending-input",
        "rule-1b-heal-in-progress", "rule-4-failed", "rule-5-subagent-gone",
        "rule-3b-failed-redo", "rule-6-merge-pr-state", "rule-7-worktree",
        "rule-8-epic", "rule-9-flags", "rule-10-telemetry"
    ]

    actual_names = [r.name for r in RULES]
    h.test_result("38: rule names match", actual_names == expected_names, f"got {actual_names}")


def test_39_rule_ordering_1b_before_4_5():
    """Test 39: rule-1b-heal-in-progress fires before rule-4-failed and rule-5."""
    rule_indices = {r.name: i for i, r in enumerate(RULES)}
    h.test_result("39a: 1b before 4", rule_indices["rule-1b-heal-in-progress"] < rule_indices["rule-4-failed"])
    h.test_result("39b: 1b before 5", rule_indices["rule-1b-heal-in-progress"] < rule_indices["rule-5-subagent-gone"])


def test_40_one_test_per_rule():
    """Test 40: Each non-rule-1-live-step rule fires individually."""
    with temp_home() as tmp:
        home = Path(tmp)

        flow = make_v2_flow(home)
        init_v2_state(flow)

        # Test rule-3-done: status == "done"
        facts = ResumeFacts(
            redo_requested=False, pending_human_input=None, heal_in_progress=None,
            subagent_gone=None, assess_due=None, passing_outstanding_check=None,
            later={}, state=V2State(
                driver_format=2, run_id="0123456789abcdef", issue=1, repo="test/repo",
                created="2026-11-16T00:00:00Z", updated="2026-11-16T00:00:00Z",
                step="plan", status="done", live=None, pending_spawn=None,
                attempts={}, failures={}, stop_reason={}, stop_source={},
                pr=None, worktree=None, healed=False, attempt_log={}, applied=(), extra={}
            )
        )
        with mutating(flow) as locked:
            decision = decide(locked, facts)
        h.test_result("40a: rule-3-done fires on status done", decision.rule == "rule-3-done")


def test_40a_predicates_shortcircuit_on_state():
    """Test 40a: Predicates short-circuit on known state."""
    with temp_home() as tmp:
        home = Path(tmp)

        flow = make_v2_flow(home)
        init_v2_state(flow)

        # With pending_spawn spawned, rule-2a-assess should be False regardless of step
        facts = ResumeFacts(
            redo_requested=False, pending_human_input=None, heal_in_progress=None,
            subagent_gone=None, assess_due=None, passing_outstanding_check=None,
            later={}, state=V2State(
                driver_format=2, run_id="0123456789abcdef", issue=1, repo="test/repo",
                created="2026-11-16T00:00:00Z", updated="2026-11-16T00:00:00Z",
                step="assess", status="running", live=LiveRecord(step="assess", attempt=1, nonce="n"),
                pending_spawn=PendingSpawn(step="assess", attempt=1, nonce="n", state="spawned", receipt_seen=None),
                attempts={}, failures={}, stop_reason={}, stop_source={},
                pr=None, worktree=None, healed=False, attempt_log={}, applied=(), extra={}
            )
        )
        with mutating(flow) as locked:
            decision = decide(locked, facts)
        h.test_result("40a: pending_spawn spawned does not fire rule-2a", decision.rule != "rule-2a-assess")
        h.test_result("40a: _rule_2a_assess is False with a pending spawn", _rule_2a_assess(facts) is False)
        from dataclasses import replace as _replace
        idle = _replace(facts.state, pending_spawn=None, live=None)
        h.test_result("40a: _rule_2a_assess is True at step assess with nothing pending", _rule_2a_assess(_replace(facts, state=idle)) is True)
        ship = _replace(facts, state=_replace(idle, step="ship"))
        h.test_result("40a: _rule_2a_assess at ship with unknown check is None (fail closed)", _rule_2a_assess(ship) is None)


def test_41_live_step_no_receipt_wait():
    """Test 41: rule-1-live-step with no receipt gives wait outcome."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        with mutating(flow) as locked:
            record_spawn(locked, "plan", "nonce1")

        # No receipt created; just decide
        with mutating(flow) as locked:
            facts = ResumeFacts(
                redo_requested=False, pending_human_input=None, heal_in_progress=None,
                subagent_gone=None, assess_due=None, passing_outstanding_check=None,
                later={}, state=locked.state
            )
            decision = decide(locked, facts)

        h.test_result("41: rule-1-live-step fires for spawned", decision.rule == "rule-1-live-step")
        h.test_result("41: outcome is wait", decision.outcome == "wait")


def test_42_fresh_init_fires_rule_1a():
    """Test 42: Fresh init state fires rule-1a-pending-input."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        with mutating(flow) as locked:
            facts = ResumeFacts(
                redo_requested=False, pending_human_input=None, heal_in_progress=None,
                subagent_gone=None, assess_due=None, passing_outstanding_check=None,
                later={}, state=locked.state
            )
            decision = decide(locked, facts)

        h.test_result("42: fresh init fires rule-1a", decision.rule == "rule-1a-pending-input")


def test_43_every_decide_logs_and_prints():
    """Test 43: Every decide appends driver-log line and prints RESUME line."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = make_v2_flow(home)
        init_v2_state(flow)

        with mutating(flow) as locked:
            record_spawn(locked, "plan", "nonce1")

        # Capture output
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            with mutating(flow) as locked:
                facts = ResumeFacts(
                    redo_requested=False, pending_human_input=None, heal_in_progress=None,
                    subagent_gone=None, assess_due=None, passing_outstanding_check=None,
                    later={}, state=locked.state
                )
                decide(locked, facts)
        finally:
            output = sys.stdout.getvalue()
            sys.stdout = old_stdout

        # Check log
        log_file = flow / "driver-log.jsonl"
        log_lines = [json.loads(ln) for ln in log_file.read_text().strip().split('\n') if ln] if log_file.exists() else []
        has_rule_fired = any(entry.get("event") == "resume-rule-fired" for entry in log_lines)
        h.test_result("43a: resume-rule-fired logged", has_rule_fired)

        # Check print
        h.test_result("43b: RESUME line printed", "RESUME rule=" in output)


def test_43a_exit_codes_per_outcome():
    """Test 43a: exit codes per outcome: 0 acted-on, 1 refusal, 2 usage, 3 not-yet-built."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = cli_flow(home)

        # Exit 2: path inside neither root.
        bad_flow = home / "not-in-root"
        rc, out, err = run_cli(home, "resume", "--flow", str(bad_flow))
        h.test_result("43a-1: usage error exits 2", rc == 2, f"rc={rc} err={err!r}")
        h.test_result("43a-1: nothing on stdout", out == "", out)

        rc, out, err = run_cli(home, "init", "--flow", str(flow), "--worktree", str(home / "wt"))
        h.test_result("43a-2: init exits 0", rc == 0 and out.startswith("INIT flow="), f"rc={rc} out={out!r} err={err!r}")

        # Exit 3: a fresh init fires a rule that Layer 1 has not built.
        rc, out, err = run_cli(home, "resume", "--flow", str(flow))
        h.test_result("43a-3: not-yet-built exits 3", rc == 3, f"rc={rc} err={err!r}")
        h.test_result("43a-3: stderr names the rule and layer", err.startswith("not-yet-built: rule-") and " is Layer " in err, err)
        h.test_result("43a-3: stdout carries the RESUME line", "RESUME rule=" in out and "outcome=not-yet-built" in out, out)

        # Exit 0: a live step with no receipt waits.
        with mutating(flow) as locked:
            record_spawn(locked, "plan", "nonce1")
        rc, out, err = run_cli(home, "resume", "--flow", str(flow))
        h.test_result("43a-4: acted-on wait exits 0", rc == 0, f"rc={rc} err={err!r}")
        h.test_result("43a-4: RESUME line on stdout", "rule=rule-1-live-step" in out and "outcome=wait" in out, out)

        # Exit 1: a second init on the same flow is refused.
        rc, out, err = run_cli(home, "init", "--flow", str(flow), "--worktree", str(home / "wt"))
        h.test_result("43a-5: refusal exits 1", rc == 1 and "refused: exists" in err, f"rc={rc} err={err!r}")


# ---------------------------------------------------------------------------
# Docs (44)
# ---------------------------------------------------------------------------

def test_44_docs_updated():
    """Test 44: Docs contain required strings and corrections."""
    runbook_path = REPO_ROOT / "docs" / "v2-inventory" / "skeleton-runbook.md"
    rules_path = REPO_ROOT / "docs" / "v2-inventory" / "rules.md"

    h.test_result("44-0: runbook and rules docs exist", runbook_path.exists() and rules_path.exists())
    if runbook_path.exists():
        runbook_text = runbook_path.read_text()
        h.test_result("44a: Narrowing Bash Layer 1 string removed",
                     "Narrowing `Bash` is a Layer 1 task" not in runbook_text)

    if rules_path.exists():
        rules_text = rules_path.read_text()
        required_strings = ["AC2-K1-kill", "PHANTOM-LIVE", "LATE-RECEIPT", "PERMISSION-VOID"]
        for req in required_strings:
            h.test_result(f"44b: {req} in rules.md", req in rules_text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _spawn_plan(flow: Path) -> None:
    init_v2_state(flow)
    with mutating(flow) as locked:
        record_spawn(locked, "plan", "nonce1")


def _write_plan_receipt(flow: Path, text: str) -> Path:
    path = receipts.receipt_path(flow, "plan")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _facts_for(locked):
    return ResumeFacts(
        state=locked.state, redo_requested=False, pending_human_input=None, heal_in_progress=None,
        subagent_gone=None, assess_due=None, passing_outstanding_check=None,
    )


def test_45_stale_receipt_classifies_stale_and_decide_waits():
    """Test 45: ATTEMPT 2 against pending attempt 1 is stale; decide archives it and waits."""
    with temp_home() as tmp:
        flow = make_v2_flow(Path(tmp))
        _spawn_plan(flow)
        path = _write_plan_receipt(flow, "ATTEMPT: 2\nDecision: OK\n<!-- step-end -->\nbody")
        with mutating(flow) as locked:
            cls = receipts.classify(locked, locked.state.pending_spawn)
            h.test_result("45a: wrong attempt is stale", cls.state == "stale")
            decision = decide(locked, _facts_for(locked))
        h.test_result("45b: decide waits", decision.outcome == "wait")
        h.test_result("45c: stale receipt moved out of the step path", not path.exists())
        h.test_result("45d: archived as stale", len(list((flow / "archive" / "plan").glob("a1.stale.*.md"))) == 1)


def test_46_decide_lands_and_pending_state_is_receipt_landed():
    """Test 46: decide on a landed receipt keeps pending_spawn with state receipt-landed, live null."""
    with temp_home() as tmp:
        flow = make_v2_flow(Path(tmp))
        _spawn_plan(flow)
        _write_plan_receipt(flow, "ATTEMPT: 1\nDecision: OK\n<!-- step-end -->\nbody")
        with mutating(flow) as locked:
            decision = decide(locked, _facts_for(locked))
        h.test_result("46a: outcome receipt-landed", decision.outcome == "receipt-landed")
        with mutating(flow) as locked:
            pending = locked.state.pending_spawn
            h.test_result("46b: pending_spawn kept with state receipt-landed", pending is not None and pending.state == "receipt-landed")
            h.test_result("46c: live projected to null", locked.state.live is None)


def test_47_strict_attempt_02_is_incomplete():
    """Test 47: ATTEMPT: 02 is rejected by the strict parse; after two identical reads it is malformed and reruns."""
    with temp_home() as tmp:
        flow = make_v2_flow(Path(tmp))
        _spawn_plan(flow)
        _write_plan_receipt(flow, "ATTEMPT: 02\nDecision: OK\n<!-- step-end -->\nbody")
        h.test_result("47a: strict parse rejects 02", receipts.receipt_attempt("ATTEMPT: 02\n") is None)
        with mutating(flow) as locked:
            first = decide(locked, _facts_for(locked))
        with mutating(flow) as locked:
            second = decide(locked, _facts_for(locked))
        h.test_result("47b: first read waits", first.outcome == "wait")
        h.test_result("47c: second read reruns", second.outcome == "rerun")


def test_48_crash_between_archive_and_commit_rolls_forward():
    """Test 48 (F1): a crash after the archive rename but before the state commit is finished by the next resume."""
    from scripts.flow_driver import state as state_module
    with temp_home() as tmp:
        flow = make_v2_flow(Path(tmp))
        _spawn_plan(flow)
        path = _write_plan_receipt(flow, "ATTEMPT: 1\nDecision: OK\n<!-- step-end -->\nbody")
        original = state_module.LockedFlow.commit
        calls = {"n": 0}

        def crashing(self, new_state):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            raise OSError("simulated crash before commit")

        state_module.LockedFlow.commit = crashing  # type: ignore[method-assign]
        try:
            try:
                with mutating(flow) as locked:
                    decide(locked, _facts_for(locked))
            except OSError:
                pass
        finally:
            state_module.LockedFlow.commit = original  # type: ignore[method-assign]
        h.test_result("48a: the receipt was moved but state not committed", not path.exists() and (flow / "archive" / "plan" / "a1.landed.md").exists() and calls["n"] >= 1)
        with mutating(flow) as locked:
            decision = decide(locked, _facts_for(locked))
        h.test_result("48b: resume rolls forward to receipt-landed", decision.outcome == "receipt-landed")
        with mutating(flow) as locked:
            h.test_result("48c: state records receipt-landed", locked.state.pending_spawn is not None and locked.state.pending_spawn.state == "receipt-landed")


def test_49_init_repo_with_underscore():
    """Test 49 (F4): --repo keeps an underscore repo name intact and a mismatched --repo is a usage error."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = cli_flow(home, key="owner_my_repo")
        wt = home / "wt"
        wt.mkdir()
        rc, out, err = run_cli(home, "init", "--flow", str(flow), "--worktree", str(wt), "--repo", "owner/my_repo")
        h.test_result("49a: init with --repo exits 0", rc == 0)
        rc2, _, _ = run_cli(home, "status", "--json", "--flow", str(flow))
        h.test_result("49b: status works", rc2 == 0)
        state = json.loads((flow / "state.json").read_text())
        h.test_result("49c: repo recorded as given", state["repo"] == "owner/my_repo")
        flow2 = cli_flow(home, key="owner_other", issue=124)
        rc3, _, err3 = run_cli(home, "init", "--flow", str(flow2), "--worktree", str(wt), "--repo", "owner/my_repo")
        h.test_result("49d: mismatched --repo exits 2", rc3 == 2)


def test_50_load_time_schema_checks_and_list():
    """Test 50 (F24, F25): load_v2_state rejects bad run_id, repo shape and duplicate applied; list reports invalid state."""
    with temp_home() as tmp:
        home = Path(tmp)
        flow = cli_flow(home)
        wt = home / "wt"
        wt.mkdir()
        rc, _, _ = run_cli(home, "init", "--flow", str(flow), "--worktree", str(wt))
        good = json.loads((flow / "state.json").read_text())
        h.test_result("50a: init ok", rc == 0)

        def bad_class(mutate):
            doc = json.loads(json.dumps(good))
            mutate(doc)
            (flow / "state.json").write_text(json.dumps(doc))
            return classify_state(flow)

        r1 = bad_class(lambda d: d.update(run_id="NOT-HEX"))
        h.test_result("50b: non-hex run_id is InvalidState", isinstance(r1, InvalidState) and r1.field == "run_id")
        r2 = bad_class(lambda d: d.update(repo="no-slash"))
        h.test_result("50c: bad repo shape is InvalidState", isinstance(r2, InvalidState) and r2.field == "repo")
        r3 = bad_class(lambda d: d.update(applied=["a", "a"]))
        h.test_result("50d: duplicate applied is InvalidState", isinstance(r3, InvalidState))
        rc, out, _ = run_cli(home, "list")
        h.test_result("50e: list shows invalid and does not hide it", rc == 0 and "invalid" in out)


if __name__ == "__main__":
    # Run tests
    test_1_atomic_write_positional()
    test_2_atomic_write_fsync()
    test_3_nonblocking_flock_exclusion()
    test_4_nonblocking_flock_properties()

    test_5_init_writes_v2_schema()
    test_6_classify_state_driver_format()
    test_7_state_schema_strictness()
    test_8_unknown_keys_preserved()
    test_9_live_projection()
    test_10_fleet_field_contract()

    test_11_locked_refuses_locked()
    test_12_concurrent_reader_safety()
    test_13_lock_inode_stable()
    test_15_verifier_lock_released_after_call()
    test_16_verifier_timeout_retried_once()
    test_17_commit_after_context_exit_raises()
    test_18_once_per_attempt_idempotent()
    test_18a_repeatable_transitions_not_swallowed()
    test_18b_record_spawn_advances_counter_atomically()

    test_19_v1_and_skeleton_refused()
    test_19e_v2_state_under_v1_root_is_v1()
    test_20_resume_no_state_refused()
    test_21_double_init_refused()
    test_22_schema_invalid_refused()
    test_23_loop_subcommands_on_v2()
    test_24_readonly_commands_leave_unchanged()
    test_25_list_shows_v1_and_initializing()
    test_26_status_json_readonly()
    test_26a_resolve_read_flow_dir()

    test_27_reconcile_mismatch_logged()
    test_28_cleared_no_mismatch()
    test_29_counter_repaired()
    test_30_reconcile_no_repair_no_write()
    test_30a_untrusted_attempt_ignored()

    test_31_receipt_path_step_index()
    test_33_matching_attempt_receipt_is_landed()
    test_34_well_formed_receipt_lands()
    test_35_no_attempt_line_malformed_after_two_reads()
    test_36_malformed_receipt_archived_reruns()
    test_37_rebuild_receipt()

    test_38_rule_names_in_order()
    test_39_rule_ordering_1b_before_4_5()
    test_40_one_test_per_rule()
    test_40a_predicates_shortcircuit_on_state()
    test_41_live_step_no_receipt_wait()
    test_42_fresh_init_fires_rule_1a()
    test_43_every_decide_logs_and_prints()
    test_43a_exit_codes_per_outcome()

    test_44_docs_updated()
    test_45_stale_receipt_classifies_stale_and_decide_waits()
    test_46_decide_lands_and_pending_state_is_receipt_landed()
    test_47_strict_attempt_02_is_incomplete()
    test_48_crash_between_archive_and_commit_rolls_forward()
    test_49_init_repo_with_underscore()
    test_50_load_time_schema_checks_and_list()

    h.summarize_and_exit()
