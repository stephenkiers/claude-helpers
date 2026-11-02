#!/usr/bin/env python3
"""
Tests for correction-of-a-correction chain handling in scripts/flow-ledger.py (#288 Item 3).

Tests that binding must survive a chain of corrections, where a correction supersedes another correction.

Run with: python3 tests/test_flow_ledger_correction_chain.py
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


def list_json(flow):
    """Run `list --json` and parse the output."""
    rc, out, err = run("list", flow, "--json")
    if rc != 0:
        return None
    return json.loads(out)


def main():
    h = Harness("FLOW-LEDGER CORRECTION-OF-CORRECTION CHAIN TESTS (#288 Item 3)")

    with tempfile.TemporaryDirectory() as tmp:
        print("[Test 1] Correction chain: direction → correction → correction")

        flow = flow_dir(tmp, "chain_test1")

        # Step 1: Append an initial direction
        rc, out, err = append(flow, kind="direction", text="initial direction", step="plan")
        h.test_result("append direction succeeds", rc == 0, f"stderr: {err}")

        records = list_json(flow)
        h.test_result("initial record exists", len(records) == 1, f"records: {records}")
        initial_seq = records[0]["seq"]
        initial_kind = records[0]["kind"]

        h.test_result("initial record is direction", initial_kind == "direction",
                      f"kind: {initial_kind}")

        # Step 2: Append first correction (supersedes seq 1)
        rc, out, err = append(flow, kind="correction", text="first correction", step="plan",
                             extra=("--supersedes", str(initial_seq)))
        h.test_result("append first correction succeeds", rc == 0, f"stderr: {err}")

        records = list_json(flow)
        h.test_result("after first correction, have 2 records", len(records) == 2, f"records count: {len(records)}")

        # Find the correction record
        correction1 = [r for r in records if r["kind"] == "correction" and r["supersedes"] == initial_seq]
        h.test_result("first correction record exists", len(correction1) == 1, f"correction1: {correction1}")
        correction1_seq = correction1[0]["seq"]

        # Verify the original direction is now superseded
        original = [r for r in records if r["seq"] == initial_seq]
        h.test_result("original record is marked superseded",
                      original[0]["status"] == "superseded",
                      f"original status: {original[0]['status']}")

        # Step 3: Append second correction (supersedes seq 2, the first correction)
        rc, out, err = append(flow, kind="correction", text="second correction", step="plan",
                             extra=("--supersedes", str(correction1_seq)))
        h.test_result("append second correction succeeds", rc == 0, f"stderr: {err}")

        records = list_json(flow)
        h.test_result("after second correction, have 3 records", len(records) == 3,
                      f"records count: {len(records)}")

        # Verify the chain is intact
        correction2 = [r for r in records if r["supersedes"] == correction1_seq]
        h.test_result("second correction references first correction", len(correction2) == 1,
                      f"correction2: {correction2}")

        # Verify all statuses are correct
        first_correction = [r for r in records if r["seq"] == correction1_seq][0]
        h.test_result("first correction is superseded", first_correction["status"] == "superseded",
                      f"status: {first_correction['status']}")

        original_still_superseded = [r for r in records if r["seq"] == initial_seq][0]
        h.test_result("original is still superseded after second correction",
                      original_still_superseded["status"] == "superseded",
                      f"status: {original_still_superseded['status']}")

        print()
        print("[Test 2] Binding survives correction chain")

        # Verify that a correction chain doesn't break the binding logic
        # All records in the chain should be traceable to understand the dependency

        second_correction = [r for r in records if r["supersedes"] == correction1_seq][0]

        # The latest correction should still be pending
        h.test_result("latest correction is still pending",
                      second_correction["status"] == "pending",
                      f"status: {second_correction['status']}")

        # Verify seq numbers are sequential
        seqs = sorted([r["seq"] for r in records])
        h.test_result("seq numbers are sequential", seqs == [1, 2, 3], f"seqs: {seqs}")

        print()
        print("[Test 3] Three-level correction chain")

        flow2 = flow_dir(tmp, "chain_test2")

        # Create a three-level chain: dir → corr → corr → corr
        rc, out, err = append(flow2, kind="direction", text="level 0", step="plan")
        h.test_result("append level 0 (direction)", rc == 0, f"stderr: {err}")

        records = list_json(flow2)
        current_seq = records[0]["seq"]

        # Create 3 corrections in a chain
        for i in range(1, 4):
            rc, out, err = append(flow2, kind="correction", text=f"level {i} correction",
                                 step="plan", extra=("--supersedes", str(current_seq)))
            h.test_result(f"append level {i} correction", rc == 0, f"stderr: {err}")

            records = list_json(flow2)
            current_record = [r for r in records if r["supersedes"] == current_seq]
            h.test_result(f"level {i} correction exists", len(current_record) == 1,
                         f"records: {current_record}")
            current_seq = current_record[0]["seq"]

        # Final check: should have 4 records (1 direction + 3 corrections)
        records = list_json(flow2)
        h.test_result("four-level chain complete", len(records) == 4, f"records count: {len(records)}")

        # Only the latest should be pending
        pending = [r for r in records if r["status"] == "pending"]
        h.test_result("only latest correction is pending", len(pending) == 1,
                      f"pending count: {len(pending)}")

        print()
        h.summarize_and_exit()


if __name__ == "__main__":
    main()
