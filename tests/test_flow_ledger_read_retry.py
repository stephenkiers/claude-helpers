#!/usr/bin/env python3
"""
Tests for read_records retry behavior in scripts/flow-ledger.py (#288 Item 15).

Tests that lock-free read_records can retry on transient failures and that error messages
mention the failure may be transient.

Run with: python3 tests/test_flow_ledger_read_retry.py
"""

import json
import subprocess
import sys
import tempfile
import time
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


def list_json(flow):
    """Run `list --json` and parse the output."""
    rc, out, err = run("list", flow, "--json")
    return rc, out, err


def main():
    h = Harness("FLOW-LEDGER READ_RECORDS RETRY TESTS (#288 Item 15)")

    with tempfile.TemporaryDirectory() as tmp:
        print("[Test 1] read_records succeeds when ledger is empty")

        flow = flow_dir(tmp, "empty_ledger")
        rc, out, err = list_json(flow)
        h.test_result("list succeeds on empty ledger", rc == 0, f"stderr: {err}")
        h.test_result("empty ledger returns empty JSON array", out.strip() == "[]",
                      f"output: {out}")

        print()
        print("[Test 2] read_records succeeds when ledger exists and is valid")

        flow2 = flow_dir(tmp, "valid_ledger")
        # Create a valid ledger file
        ledger_path = flow2 / "human.jsonl"
        record = {
            "seq": 1,
            "ts": "2026-10-01T00:00:00Z",
            "step": "plan",
            "attempt": 1,
            "kind": "answer",
            "text": "test",
            "transcript": None,
            "status": "pending",
            "addressed_by": None,
            "supersedes": None,
        }
        ledger_path.write_text(json.dumps(record) + "\n")

        rc, out, err = list_json(flow2)
        h.test_result("list succeeds on valid ledger", rc == 0, f"stderr: {err}")
        records = json.loads(out)
        h.test_result("valid ledger record is returned", len(records) == 1 and records[0]["seq"] == 1,
                      f"output: {out}")

        print()
        print("[Test 3] read_records handles corrupted ledger")

        flow3 = flow_dir(tmp, "corrupt_ledger")
        # Create a corrupted ledger file (invalid JSON)
        ledger_path = flow3 / "human.jsonl"
        ledger_path.write_text('{"invalid json\n')

        rc, out, err = list_json(flow3)
        h.test_result("list fails on corrupted ledger", rc != 0,
                      f"rc: {rc}, stderr: {err}")
        h.test_result("error message mentions transient (may retry)",
                      "transient" in err.lower() or "may be" in err.lower() or "retry" in err.lower(),
                      f"stderr: {err}")

        print()
        print("[Test 4] read_records handles missing ledger file")

        flow4 = flow_dir(tmp, "missing_ledger")
        # Don't create a ledger file - it's missing

        rc, out, err = list_json(flow4)
        h.test_result("list succeeds on missing ledger (returns empty)", rc == 0,
                      f"stderr: {err}")
        h.test_result("missing ledger returns empty JSON array", out.strip() == "[]",
                      f"output: {out}")

        print()
        print("[Test 5] Malformed JSON in ledger is detected")

        flow5 = flow_dir(tmp, "malformed_ledger")
        ledger_path = flow5 / "human.jsonl"
        # Write multiple lines where one is invalid
        valid_record = {
            "seq": 1,
            "ts": "2026-10-01T00:00:00Z",
            "step": "plan",
            "attempt": 1,
            "kind": "answer",
            "text": "valid",
            "transcript": None,
            "status": "pending",
            "addressed_by": None,
            "supersedes": None,
        }
        invalid_line = '{"invalid": json}\n'
        ledger_path.write_text(json.dumps(valid_record) + "\n" + invalid_line)

        rc, out, err = list_json(flow5)
        h.test_result("list fails on malformed JSON in ledger", rc != 0,
                      f"rc: {rc}, stderr: {err}")

        print()
        h.summarize_and_exit()


if __name__ == "__main__":
    main()
