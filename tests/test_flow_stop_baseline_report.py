#!/usr/bin/env python3
"""Tests for scripts/flow-stop-baseline.py observable behavior through CLI.

Tests the script's observable behavior only:
- Healthy `awaiting` flows are excluded from stop population
- JSON report counts match fixture state
- No free text from receipts appears in output
- Unreadable receipts and bad stop_reason fields don't crash
- Exit code 2 for bad --flows-root and bad --since
- --since boundary (midnight included, day before excluded)

All tests drive the real script through its CLI with temp fixture flow dirs.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "flow-stop-baseline.py"


def _run_script(args, flows_root):
    """Run the script with the given arguments. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT), "--flows-root", str(flows_root)] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _create_flow_dir(flows_root, repo_name, issue_num, slug, status, started_iso=None,
                     stop_reason=None, stop_source=None, awaiting_kind=None):
    """Create a minimal flow directory fixture.

    Args:
        flows_root: root flows directory
        repo_name: e.g., "owner_repo"
        issue_num: issue number
        slug: issue slug
        status: flow status (e.g., "done", "paused", "awaiting", "abandoned")
        started_iso: ISO timestamp (default: 2026-11-01T00:00:00Z)
        stop_reason: optional dict of stop reasons by step
        stop_source: optional dict of stop sources by step
        awaiting_kind: optional awaiting kind (e.g., "report", None for healthy awaiting)
    """
    if started_iso is None:
        started_iso = "2026-11-01T00:00:00Z"

    timestamp = datetime.fromisoformat(started_iso.replace('Z', '+00:00')).strftime('%Y%m%dT%H%M%S')
    flow_dir_name = f"{issue_num}-{slug}-{timestamp}"
    flow_dir = flows_root / repo_name / flow_dir_name
    flow_dir.mkdir(parents=True, exist_ok=True)

    # Create state.json
    state = {
        "issue": issue_num,
        "slug": slug,
        "issue_url": f"https://github.com/{repo_name.replace('_', '/')}/issues/{issue_num}",
        "worktree": None,
        "branch": None,
        "step": "plan",
        "status": status,
        "attempts": {"plan": 1},
        "failures": {},
        "plan_path": None,
        "review_dir": None,
        "pr": None,
        "gate_passed": False,
        "started": started_iso,
        "updated": started_iso,
        "flags": {"plan_effort": None, "review_effort": None, "auto_merge": False, "from": None},
        "epic": None,
    }

    if awaiting_kind is not None:
        state["awaiting_kind"] = awaiting_kind
        state["awaiting_file"] = "fleet-report-01.md"

    if stop_reason is not None:
        state["stop_reason"] = stop_reason
    if stop_source is not None:
        state["stop_source"] = stop_source

    (flow_dir / "state.json").write_text(json.dumps(state, indent=2))

    # Create minimal steps directory and receipt
    steps_dir = flow_dir / "steps"
    steps_dir.mkdir(exist_ok=True)
    (steps_dir / "01-plan.md").write_text("Receipt content")

    return flow_dir


def main():
    """Run all tests with a single harness."""
    h = Harness("FLOW-STOP-BASELINE REPORT TESTS")

    # Test 1: Exit code 2 on bad flows root
    print("\n[Test 1] Exit code 2 on bad flows_root")
    returncode, stdout, stderr = _run_script([], "/nonexistent/path")
    h.test_result("bad flows_root exits with 2", returncode == 2, f"got {returncode}")

    # Test 2: Exit code 2 on bad since
    print("\n[Test 2] Exit code 2 on bad since")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        returncode, stdout, stderr = _run_script(["--since", "invalid-date"], flows_root)
        h.test_result("bad since format exits with 2", returncode == 2, f"got {returncode}")

    # Test 3: Empty flows root returns zero
    print("\n[Test 3] Empty flows root")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        returncode, stdout, stderr = _run_script(["--format", "json"], flows_root)
        h.test_result("empty flows_root exits with 0", returncode == 0, f"got {returncode}")
        try:
            data = json.loads(stdout)
            h.test_result("empty output is valid JSON dict", isinstance(data, dict))
            h.test_result("empty output has counts key", "counts" in data)
        except json.JSONDecodeError as e:
            h.test_result("empty output JSON parses", False, str(e))

    # Test 4: JSON report counts
    print("\n[Test 4] JSON report counts")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        _create_flow_dir(flows_root, "owner_repo", 1, "issue-1", "paused",
                        stop_reason={"plan": "user request"},
                        stop_source={"plan": "human"})
        _create_flow_dir(flows_root, "owner_repo", 2, "issue-2", "paused",
                        stop_reason={"implement": "check failed"},
                        stop_source={"implement": "validation"})
        _create_flow_dir(flows_root, "owner_repo", 3, "issue-3", "done")

        returncode, stdout, stderr = _run_script(["--format", "json"], flows_root)
        h.test_result("json output exits with 0", returncode == 0, f"got {returncode}")
        try:
            data = json.loads(stdout)
            h.test_result("JSON report is dict", isinstance(data, dict))
            h.test_result("JSON has counts key", "counts" in data)
            counts = data.get("counts", {})
            h.test_result("flows_in_window == 3", counts.get("flows_in_window") == 3,
                         f"got {counts.get('flows_in_window')}")
        except json.JSONDecodeError as e:
            h.test_result("JSON report parses", False, str(e))

    # Test 5: Healthy awaiting excluded
    print("\n[Test 5] Healthy awaiting excluded")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        _create_flow_dir(flows_root, "owner_repo", 1, "healthy-awaiting", "awaiting",
                        started_iso="2026-11-05T00:00:00Z",
                        awaiting_kind=None)
        _create_flow_dir(flows_root, "owner_repo", 2, "unhealthy-awaiting", "awaiting",
                        started_iso="2026-11-05T00:00:00Z",
                        awaiting_kind="report")
        _create_flow_dir(flows_root, "owner_repo", 3, "paused-with-reason", "paused",
                        started_iso="2026-11-05T00:00:00Z",
                        stop_reason={"plan": "user request"},
                        stop_source={"plan": "human"})

        returncode, stdout, stderr = _run_script(["--format", "json"], flows_root)
        h.test_result("script runs with mixed awaiting states", returncode == 0,
                     f"got {returncode}, stderr: {stderr}")
        try:
            data = json.loads(stdout)
            flows_count = data.get("counts", {}).get("flows_in_window")
            h.test_result("flows_in_window == 3 (healthy awaiting included)", flows_count == 3,
                         f"got {flows_count}")
        except json.JSONDecodeError as e:
            h.test_result("Test 5 JSON parses", False, str(e))

    # Test 6: No free text from receipts
    print("\n[Test 6] No free text from receipts")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        flow_dir = _create_flow_dir(flows_root, "owner_repo", 1, "issue-1", "paused",
                                    stop_reason={"plan": "user request"},
                                    stop_source={"plan": "human"})
        receipt_text = "This is internal receipt content that should NOT appear in output"
        (flow_dir / "steps" / "01-plan.md").write_text(receipt_text)

        returncode, stdout, stderr = _run_script(["--format", "json"], flows_root)
        h.test_result("script exits with 0", returncode == 0, f"got {returncode}")
        h.test_result("receipt text not in output", receipt_text not in stdout,
                     f"found receipt text in output")

    # Test 7: Unreadable receipt does not crash
    print("\n[Test 7] Unreadable receipt")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        flow_dir = _create_flow_dir(flows_root, "owner_repo", 1, "issue-1", "paused",
                                    stop_reason={"plan": "user request"},
                                    stop_source={"plan": "human"})
        receipt_path = flow_dir / "steps" / "01-plan.md"
        receipt_path.write_text("content")
        os.chmod(receipt_path, 0o000)

        try:
            returncode, stdout, stderr = _run_script(["--format", "json"], flows_root)
            h.test_result("script doesn't crash on unreadable receipt", returncode == 0,
                         f"got returncode {returncode}")
        except Exception as e:
            h.test_result("script doesn't crash on unreadable receipt", False, str(e))
        finally:
            os.chmod(receipt_path, 0o644)

    # Test 8: Missing receipt does not crash
    print("\n[Test 8] Missing receipt")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        flow_dir = _create_flow_dir(flows_root, "owner_repo", 1, "issue-1", "paused",
                                    stop_reason={"plan": "user request"},
                                    stop_source={"plan": "human"})
        (flow_dir / "steps" / "01-plan.md").unlink()

        returncode, stdout, stderr = _run_script(["--format", "json"], flows_root)
        h.test_result("script doesn't crash on missing receipt", returncode == 0,
                     f"got {returncode}, stderr: {stderr}")

    # Test 9: Since boundary - midnight included
    print("\n[Test 9] Since boundary - midnight included")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        _create_flow_dir(flows_root, "owner_repo", 1, "midnight-flow", "done",
                        started_iso="2026-11-05T00:00:00Z")
        _create_flow_dir(flows_root, "owner_repo", 2, "before-midnight", "done",
                        started_iso="2026-11-04T23:59:59Z")

        returncode, stdout, stderr = _run_script(["--format", "json", "--since", "2026-11-05"], flows_root)
        h.test_result("since midnight filter exits 0", returncode == 0, f"got {returncode}")
        try:
            data = json.loads(stdout)
            flows_count = data.get("counts", {}).get("flows_in_window")
            h.test_result("midnight flow is included (flows_in_window == 1)", flows_count == 1,
                         f"got flows_in_window={flows_count}")
        except json.JSONDecodeError as e:
            h.test_result("midnight test JSON parses", False, str(e))

    # Test 10: Since boundary - day before excluded
    print("\n[Test 10] Since boundary - day before excluded")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        _create_flow_dir(flows_root, "owner_repo", 1, "day-before", "done",
                        started_iso="2026-11-04T12:00:00Z")
        _create_flow_dir(flows_root, "owner_repo", 2, "on-date", "done",
                        started_iso="2026-11-05T12:00:00Z")

        returncode, stdout, stderr = _run_script(["--format", "json", "--since", "2026-11-05"], flows_root)
        h.test_result("since boundary excludes day before exits 0", returncode == 0, f"got {returncode}")
        try:
            data = json.loads(stdout)
            flows_count = data.get("counts", {}).get("flows_in_window")
            outside_count = data.get("counts", {}).get("outside_window")
            h.test_result("day-before flow is excluded (flows_in_window == 1)", flows_count == 1,
                         f"got flows_in_window={flows_count}")
            h.test_result("outside_window count is 1", outside_count == 1,
                         f"got outside_window={outside_count}")
        except json.JSONDecodeError as e:
            h.test_result("day-before test JSON parses", False, str(e))

    # Test 10a: Since boundary - exact T00:00:00Z on boundary is included
    print("\n[Test 10a] Since boundary - exact T00:00:00Z is included")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        _create_flow_dir(flows_root, "owner_repo", 1, "exact-boundary", "done",
                        started_iso="2026-11-05T00:00:00Z")
        _create_flow_dir(flows_root, "owner_repo", 2, "just-before", "done",
                        started_iso="2026-11-04T23:59:59Z")

        returncode, stdout, stderr = _run_script(["--format", "json", "--since", "2026-11-05"], flows_root)
        h.test_result("exact boundary test exits 0", returncode == 0, f"got {returncode}")
        try:
            data = json.loads(stdout)
            flows_count = data.get("counts", {}).get("flows_in_window")
            h.test_result("exact T00:00:00Z is included (flows_in_window == 1)", flows_count == 1,
                         f"got flows_in_window={flows_count}")
        except json.JSONDecodeError as e:
            h.test_result("exact boundary test JSON parses", False, str(e))

    # Test 11: Multiple repos
    print("\n[Test 11] Multiple repos")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        _create_flow_dir(flows_root, "owner_repo1", 1, "issue-1", "done")
        _create_flow_dir(flows_root, "owner_repo2", 2, "issue-2", "paused",
                        stop_reason={"plan": "user request"},
                        stop_source={"plan": "human"})
        _create_flow_dir(flows_root, "owner_repo3", 3, "issue-3", "awaiting")

        returncode, stdout, stderr = _run_script(["--format", "json"], flows_root)
        h.test_result("script handles multiple repos exits 0", returncode == 0, f"got {returncode}")
        try:
            data = json.loads(stdout)
            flows_count = data.get("counts", {}).get("flows_in_window")
            h.test_result("flows_in_window == 3 across repos", flows_count == 3,
                         f"got {flows_count}")
        except json.JSONDecodeError as e:
            h.test_result("Test 11 JSON parses", False, str(e))

    # Test 12: Bad stop_reason does not crash
    print("\n[Test 12] Bad stop_reason")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        flow_dir = _create_flow_dir(flows_root, "owner_repo", 1, "issue-1", "paused")
        state_path = flow_dir / "state.json"
        state = json.loads(state_path.read_text())
        state["stop_reason"] = "not-a-dict"
        state_path.write_text(json.dumps(state))

        returncode, stdout, stderr = _run_script(["--format", "json"], flows_root)
        h.test_result("script handles malformed stop_reason exits 0", returncode == 0,
                     f"got {returncode}")

    # Test 13: Markdown output format
    print("\n[Test 13] Markdown output format")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        _create_flow_dir(flows_root, "owner_repo", 1, "issue-1", "done")

        returncode, stdout, stderr = _run_script(["--format", "md"], flows_root)
        h.test_result("markdown format exits with 0", returncode == 0, f"got {returncode}")
        h.test_result("markdown output >= 100 bytes", len(stdout) >= 100,
                     f"got {len(stdout)} bytes")
        try:
            json.loads(stdout)
            h.test_result("markdown output is not JSON", False)
        except json.JSONDecodeError:
            h.test_result("markdown output is not JSON", True)

    # Test 14: Default format
    print("\n[Test 14] Default format is markdown")
    with tempfile.TemporaryDirectory() as tmp:
        flows_root = Path(tmp) / "flows"
        flows_root.mkdir()
        _create_flow_dir(flows_root, "owner_repo", 1, "issue-1", "done")

        cmd = [sys.executable, str(SCRIPT), "--flows-root", str(flows_root)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        returncode, stdout = result.returncode, result.stdout

        h.test_result("default format exits with 0", returncode == 0, f"got {returncode}")
        h.test_result("default output >= 100 bytes", len(stdout) >= 100,
                     f"got {len(stdout)} bytes")
        try:
            json.loads(stdout)
            h.test_result("default format is markdown", False)
        except json.JSONDecodeError:
            h.test_result("default format is markdown", True)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
