#!/usr/bin/env python3
"""
Unit tests for scripts/flow-accept.py, the eligibility allowlist and trail writer behind the
human-attested "Accept and continue" option at the /expert-flow hard stop (#365).

The script is driven as a black box through its CLI (`eligible` and `record`) against temporary
flow dirs holding a fixture state.json, log.md and step receipt. Covered:

1. eligible, allowlist: one case per allowlisted class; every plan step passes the step check.
2. eligible, refusals: W2 self-reports (stop_source receipt), stale or mismatched stop_source and
   reason pairs, unlisted reasons (missing-marker, artifacts-missing, check-gate, resume, unknown),
   the missing-output precondition, the not-failed newest line, excluded steps, verify.
3. eligible, log suffix independence: a bare, drifted or matching `failed (...)` suffix is ignored.
4. eligible, usage and IO errors exit 2.
5. record: one block per accept (appended), refusal with exit 1 and the file untouched, sentinel
   refusal, verbatim FAILED_REASON, NOTE: none for empty stdin.

Run with: python3 tests/test_flow_accept.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

FLOW_ACCEPT = REPO_ROOT / "scripts" / "flow-accept.py"
NN = "03"
TS = "2026-10-29T10:00:00Z"
ACCEPTED_END = "<!-- accepted-end -->"
OK_RECEIPT = "ATTEMPT=1\nFINAL_PLAN_PATH: /path/to/plan\nDecision: OK\n<!-- step-end -->\n"
ALLOWED_STEPS = ("plan", "implement", "review", "fix", "verify")

# (class, stop_source, reason as the verifier or audit wrote it, step)
ALLOWED = [
    ("audit-reduced", "audit", "reduced run (audit): 2 units uncovered", "implement"),
    ("receipt-reduction", "validation", "reduced run: 2 units uncovered", "fix"),
    ("receipt-deviation", "validation", "receipt admits deviation", "review"),
    ("plan-checkpoint", "validation", "plan checkpoint incomplete (2 of 3 rounds)", "plan"),
    ("review-rulings", "validation", "review rulings incomplete (1 ruling pending)", "review"),
]

# (name, stop_source, reason, expected output prefix). Every row must be refused.
REFUSED = [
    ("W2: agent self-report, reduced run", "receipt", "reduced run: 2 units uncovered",
     "ACCEPT: no stop-source-receipt"),
    ("W2: agent self-report, deviation", "receipt", "receipt admits deviation",
     "ACCEPT: no stop-source-receipt"),
    ("stale audit source with a verifier reason", "audit", "receipt admits deviation",
     "ACCEPT: no unlisted-reason"),
    ("validation source with an audit reason", "validation",
     "reduced run (audit): 2 units uncovered", "ACCEPT: no unlisted-reason"),
    ("exact match only: trailing words after receipt admits deviation", "validation",
     "receipt admits deviation, see the note", "ACCEPT: no unlisted-reason"),
    ("artifacts missing family", "validation", "artifacts missing: steps/03-implement.audit.md",
     "ACCEPT: no unlisted-reason"),
    ("missing ROUND 1 marker (the #363 class)", "validation",
     "receipt missing ROUND 1 section header", "ACCEPT: no unlisted-reason"),
    ("missing OUTSTANDING marker (the #343 class)", "validation",
     "receipt missing OUTSTANDING", "ACCEPT: no unlisted-reason"),
    ("check-gate reason", "validation", "check gate failed: just check exited 1 (lint)",
     "ACCEPT: no unlisted-reason"),
    ("resume ok without audit evidence", "validation", "resume: ok without audit evidence",
     "ACCEPT: no unlisted-reason"),
    ("artifact check could not run", "validation", "artifact check could not run",
     "ACCEPT: no unlisted-reason"),
    ("made-up reason no verifier owns", "validation", "the verifier invented this",
     "ACCEPT: no unlisted-reason"),
]

# (name, receipt text or None for no file). Each is refused as missing output.
MISSING_OUTPUT = [
    ("receipt says Decision: FAILED", "ATTEMPT=1\nDecision: FAILED\n<!-- step-end -->\n"),
    ("receipt without step-end", "ATTEMPT=1\nDecision: OK\n"),
    ("last Decision line is FAILED after an earlier OK",
     "ATTEMPT=1\nDecision: OK\nDecision: FAILED\n<!-- step-end -->\n"),
    ("receipt file absent", None),
]

# (name, log lines after the failed line). The newest lifecycle line must be a failed line.
NOT_FAILED = [
    ("newest line is ok", [f"{TS} implement ok"]),
    ("newest line is begin after the failed line", [f"{TS} implement begin"]),
    ("newest line is accepted (human decision)", [f"{TS} implement accepted (receipt-deviation)"]),
]

# Log suffix variants that must not change the answer (eligibility reads typed state only).
SUFFIX_VARIANTS = [
    ("bare failed line", "receipt-deviation", "receipt admits deviation", None),
    ("drifted suffix wording", "receipt-deviation", "receipt admits deviation",
     "the verifier said something else entirely"),
    ("suffix equal to the reason", "receipt-deviation", "receipt admits deviation",
     "receipt admits deviation"),
    ("bare failed line, audit class", "audit-reduced", "reduced run (audit): 2 units uncovered", None),
    ("drifted suffix, audit class", "audit-reduced", "reduced run (audit): 2 units uncovered",
     "reduced run, audit found gaps"),
]

def state_for(step, source=None, reason=None):
    """The state.json contents for one step. A None source or reason leaves that key absent."""
    state = {}
    if source is not None:
        state["stop_source"] = {step: source}
    if reason is not None:
        state["stop_reason"] = {step: reason}
    return state


def failed_line(step="implement", suffix=None):
    """A newest-lifecycle `failed` line, with the optional display suffix (never parsed)."""
    return f"{TS} {step} failed ({suffix})" if suffix is not None else f"{TS} {step} failed"


def make_flow(root, name, state, log_lines, step="implement", receipt=OK_RECEIPT, nn=NN):
    """Create a flow dir with state.json, log.md and the step receipt (receipt=None writes none)."""
    flow = Path(root) / name
    (flow / "steps").mkdir(parents=True, exist_ok=True)
    (flow / "state.json").write_text(json.dumps(state))
    (flow / "log.md").write_text("\n".join(log_lines) + "\n")
    if receipt is not None:
        (flow / "steps" / f"{nn}-{step}.md").write_text(receipt)
    return flow


def run(argv, stdin=None):
    """Run flow-accept.py with argv. stdin=None gives an empty stdin, a string is passed through."""
    kwargs = {"capture_output": True, "text": True, "timeout": 60}
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    result = subprocess.run([sys.executable, str(FLOW_ACCEPT), *argv], **kwargs)
    return result.returncode, result.stdout, result.stderr


def eligible(flow, step="implement", nn=NN):
    return run(["eligible", "--flow-dir", str(flow), "--step", step, "--nn", nn])


def record(flow, step="implement", nn=NN, attempt="2", note=""):
    return run(["record", "--flow-dir", str(flow), "--step", step, "--nn", nn,
                "--attempt", str(attempt), "--note-stdin"], stdin=note)


def accepted_file(flow, step="implement", nn=NN):
    return flow / "steps" / f"{nn}-{step}.accepted.md"


def main():
    h = Harness("FLOW ACCEPT (flow-accept.py) TEST SUITE")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        print("[Section 1] Eligibility: allowlisted classes")
        for cls, source, reason, step in ALLOWED:
            flow = make_flow(root, f"allowed-{cls}", state_for(step, source, reason),
                             [failed_line(step, reason)], step=step)
            rc, out, err = eligible(flow, step)
            h.test_result(f"allowlisted {cls} gives exactly one line: ACCEPT: yes {cls}",
                          rc == 0 and out == f"ACCEPT: yes {cls}\n",
                          f"rc={rc}, out={out!r}, err={err!r}")

        for step in ALLOWED_STEPS:
            flow = make_flow(root, f"step-{step}", state_for(step, "validation", "receipt admits deviation"),
                             [failed_line(step, "receipt admits deviation")], step=step)
            rc, out, err = eligible(flow, step)
            h.test_result(f"step {step} passes the step check (receipt-deviation yes)",
                          rc == 0 and out == "ACCEPT: yes receipt-deviation\n",
                          f"rc={rc}, out={out!r}, err={err!r}")

        print()
        print("[Section 2] Eligibility: refusals")
        for index, (name, source, reason, want) in enumerate(REFUSED):
            flow = make_flow(root, f"refuse-{index:02d}", state_for("implement", source, reason),
                             [failed_line("implement", reason)])
            rc, out, err = eligible(flow)
            h.test_result(f"refused: {name}", rc == 0 and out.startswith(want),
                          f"rc={rc}, out={out!r}, err={err!r}")

        flow = make_flow(root, "refuse-missing-stop-source", state_for("implement", None, "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        rc, out, err = eligible(flow)
        h.test_result("absent stop_source is refused", rc == 0 and out.startswith("ACCEPT: no "),
                      f"rc={rc}, out={out!r}")

        flow = make_flow(root, "refuse-missing-stop-reason", state_for("implement", "validation", None),
                         [failed_line("implement")])
        rc, out, err = eligible(flow)
        h.test_result("absent stop_reason is refused as unlisted-reason",
                      rc == 0 and out.startswith("ACCEPT: no unlisted-reason"),
                      f"rc={rc}, out={out!r}")

        for index, (name, receipt) in enumerate(MISSING_OUTPUT):
            flow = make_flow(root, f"missing-{index:02d}",
                             state_for("implement", "validation", "receipt admits deviation"),
                             [failed_line("implement", "receipt admits deviation")], receipt=receipt)
            rc, out, err = eligible(flow)
            h.test_result(f"missing output refused: {name}",
                          rc == 0 and out.startswith("ACCEPT: no missing-output"),
                          f"rc={rc}, out={out!r}, err={err!r}")

        for index, (name, extra) in enumerate(NOT_FAILED):
            flow = make_flow(root, f"not-failed-{index:02d}",
                             state_for("implement", "validation", "receipt admits deviation"),
                             [failed_line("implement", "receipt admits deviation")] + extra)
            rc, out, err = eligible(flow)
            h.test_result(f"not-failed refused: {name}",
                          rc == 0 and out.startswith("ACCEPT: no not-failed"),
                          f"rc={rc}, out={out!r}, err={err!r}")

        for step in ("merge", "ship", "worktree", "track"):
            flow = make_flow(root, f"excluded-{step}", state_for(step, "validation", "receipt admits deviation"),
                             [failed_line(step, "receipt admits deviation")], step=step)
            rc, out, err = eligible(flow, step)
            h.test_result(f"excluded step {step} refused with no step-{step}",
                          rc == 0 and out.startswith(f"ACCEPT: no step-{step}"),
                          f"rc={rc}, out={out!r}, err={err!r}")

        flow = make_flow(root, "verify-check-gate", state_for("verify", "validation", "just check exited 1 (lint)"),
                         [failed_line("verify", "just check exited 1 (lint)")], step="verify")
        rc, out, err = eligible(flow, "verify")
        h.test_result("verify with a check-gate stop_reason is refused as unlisted-reason",
                      rc == 0 and out.startswith("ACCEPT: no unlisted-reason"),
                      f"rc={rc}, out={out!r}, err={err!r}")

        print()
        print("[Section 3] Eligibility: log suffix is display text only")
        for index, (name, cls, reason, suffix) in enumerate(SUFFIX_VARIANTS):
            flow = make_flow(root, f"suffix-{index:02d}", state_for("implement", _source_for(cls), reason),
                             [failed_line("implement", suffix)])
            rc, out, err = eligible(flow)
            h.test_result(f"{name}: still ACCEPT: yes {cls}",
                          rc == 0 and out == f"ACCEPT: yes {cls}\n",
                          f"rc={rc}, out={out!r}, err={err!r}")

        print()
        print("[Section 4] Eligibility: usage and IO errors exit 2")
        rc, out, err = eligible(root / "no-such-flow")
        h.test_result("missing flow dir exits 2", rc == 2, f"rc={rc}, out={out!r}, err={err!r}")

        flow = make_flow(root, "usage-no-step", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        rc, out, err = run(["eligible", "--flow-dir", str(flow), "--nn", NN])
        h.test_result("missing --step exits 2", rc == 2, f"rc={rc}, out={out!r}, err={err!r}")

        print()
        print("[Section 5] record: trail writer")
        flow = make_flow(root, "record-basic", state_for("implement", "audit", "reduced run (audit): 2 units uncovered"),
                         [failed_line("implement", "reduced run (audit): 2 units uncovered")])
        note = 'use $(whoami) "quoted" and C:\\path'
        rc, out, err = record(flow, attempt="2", note=note)
        h.test_result("record exits 0 on an eligible failure and prints the class",
                      rc == 0 and "audit-reduced" in out, f"rc={rc}, out={out!r}, err={err!r}")
        trail = accepted_file(flow)
        h.test_result("record creates steps/<NN>-<STEP>.accepted.md", trail.is_file(), f"missing {trail}")
        lines = trail.read_text().splitlines() if trail.is_file() else []
        for want in ("STEP: implement", "ATTEMPT: 2", "ACCEPT_CLASS: audit-reduced",
                     "STOP_SOURCE: audit", "FAILED_REASON: reduced run (audit): 2 units uncovered",
                     f"NOTE: {note}", "ACCEPTED_BY: human"):
            h.test_result(f"trail block has the line {want!r}", want in lines, f"lines={lines}")
        h.test_result("trail block has a TS: line", any(line.startswith("TS: ") for line in lines),
                      f"lines={lines}")
        h.test_result("trail block ends with the accepted-end sentinel",
                      ACCEPTED_END in lines, f"lines={lines}")

        flow = make_flow(root, "record-empty-note", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        rc, out, err = record(flow, attempt="1", note="")
        lines = accepted_file(flow).read_text().splitlines() if accepted_file(flow).is_file() else []
        h.test_result("empty stdin writes NOTE: none", rc == 0 and "NOTE: none" in lines,
                      f"rc={rc}, err={err!r}, lines={lines}")

        flow = make_flow(root, "record-two", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        record(flow, attempt="1", note="first")
        before = accepted_file(flow).read_text()
        rc2, _, err2 = record(flow, attempt="2", note="second")
        after = accepted_file(flow).read_text() if accepted_file(flow).is_file() else ""
        h.test_result("second accept appends a block and keeps the first byte-for-byte",
                      rc2 == 0 and after.startswith(before) and after.count(ACCEPTED_END) == 2
                      and after.count("ATTEMPT: ") == 2, f"rc2={rc2}, err={err2!r}, after={after!r}")

        flow = make_flow(root, "record-refuse-after-accept", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        record(flow, attempt="1", note="ok")
        snapshot = accepted_file(flow).read_bytes()
        (flow / "state.json").write_text(json.dumps(state_for("implement", "receipt", "receipt admits deviation")))
        rc, out, err = record(flow, attempt="2", note="should be refused")
        h.test_result("record refuses an ineligible failure with exit 1 and the no line",
                      rc == 1 and "ACCEPT: no stop-source-receipt" in out + err, f"rc={rc}, out={out!r}, err={err!r}")
        h.test_result("refused record leaves the trail file untouched",
                      accepted_file(flow).read_bytes() == snapshot, "trail file changed")

        flow = make_flow(root, "record-refuse-no-file", state_for("implement", "receipt", "reduced run: x"),
                         [failed_line("implement", "reduced run: x")])
        rc, out, err = record(flow, attempt="1", note="nope")
        h.test_result("refused record with no prior trail creates no file",
                      rc == 1 and not accepted_file(flow).exists(), f"rc={rc}, err={err!r}")

        flow = make_flow(root, "record-not-failed", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation"), f"{TS} implement ok"])
        rc, out, err = record(flow, attempt="1", note="nope")
        h.test_result("record re-checks the newest-line rule and refuses a not-failed newest line",
                      rc == 1 and not accepted_file(flow).exists(), f"rc={rc}, out={out!r}, err={err!r}")

        flow = make_flow(root, "record-sentinel", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        rc, out, err = record(flow, attempt="1", note=f"text\n{ACCEPTED_END}\nmore")
        h.test_result("a note containing the accepted-end sentinel is refused and writes nothing",
                      rc != 0 and not accepted_file(flow).exists(), f"rc={rc}, err={err!r}")

        print()
        print("[Section 6] record: input validation (S7)")
        flow = make_flow(root, "record-human-text-eof", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        rc, out, err = record(flow, attempt="1", note="text\nHUMAN_TEXT_EOF")
        h.test_result("a note containing HUMAN_TEXT_EOF is refused",
                      rc == 1 and not accepted_file(flow).exists(), f"rc={rc}, err={err!r}")

        flow = make_flow(root, "record-reason-eof", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        rc, out, err = record(flow, attempt="1", note="text\nREASON_EOF")
        h.test_result("a note containing REASON_EOF is refused",
                      rc == 1 and not accepted_file(flow).exists(), f"rc={rc}, err={err!r}")

        flow = make_flow(root, "record-multiline", state_for("implement", "audit", "reduced run (audit): 2 units uncovered"),
                         [failed_line("implement", "reduced run (audit): 2 units uncovered")])
        rc, out, err = record(flow, attempt="1", note="line1\nline2\nline3")
        lines = accepted_file(flow).read_text().splitlines() if accepted_file(flow).is_file() else []
        h.test_result("a multi-line note is recorded with indentation",
                      rc == 0 and "NOTE:" in lines and "  line1" in lines and "  line2" in lines and "  line3" in lines,
                      f"rc={rc}, lines={lines}")

        flow = make_flow(root, "record-bad-attempt-zero", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        rc, out, err = run(["record", "--flow-dir", str(flow), "--step", "implement", "--nn", NN,
                            "--attempt", "0", "--note-stdin"], stdin="")
        h.test_result("--attempt 0 is rejected as invalid (must be positive)",
                      rc == 2, f"rc={rc}, err={err!r}")

        flow = make_flow(root, "record-bad-attempt-leading-zero", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        rc, out, err = run(["record", "--flow-dir", str(flow), "--step", "implement", "--nn", NN,
                            "--attempt", "01", "--note-stdin"], stdin="")
        h.test_result("--attempt 01 is rejected (no leading zeros)",
                      rc == 2, f"rc={rc}, err={err!r}")

        flow = make_flow(root, "record-bad-nn-hex", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        rc, out, err = run(["eligible", "--flow-dir", str(flow), "--step", "implement", "--nn", "0x03"])
        h.test_result("--nn 0x03 is rejected (digits only)",
                      rc == 2, f"rc={rc}, err={err!r}")

        flow = make_flow(root, "record-non-utf8", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        # Pass invalid UTF-8 as stdin (note: using raw bytes through subprocess)
        result = subprocess.run([sys.executable, str(FLOW_ACCEPT), "record", "--flow-dir", str(flow),
                                "--step", "implement", "--nn", NN, "--attempt", "1", "--note-stdin"],
                               input=b"\xff\xfe not utf-8", capture_output=True, timeout=60)
        h.test_result("non-UTF-8 stdin is rejected with exit 1",
                      result.returncode == 1 and not accepted_file(flow).exists(),
                      f"rc={result.returncode}, stderr={result.stderr!r}")

        print()
        print("[Section 7] Log and state edge cases (S8)")
        # Padded-log test: create a log larger than LOG_READ_CAP_BYTES with the failed line inside the tail
        flow = Path(root) / "padded-log"
        (flow / "steps").mkdir(parents=True, exist_ok=True)
        (flow / "state.json").write_text(json.dumps(state_for("implement", "validation", "receipt admits deviation")))
        # Create a log that is slightly over the cap, with the failed line near the end
        padding = "x" * (1_100_000 - len(failed_line("implement", "receipt admits deviation")))
        log_content = padding + "\n" + failed_line("implement", "receipt admits deviation") + "\n"
        (flow / "log.md").write_text(log_content)
        (flow / "steps" / f"{NN}-implement.md").write_text(OK_RECEIPT)
        rc, out, err = eligible(flow)
        h.test_result("log truncation: failed line inside tail window is found and eligible",
                      rc == 0 and out == "ACCEPT: yes receipt-deviation\n",
                      f"rc={rc}, out={out!r}, err={err!r}")

        flow = make_flow(root, "invalid-json-state", state_for("implement", "validation", "receipt admits deviation"),
                         [failed_line("implement", "receipt admits deviation")])
        (flow / "state.json").write_text("{not valid json}")
        rc, out, err = eligible(flow)
        h.test_result("invalid JSON in state.json exits 2",
                      rc == 2, f"rc={rc}, err={err!r}")

        print()
        print("[Section 6] outstanding set reads an accepted step as taken-over")
        verify = _load_verify_receipt()
        for name, log, want in [
            ("accepted line", f"{TS} implement begin\n{TS} implement accepted (audit-reduced: x)\n", "taken-over"),
            ("a trailing gate line does not hide accepted",
             f"{TS} implement accepted (receipt-reduction: x)\n{TS} implement gate\n", "taken-over"),
            ("hand-finished still reads as taken-over", f"{TS} implement hand-finished\n", "taken-over"),
            ("a later ok supersedes an earlier accepted",
             f"{TS} implement accepted (audit-reduced: x)\n{TS} implement ok\n", "ok"),
            ("accepted for another step is ignored", f"{TS} fix accepted (audit-reduced: x)\n", None),
        ]:
            got = verify._newest_step_status(log, "implement")
            h.test_result(f"_newest_step_status: {name}", got == want, f"got {got!r}, want {want!r}")

        print()
        h.summarize_and_exit()


def _load_verify_receipt():
    import importlib.util
    spec = importlib.util.spec_from_file_location("flow_verify_receipt", REPO_ROOT / "scripts" / "flow-verify-receipt.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec.loader.exec_module(mod)
    return mod


def _source_for(cls):
    """The stop_source that pairs with an allowlisted class in SUFFIX_VARIANTS."""
    return "audit" if cls == "audit-reduced" else "validation"


if __name__ == "__main__":
    main()
