#!/usr/bin/env python3
"""
Tests for scripts/workflow/check_diagnostics.py and its wiring into apply_cleanup:
full-output log files, bounded failure excerpts, and best-effort environment snapshots.

Run with: python3 tests/test_workflow_check_diagnostics.py
"""

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow import check_diagnostics as diag
from workflow.checks import CheckResult, execute_check
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("CHECK DIAGNOSTICS TEST SUITE")
    test_result = h.test_result
    tmp_root = Path(tempfile.mkdtemp(prefix="diag-test-"))

    try:
        # --- execute_check captures stdout ---
        r = execute_check("echo out-line; echo err-line >&2; exit 3", cwd=tmp_root)
        test_result("execute_check captures stdout", "out-line" in r.stdout, r.stdout)
        test_result("execute_check captures stderr and exit code", "err-line" in r.stderr and r.returncode == 3)

        # --- excerpt ---
        stdout = "running 3 tests\ntest a ... ok\ntest b ... FAILED\nfailures:\n    b\n" + "noise\n" * 100
        ex = diag.build_failure_excerpt(stdout, "thread 'b' panicked at src/lib.rs:1")
        test_result("excerpt includes FAILED line", "test b ... FAILED" in ex, ex)
        test_result("excerpt includes panic from stderr", "panicked" in ex, ex)
        test_result("excerpt omits non-matching noise", "noise" not in ex, ex)

        tail_src = "\n".join(f"line{i}" for i in range(200))
        ex = diag.build_failure_excerpt(tail_src, "")
        test_result("excerpt falls back to last 40 lines", "line199" in ex and "line160" in ex and "line159" not in ex, ex)

        ex = diag.build_failure_excerpt("error: " + "x" * 10000, "")
        test_result("excerpt is size bounded", len(ex) <= diag.EXCERPT_MAX_CHARS + 20, str(len(ex)))
        test_result("excerpt of empty output is empty", diag.build_failure_excerpt("", "") == "")

        # --- log writing ---
        log_dir = tmp_root / "logs"
        log_dir.mkdir()
        fail = CheckResult(success=False, returncode=1, stdout="STDOUT-BODY", stderr="STDERR-BODY")
        with mock.patch.object(diag, "environment_snapshot", return_value="SNAP") as snap:
            p = diag.write_check_log(log_dir, 0, "just check", fail, datetime.now(timezone.utc), 1.5, tmp_root)
            text = p.read_text()
            test_result("failed log has stdout, stderr, command, exit code",
                        all(s in text for s in ("STDOUT-BODY", "STDERR-BODY", "command: just check", "exit_code: 1", "duration_secs: 1.50")), text)
            test_result("failed log includes environment snapshot", "SNAP" in text and snap.called)

            ok = CheckResult(success=True, returncode=0, stdout="fine")
            snap.reset_mock()
            p2 = diag.write_check_log(log_dir, 1, "true", ok, datetime.now(timezone.utc), 0.1, tmp_root)
            test_result("passing check is logged without snapshot", "fine" in p2.read_text() and not snap.called)

        test_result("None log_dir yields None", diag.write_check_log(None, 0, "x", fail, datetime.now(timezone.utc), 0, None) is None)
        test_result("unwritable log_dir yields None, no raise",
                    diag.write_check_log(tmp_root / "missing", 0, "x", fail, datetime.now(timezone.utc), 0, None) is None)

        # --- snapshot never raises ---
        with mock.patch("subprocess.run", side_effect=OSError("boom")), \
                mock.patch("shutil.disk_usage", side_effect=OSError("nodisk")):
            s = diag.environment_snapshot(tmp_root)
        test_result("snapshot survives subprocess/disk errors", "unavailable" in s, s)
        s = diag.environment_snapshot(tmp_root)
        test_result("snapshot has expected sections",
                    all(k in s for k in ("main HEAD", "git status --porcelain", "cargo/rustc/just", "free disk")), s)

        # --- apply_cleanup wiring ---
        import json
        from workflow.cleanup import apply_cleanup, CleanupPlan

        wt_dir = tmp_root / "worktree"
        wt_dir.mkdir()

        def run_cleanup(check_result):
            plan = CleanupPlan(target_worktree=str(wt_dir), current_branch="feature", pr_state="MERGED",
                               expected_head_sha="abc123", cache_hash=None, check_commands=["just check"])
            with mock.patch("workflow.git.get_current_branch", return_value="feature"), \
                    mock.patch("workflow.git.get_head_sha", return_value="abc123"), \
                    mock.patch("workflow.git.pull_ff_only", return_value=(True, None)), \
                    mock.patch("workflow.git.delete_branch", return_value=(True, None)), \
                    mock.patch("workflow.git.remove_worktree", return_value=(True, None)), \
                    mock.patch("workflow.checks.execute_check", return_value=check_result), \
                    mock.patch.object(diag, "environment_snapshot", return_value="SNAP"):
                return apply_cleanup(json.dumps(plan.to_dict()))[0]

        res = run_cleanup(CheckResult(success=False, returncode=1,
                                      stdout="test rust_test::b ... FAILED\nfailures:\n    b\n", stderr=""))
        msg = res.validation_failures[0] if res.validation_failures else ""
        test_result("stdout-only failure is diagnosable from the message", "rust_test::b ... FAILED" in msg, msg)
        test_result("validation_passed is still False", res.validation_passed is False)
        log_path = msg.split("(full log: ")[-1].rstrip(")") if "(full log: " in msg else ""
        test_result("failure message carries a log path that exists",
                    bool(log_path) and Path(log_path).exists(), msg)
        if log_path:
            logged = Path(log_path).read_text()
            test_result("log has full stdout and snapshot", "failures:" in logged and "SNAP" in logged)
            shutil.rmtree(Path(log_path).parent, ignore_errors=True)

        res = run_cleanup(CheckResult(success=True, returncode=0, stdout="ok"))
        test_result("passing check still passes", res.validation_passed is True and not res.validation_failures)
    except SystemExit:
        raise
    except Exception as e:  # pragma: no cover - surfaces setup problems as a failure
        test_result("apply_cleanup wiring setup", False, repr(e))
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    h.summarize_and_exit()
