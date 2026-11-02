#!/usr/bin/env python3
"""
Tests for --ledger-close functionality in scripts/flow-ledger.py (#288 Item 1).

Tests the round-trip behavior where the ship close-out passes the relative receipt path
from the CLOSE: line as --by to flow-ledger.py mark; an absolute --by is rejected.

Run with: python3 tests/test_flow_ledger_close.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

LEDGER = REPO_ROOT / "scripts" / "flow-ledger.py"


def flow_dir(root, name):
    """Create a flow dir with a stub state.json and return its path."""
    flow = Path(root) / name
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "state.json").write_text("{}\n")
    return flow


def run(sub, flow, *args, stdin=None):
    """Run `flow-ledger.py <sub> --flow-dir <flow> ...`. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(LEDGER), sub, "--flow-dir", str(flow), *args]
    kwargs = {"capture_output": True, "timeout": 60}
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    result = subprocess.run(cmd, **kwargs)
    return result.returncode, result.stdout.decode("utf-8"), result.stderr.decode("utf-8")


def append(flow, kind="answer", text="a response", step="plan", attempt=1, extra=(), stdin=None):
    """Run `append` with --text (or --text-stdin when stdin is given)."""
    args = ["--kind", kind, "--step", step, "--attempt", str(attempt), *extra]
    if stdin is None:
        args += ["--text", text]
        return run("append", flow, *args)
    args += ["--text-stdin"]
    return run("append", flow, *args, stdin=stdin)


def mark(flow, seq, status, by=None):
    """Run `mark` for one record."""
    args = ["--seq", str(seq), "--status", status]
    if by is not None:
        args += ["--by", by]
    return run("mark", flow, *args)


def list_json(flow):
    """Run `list --json` and parse the output."""
    rc, out, err = run("list", flow, "--json")
    if rc != 0:
        return None
    return json.loads(out)


def main():
    h = Harness("FLOW-LEDGER --ledger-close ROUND-TRIP TESTS (#288 Item 1)")

    with tempfile.TemporaryDirectory() as tmp:
        print("[Test 1] --ledger-close: relative path round-trip (append → mark)")

        flow = flow_dir(tmp, "test1")

        # Append a question
        rc, out, err = append(flow, kind="question", text="a question", step="plan")
        h.test_result("append question succeeds", rc == 0, f"stderr: {err}")

        # Verify the record was created
        records = list_json(flow)
        h.test_result("question record exists", len(records) > 0 and records[0]["kind"] == "question",
                      f"records: {records}")

        seq = records[0]["seq"]

        # Mark with relative receipt path (from current step) - simulating the CLOSE: line format
        receipt_path = "steps/02-implement.md"
        rc, out, err = mark(flow, seq, "addressed", by=receipt_path)
        h.test_result("mark with relative receipt path succeeds", rc == 0, f"stderr: {err}")

        # Verify the record was marked as addressed with the relative path
        records = list_json(flow)
        marked = [r for r in records if r["seq"] == seq]
        h.test_result("record marked as addressed", len(marked) > 0 and marked[0]["status"] == "addressed",
                      f"marked: {marked}")
        h.test_result("record shows addressed_by with relative path",
                      marked[0]["addressed_by"] == receipt_path,
                      f"addressed_by: {marked[0].get('addressed_by')}")

        print()
        print("[Test 2] --ledger-close: absolute path is rejected")

        flow2 = flow_dir(tmp, "test2")

        # Append a question
        rc, out, err = append(flow2, kind="question", text="another question", step="plan")
        h.test_result("append succeeds", rc == 0, f"stderr: {err}")

        records = list_json(flow2)
        seq = records[0]["seq"]

        # Try to mark with absolute path - should fail
        absolute_path = f"{flow2}/steps/02-implement.md"
        rc, out, err = mark(flow2, seq, "addressed", by=absolute_path)
        h.test_result("mark with absolute path is rejected", rc != 0,
                      f"rc: {rc}, stderr: {err}")

        print()
        print("[Test 3] --ledger-close: relative path with nested directories")

        flow3 = flow_dir(tmp, "test3")

        # Append a question
        rc, out, err = append(flow3, kind="question", text="nested test", step="review")
        h.test_result("append succeeds", rc == 0, f"stderr: {err}")

        records = list_json(flow3)
        seq = records[0]["seq"]

        # Mark with nested relative path
        nested_path = "steps/subdir/nested-result.md"
        rc, out, err = mark(flow3, seq, "addressed", by=nested_path)
        h.test_result("mark with nested relative path succeeds", rc == 0, f"stderr: {err}")

        records = list_json(flow3)
        marked = [r for r in records if r["seq"] == seq]
        h.test_result("record shows addressed_by with nested path",
                      marked[0]["addressed_by"] == nested_path,
                      f"addressed_by: {marked[0].get('addressed_by')}")

        print()
        h.summarize_and_exit()


if __name__ == "__main__":
    main()
