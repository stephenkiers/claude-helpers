#!/usr/bin/env python3
"""
Tests for parked-record handling in flow-verify-receipt.py (#288): producers exist today
(`flow-ledger.py mark --status parked` in the Redo / Take over / Abort paths), so deferred
direction records must reach the assess relay, and closed ones must not be re-asked.

Run with: python3 tests/test_flow_parked_outstanding.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

LEDGER = REPO_ROOT / "scripts" / "flow-ledger.py"
VERIFY = REPO_ROOT / "scripts" / "flow-verify-receipt.py"


def sh(script, *args):
    r = subprocess.run([sys.executable, str(script), *args], capture_output=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")


def make_flow(root):
    flow = Path(root) / "flow"
    (flow / "steps").mkdir(parents=True)
    (flow / "state.json").write_text(json.dumps({"ledger_format": 1}) + "\n")
    (flow / "log.md").write_text("")
    return flow


def append(flow, kind, text):
    return sh(LEDGER, "append", "--flow-dir", str(flow), "--kind", kind, "--step", "plan",
              "--attempt", "1", "--text", text)


def mark(flow, seq, status, by=None):
    args = ["mark", "--flow-dir", str(flow), "--seq", str(seq), "--status", status]
    if by:
        args += ["--by", by]
    return sh(LEDGER, *args)


def outstanding_ids(flow):
    rc, out, err = sh(VERIFY, "--outstanding-set", "--flow-dir", str(flow))
    ids = [ln.split(" | ")[0] for ln in out.splitlines() if ln.startswith("ledger:")]
    return rc, ids, out + err


def close_lines(flow):
    rc, out, err = sh(VERIFY, "--ledger-close", "--flow-dir", str(flow))
    return rc, [ln for ln in out.splitlines() if ln.startswith("CLOSE:")], out + err


def main():
    h = Harness("PARKED RECORDS REACH THE ASSESS RELAY (#288)")
    with tempfile.TemporaryDirectory() as tmp:
        flow = make_flow(tmp)

        append(flow, "direction", "deferred redo note")      # seq 1: marked parked
        append(flow, "direction", "closed note")             # seq 2: addressed
        append(flow, "parked", "pending parked kind")        # seq 3: pending kind=parked
        append(flow, "parked", "closed parked kind")         # seq 4: addressed
        mark(flow, 1, "parked")
        mark(flow, 2, "addressed", by="steps/01-plan.md")
        mark(flow, 4, "addressed", by="steps/01-plan.md")

        rc, ids, text = outstanding_ids(flow)
        h.test_result("outstanding-set exits 0", rc == 0, text)
        h.test_result("direction marked parked is outstanding", "ledger:parked-1" in ids, text)
        h.test_result("pending parked-kind record is outstanding", "ledger:parked-3" in ids, text)
        h.test_result("addressed direction is not outstanding", "ledger:parked-2" not in ids, text)
        h.test_result("closed parked-kind record is not re-asked", "ledger:parked-4" not in ids, text)

        rc, closes, text = close_lines(flow)
        h.test_result("ledger-close prints CLOSE: 3 parked for the pending parked record",
                      "CLOSE: 3 parked" in closes, text)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
