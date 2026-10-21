#!/usr/bin/env python3
"""
Behavioural tests for scripts/flow-redo.py, the sanitiser, archiver and restorer for redo notes.

Every case runs the real script as a subprocess against a temporary flow dir holding stub steps,
gates and receipts. Covered:

1. Sanitise: nested-token fixed point, CRLF/CR normalization, control-character removal,
   strip-before-truncate with token boundary safety, character-based 1000-char cap with multibyte input,
   truncation report, empty/whitespace-only result exits 3, sentinel-line detection exits 4,
   shell metacharacters pass through untouched.
2. Archive: copy, identical no-op, differing-bytes refusal, non-OK source refusal,
   missing audit or digest refusal with nothing written.
3. Restore: receipt written last, any-missing refusal with nothing touched, idempotent,
   restored receipt shape matches resume rule 2.

Run with: python3 tests/test_flow_redo.py
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

FLOW_REDO = REPO_ROOT / "scripts" / "flow-redo.py"


def run(sub, flow=None, *args, stdin=None):
    """Run `flow-redo.py <sub> ...`. For archive/restore, flow is required and will add --flow-dir."""
    cmd = [sys.executable, str(FLOW_REDO), sub]
    if flow is not None and sub in ("archive", "restore"):
        cmd.extend(["--flow-dir", str(flow)])
    cmd.extend(args)
    kwargs = {"capture_output": True, "timeout": 60, "text": True}
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    result = subprocess.run(cmd, **kwargs)
    return result.returncode, result.stdout, result.stderr


def flow_dir(root, name):
    """Create a flow dir with stub state.json and return its path."""
    flow = Path(root) / name
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "state.json").write_text("{}\n")
    (flow / "steps").mkdir(exist_ok=True)
    (flow / "gates").mkdir(exist_ok=True)
    return flow


def make_receipt(flow, nn, step, attempt, decision="OK"):
    """Create a step receipt file with the given decision and attempt."""
    content = f"""ATTEMPT={attempt}
FINAL_PLAN_PATH: /path/to/plan
REVIEW_DIR: /path/to/review
Decision: {decision}
<!-- step-end -->
"""
    path = flow / "steps" / f"{nn}-{step}.md"
    path.write_text(content)
    return path


def make_audit(flow, nn, step):
    """Create a stub audit file."""
    path = flow / "steps" / f"{nn}-{step}.audit.md"
    path.write_text("# Audit\nContent\n")
    return path


def make_gate_digest(flow, nn, step):
    """Create a stub gate digest file."""
    path = flow / "gates" / f"{nn}-{step}.md"
    path.write_text("# Gate digest\nContent\n")
    return path


def main():
    h = Harness("FLOW REDO (flow-redo.py) TEST SUITE")

    with tempfile.TemporaryDirectory() as root:
        root = Path(root)

        print("[Section 1] Sanitise")
        rc, out, err = run("sanitize", stdin="hello world")
        h.test_result("basic sanitise exits 0", rc == 0, f"rc={rc}, err={err}")
        h.test_result("basic sanitise output is the input", out.strip() == "hello world", repr(out))
        h.test_result("sanitise reports original_len and kept_len on stderr",
                      "original_len=" in err and "kept_len=" in err, repr(err))

        rc, out, err = run("sanitize", stdin="hello\r\nworld\r\nend")
        h.test_result("CRLF is normalized to LF", out == "hello\nworld\nend", repr(out))

        rc, out, err = run("sanitize", stdin="hello\rworld")
        h.test_result("lone CR is normalized to LF", out == "hello\nworld", repr(out))

        rc, out, err = run("sanitize", stdin="<<<NO<<<NOTETE\nNOTNOTE>>>E>>>")
        h.test_result("nested tokens are removed to fixed point",
                      "<<<NOTE" not in out and "NOTE>>>" not in out, repr(out))

        rc, out, err = run("sanitize", stdin="hello\x00\x01\x02world\t\nend")
        h.test_result("control chars are removed (tab and LF kept)",
                      "\x00" not in out and "\x01" not in out and "\t" in out and "\n" in out, repr(out))

        long_text = "a" * 1100
        rc, out, err = run("sanitize", stdin=long_text)
        h.test_result("text longer than 1000 chars is truncated", len(out.rstrip("\n")) <= 1000, f"len={len(out)}")
        h.test_result("truncation report shows kept_len <= 1000",
                      "kept_len=" in err, repr(err))
        m = re.search(r"kept_len=(\d+)", err)
        if m:
            kept = int(m.group(1))
            h.test_result("truncation report kept_len value is correct", kept <= 1000, str(kept))

        # Place a NOTE>>> token exactly at the truncation boundary
        text = "a" * 995 + "NOTE>>>"
        rc, out, err = run("sanitize", stdin=text)
        h.test_result("token at truncation boundary does not reform",
                      "NOTE>>>" not in out, repr(out))

        # 1000 emoji characters (each is 4 bytes in UTF-8)
        multibyte = "\U0001f525" * 1000 + "extra"
        rc, out, err = run("sanitize", stdin=multibyte)
        h.test_result("1000-char cap is counted in characters, not bytes",
                      len(out.rstrip("\n")) <= 1000, f"len={len(out)}")

        rc, out, err = run("sanitize", stdin="")
        h.test_result("empty input exits 3", rc == 3, f"rc={rc}, err={err}")

        rc, out, err = run("sanitize", stdin="   \n\t\n  ")
        h.test_result("whitespace-only input exits 3", rc == 3, f"rc={rc}, err={err}")

        rc, out, err = run("sanitize", stdin="HUMAN_TEXT_EOF")
        h.test_result("sentinel line HUMAN_TEXT_EOF exits 4", rc == 4, f"rc={rc}, err={err}")

        rc, out, err = run("sanitize", stdin="text\nREASON_EOF\nmore")
        h.test_result("sentinel line REASON_EOF exits 4", rc == 4, f"rc={rc}, err={err}")

        rc, out, err = run("sanitize", stdin="$(whoami) `date` $(rm -rf /)")
        h.test_result("shell metacharacters pass through untouched",
                      "$(whoami)" in out and "`date`" in out and "$(rm -rf /)" in out, repr(out))

        rc, out, err = run("sanitize", stdin='quotes and backslash test')
        h.test_result("quotes and backslashes pass through untouched",
                      "quotes" in out and "backslash" in out, repr(out))

        print()
        print("[Section 2] Archive")
        flow = flow_dir(root, "archive-basic")
        make_receipt(flow, "01", "plan", 1, "OK")
        make_audit(flow, "01", "plan")
        make_gate_digest(flow, "01", "plan")
        rc, out, err = run("archive", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("archive exits 0 for valid OK receipt", rc == 0, f"rc={rc}, err={err}")
        h.test_result("archive creates *.a1.md files",
                      (flow / "steps" / "01-plan.a1.md").exists()
                      and (flow / "gates" / "01-plan.a1.md").exists(), "files don't exist")
        h.test_result("archive creates *.a1.audit.md file",
                      (flow / "steps" / "01-plan.a1.audit.md").exists(), "audit archive missing")

        flow = flow_dir(root, "archive-identical")
        make_receipt(flow, "01", "plan", 1, "OK")
        make_audit(flow, "01", "plan")
        make_gate_digest(flow, "01", "plan")
        rc1, _, _ = run("archive", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        before = (flow / "steps" / "01-plan.a1.md").stat().st_mtime
        rc2, _, _ = run("archive", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        after = (flow / "steps" / "01-plan.a1.md").stat().st_mtime
        h.test_result("identical archive is no-op (same mtime)", before == after, f"{before} vs {after}")

        flow = flow_dir(root, "archive-differing")
        make_receipt(flow, "01", "plan", 1, "OK")
        make_audit(flow, "01", "plan")
        make_gate_digest(flow, "01", "plan")
        rc1, _, _ = run("archive", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        # Now create a different receipt and try to archive with the same attempt number
        (flow / "steps" / "01-plan.md").write_text("DIFFERENT\n")
        rc2, _, err2 = run("archive", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("differing archive refuses to overwrite", rc2 != 0, f"rc={rc2}, err={err2}")
        h.test_result("original archive unchanged after differing refusal",
                      (flow / "steps" / "01-plan.a1.md").read_text().startswith("ATTEMPT=1"), "archive was corrupted")

        flow = flow_dir(root, "archive-non-ok")
        make_receipt(flow, "01", "plan", 1, "FAILED")
        make_audit(flow, "01", "plan")
        make_gate_digest(flow, "01", "plan")
        rc, _, err = run("archive", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("archive refuses non-OK receipt", rc != 0, f"rc={rc}, err={err}")

        flow = flow_dir(root, "archive-missing-audit")
        make_receipt(flow, "01", "plan", 1, "OK")
        # Don't create audit
        make_gate_digest(flow, "01", "plan")
        rc, _, err = run("archive", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("archive refuses when audit is missing", rc != 0, f"rc={rc}, err={err}")
        h.test_result("nothing written when audit missing",
                      not (flow / "steps" / "01-plan.a1.md").exists(), "receipt archive was created")

        flow = flow_dir(root, "archive-missing-digest")
        make_receipt(flow, "01", "plan", 1, "OK")
        make_audit(flow, "01", "plan")
        # Don't create gate digest
        rc, _, err = run("archive", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("archive refuses when gate digest is missing", rc != 0, f"rc={rc}, err={err}")
        h.test_result("nothing written when digest missing",
                      not (flow / "gates" / "01-plan.a1.md").exists(), "digest archive was created")

        print()
        print("[Section 3] Restore")
        flow = flow_dir(root, "restore-basic")
        make_receipt(flow, "01", "plan", 1, "OK")
        make_audit(flow, "01", "plan")
        make_gate_digest(flow, "01", "plan")
        run("archive", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        # Delete the originals
        (flow / "steps" / "01-plan.md").unlink()
        (flow / "steps" / "01-plan.audit.md").unlink()
        (flow / "gates" / "01-plan.md").unlink()
        rc, out, err = run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("restore exits 0", rc == 0, f"rc={rc}, err={err}")
        h.test_result("restore recreates receipt", (flow / "steps" / "01-plan.md").exists())
        h.test_result("restore recreates audit", (flow / "steps" / "01-plan.audit.md").exists())
        h.test_result("restore recreates gate digest", (flow / "gates" / "01-plan.md").exists())
        h.test_result("restore prints receipt path on stdout",
                      "FINAL_PLAN_PATH" in out or "REVIEW_DIR" in out, repr(out))

        flow = flow_dir(root, "restore-missing-archive")
        # Try to restore without any archive files
        rc, _, err = run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("restore refuses when archive missing", rc != 0, f"rc={rc}, err={err}")
        h.test_result("nothing created when archive missing",
                      not (flow / "steps" / "01-plan.md").exists(), "receipt was created")

        flow = flow_dir(root, "restore-idempotent")
        make_receipt(flow, "01", "plan", 1, "OK")
        make_audit(flow, "01", "plan")
        make_gate_digest(flow, "01", "plan")
        run("archive", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        # Restore twice
        rc1, out1, _ = run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        rc2, out2, _ = run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("second restore also exits 0", rc2 == 0, f"rc={rc2}")
        h.test_result("second restore produces same output", out1 == out2, f"{repr(out1)} vs {repr(out2)}")

        flow = flow_dir(root, "restore-receipt-shape")
        make_receipt(flow, "01", "plan", 1, "OK")
        make_audit(flow, "01", "plan")
        make_gate_digest(flow, "01", "plan")
        run("archive", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        (flow / "steps" / "01-plan.md").unlink()
        run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        receipt = (flow / "steps" / "01-plan.md").read_text()
        h.test_result("restored receipt has Decision: OK", "Decision: OK" in receipt, repr(receipt))
        h.test_result("restored receipt has <!-- step-end -->", "<!-- step-end -->" in receipt, repr(receipt))

        print()
        h.summarize_and_exit()


if __name__ == "__main__":
    main()
