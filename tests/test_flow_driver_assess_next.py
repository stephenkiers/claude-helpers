#!/usr/bin/env python3
"""
Tests for assess.py's v2 stage: the outstanding set from receipts, assessor-record parsing, the batch, the disposition gate,
range/anchor validation, ref checks, and item id quoting in directives.

Run with: python3 tests/test_flow_driver_assess_next.py
"""

import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import Harness  # noqa: E402

from scripts.flow_driver import assess, receipts  # noqa: E402
from scripts.flow_driver.state import AuditRecord, InitSpec, init_state, mutating  # noqa: E402

# v1 flow-assessor reply grammar (#538); a records file ends with the assessment-end sentinel.
ASSESS_SENTINEL = "<!-- assessment-end -->"
RECORD = (
    "{id} | status={status} | kind=decision | scope=in-scope | leave_as_is_safe=yes | cause=c | evidence=e"
    " | finish_plan=f | verification_proof=v | options=Finish it => pause"
)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check(h: Harness, name: str, fn: Callable[[Path], None]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        flow = Path(tmp) / "flow"
        flow.mkdir()
        try:
            fn(flow)
        except AssertionError as exc:
            h.test_result(name, False, str(exc) or "assertion failed")
        except Exception as exc:
            h.test_result(name, False, f"{type(exc).__name__}: {exc}")
        else:
            h.test_result(name, True)


def receipt(flow: Path, step: str, decision: str, outstanding: str) -> None:
    path = receipts.receipt_path(flow, step)  # type: ignore[arg-type]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"ATTEMPT: 1\nDecision: {decision}\n{outstanding}<!-- step-end -->\nbody\n", encoding="utf-8")


def run_next(flow: Path) -> assess.AssessOutcome:
    spec = InitSpec(repo="o/n", issue=1, worktree=str(flow))
    if not (flow / "state.json").exists():
        with mutating(flow, create=spec) as locked:
            init_state(locked, spec)
    with mutating(flow) as locked:
        return assess.assess_next(locked, flow, locked.require_state(), nonce="n0", attempt=1)


def test_set_reads_ok_receipts(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    receipt(flow, "fix", "FAILED", "OUTSTANDING: D1 (never mind) [verifier]\n")
    items, err = assess.get_outstanding_set(flow)
    expect(err is None, str(err))
    expect([i["id"] for i in items] == ["implement:Step 3"], str(items))


def test_set_empty_and_malformed(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: none\n")
    items, err = assess.get_outstanding_set(flow)
    expect(items == [] and err is None, f"{items} {err}")
    receipt(flow, "fix", "OK", "OUTSTANDING: not a valid line\n")
    items, err = assess.get_outstanding_set(flow)
    expect(err is not None, "a malformed OUTSTANDING line must fail closed")


def test_nothing_outstanding_advances(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: none\n")
    expect(isinstance(run_next(flow), assess.AssessAdvance), "no items, no assess")


def test_spawn_then_ask_then_gate(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    out = run_next(flow)
    expect(isinstance(out, assess.AssessSpawn), f"first call spawns: {out}")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 3", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    out = run_next(flow)
    expect(isinstance(out, assess.AssessAsk), f"records land, the batch is asked: {out}")
    expect((flow / "questions" / "assess-1.json").exists(), "question file written")
    expect(isinstance(run_next(flow), assess.AssessAsk), "unanswered stays asked")
    err = assess.save_item_answer(flow, 1, "implement:Step 3", "Leave as-is", None, set())
    expect(err is None, str(err))
    out = run_next(flow)
    expect(isinstance(out, assess.AssessAdvance), f"all answered advances: {out}")
    expect((flow / "steps" / "08-ship.outstanding.md").exists(), "disposition marker written")


def test_bad_records_stop(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 9", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    out = run_next(flow)
    expect(isinstance(out, assess.AssessStop), f"an id outside the set stops: {out}")
    assess.assess_records_path(flow).write_text(ASSESS_SENTINEL + "\n", encoding="utf-8")
    out = run_next(flow)
    expect(isinstance(out, assess.AssessStop) and "no record" in out.reason, f"a missing record stops: {out}")


def test_done_items_ask_nothing(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 3", status="already-done") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    expect(isinstance(run_next(flow), assess.AssessAdvance), "an already-done item needs no answer")


def test_drop_needs_a_note_and_gate_catches_gaps(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 3", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    run_next(flow)
    expect(assess.save_item_answer(flow, 1, "implement:Step 3", "Drop with a reason", None, set()) is not None, "Drop needs a note")
    (flow / "answers").mkdir(exist_ok=True)
    (flow / "answers" / "assess-1.json").write_text(json.dumps({"answers": {}, "annotations": {}}), encoding="utf-8")
    expect(assess.invoke_outstanding_gate(flow, 1) is not None, "an unanswered asked item blocks the gate")


def test_save_item_answer_refuses_invalid_options(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 3", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    run_next(flow)
    err = assess.save_item_answer(flow, 1, "implement:Step 3", "InvalidOption", None, set())
    expect(err is not None, "an invalid option must be refused")
    err = assess.save_item_answer(flow, 1, "unknown:Item", "Finish it", None, set())
    expect(err is not None, "an unknown item must be refused")


def test_assess_disposition_gate_requires_all_answered(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\nOUTSTANDING: Step 4 (more) [verifier]\n")
    assess.assess_records_path(flow).write_text(
        RECORD.format(id="implement:Step 3", status="unfinished") + "\n" +
        RECORD.format(id="implement:Step 4", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n",
        encoding="utf-8"
    )
    run_next(flow)
    (flow / "answers").mkdir(exist_ok=True)
    (flow / "answers" / "assess-1.json").write_text(json.dumps({"answers": {"implement:Step 3": "Finish it"}, "annotations": {}}), encoding="utf-8")
    err = assess.invoke_outstanding_gate(flow, 1)
    expect(err is not None, "gate must fail when not all items are answered")
    (flow / "answers" / "assess-1.json").write_text(json.dumps({"answers": {"implement:Step 3": "Leave as-is", "implement:Step 4": "Leave as-is"}, "annotations": {}}), encoding="utf-8")
    err = assess.invoke_outstanding_gate(flow, 1)
    expect(err is None, "gate must pass when all items are answered")


def test_assess_disposition_gate_fails_on_missing_questions_file(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 3", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    run_next(flow)
    (flow / "answers").mkdir(exist_ok=True)
    (flow / "answers" / "assess-1.json").write_text(json.dumps({"answers": {"implement:Step 3": "Finish it"}, "annotations": {}}), encoding="utf-8")
    (flow / "questions" / "assess-1.json").unlink(missing_ok=True)
    err = assess.invoke_outstanding_gate(flow, 1)
    expect(err is not None, "gate must fail closed when questions file is missing")


def test_check_ref_valid(flow: Path) -> None:
    """_check_ref should return None for valid refs (strings without '-')."""
    result = assess._check_ref("main")
    expect(result is None, f"valid ref 'main' returned {result!r}, expected None")
    result = assess._check_ref("abc123def")
    expect(result is None, f"valid ref 'abc123def' returned {result!r}, expected None")


def test_check_ref_non_string(flow: Path) -> None:
    """_check_ref should reject non-strings."""
    result = assess._check_ref(123)
    expect(result is not None and "must be a string" in result,
           f"non-string ref returned {result!r}, expected error about string type")


def test_check_ref_dash_prefix(flow: Path) -> None:
    """_check_ref should reject refs starting with '-'."""
    result = assess._check_ref("-foo")
    expect(result is not None and "starts with" in result,
           f"ref '-foo' returned {result!r}, expected error about dash")


def test_resolve_anchor_valid(flow: Path) -> None:
    """_resolve_anchor should return None for valid paths inside the worktree."""
    wt = flow / "wt"
    wt.mkdir()
    # Initialize a git repo
    subprocess.run(["git", "-C", str(wt), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init"], check=True, capture_output=True)

    # Path to a file inside the worktree
    test_file = wt / "test.txt"
    test_file.write_text("test", encoding="utf-8")

    result = assess._resolve_anchor(str(wt), "test.txt")
    expect(result is None, f"valid anchor 'test.txt' returned {result!r}, expected None")


def test_resolve_anchor_escaping(flow: Path) -> None:
    """_resolve_anchor should reject paths with '..' or leading '/'."""
    wt = flow / "wt"
    wt.mkdir()
    subprocess.run(["git", "-C", str(wt), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init"], check=True, capture_output=True)

    result = assess._resolve_anchor(str(wt), "../outside")
    expect(result is not None and "escapes" in result,
           f"escaped path returned {result!r}, expected error about escaping")

    result = assess._resolve_anchor(str(wt), "/absolute")
    expect(result is not None and "escapes" in result,
           f"absolute path returned {result!r}, expected error about escaping")


def test_resolve_anchor_toplevel(flow: Path) -> None:
    """_resolve_anchor should accept the toplevel directory itself."""
    wt = flow / "wt"
    wt.mkdir()
    subprocess.run(["git", "-C", str(wt), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init"], check=True, capture_output=True)

    result = assess._resolve_anchor(str(wt), ".")
    expect(result is None, f"toplevel anchor '.' returned {result!r}, expected None")


# --- assess-reply: the one writer of the records file (#538) ---

REPLY = (RECORD.format(id="implement:Step 3", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n").encode("utf-8")


def live_assess_gate(flow: Path, nonce: str = "n1", gate_state: str = "spawned") -> None:
    """A flow whose assess_gate is the assessor spawn with this nonce, in gate_state."""
    spec = InitSpec(repo="o/n", issue=1, worktree=str(flow))
    with mutating(flow, create=spec) as locked:
        init_state(locked, spec)
    with mutating(flow) as locked:
        state = locked.require_state()
        locked.commit(dataclasses.replace(state, assess_gate=AuditRecord("assess", 1, nonce, gate_state)))  # type: ignore[arg-type]


def reply(flow: Path, data: bytes, nonce: str = "n1") -> object:
    with mutating(flow) as locked:
        return assess.assess_reply(locked, flow, nonce, io.BytesIO(data))


def test_assess_reply_live_nonce_writes_records(flow: Path) -> None:
    live_assess_gate(flow)
    out = reply(flow, REPLY)
    expect(isinstance(out, assess.Accepted), f"the live nonce is accepted: {out}")
    expect(assess.assess_records_path(flow).read_bytes() == REPLY, "the records file holds the reply byte for byte")


def test_assess_reply_wrong_or_spent_nonce_is_refused(flow: Path) -> None:
    live_assess_gate(flow)
    out = reply(flow, REPLY, nonce="other")
    expect(isinstance(out, assess.Refused) and "not the live assessor spawn" in out.reason, f"a wrong nonce: {out}")
    expect(not assess.assess_records_path(flow).exists(), "a refused reply writes nothing")
    with mutating(flow) as locked:
        state = locked.require_state()
        locked.commit(dataclasses.replace(state, assess_gate=AuditRecord("assess", 1, "n1", "passed")))  # type: ignore[arg-type]
    out = reply(flow, REPLY)
    expect(isinstance(out, assess.Refused), f"the nonce of a gate no longer spawned is refused: {out}")
    expect(not assess.assess_records_path(flow).exists(), "a refused reply writes nothing")


def test_assess_reply_cap_boundary(flow: Path) -> None:
    live_assess_gate(flow)
    body = RECORD.format(id="implement:Step 3", status="unfinished") + "\n"
    tail = "\n" + ASSESS_SENTINEL + "\n"
    at_cap = (body + " " * (assess.REPLY_CAP - len(body) - len(tail)) + tail).encode("utf-8")
    over_cap = (body + " " * (assess.REPLY_CAP + 1 - len(body) - len(tail)) + tail).encode("utf-8")
    expect(len(at_cap) == assess.REPLY_CAP and len(over_cap) == assess.REPLY_CAP + 1, "fixture sizes")
    out = reply(flow, over_cap)
    expect(isinstance(out, assess.AssessStop) and "256 KiB cap" in out.reason, f"one byte over the cap: {out}")
    expect(not assess.assess_records_path(flow).exists(), "an over-cap reply writes nothing")
    out = reply(flow, at_cap)
    expect(isinstance(out, assess.Accepted), f"a reply of exactly the cap is accepted: {out}")


def test_assess_reply_bad_bytes_fail_closed(flow: Path) -> None:
    live_assess_gate(flow)
    cases = {
        "NUL byte": (REPLY.replace(b"cause=c", b"cause=\x00"), "NUL"),
        "invalid UTF-8": (REPLY.replace(b"cause=c", b"cause=\xff\xfe"), "not UTF-8"),
        "second sentinel": ((ASSESS_SENTINEL + "\n").encode("utf-8") + REPLY, "more than one"),
        "no sentinel": (RECORD.format(id="implement:Step 3", status="unfinished").encode("utf-8") + b"\n", "no trailing"),
    }
    wrong = []
    for name, (data, needle) in cases.items():
        out = reply(flow, data)
        if not (isinstance(out, assess.AssessStop) and needle in out.reason):
            wrong.append(f"{name}: {out}")
        if assess.assess_records_path(flow).exists():
            wrong.append(f"{name}: the records file was written")
    expect(not wrong, "; ".join(wrong))


def test_assess_reply_never_overwrites_or_follows_a_link(flow: Path) -> None:
    live_assess_gate(flow)
    path = assess.assess_records_path(flow)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"earlier")
    out = reply(flow, REPLY)
    expect(isinstance(out, assess.Refused) and "already written" in out.reason, f"a pre-existing file: {out}")
    expect(path.read_bytes() == b"earlier", "a pre-existing file is kept")
    path.unlink()
    dangling = flow / "steps" / "elsewhere.txt"
    os.symlink(dangling, path)
    out = reply(flow, REPLY)
    expect(not isinstance(out, assess.Accepted), f"a symlink inside the flow is not written through: {out}")
    expect(not dangling.exists(), "the symlink target is not created")
    path.unlink()
    outside = flow.parent / "outside.txt"
    outside.write_bytes(b"outside")
    os.symlink(outside, path)
    out = reply(flow, REPLY)
    expect(not isinstance(out, assess.Accepted), f"a symlink out of the flow is not written through: {out}")
    expect(outside.read_bytes() == b"outside", "the outside target is unchanged")


def test_assess_reply_duplicate_is_refused(flow: Path) -> None:
    """D4: a second delivery for the same spawn is refused and the first records are kept."""
    live_assess_gate(flow)
    expect(isinstance(reply(flow, REPLY), assess.Accepted), "the first reply is accepted")
    second = REPLY.replace(b"cause=c", b"cause=second")
    out = reply(flow, second)
    expect(isinstance(out, assess.Refused) and "already written" in out.reason, f"a duplicate reply: {out}")
    expect(assess.assess_records_path(flow).read_bytes() == REPLY, "the first reply's records are kept")


# --- option routes (#538) ---

ROUTED = RECORD.replace("options=Finish it => pause", "options=Finish it => pause ;; Split it => issue")


def routed_batch(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (a) [self-report]\nOUTSTANDING: Step 4 (b) [self-report]\n")
    assess.assess_records_path(flow).write_text(
        ROUTED.format(id="implement:Step 3", status="unfinished") + "\n"
        + ROUTED.format(id="implement:Step 4", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    expect(isinstance(run_next(flow), assess.AssessAsk), "the batch is asked")


def test_option_routes_are_recorded(flow: Path) -> None:
    routed_batch(flow)
    questions = json.loads((flow / "questions" / "assess-1.json").read_text(encoding="utf-8"))["questions"]
    expect(all(q["option_routes"] == {"Finish it": "pause", "Split it": "issue"} for q in questions),
           f"each question stores its assessor routes: {questions}")
    expect(assess.save_item_answer(flow, 1, "implement:Step 3", "Finish it", None, {"implement:Step 4"}) is None, "answer 1")
    expect(assess.save_item_answer(flow, 1, "implement:Step 4", "Split it", None, set()) is None, "answer 2")
    per_item = json.loads((flow / "answers" / "assess-1.d" / "implement:Step 3.json").read_text(encoding="utf-8"))
    expect(per_item.get("route") == "pause", f"the per-item answer records the route: {per_item}")
    expect(isinstance(run_next(flow), assess.AssessAdvance), "all answered advances")
    notes = json.loads((flow / "answers" / "assess-1.json").read_text(encoding="utf-8"))["annotations"]
    expect(notes["implement:Step 3"] == {"route": "pause", "disposition": assess.DISPOSITION_PAUSE}, f"pause: {notes}")
    expect(notes["implement:Step 4"] == {"route": "issue", "disposition": assess.DISPOSITION_ISSUE}, f"issue: {notes}")
    expect(assess.invoke_outstanding_gate(flow, 1) is None, "the untampered gate passes")


def test_gate_checks_the_recorded_route(flow: Path) -> None:
    routed_batch(flow)
    assess.save_item_answer(flow, 1, "implement:Step 3", "Finish it", None, {"implement:Step 4"})
    assess.save_item_answer(flow, 1, "implement:Step 4", "Split it", None, set())
    expect(isinstance(run_next(flow), assess.AssessAdvance), "all answered advances")
    answers_path = flow / "answers" / "assess-1.json"
    questions_path = flow / "questions" / "assess-1.json"
    good_answers = answers_path.read_text(encoding="utf-8")
    good_questions = questions_path.read_text(encoding="utf-8")

    def tamper_note(field: str, value: str) -> None:
        data = json.loads(good_answers)
        data["annotations"]["implement:Step 3"][field] = value
        answers_path.write_text(json.dumps(data), encoding="utf-8")

    tamper_note("route", "issue")
    err = assess.invoke_outstanding_gate(flow, 1)
    expect(err is not None and "no matching route record" in err, f"pause answered, issue recorded: {err}")
    tamper_note("disposition", assess.DISPOSITION_ISSUE)
    err = assess.invoke_outstanding_gate(flow, 1)
    expect(err is not None and "no matching disposition" in err, f"pause route with an issue disposition: {err}")
    answers_path.write_text(good_answers, encoding="utf-8")
    data = json.loads(good_questions)
    data["questions"][0]["option_routes"]["Finish it"] = "keep"
    questions_path.write_text(json.dumps(data), encoding="utf-8")
    err = assess.invoke_outstanding_gate(flow, 1)
    expect(err is not None and "has route" in err, f"a stored route outside pause/issue: {err}")
    questions_path.write_text(good_questions, encoding="utf-8")
    expect(assess.invoke_outstanding_gate(flow, 1) is None, "restored files pass")


# --- sanitising what the human sees (#538) ---

def test_cause_is_cleaned_and_cut(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (a) [self-report]\n")
    cause = "\x1b[31mred\x1b[0m\x07bell " + "x" * 2500
    assess.assess_records_path(flow).write_text(
        RECORD.replace("cause=c", f"cause={cause}").format(id="implement:Step 3", status="unfinished") + "\n"
        + ASSESS_SENTINEL + "\n", encoding="utf-8")
    expect(isinstance(run_next(flow), assess.AssessAsk), "the batch is asked")
    summary = json.loads((flow / "questions" / "assess-1.json").read_text(encoding="utf-8"))["questions"][0]["summary"]
    shown = summary.split(": ", 1)[1]
    expect("\x1b" not in summary and "\x07" not in summary, f"escapes and controls stripped: {summary[:60]!r}")
    expect(shown.startswith("redbell x"), f"the cleaned text is kept: {shown[:40]!r}")
    expect(shown.endswith(assess.REASON_TRUNCATED) and len(shown) == assess.REASON_MAX + len(assess.REASON_TRUNCATED),
           f"cut at {assess.REASON_MAX} with the marker: len {len(shown)}")
    report = (flow / "steps" / "07-assess.md").read_text(encoding="utf-8")
    cause_line = next(line for line in report.splitlines() if line.startswith("Cause: "))
    expect("\x1b" not in cause_line and cause_line.endswith(assess.REASON_TRUNCATED), "the report's Cause line is cleaned and cut")


def test_cause_at_the_limit_is_not_cut(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (a) [self-report]\n")
    cause = "y" * assess.REASON_MAX
    assess.assess_records_path(flow).write_text(
        RECORD.replace("cause=c", f"cause={cause}").format(id="implement:Step 3", status="unfinished") + "\n"
        + ASSESS_SENTINEL + "\n", encoding="utf-8")
    run_next(flow)
    summary = json.loads((flow / "questions" / "assess-1.json").read_text(encoding="utf-8"))["questions"][0]["summary"]
    expect(summary.endswith(": " + cause), f"a cause of exactly {assess.REASON_MAX} characters is shown whole")


TESTS = [
    ("1: the set reads OK receipts only", test_set_reads_ok_receipts),
    ("2: empty set and malformed line", test_set_empty_and_malformed),
    ("3: nothing outstanding advances", test_nothing_outstanding_advances),
    ("4: spawn, ask, answer, gate", test_spawn_then_ask_then_gate),
    ("5: bad assessor records stop", test_bad_records_stop),
    ("6: done items ask nothing", test_done_items_ask_nothing),
    ("7: Drop note and gate gaps", test_drop_needs_a_note_and_gate_catches_gaps),
    ("8: save_item_answer refuses invalid options", test_save_item_answer_refuses_invalid_options),
    ("9: assess disposition gate requires all answered", test_assess_disposition_gate_requires_all_answered),
    ("10: assess disposition gate fails on missing questions file", test_assess_disposition_gate_fails_on_missing_questions_file),
    ("11: _check_ref accepts valid ref", test_check_ref_valid),
    ("12: _check_ref rejects non-string", test_check_ref_non_string),
    ("13: _check_ref rejects dash-prefix", test_check_ref_dash_prefix),
    ("14: _resolve_anchor accepts valid path", test_resolve_anchor_valid),
    ("15: _resolve_anchor rejects escaping", test_resolve_anchor_escaping),
    ("16: _resolve_anchor accepts toplevel", test_resolve_anchor_toplevel),
    ("17: assess-reply with the live nonce writes the records", test_assess_reply_live_nonce_writes_records),
    ("18: assess-reply refuses a wrong or spent nonce", test_assess_reply_wrong_or_spent_nonce_is_refused),
    ("19: assess-reply 256 KiB cap boundary", test_assess_reply_cap_boundary),
    ("20: assess-reply NUL, invalid UTF-8 and sentinel errors fail closed", test_assess_reply_bad_bytes_fail_closed),
    ("21: assess-reply never overwrites a file or writes through a symlink", test_assess_reply_never_overwrites_or_follows_a_link),
    ("22: D4 a duplicate assess-reply is refused", test_assess_reply_duplicate_is_refused),
    ("23: pause and issue routes are recorded with their dispositions", test_option_routes_are_recorded),
    ("24: the gate checks the recorded route", test_gate_checks_the_recorded_route),
    ("25: a cause is cleaned of ANSI and controls and cut at REASON_MAX", test_cause_is_cleaned_and_cut),
    ("26: a cause of exactly REASON_MAX is not cut", test_cause_at_the_limit_is_not_cut),
]


def main() -> None:
    h = Harness("FLOW DRIVER ASSESS NEXT")
    for name, fn in TESTS:
        check(h, name, fn)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
