#!/usr/bin/env python3
"""
Behavioural tests for scripts/flow-redo.py, the sanitiser, archiver, restorer and checker for redo notes.

Every case runs the real script as a subprocess against a temporary flow dir holding stub steps,
gates and receipts. Covered:

1. Sanitise: exact outputs (CRLF/CR, nested tokens, control and invisible characters, shell and
   quote/backslash pass-through), 1000-character cap counted in characters, strip after truncation,
   sentinel lines (including sentinels formed by removing invisible characters or tokens), empty
   input exits 3, --file input.
2. Archive: copy, byte-identical no-op, differing-content refusal (valid receipt, one byte changed),
   ATTEMPT cross-check, table of refusals with nothing written.
3. Restore: receipt written last, attempt printed on stdout, table of refusals with nothing touched,
   a target that is not a regular file, a read-only target directory.
4. Check: complete archive exits 0, incomplete, wrong-attempt, audit-only and ambiguous archives exit 5.
5. CLI validation: table of invalid NN, STEP and attempt values.

Run with: python3 tests/test_flow_redo.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

FLOW_REDO = REPO_ROOT / "scripts" / "flow-redo.py"
FLOW_SUBCOMMANDS = ("archive", "restore", "check")
DEFAULT_AUDIT = "# Audit\nContent\n"
DEFAULT_DIGEST = "# Gate digest\nContent\n"


def run(sub, flow=None, *args, stdin=None):
    """Run `flow-redo.py <sub> ...`. For archive/restore/check, flow adds --flow-dir."""
    cmd = [sys.executable, str(FLOW_REDO), sub]
    if flow is not None and sub in FLOW_SUBCOMMANDS:
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


def standard_receipt(attempt=1, decision="OK"):
    return f"""ATTEMPT={attempt}
FINAL_PLAN_PATH: /path/to/plan
REVIEW_DIR: /path/to/review
Decision: {decision}
<!-- step-end -->
"""


def populate(flow, nn="01", step="plan", receipt=None, receipt_attempt=1, decision="OK",
             audit=DEFAULT_AUDIT, digest=DEFAULT_DIGEST):
    """Write a step's live files. receipt=None writes the standard receipt; audit/digest None omit them."""
    text = receipt if receipt is not None else standard_receipt(receipt_attempt, decision)
    (flow / "steps" / f"{nn}-{step}.md").write_text(text)
    if audit is not None:
        (flow / "steps" / f"{nn}-{step}.audit.md").write_text(audit)
    if digest is not None:
        (flow / "gates" / f"{nn}-{step}.md").write_text(digest)


def archive_attempt(flow, attempt=1, nn="01", step="plan"):
    return run("archive", flow, "--nn", nn, "--step", step, "--attempt", str(attempt))


def live_files(flow, nn="01", step="plan"):
    return [flow / "steps" / f"{nn}-{step}.md",
            flow / "steps" / f"{nn}-{step}.audit.md",
            flow / "gates" / f"{nn}-{step}.md"]


def archive_files(flow, attempt, nn="01", step="plan"):
    return [flow / "steps" / f"{nn}-{step}.a{attempt}.md",
            flow / "steps" / f"{nn}-{step}.a{attempt}.audit.md",
            flow / "gates" / f"{nn}-{step}.a{attempt}.md"]


def stray_archives(flow):
    """Any attempt-numbered archive file under the flow dir (audit files excluded by the pattern)."""
    return sorted(p.name for p in flow.rglob("*.a[0-9]*.md"))


def is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


# Sanitiser table: (name, stdin, exact expected stdout)
SANITIZE_EXACT = [
    ("basic input passes through", "hello world", "hello world"),
    ("CRLF normalised to LF", "hello\r\nworld\r\nend", "hello\nworld\nend"),
    ("lone CR normalised to LF", "hello\rworld", "hello\nworld"),
    ("nested tokens removed to fixed point", "AB<<<NO<<<NOTETEC", "ABC"),
    ("token at both ends removed", "<<<NOTE keep NOTE>>>", "keep"),
    ("control chars removed, tab and LF kept", "hello\x00\x01\x02world\t\nend", "helloworld\t\nend"),
    ("invisible format and separator chars removed", "a\u200bb\u202ec\u2028d\ufeffe", "abcde"),
    ("no-break space is not removed", "x\u00a0y", "x\u00a0y"),
    ("shell metacharacters pass through exactly", "$(whoami) `date` $(rm -rf /)",
     "$(whoami) `date` $(rm -rf /)"),
    ("quotes and backslashes pass through exactly", 'say "hi" \\ C:\\path\\to',
     'say "hi" \\ C:\\path\\to'),
    ("token at truncation boundary is removed", "a" * 995 + "NOTE>>>", "a" * 995),
    ("strip happens after truncation", "a" * 999 + " " + "bbb", "a" * 999),
    ("padded sentinel is not an exact line and passes", "text\n  HUMAN_TEXT_EOF  \nmore",
     "text\n  HUMAN_TEXT_EOF  \nmore"),
    ("1000-char cap counts characters, not bytes", "\U0001f525" * 1000 + "extra", "\U0001f525" * 1000),
]

# Sanitiser refusals: (name, stdin, expected exit code)
SANITIZE_REFUSALS = [
    ("empty input exits 3", "", 3),
    ("whitespace-only input exits 3", "   \n\t\n  ", 3),
    ("HUMAN_TEXT_EOF line exits 4", "HUMAN_TEXT_EOF", 4),
    ("REASON_EOF line in the middle exits 4", "text\nREASON_EOF\nmore", 4),
    ("sentinel formed by removing an invisible char exits 4", "HUMAN_TEXT_\u200bEOF", 4),
    ("sentinel formed by removing a token exits 4", "HUMAN_TEXT_<<<NOTEEOF", 4),
]


def main():
    h = Harness("FLOW REDO (flow-redo.py) TEST SUITE")

    with tempfile.TemporaryDirectory() as root:
        root = Path(root)

        print("[Section 1] Sanitise")
        for name, stdin, expected in SANITIZE_EXACT:
            rc, out, err = run("sanitize", stdin=stdin)
            h.test_result(f"{name}: rc 0 and exact output", rc == 0 and out == expected,
                          f"rc={rc}, out={out!r}, err={err!r}")

        rc, out, err = run("sanitize", stdin="hello world")
        h.test_result("sanitise reports original_len and kept_len on stderr",
                      err.strip() == "original_len=11 kept_len=11", repr(err))

        rc, out, err = run("sanitize", stdin="a" * 1100)
        h.test_result("text longer than 1000 chars is truncated to exactly 1000",
                      rc == 0 and len(out) == 1000, f"rc={rc}, len={len(out)}")
        h.test_result("truncation report shows kept_len=1000 and original_len=1100",
                      err.strip() == "original_len=1100 kept_len=1000", repr(err))

        for name, stdin, expected_rc in SANITIZE_REFUSALS:
            rc, out, err = run("sanitize", stdin=stdin)
            h.test_result(name, rc == expected_rc and out == "", f"rc={rc}, err={err!r}")

        rc, out, err = run("sanitize", stdin="HUMAN_TEXT_EOF")
        h.test_result("sentinel refusal names the situation in one line",
                      err.count("\n") == 1 and "HUMAN_TEXT_EOF" in err, repr(err))

        note = root / "note-ok.txt"
        note.write_text("line one\nline two\n", encoding="utf-8")
        rc, out, err = run("sanitize", None, "--file", str(note))
        h.test_result("--file reads the note and sanitises it exactly",
                      rc == 0 and out == "line one\nline two", f"rc={rc}, out={out!r}")

        sentinel_note = root / "note-sentinel.txt"
        sentinel_note.write_text("text\nHUMAN_TEXT_EOF\n", encoding="utf-8")
        rc, out, err = run("sanitize", None, "--file", str(sentinel_note))
        h.test_result("--file with a sentinel line still exits 4", rc == 4, f"rc={rc}, err={err!r}")

        rc, out, err = run("sanitize", None, "--file", str(root / "no-such-note.txt"))
        h.test_result("--file missing path refused with a plain message",
                      rc == 1 and "cannot read note file" in err and out == "", f"rc={rc}, err={err!r}")

        print()
        print("[Section 2] Archive")
        flow = flow_dir(root, "archive-basic")
        populate(flow)
        rc, out, err = archive_attempt(flow, 1)
        h.test_result("archive exits 0 for valid OK receipt", rc == 0, f"rc={rc}, err={err}")
        h.test_result("archive prints the archived attempt J on stdout", out == "1\n", repr(out))
        h.test_result("archive creates *.a1.md files",
                      (flow / "steps" / "01-plan.a1.md").exists()
                      and (flow / "gates" / "01-plan.a1.md").exists(), "files don't exist")
        h.test_result("archive creates *.a1.audit.md file",
                      (flow / "steps" / "01-plan.a1.audit.md").exists(), "audit archive missing")
        h.test_result("archive copies bytes exactly",
                      (flow / "steps" / "01-plan.a1.md").read_bytes()
                      == (flow / "steps" / "01-plan.md").read_bytes(), "receipt bytes differ")

        flow = flow_dir(root, "archive-identical")
        populate(flow)
        archive_attempt(flow, 1)
        before = (flow / "steps" / "01-plan.a1.md").stat().st_mtime_ns
        rc2, out2, _ = archive_attempt(flow, 1)
        after = (flow / "steps" / "01-plan.a1.md").stat().st_mtime_ns
        h.test_result("identical archive is no-op (same mtime) and still exits 0",
                      before == after and rc2 == 0 and out2 == "1\n", f"{before} vs {after}, rc={rc2}")

        flow = flow_dir(root, "archive-differing")
        populate(flow)
        archive_attempt(flow, 1)
        # Keep the receipt valid but change one byte of the live copy.
        (flow / "steps" / "01-plan.md").write_text(standard_receipt(1).replace("plan", "plaN"))
        rc2, _, err2 = archive_attempt(flow, 1)
        h.test_result("differing archive refused with 'differing content' message",
                      rc2 != 0 and "differing content" in err2, f"rc={rc2}, err={err2!r}")
        h.test_result("original archive unchanged after differing refusal",
                      (flow / "steps" / "01-plan.a1.md").read_text().startswith("ATTEMPT=1"),
                      "archive was corrupted")

        ARCHIVE_REFUSALS = [
            ("non-OK receipt refused", {"decision": "FAILED"}, 1, "Decision: OK"),
            ("missing audit refused", {"audit": None}, 1, "audit not found"),
            ("missing gate digest refused", {"digest": None}, 1, "digest not found"),
            ("receipt without step-end refused", {"receipt": "ATTEMPT=1\nDecision: OK\n"}, 1, "step-end"),
            ("ATTEMPT mismatch refused", {"receipt_attempt": 2}, 1, "says ATTEMPT=2, not 1"),
            ("indented Decision line is not a whole-line match",
             {"receipt": "ATTEMPT=1\n  Decision: OK\n<!-- step-end -->\n"}, 1, "Decision: OK"),
            ("substring-only ATTEMPT line is rejected",
             {"receipt": "XATTEMPT=1\nDecision: OK\n<!-- step-end -->\n"}, 1, "ATTEMPT"),
            ("stub audit refused", {"audit": "STUB\n"}, 1, "is a stub"),
            ("stub gate digest refused", {"digest": "STUB\n"}, 1, "is a stub"),
        ]
        for name, kwargs, want_rc, want_msg in ARCHIVE_REFUSALS:
            flow = flow_dir(root, f"archive-refuse-{name.split()[0]}-{len(name)}")
            populate(flow, **kwargs)
            rc, out, err = archive_attempt(flow, 1)
            h.test_result(f"archive: {name}, nothing written",
                          rc == want_rc and want_msg in err and out == "" and stray_archives(flow) == [],
                          f"rc={rc}, err={err!r}, written={stray_archives(flow)}")

        flow = flow_dir(root, "archive-attempt-zero")
        populate(flow)
        rc, out, err = archive_attempt(flow, 0)
        h.test_result("archive: attempt 0 refused by validator, nothing written",
                      rc == 1 and "invalid attempt" in err and stray_archives(flow) == [], f"rc={rc}, err={err!r}")

        print()
        print("[Section 3] Restore")
        flow = flow_dir(root, "restore-basic")
        populate(flow)
        archive_attempt(flow, 1)
        for path in live_files(flow):
            path.unlink()
        rc, out, err = run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("restore exits 0", rc == 0, f"rc={rc}, err={err}")
        h.test_result("restore recreates receipt, audit and gate digest",
                      all(p.exists() for p in live_files(flow)), "a live file is missing")
        h.test_result("restore prints exactly the attempt J on stdout", out == "1\n", repr(out))
        h.test_result("restore writes archive bytes exactly",
                      (flow / "steps" / "01-plan.md").read_bytes()
                      == (flow / "steps" / "01-plan.a1.md").read_bytes(), "receipt bytes differ")

        RESTORE_REFUSALS = [
            ("receipt archive missing", 0),
            ("audit archive missing", 1),
            ("gate digest archive missing", 2),
        ]
        for name, index in RESTORE_REFUSALS:
            flow = flow_dir(root, f"restore-{index}")
            populate(flow)
            archive_attempt(flow, 1)
            for path in live_files(flow):
                path.unlink()
            archive_files(flow, 1)[index].unlink()
            rc, out, err = run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
            h.test_result(f"restore refuses when {name}, nothing created",
                          rc != 0 and out == "" and not any(p.exists() for p in live_files(flow)),
                          f"rc={rc}, err={err!r}")

        flow = flow_dir(root, "restore-attempt-mismatch")
        populate(flow)
        archive_attempt(flow, 1)
        for path in live_files(flow):
            path.unlink()
        # Copy the attempt-1 archive under the attempt-2 name: the receipt still says ATTEMPT=1.
        for src, dst in zip(archive_files(flow, 1), archive_files(flow, 2)):
            dst.write_bytes(src.read_bytes())
        rc, out, err = run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "2")
        h.test_result("restore refuses when receipt ATTEMPT differs from the archive name",
                      rc != 0 and "says ATTEMPT=1, not 2" in err and not any(p.exists() for p in live_files(flow)),
                      f"rc={rc}, err={err!r}")

        flow = flow_dir(root, "restore-not-regular")
        populate(flow)
        archive_attempt(flow, 1)
        (flow / "steps" / "01-plan.audit.md").write_text("OLD AUDIT\n")
        (flow / "steps" / "01-plan.md").unlink()
        (flow / "gates" / "01-plan.md").unlink()
        (flow / "gates" / "01-plan.md").mkdir()
        rc, out, err = run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("restore refuses a target that is not a regular file, audit untouched",
                      rc != 0 and "not a regular file" in err
                      and (flow / "steps" / "01-plan.audit.md").read_text() == "OLD AUDIT\n",
                      f"rc={rc}, err={err!r}")

        flow = flow_dir(root, "restore-readonly")
        populate(flow)
        archive_attempt(flow, 1)
        (flow / "steps" / "01-plan.audit.md").write_text("OLD AUDIT\n")
        (flow / "steps" / "01-plan.md").unlink()
        (flow / "gates" / "01-plan.md").unlink()
        if is_root():
            print("  (read-only directory case skipped: root bypasses directory permissions)")
        else:
            (flow / "gates").chmod(0o555)
            try:
                rc, out, err = run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
            finally:
                (flow / "gates").chmod(0o755)
            h.test_result("restore refuses a read-only target directory, audit untouched",
                          rc != 0 and "not writable" in err
                          and (flow / "steps" / "01-plan.audit.md").read_text() == "OLD AUDIT\n",
                          f"rc={rc}, err={err!r}")

        flow = flow_dir(root, "restore-idempotent")
        populate(flow)
        archive_attempt(flow, 1)
        rc1, out1, _ = run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        rc2, out2, _ = run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("second restore also exits 0 with the same output",
                      rc2 == 0 and out1 == out2 == "1\n", f"rc={rc2}, {out1!r} vs {out2!r}")

        flow = flow_dir(root, "restore-receipt-shape")
        populate(flow)
        archive_attempt(flow, 1)
        (flow / "steps" / "01-plan.md").unlink()
        run("restore", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        receipt = (flow / "steps" / "01-plan.md").read_text()
        h.test_result("restored receipt has Decision: OK", "Decision: OK" in receipt, repr(receipt))
        h.test_result("restored receipt has <!-- step-end -->", "<!-- step-end -->" in receipt, repr(receipt))
        h.test_result("restored receipt keeps its archived ATTEMPT line", "ATTEMPT=1\n" in receipt, repr(receipt))

        print()
        print("[Section 4] Check")
        flow = flow_dir(root, "check-complete")
        populate(flow)
        archive_attempt(flow, 1)
        rc, out, err = run("check", flow, "--step", "plan", "--attempt", "1")
        h.test_result("check exits 0 and prints J for a complete archive",
                      rc == 0 and out == "1\n", f"rc={rc}, err={err!r}")
        rc, out, err = run("check", flow, "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("check with explicit --nn exits 0", rc == 0, f"rc={rc}, err={err!r}")

        CHECK_REFUSALS = [
            ("no archive for attempt 2", None, "2", "no complete archive for attempt 2"),
            ("archive incomplete (digest archive missing)", 2, "1", "missing"),
            ("audit archive only, no receipt archive", "audit-only", "1", "no complete archive"),
            ("receipt archive says another attempt", "wrong-attempt", "1", "says ATTEMPT=2, not 1"),
        ]
        for name, mode, attempt_arg, want_msg in CHECK_REFUSALS:
            flow = flow_dir(root, f"check-refuse-{attempt_arg}-{len(name)}")
            populate(flow)
            archive_attempt(flow, 1)
            if mode == 2:
                archive_files(flow, 1)[2].unlink()
            elif mode == "audit-only":
                archive_files(flow, 1)[0].unlink()
                archive_files(flow, 1)[2].unlink()
            elif mode == "wrong-attempt":
                text = archive_files(flow, 1)[0].read_text().replace("ATTEMPT=1", "ATTEMPT=2")
                archive_files(flow, 1)[0].write_text(text)
            rc, out, err = run("check", flow, "--step", "plan", "--attempt", attempt_arg)
            h.test_result(f"check refuses: {name}",
                          rc == 5 and want_msg in err and out == "" and err.count("\n") == 1,
                          f"rc={rc}, err={err!r}")

        flow = flow_dir(root, "check-ambiguous")
        populate(flow, nn="01")
        populate(flow, nn="02")
        archive_attempt(flow, 1, nn="01")
        archive_attempt(flow, 1, nn="02")
        rc, out, err = run("check", flow, "--step", "plan", "--attempt", "1")
        h.test_result("check without --nn refuses an ambiguous step archive",
                      rc == 5 and "ambiguous" in err, f"rc={rc}, err={err!r}")
        rc, out, err = run("check", flow, "--nn", "02", "--step", "plan", "--attempt", "1")
        h.test_result("check with --nn picks the right archive", rc == 0 and out == "1\n", f"rc={rc}")

        print()
        print("[Section 5] CLI validation")
        CLI_REFUSALS = [
            ("archive: NN with letters", "archive", {"nn": "x1", "step": "plan", "attempt": "1"}, "invalid NN"),
            ("archive: STEP with uppercase", "archive", {"nn": "01", "step": "Plan", "attempt": "1"}, "invalid STEP"),
            ("archive: attempt zero", "archive", {"nn": "01", "step": "plan", "attempt": "0"}, "invalid attempt"),
            ("archive: attempt with leading zero", "archive", {"nn": "01", "step": "plan", "attempt": "01"}, "invalid attempt"),
            ("restore: attempt not numeric", "restore", {"nn": "01", "step": "plan", "attempt": "abc"}, "invalid attempt"),
            ("restore: STEP with path separator", "restore", {"nn": "01", "step": "../x", "attempt": "1"}, "invalid STEP"),
            ("check: STEP empty", "check", {"step": "", "attempt": "1"}, "invalid STEP"),
            ("check: NN with letters", "check", {"nn": "0a", "step": "plan", "attempt": "1"}, "invalid NN"),
        ]
        flow = flow_dir(root, "cli-validation")
        for name, sub, opts, want_msg in CLI_REFUSALS:
            argv = []
            for key in ("nn", "step", "attempt"):
                if key in opts:
                    argv.extend([f"--{key}", opts[key]])
            rc, out, err = run(sub, flow, *argv)
            h.test_result(f"{name} refused with exit 1",
                          rc == 1 and want_msg in err and out == "", f"rc={rc}, err={err!r}")

        rc, out, err = run("archive", root / "no-such-flow", "--nn", "01", "--step", "plan", "--attempt", "1")
        h.test_result("missing flow dir refused with exit 1",
                      rc == 1 and "does not exist" in err, f"rc={rc}, err={err!r}")

        print()
        h.summarize_and_exit()


if __name__ == "__main__":
    main()
