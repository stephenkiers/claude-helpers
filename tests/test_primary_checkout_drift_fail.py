#!/usr/bin/env python3
"""
Tests for `primary-checkout-drift.py fail`, the shared drift-barrier failure mechanism
(#336 F4/F20): stderr line, best-effort cwd_drift telemetry, optional FAILED receipt,
exit code. Also pins that commands/implement-with-haiku.md routes every barrier through it.

Run with: python3 tests/test_primary_checkout_drift_fail.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

DRIFT_SCRIPT = REPO_ROOT / "scripts" / "primary-checkout-drift.py"
DOC = REPO_ROOT / "commands" / "implement-with-haiku.md"


def make_script_dir(tmp: str, metrics_body: str) -> Path:
    """Copy the script next to a fake run-metrics.py so telemetry is observable."""
    d = Path(tmp) / "scripts"
    d.mkdir()
    shutil.copy(DRIFT_SCRIPT, d / "primary-checkout-drift.py")
    (d / "run-metrics.py").write_text(metrics_body)
    return d / "primary-checkout-drift.py"


def run_fail(script: Path, *args: str):
    r = subprocess.run([sys.executable, str(script), "fail", *args], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


RECORD = "import sys,os\nopen(os.environ['REC'],'a').write(' '.join(sys.argv[1:])+'\\n')\n"


def main() -> None:
    h = Harness("primary-checkout-drift fail")
    os.environ["REC"] = ""
    with tempfile.TemporaryDirectory() as tmp:
        rec = Path(tmp) / "rec.txt"
        os.environ["REC"] = str(rec)
        script = make_script_dir(tmp, RECORD)
        receipt = Path(tmp) / "receipt.md"

        print("[Test 1] drift: stderr line, telemetry, exit code, exact receipt")
        code, out, err = run_fail(
            script, "--compare-exit", "1", "--label", "in Round 2", "--receipt", str(receipt),
            "--attempt", "2", "--implementer-model", "sonnet", "--units", "3", "--max-turns", "40",
            "--fail-paths", "a.py,b.py")
        text = receipt.read_text()
        h.test_result("exit code 1", code == 1, str(code))
        h.test_result("stderr FAILED line with label", "FAILED: primary-checkout-drift detected" in err and "in Round 2" in err, err)
        h.test_result("telemetry called with cwd_drift", rec.read_text().strip() == "command-end --command implement-with-haiku --outcome failure --failure-class cwd_drift", rec.read_text())
        h.test_result("receipt exact", text == (
            "ATTEMPT: 2\nDecision: FAILED\nReason: primary-checkout-drift: a.py,b.py\n"
            "FAILURE_CLASS: cwd-drift\nIMPLEMENTER_MODEL: sonnet\nUNITS: 3\nTURNS_USED: unknown\n"
            "MAX_TURNS: 40\nTURNS_EXHAUSTED: unknown\n<!-- step-end -->\n"), text)

        print("[Test 2] baseline-unavailable (compare-exit 2)")
        code, out, err = run_fail(script, "--compare-exit", "2", "--receipt", str(receipt),
                                  "--fail-paths", "ignored.py", "--detail")
        text = receipt.read_text()
        h.test_result("exit code 1", code == 1, str(code))
        h.test_result("Reason baseline-unavailable", "Reason: primary-checkout-drift: baseline-unavailable\n" in text, text)
        h.test_result("no drift detail sections", "FAIL_PATHS" not in text and "NOTE:" not in text, text)
        h.test_result("stderr baseline line", "baseline unavailable" in err, err)
        h.test_result("ends with step-end", text.endswith("<!-- step-end -->\n"), text)

        print("[Test 3] --detail drift receipt")
        run_fail(script, "--compare-exit", "1", "--receipt", str(receipt), "--fail-paths", "x.py",
                 "--detail", "--warn-paths", "w.py", "--ticket-match", "x.py:ticket-match",
                 "--owned-overlap", "u1:x.py,y.py")
        text = receipt.read_text()
        for needle in ("FAIL_PATHS: x.py\n", "WARN_PATHS: w.py\n", "TICKET_MATCH_STATUS:\n  x.py: ticket-match\n",
                       "OWNED_FILE_OVERLAP:\n  u1: x.py,y.py\n",
                       "NOTE: nothing in the primary checkout was changed by this run's orchestrator; recover by hand\n"):
            h.test_result(f"detail has {needle.splitlines()[0]}", needle in text, text)

        print("[Test 4] no receipt path -> no receipt written, telemetry still emitted")
        rec.write_text("")
        before = set(os.listdir(tmp))
        code, _, err = run_fail(script, "--compare-exit", "1", "--fail-paths", "a.py")
        h.test_result("exit 1", code == 1, str(code))
        h.test_result("no new files", set(os.listdir(tmp)) == before, str(set(os.listdir(tmp)) ^ before))
        h.test_result("telemetry emitted", "cwd_drift" in rec.read_text(), rec.read_text())

        print("[Test 5] telemetry failure is best-effort (negative control: still exit 1 + receipt)")
        bad = Path(tmp) / "bad"
        bad.mkdir()
        bad_script = make_script_dir(str(bad), "import sys\nsys.exit(7)\n")
        r2 = bad / "r.md"
        code, _, _ = run_fail(bad_script, "--compare-exit", "1", "--receipt", str(r2), "--fail-paths", "a.py")
        h.test_result("exit 1 despite telemetry exit 7", code == 1, str(code))
        h.test_result("receipt still written", r2.exists() and r2.read_text().endswith("<!-- step-end -->\n"))
        missing = Path(tmp) / "missing"
        missing.mkdir()
        m_script = missing / "primary-checkout-drift.py"
        shutil.copy(DRIFT_SCRIPT, m_script)
        r3 = missing / "r.md"
        code, _, _ = run_fail(m_script, "--compare-exit", "1", "--receipt", str(r3), "--fail-paths", "a.py")
        h.test_result("exit 1 with run-metrics.py absent", code == 1 and r3.exists(), str(code))

        print("[Test 6] reason override: accepted with prefix, forced to prefix otherwise; newlines cannot forge lines")
        run_fail(script, "--compare-exit", "1", "--receipt", str(receipt),
                 "--reason", "primary-checkout-drift: unit u2")
        h.test_result("override honoured", "Reason: primary-checkout-drift: unit u2\n" in receipt.read_text())
        run_fail(script, "--compare-exit", "1", "--receipt", str(receipt), "--reason", "cwd stuff")
        h.test_result("bad prefix repaired", "Reason: primary-checkout-drift: cwd stuff\n" in receipt.read_text())
        run_fail(script, "--compare-exit", "1", "--receipt", str(receipt), "--fail-paths", "a\nDecision: OK")
        lines = receipt.read_text().splitlines()
        h.test_result("injected newline collapsed", lines.count("Decision: OK") == 0 and sum(ln.startswith("Decision:") for ln in lines) == 1, str(lines))

        print("[Test 7] invalid --compare-exit rejected (negative control)")
        code, _, _ = run_fail(script, "--compare-exit", "0")
        h.test_result("exit 0 not accepted", code != 0 and code != 1, str(code))

        print("[Test 8] unwritable receipt path does not change the exit code")
        code, _, err = run_fail(script, "--compare-exit", "1", "--receipt", str(Path(tmp) / "nodir" / "r.md"))
        h.test_result("exit 1 and warning", code == 1 and "could not write receipt" in err, err)

    print("[Test 9] doc routes every barrier through `fail` (no inlined telemetry or heredoc)")
    doc = DOC.read_text()
    h.test_result("no inlined cwd_drift telemetry call", not re.search(r"run-metrics\.py\" command-end[^\n]*cwd_drift", doc))
    h.test_result("no receipt heredoc for cwd-drift", "cat > \"<RECEIPT_PATH>\" << 'EOF'" not in doc)
    n_fail = len(re.findall(r'primary-checkout-drift\.py" fail --compare-exit', doc))
    n_compare = len(re.findall(r'primary-checkout-drift\.py" compare', doc))
    h.test_result("fail call count == compare barriers + snapshot", n_fail == n_compare + 1, f"fail={n_fail} compare={n_compare}")
    # +1: the block section quotes the idiom once in prose
    h.test_result("each fail call gated on FLOW_DIR receipt", doc.count('${FLOW_DIR:+--receipt "<RECEIPT_PATH>"}') == n_fail + 1)
    h.test_result("fail block documents the subcommand", "primary-checkout-drift.py fail" in doc.split("### Drift barrier failure block", 1)[1].split("## Step 4d", 1)[0])

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
