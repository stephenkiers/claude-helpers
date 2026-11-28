#!/usr/bin/env python3
"""Tests for scripts/flow3/steps/plan.py. No real model is called: the fake claude binary answers."""

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="flow3-test-")).resolve()
os.environ["FLOW3_HOME"] = str(TMP / "home")
os.environ["HOME"] = str(TMP / "userhome")
os.environ["FLOW3_CLAUDE_BIN"] = str(ROOT / "tests" / "flow3_fake_claude.py")
SCRIPT = TMP / "fake-script.json"
LOG = TMP / "fake-log.jsonl"
os.environ["FLOW3_FAKE_SCRIPT"] = str(SCRIPT)
os.environ["FLOW3_FAKE_LOG"] = str(LOG)

from _test_harness import Harness  # noqa: E402

from scripts.flow3 import context, heal, questions, repoconfig, runner  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.steps import plan, worktree  # noqa: E402
from scripts.flow3.types import FlowStop  # noqa: E402

heal._sleep = lambda s: None  # type: ignore[assignment]
runner._sleep = lambda s: None  # type: ignore[assignment]


def sh(args, cwd):
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


_COUNTER = [200]


def make_ctx():
    """A sandbox repo with principles, a local-provider issue, a flow, and a ready worktree."""
    _COUNTER[0] += 1
    issue = _COUNTER[0]
    origin = TMP / ("o%d.git" % issue)
    clone = TMP / ("c%d" % issue)
    sh(["git", "init", "--bare", "-q", "-b", "main", str(origin)], TMP)
    sh(["git", "clone", "-q", str(origin), str(clone)], TMP)
    sh(["git", "config", "user.name", "t"], clone)
    sh(["git", "config", "user.email", "t@example.invalid"], clone)
    (clone / ".claude").mkdir()
    (clone / ".claude" / "flow.yaml").write_text("provider: local\ncheck: {full: 'true'}\n")
    (clone / "README.md").write_text("hi\n")
    (clone / "docs").mkdir()
    (clone / "docs" / "principles.md").write_text(
        "# Goal\nA small tool.\n\n## P-x: Reuse\nStatement: Reuse the helper.\nSpirit: One place to fix.\n"
        "Revisit if: The helper does not fit.\n")
    sh(["git", "add", "-A"], clone)
    sh(["git", "commit", "-q", "-m", "init"], clone)
    sh(["git", "push", "-q", "origin", "main"], clone)
    issues = origin / "flow3" / "issues"
    issues.mkdir(parents=True)
    (issues / ("%d.md" % issue)).write_text("# Plan ticket\n\n\nAdd a thing to src/mod.py.\n")
    flow = FlowDir.create(repoconfig.repo_name(clone), issue, "Plan ticket", clone)
    ctx = context.build(flow)
    worktree.run(ctx)
    return ctx


def script(calls, default=None):
    for p in (SCRIPT, Path(str(SCRIPT) + ".consumed"), LOG):
        if p.exists():
            p.unlink()
    doc = {"calls": calls}
    if default is not None:
        doc["default"] = default
    SCRIPT.write_text(json.dumps(doc))


def invocations(role=None):
    if not LOG.exists():
        return []
    rows = [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]
    return [r for r in rows if role is None or r["role"] == role]


NICK = "North Star Nick"
PICKED = ["Rachel", "Tara TypeSafe"]


def router(names=None):
    return {"role": "plan_router", "output": {"experts": names if names is not None else PICKED}}


def experts(names=None):
    return [{"role": "plan_expert", "label": n, "output": {"advice": ["ADVICE-" + n]}}
            for n in (names or [NICK] + PICKED)]


def good_plan():
    return {
        "goal": "Add the thing",
        "non_goals": [],
        "acceptance_criteria": [{"id": "c1", "text": "The thing works"}, {"id": "c2", "text": "The thing is tested"}],
        "units": [
            {"id": "u1", "title": "Core", "files": ["src/mod.py"], "steps": ["write it"], "acceptance": ["c1"]},
            {"id": "u2", "title": "Tests", "files": ["tests/test_mod.py"], "steps": ["test it"],
             "acceptance": ["c2"], "after": ["u1"]},
        ],
        "interfaces": [],
        "test_spec": [{"id": "t1", "behaviour": "it works", "test_file": "tests/test_mod.py"}],
    }


def synth_done(plan_doc=None):
    return {"role": "plan_synthesis", "output": {"status": "done", "plan": plan_doc or good_plan()}}


def synth_asks(*qs):
    return {"role": "plan_synthesis", "output": {"status": "needs_answers", "questions": list(qs)}}


AUDIT_PASS = {"role": "plan_auditor", "output": {"verdict": "pass"}}


def q(qid, kind="architecture", rec="a", **extra):
    doc = {"id": qid, "question": "Question %s?" % qid, "context": "While planning src/mod.py the step hit %s." % qid,
           "options": [{"id": "a", "label": "Option A", "consequence": "A changes the module"},
                       {"id": "b", "label": "Option B", "consequence": "B adds a file"}],
           "recommended": rec, "kind": kind, "reversible": True, "themes": ["reuse"]}
    doc.update(extra)
    return doc


def verdict(qid, outcome="none"):
    return {"role": "resolver", "label": qid, "output": {"results": [
        {"question_id": qid, "outcome": outcome, "principle_id": "", "option_id": "", "reason": "n/a"}]}}


def stop_of(ctx):
    try:
        plan.run(ctx)
    except FlowStop as exc:
        return exc
    return None


h = Harness("Flow v3 plan stage")

# ---- validate_plan: each rule passes and fails -------------------------------------------------
ok = good_plan()
h.test_result("validate_plan: a good plan has no errors", plan.validate_plan(ok) == [], repr(plan.validate_plan(ok)))


def broken(mutate):
    p = copy.deepcopy(good_plan())
    mutate(p)
    return plan.validate_plan(p)


def has(errors, text):
    return any(text in e for e in errors)


e = broken(lambda p: p["units"].append(dict(p["units"][0], title="again")))
h.test_result("validate_plan: duplicate unit ids", has(e, "more than once"), repr(e))
e = broken(lambda p: p["units"][0].update(files=[" "]))
h.test_result("validate_plan: a unit with no files", has(e, "names no files"), repr(e))
e = broken(lambda p: p["units"][1].update(after=["u9"]))
h.test_result("validate_plan: after names a missing unit", has(e, "does not exist"), repr(e))
e = broken(lambda p: p["units"][1].update(after=["u2"]))
h.test_result("validate_plan: a unit after itself", has(e, "after itself"), repr(e))
e = broken(lambda p: p["units"][0].update(after=["u2"]))
h.test_result("validate_plan: a cycle", has(e, "cycle"), repr(e))
e = broken(lambda p: p["units"][0].update(acceptance=["c1", "c9"]))
h.test_result("validate_plan: an unknown acceptance criterion", has(e, "c9"), repr(e))
e = broken(lambda p: p["units"][1].update(files=["src/mod.py"], after=[]))
h.test_result("validate_plan: shared files with no order", has(e, "neither is after the other"), repr(e))
e = broken(lambda p: p["units"][1].update(files=["src/mod.py"]))
h.test_result("validate_plan: shared files are fine when ordered", e == [], repr(e))
p3 = good_plan()
p3["units"].append({"id": "u3", "title": "More", "files": ["src/mod.py"], "steps": ["s"], "acceptance": ["c1"], "after": ["u2"]})
h.test_result("validate_plan: shared files ordered transitively are fine", plan.validate_plan(p3) == [], repr(plan.validate_plan(p3)))
e = broken(lambda p: p["units"][1].update(acceptance=["c1"]))
h.test_result("validate_plan: a criterion no unit lists", has(e, "c2") and has(e, "not listed"), repr(e))
e = broken(lambda p: p.update(test_spec=[]))
h.test_result("validate_plan: no test_spec and no reason", has(e, "untestable_reason"), repr(e))
e = broken(lambda p: p.update(test_spec=[], untestable_reason="pure documentation"))
h.test_result("validate_plan: an untestable reason excuses an empty test_spec", e == [], repr(e))

# ---- seating -----------------------------------------------------------------------------------
ctx = make_ctx()
script([router(["Rachel"]), synth_done(), AUDIT_PASS] , default={"output": {"advice": ["x"]}})
plan.run(ctx)
chosen = ctx.flow.data("plan")["chosen"]
h.test_result("Nick is always seated, and a thin router answer is topped up to three",
              chosen[0] == NICK and len(chosen) == 3 and len(set(chosen)) == 3, repr(chosen))

ctx = make_ctx()
many = ["Rachel", "Tara TypeSafe", "Sam System", "Mozart", "Ariadne", "Uncle Bob", "Security Sage", NICK]
script([router(many), synth_done(), AUDIT_PASS], default={"output": {"advice": ["x"]}})
plan.run(ctx)
chosen = ctx.flow.data("plan")["chosen"]
h.test_result("the expert count is clamped to five, Nick first", len(chosen) == 5 and chosen[0] == NICK
              and len(invocations("plan_expert")) == 5, repr(chosen))

ctx = make_ctx()
script([router(["Nobody Real", "Data Scientist Dana"]), synth_done(), AUDIT_PASS], default={"output": {"advice": ["x"]}})
plan.run(ctx)
chosen = ctx.flow.data("plan")["chosen"]
h.test_result("unknown and named-only experts are ignored; the panel is still three",
              len(chosen) == 3 and "Data Scientist Dana" not in chosen and NICK in chosen, repr(chosen))

# ---- happy path: isolation, record, re-entry ---------------------------------------------------------
ctx = make_ctx()
script([router()] + experts() + [synth_done(), AUDIT_PASS])
res = plan.run(ctx)
d = ctx.flow.data("plan")
h.test_result("happy path: done, with data.plan.plan", res.kind == "done" and d["plan"] == good_plan()
              and d["phase"] == "done", repr(res))
h.test_result("happy path: a step record and a journal line",
              len(list((ctx.flow.path / "steps").glob("*-plan.md"))) == 1
              and "Plan written" in (ctx.flow.path / "journal.md").read_text())
record_text = next((ctx.flow.path / "steps").glob("*-plan.md")).read_text()
h.test_result("the record shows the units, the advice and the audit",
              "u1" in record_text and "ADVICE-Rachel" in record_text and "Verdict: pass" in record_text)
exp_calls = invocations("plan_expert")
h.test_result("experts ran isolated: no expert prompt holds another's advice",
              len(exp_calls) == 3 and all("ADVICE-" not in c["prompt"] for c in exp_calls)
              and all("--resume" not in c["argv"] for c in exp_calls))
syn = invocations("plan_synthesis")
h.test_result("synthesis saw every expert's advice, once",
              len(syn) == 1 and all(("ADVICE-" + n) in syn[0]["prompt"] for n in [NICK] + PICKED))
h.test_result("one router, one synthesis, one audit",
              len(invocations("plan_router")) == 1 and len(invocations("plan_auditor")) == 1)
before = len(invocations())
again = plan.run(ctx)
h.test_result("re-entry after done makes no calls", again.kind == "done" and len(invocations()) == before)

# Re-entry after the experts finished but the synthesis crashed does not repeat the experts.
ctx = make_ctx()
script([router()] + experts() + [{"role": "plan_synthesis", "mode": "api_error", "status": 404}])
exc = stop_of(ctx)
h.test_result("a synthesis failure stops", exc is not None, repr(exc))
n_exp = len(invocations("plan_expert"))
script([synth_done(), AUDIT_PASS])
res = plan.run(ctx)
h.test_result("re-entry after a stop repeats no router or expert call",
              res.kind == "done" and n_exp == 3 and invocations("plan_expert") == [] and invocations("plan_router") == [])

# ---- auto-answered questions resume the synthesis session ------------------------------------------
ctx = make_ctx()
script([router()] + experts() + [
    synth_asks(q("f1", "fact")),
    {"role": "fact_scout", "label": "f1", "output": {"answers": [
        {"question_id": "f1", "found": True, "option_id": "b", "answer": "B", "evidence": "README.md line 1"}]}},
    synth_done(), AUDIT_PASS])
res = plan.run(ctx)
syn = invocations("plan_synthesis")
h.test_result("an auto-answered fact does not park and resumes the synthesis session",
              res.kind == "done" and len(syn) == 2 and "--resume" in syn[1]["argv"] and "--resume" not in syn[0]["argv"]
              and questions.open_batch(ctx.flow) is None)
h.test_result("the resume prompt carries the answer", "Option B" in syn[1]["prompt"] or "README.md" in syn[1]["prompt"])
h.test_result("the record lists the question and who decided it",
              "f1" in next((ctx.flow.path / "steps").glob("*-plan.md")).read_text())

ctx = make_ctx()
script([router()] + experts() + [
    synth_asks(q("d1", "detail")), verdict("d1"), synth_done(), AUDIT_PASS])
res = plan.run(ctx)
h.test_result("an assumed detail does not park", res.kind == "done" and len(invocations("plan_synthesis")) == 2
              and questions.open_batch(ctx.flow) is None)

# ---- a human question parks, then resumes without redoing experts -----------------------------------
ctx = make_ctx()
script([router()] + experts() + [synth_asks(q("h1", "architecture")), verdict("h1")])
res = plan.run(ctx)
batch = questions.open_batch(ctx.flow)
h.test_result("a human-kind question parks with an open batch",
              res.kind == "needs_human" and batch is not None and [c["id"] for c in batch["cards"]] == ["h1"])
n_all = len(invocations())
res = plan.run(ctx)
h.test_result("re-entering while the card is open parks again and calls nothing",
              res.kind == "needs_human" and len(invocations()) == n_all)
questions.apply_reply(ctx, "h1: b")
script([synth_done(), AUDIT_PASS])
res = plan.run(ctx)
syn = invocations("plan_synthesis")
h.test_result("after the reply the plan finishes without re-running experts or the router",
              res.kind == "done" and invocations("plan_expert") == [] and invocations("plan_router") == []
              and len(syn) == 1 and "--resume" in syn[0]["argv"])
h.test_result("the human's answer reached the synthesis", "Option B" in syn[0]["prompt"] or "Chosen option: b" in syn[0]["prompt"])
h.test_result("the batch was consumed", questions.open_batch(ctx.flow) is None)

# ---- audit gaps go back to synthesis exactly once ---------------------------------------------------------
ctx = make_ctx()
script([router()] + experts() + [
    synth_done(),
    {"role": "plan_auditor", "output": {"verdict": "gaps", "gaps": [{"text": "No rollback step", "kind": "plan_gap"}]}},
    synth_done(), AUDIT_PASS])
res = plan.run(ctx)
syn = invocations("plan_synthesis")
h.test_result("audit gaps go back to synthesis once, and the audit does not run again",
              res.kind == "done" and len(syn) == 2 and "--resume" in syn[1]["argv"]
              and "No rollback step" in syn[1]["prompt"] and len(invocations("plan_auditor")) == 1)
h.test_result("the record notes the audit gap", "No rollback step" in next((ctx.flow.path / "steps").glob("*-plan.md")).read_text())

# An unasked-question gap with its card is resolved, and the answer goes back to the synthesis.
ctx = make_ctx()
script([router()] + experts() + [
    synth_done(),
    {"role": "plan_auditor", "output": {"verdict": "gaps", "gaps": [{"text": "Nobody chose A or B", "kind": "unasked_question"}],
                                        "questions": [q("g1", "detail")]}},
    verdict("g1"), synth_done(), AUDIT_PASS])
res = plan.run(ctx)
syn = invocations("plan_synthesis")
h.test_result("an audit question is resolved and its answer goes back to synthesis",
              res.kind == "done" and len(syn) == 2 and "g1" in syn[1]["prompt"])

# The auditor is shown the experts' questions (not their advice) so a silently settled one is caught.
ctx = make_ctx()
exps = experts()
exps[0]["output"]["questions"] = [dict(q("x1", "behaviour"), question="RAISED-BY-EXPERT?")]
script([router()] + exps + [synth_done(), AUDIT_PASS])
res = plan.run(ctx)
aud = invocations("plan_auditor")
h.test_result("the auditor sees the experts' questions but not their advice",
              res.kind == "done" and len(aud) == 1 and "RAISED-BY-EXPERT?" in aud[0]["prompt"]
              and "ADVICE-" not in aud[0]["prompt"])

# ---- an invalid plan is sent back once, then stops ----------------------------------------------------------
bad = good_plan()
bad["acceptance_criteria"].append({"id": "c3", "text": "Never covered"})
ctx = make_ctx()
script([router()] + experts() + [synth_done(bad), AUDIT_PASS, synth_done()])
res = plan.run(ctx)
syn = invocations("plan_synthesis")
h.test_result("an invalid plan is sent back once and then passes",
              res.kind == "done" and len(syn) == 2 and "c3" in syn[1]["prompt"] and "--resume" in syn[1]["argv"])

ctx = make_ctx()
script([router()] + experts() + [synth_done(bad), AUDIT_PASS, synth_done(bad)])
exc = stop_of(ctx)
h.test_result("a plan still invalid after one revision stops (runner)",
              exc is not None and exc.cls == "runner" and "c3" in exc.reason and len(invocations("plan_synthesis")) == 2, repr(exc))
h.test_result("the invalid plan is not recorded as done", ctx.flow.data("plan").get("phase") != "done")

# ---- synthesis protocol errors ------------------------------------------------------------------------------
ctx = make_ctx()
script([router()] + experts() + [{"role": "plan_synthesis", "output": {"status": "needs_answers"}}])
exc = stop_of(ctx)
h.test_result("needs_answers with no questions is a runner stop", exc is not None and exc.cls == "runner", repr(exc))

ctx = make_ctx()
script([router()] + experts() + [{"role": "plan_synthesis", "output": {"status": "done"}}])
exc = stop_of(ctx)
h.test_result("done with no plan is a runner stop", exc is not None and exc.cls == "runner", repr(exc))

# ---- unasked_question gaps without cards: synthesis done vs needs_answers -------------------------
# When auditor returns unasked_question gap with no cards, and synthesis returns done (not needs_answers),
# the plan stage returns needs_human and the plan is not accepted.
ctx = make_ctx()
script([router()] + experts() + [
    synth_done(),
    {"role": "plan_auditor", "output": {"verdict": "gaps", "gaps": [{"text": "Need to decide between X and Y", "kind": "unasked_question"}]}},
    synth_done(), AUDIT_PASS])
res = plan.run(ctx)
h.test_result("unasked_question gap without cards, synthesis done: returns needs_human",
              res.kind == "needs_human")
plan_data = ctx.flow.data("plan")
h.test_result("the plan is not accepted (phase is not done)",
              plan_data.get("phase") != "done")

# When auditor returns unasked_question gap with no cards, but synthesis returns needs_answers with question,
# the normal question path works (doesn't crash, processes the questions).
ctx = make_ctx()
q_unasked = q("uq1", "architecture", rec="a")
script([router()] + experts() + [
    synth_done(),
    {"role": "plan_auditor", "output": {"verdict": "gaps", "gaps": [{"text": "Need to decide between X and Y", "kind": "unasked_question"}]}},
    synth_asks(q_unasked)])
res = plan.run(ctx)
# When synthesis returns needs_answers with questions, the step should return needs_human (waiting for answers)
# not needs_human from the unasked_question path (which only happens if status != needs_answers).
h.test_result("unasked_question gap without cards, synthesis needs_answers with question: parks for question",
              res.kind == "needs_human")
batch = questions.open_batch(ctx.flow)
h.test_result("the question is in an open batch",
              batch is not None and len(batch.get("cards") or []) > 0 and batch["cards"][0].get("id") == "uq1")

# ---- settled rendering: "Decisions made for you" section -----------------------------------------------
# A plan with settled renders the section correctly.
plan_with_settled = good_plan()
plan_with_settled["settled"] = [
    {"question": "Should we use X or Y?", "chosen": "X", "basis": "simplicity", "evidence": "RFC-123"},
    {"question": "Should we add Z?", "chosen": "yes", "basis": "principle", "evidence": "P-reuse"}
]
sections = plan._render_plan(plan_with_settled)
section_dict = {name: text for name, text in sections}
h.test_result("settled items render as 'Decisions made for you' section",
              "Decisions made for you" in section_dict and
              "Should we use X or Y?" in section_dict.get("Decisions made for you", "") and
              "Chose X" in section_dict.get("Decisions made for you", ""))

# Malformed settled (not a list) renders nothing.
plan_malformed_settled_str = good_plan()
plan_malformed_settled_str["settled"] = "not a list"
sections_str = plan._render_plan(plan_malformed_settled_str)
section_dict_str = {name: text for name, text in sections_str}
h.test_result("malformed settled (string) does not crash and renders nothing",
              "Decisions made for you" not in section_dict_str)

# Malformed settled (list of non-dicts) renders nothing.
plan_malformed_settled_list = good_plan()
plan_malformed_settled_list["settled"] = ["not a dict", 123, None]
sections_list = plan._render_plan(plan_malformed_settled_list)
section_dict_list = {name: text for name, text in sections_list}
h.test_result("malformed settled (non-dict items) does not crash and renders nothing",
              "Decisions made for you" not in section_dict_list)

# Malformed settled (dict without question) is skipped.
plan_incomplete_settled = good_plan()
plan_incomplete_settled["settled"] = [
    {"chosen": "X", "basis": "simplicity"},  # missing question
    {"question": "Valid one", "chosen": "Y", "basis": "test", "evidence": ""}
]
sections_incomplete = plan._render_plan(plan_incomplete_settled)
section_dict_incomplete = {name: text for name, text in sections_incomplete}
h.test_result("settled items without question are skipped",
              "Decisions made for you" in section_dict_incomplete and
              "Valid one" in section_dict_incomplete.get("Decisions made for you", "") and
              "X" not in section_dict_incomplete.get("Decisions made for you", ""))

# ---- _raised_text: render "?" for missing label or consequence ----------
# Options with missing label or consequence render as "?".
test_advice = {
    "rachel": {
        "advice": ["advice1"],
        "questions": [
            q("opt1", "behaviour", rec="a"),  # normal question with label and consequence
            {
                "id": "opt2", "question": "About features?", "kind": "architecture",
                "options": [
                    {"id": "a", "consequence": "Consequence A"},  # missing label
                    {"id": "b", "label": "Option B"}  # missing consequence
                ],
                "recommended": "a"
            }
        ]
    }
}
d_test = {
    "chosen": ["rachel"],
    "advice": test_advice
}
raised = plan._raised_text(d_test)
h.test_result("_raised_text renders '?' for missing label",
              "? (Consequence A)" in raised)
h.test_result("_raised_text renders '?' for missing consequence",
              "Option B (?)" in raised)

h.summarize_and_exit()
