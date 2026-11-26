#!/usr/bin/env python3
"""Tests for scripts/flow3/questions.py. No real model is called: the fake claude binary answers."""

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

from scripts.flow3 import context, principles, questions, record, repoconfig, schemas, voice  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.types import FlowStop  # noqa: E402


def sh(args, cwd):
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


_COUNTER = [0]


def make_ctx(with_principles=True, title="Questions ticket"):
    """A sandbox repo (bare origin + clone), a local-provider issue, a flow, and its Ctx."""
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
    if with_principles:
        (clone / "docs").mkdir()
        (clone / "docs" / "principles.md").write_text(
            "# Goal\nA small tool.\n\n## P-x: Reuse\nStatement: Reuse the helper.\nSpirit: One place to fix.\n"
            "Revisit if: The helper does not fit.\n")
    sh(["git", "add", "-A"], clone)
    sh(["git", "commit", "-q", "-m", "init"], clone)
    sh(["git", "push", "-q", "origin", "main"], clone)
    issues = origin / "flow3" / "issues"
    issues.mkdir(parents=True)
    (issues / ("%d.md" % issue)).write_text("# %s\n\n\nThe first real line of the body.\nMore.\n" % title)
    flow = FlowDir.create(repoconfig.repo_name(clone), issue, title, clone)
    return context.build(flow)


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


def q(qid, kind="architecture", rec="a", **extra):
    doc = {"id": qid, "question": "Question %s?" % qid, "context": "While editing src/mod.py the step hit %s." % qid,
           "options": [{"id": "a", "label": "Option A", "consequence": "A changes the module"},
                       {"id": "b", "label": "Option B", "consequence": "B adds a file"}],
           "recommended": rec, "kind": kind, "reversible": True, "themes": ["reuse"]}
    doc.update(extra)
    return doc


def verdict(qid, outcome, pid="", option="", reason="because"):
    return {"results": [{"question_id": qid, "outcome": outcome, "principle_id": pid, "option_id": option, "reason": reason}]}


def decision_rows(ctx):
    return principles.read_decisions(ctx.repo)


h = Harness("Flow v3 questions")

# ---- resolver order (PLAN section 12, "Questions") --------------------------------------------

# A fact question is answered by the scout and never reaches the human.
ctx = make_ctx()
script([{"role": "fact_scout", "label": "f1", "output": {"answers": [
    {"question_id": "f1", "found": True, "option_id": "b", "answer": "B", "evidence": "README.md line 1"}]}}])
auto, human = questions.resolve(ctx, "plan", [q("f1", "fact")])
a = auto.get("f1")
h.test_result("fact question is answered by the scout", a is not None and a.source == "fact" and a.option_id == "b"
              and a.text == "Option B" and "README.md" in a.reason and human == [], repr(auto))
h.test_result("fact answer: no batch, no resolver call, read-only tools, cwd is the repo",
              questions.open_batch(ctx.flow) is None and not (ctx.flow.path / "questions.json").exists()
              and invocations("resolver") == []
              and invocations("fact_scout")[0]["argv"][invocations("fact_scout")[0]["argv"].index("--tools") + 1] == "Read,Grep,Glob"
              and os.path.realpath(invocations("fact_scout")[0]["cwd"]) == os.path.realpath(str(ctx.repo_root)))
rows = decision_rows(ctx)
h.test_result("auto answer: decision file, decisions.jsonl row and journal line",
              (ctx.flow.path / "decisions" / "f1.json").exists() and len(rows) == 1 and rows[0]["source"] == "fact"
              and rows[0]["id"] == "f1" and rows[0]["answer"] == "b" and rows[0]["overruled"] is False
              and rows[0]["themes"] == ["reuse"] and rows[0]["flow"] == ctx.flow.name and rows[0]["issue"] == ctx.issue
              and (ctx.flow.path / "journal.md").read_text().count("Question f1") == 1)
why = record.why(ctx.flow, "f1")
h.test_result("record.why works for an auto answer", "Decided by: fact" in why and "README.md" in why, why)

# ... even when the scout cannot decide.
ctx = make_ctx()
script([{"role": "fact_scout", "label": "f2", "output": {"answers": [{"question_id": "f2", "found": False}]}}])
auto, human = questions.resolve(ctx, "plan", [q("f2", "fact", rec="b")])
a = auto.get("f2")
h.test_result("fact the scout cannot decide takes the recommendation, never asks", a is not None and a.source == "assumed"
              and a.option_id == "b" and "could not be checked" in a.reason and human == []
              and questions.open_batch(ctx.flow) is None)

# A worktree, when it exists, is the scout's cwd.
ctx = make_ctx()
wt = TMP / "a-worktree"
wt.mkdir()
ctx.flow.set("worktree", str(wt))
script([{"role": "fact_scout", "output": {"answers": [{"question_id": "f3", "found": True, "answer": "free text"}]}}])
auto, _ = questions.resolve(ctx, "plan", [q("f3", "fact")])
h.test_result("fact scout runs in the worktree; free-text finding", os.path.realpath(invocations("fact_scout")[0]["cwd"]) == os.path.realpath(str(wt))
              and auto["f3"].option_id == "" and auto["f3"].text == "free text" and auto["f3"].source == "fact")

# A detail with a deciding principle is answered principle:<id>.
ctx = make_ctx()
script([{"role": "resolver", "label": "d1", "output": verdict("d1", "decides", "P-x", "b", "the helper exists")}])
auto, human = questions.resolve(ctx, "plan", [q("d1", "detail", rec="a")])
a = auto.get("d1")
h.test_result("detail with a deciding principle is answered by it", a is not None and a.source == "principle"
              and a.principle_id == "P-x" and a.option_id == "b" and "Reuse the helper." in a.reason and human == [])
h.test_result("principle answer row source is principle:<id>", decision_rows(ctx)[0]["source"] == "principle:P-x"
              and "principle P-x" in (ctx.flow.path / "journal.md").read_text())
h.test_result("the resolver was given the principle text and the question",
              "Reuse the helper." in invocations("resolver")[0]["prompt"] and "Question d1?" in invocations("resolver")[0]["prompt"])

# A detail with no principle is assumed = recommended.
ctx = make_ctx()
script([{"role": "resolver", "label": "d2", "output": verdict("d2", "none")}])
auto, human = questions.resolve(ctx, "plan", [q("d2", "detail", rec="b")])
h.test_result("detail with no deciding principle is assumed = recommended", auto["d2"].source == "assumed"
              and auto["d2"].option_id == "b" and human == [])

# An unknown principle id is treated as none.
ctx = make_ctx()
script([{"role": "resolver", "label": "d3", "output": verdict("d3", "decides", "P-ghost", "b")}])
auto, human = questions.resolve(ctx, "plan", [q("d3", "detail", rec="a")])
h.test_result("a verdict naming an unknown principle is ignored", auto["d3"].source == "assumed" and auto["d3"].option_id == "a")

# Architecture/behaviour with "none" go to the human.
ctx = make_ctx()
script([], default={"output": verdict("x", "none")})
script([{"role": "resolver", "label": "a1", "output": verdict("a1", "none")},
        {"role": "resolver", "label": "b1", "output": verdict("b1", "none")}])
auto, human = questions.resolve(ctx, "plan", [q("a1", "architecture"), q("b1", "behaviour")])
h.test_result("architecture and behaviour with no principle go to the human",
              auto == {} and [c["id"] for c in human] == ["a1", "b1"] and all(c["status"] == "open" for c in human)
              and decision_rows(ctx) == [])

# Architecture with a directly deciding principle is answered by it.
ctx = make_ctx()
script([{"role": "resolver", "label": "a2", "output": verdict("a2", "decides", "P-x", "a")}])
auto, human = questions.resolve(ctx, "plan", [q("a2", "architecture")])
h.test_result("architecture with a directly deciding principle is answered by it",
              auto["a2"].source == "principle" and auto["a2"].principle_id == "P-x" and human == [])

# A "revisit" verdict goes to the human with the principle shown.
ctx = make_ctx()
script([{"role": "resolver", "label": "r1", "output": verdict("r1", "revisit", "P-x", "", "the helper does not fit here")}])
auto, human = questions.resolve(ctx, "plan", [q("r1", "behaviour")])
card = human[0]
h.test_result("revisit goes to the human with the principle and why on the card",
              auto == {} and card["revisit"] == {"principle_id": "P-x", "why": "the helper does not fit here"}
              and card["before"]["principles"] == [{"id": "P-x", "title": "Reuse", "statement": "Reuse the helper."}])
batch = questions.open_batch(ctx.flow)
md = (ctx.flow.path / "questions.md").read_text()
h.test_result("revisit shows in the markdown", "P-x" in md and "the helper does not fit here" in md and batch is not None)

# risk and purpose go to the human WITHOUT any resolver call, even when the fake would say "decides".
ctx = make_ctx()
script([], default={"output": verdict("whatever", "decides", "P-x", "a")})
auto, human = questions.resolve(ctx, "plan", [q("k1", "risk", risk="The only test of retries would be deleted."), q("p1", "purpose")])
h.test_result("risk and purpose reach the human with no resolver call",
              auto == {} and [c["id"] for c in human] == ["k1", "p1"] and invocations("resolver") == [] and invocations() == [])

# A question that names a protected path is a risk whatever kind the step gave it: it reaches the human.
ctx = make_ctx()
ctx.flow.set("protected", ["tests/test_spec_thing.py"])
script([], default={"output": verdict("whatever", "decides", "P-x", "a")})
auto, human = questions.resolve(ctx, "fix", [
    q("pp1", "detail", question="The test has to live in tests/test_spec_thing.py, which is protected. How should it be covered?"),
    q("pp2", "detail", context="The spec test tests/test_spec_thing.py expects a helper.")])
h.test_result("a detail naming a protected path becomes a risk for the human, with no resolver call for it",
              [c["id"] for c in human] == ["pp1"] and human[0]["kind"] == "risk" and "tests/test_spec_thing.py" in human[0]["risk"]
              and questions.card_missing(human[0]) == [] and "pp1" not in auto, repr((auto, human)))
h.test_result("a protected path named only in the context does not escalate", "pp2" in auto, repr(auto))

# With no principles at all, no resolver call is made.
ctx = make_ctx(with_principles=False)
script([], default={"output": verdict("whatever", "decides", "P-x", "a")})
auto, human = questions.resolve(ctx, "plan", [q("n1", "detail"), q("n2", "architecture")])
h.test_result("no principles: no resolver call; detail assumed, architecture asked",
              invocations("resolver") == [] and auto["n1"].source == "assumed" and [c["id"] for c in human] == ["n2"])

# Several questions at once: independent calls, all recorded, batch written with the auto answers.
ctx = make_ctx()
script([{"role": "resolver", "label": "m1", "output": verdict("m1", "decides", "P-x", "b")},
        {"role": "resolver", "label": "m2", "output": verdict("m2", "none")},
        {"role": "fact_scout", "label": "m3", "output": {"answers": [{"question_id": "m3", "found": True, "option_id": "a", "answer": "A"}]}}])
auto, human = questions.resolve(ctx, "review", [q("m1", "detail"), q("m2", "behaviour"), q("m3", "fact")])
batch = json.loads((ctx.flow.path / "questions.json").read_text())
h.test_result("mixed batch: auto answers returned, human cards listed, batch carries both",
              sorted(auto) == ["m1", "m3"] and [c["id"] for c in human] == ["m2"] and batch["stage"] == "review"
              and batch["status"] == "open" and sorted(batch["auto"]) == ["m1", "m3"] and batch["auto"]["m1"]["principle_id"] == "P-x"
              and [c["id"] for c in batch["cards"]] == ["m2"] and batch["created"]
              and len(decision_rows(ctx)) == 2 and (ctx.flow.path / "questions.md").exists())
h.test_result("resolve with no questions does nothing", questions.resolve(ctx, "plan", []) == ({}, []))

# ---- cards --------------------------------------------------------------------------------------

ctx = make_ctx()
question = q("c1", "behaviour", expert_views=[{"expert": "Ada", "favours": "a", "why": "simpler"},
                                              {"expert": "Bob", "favours": "b", "why": "safer"}])
card = questions.build_card(ctx, "plan", question)
h.test_result("a complete card passes", questions.card_missing(card) == [], repr(questions.card_missing(card)))
h.test_result("card shape", card["ticket"] == {"issue": ctx.issue, "title": "Questions ticket",
                                                "purpose": "The first real line of the body.", "stage": "plan"}
              and card["decision"] == "Question c1?" and card["why"].startswith("While editing")
              and card["recommended"] == "a" and card["status"] == "open" and card["answer"] is None
              and [e["who"] for e in card["experts"]] == ["Ada", "Bob"] and card["reversible"] is True
              and "plan" in card["while_waiting"] and "other tickets continue" in card["while_waiting"], repr(card))
dflt = questions.build_card(ctx, "plan", q("c2"))
h.test_result("a step with no expert views gets one entry from the step itself",
              dflt["experts"] == [{"who": "plan step", "favours": "a", "why": dflt["why"]}])


def broken(mutate):
    c = copy.deepcopy(card)
    mutate(c)
    return questions.card_missing(c)


h.test_result("missing ticket", broken(lambda c: c.pop("ticket")) == ["ticket"] and broken(lambda c: c["ticket"].update(purpose="")) == ["ticket"])
h.test_result("missing decision", broken(lambda c: c.update(decision="")) == ["decision"])
h.test_result("missing why", broken(lambda c: c.update(why=" ")) == ["why"])
h.test_result("missing options (fewer than two)", broken(lambda c: c.update(options=c["options"][:1])) == ["options"])
h.test_result("missing options (an empty consequence)", "options" in broken(lambda c: c["options"][0].update(consequence="")))
h.test_result("missing experts", broken(lambda c: c.update(experts=[])) == ["experts"])
h.test_result("missing recommended", broken(lambda c: c.update(recommended="")) == ["recommended"])
h.test_result("recommended must be one of the option ids", broken(lambda c: c.update(recommended="zzz")) == ["recommended"])
h.test_result("missing while_waiting", broken(lambda c: c.update(while_waiting="")) == ["while_waiting"])
rcard = questions.build_card(ctx, "plan", q("c3", "risk", risk="Retries would be unproven."))
h.test_result("a complete risk card passes", questions.card_missing(rcard) == [])
rc = copy.deepcopy(rcard)
rc["risk"] = ""
h.test_result("risk card without a risk is incomplete", questions.card_missing(rc) == ["risk"])
nr = copy.deepcopy(card)
nr["risk"] = ""
h.test_result("only a risk card needs the risk", questions.card_missing(nr) == [])

# repair: called once, fixes the card.
ctx = make_ctx()
bad = q("w1", "behaviour", context="")
seen = []


def fix(items):
    seen.append(copy.deepcopy(items))
    return [q("w1", "behaviour")]


script([{"role": "resolver", "output": verdict("w1", "none")}], default={"output": verdict("w1", "none")})
auto, human = questions.resolve(ctx, "plan", [bad], repair=fix)
h.test_result("repair is called once with the missing parts and fixes the card",
              len(seen) == 1 and seen[0] == [{"id": "w1", "missing": ["why", "experts"]}] and len(human) == 1
              and questions.card_missing(human[0]) == [] and human[0]["why"].startswith("While editing"))

# still missing after repair, or no repair: a stop, and nothing half-recorded.
ctx = make_ctx()
calls = []


def no_fix(items):
    calls.append(items)
    return [q("w2", "behaviour", context="")]


stopped = None
try:
    questions.resolve(ctx, "plan", [q("ok1", "detail"), q("w2", "behaviour", context="")], repair=no_fix)
except FlowStop as exc:
    stopped = exc
h.test_result("a card still incomplete after repair is a runner stop", stopped is not None and stopped.cls == "runner"
              and "question card w2 is missing: why" in stopped.reason and len(calls) == 1)
h.test_result("a refused batch records nothing", decision_rows(ctx) == [] and not (ctx.flow.path / "questions.json").exists())
ctx = make_ctx()
stopped = None
try:
    questions.resolve(ctx, "plan", [q("w3", "behaviour", context="")])
except FlowStop as exc:
    stopped = exc
h.test_result("an incomplete card without repair is the same stop", stopped is not None and stopped.cls == "runner"
              and "question card w3 is missing" in stopped.reason)

# a risk question whose author left out the risk text still reaches the human, with a stated fallback.
ctx = make_ctx()
stopped = None
human = []
try:
    auto, human = questions.resolve(ctx, "implement", [q("nr1", "risk")])
except FlowStop as exc:
    stopped = exc
h.test_result("a risk question without risk text becomes a card, not a stop",
              stopped is None and [c["id"] for c in human] == ["nr1"] and questions.card_missing(human[0]) == []
              and "did not say" in human[0]["risk"] and "While editing src/mod.py" in human[0]["risk"])

# markdown: a risk card leads with the risk; prior decisions on a shared theme show date and reason, nothing pre-selected.
ctx = make_ctx()
principles.append_decision(ctx.repo, {"ts": "2026-02-03T10:00:00+00:00", "flow": "9-old", "issue": 9, "stage": "plan", "id": "old1",
                                      "question": "Earlier?", "kind": "architecture", "themes": ["Reuse"], "options": [],
                                      "answer": "a", "answer_text": "Extend the shared helper", "source": "human",
                                      "reason": "I do not want two copies", "verbatim": "", "overruled": False})
script([{"role": "resolver", "output": verdict("z", "none")}], default={"output": verdict("z", "none")})
auto, human = questions.resolve(ctx, "plan", [q("e1", "architecture"), q("k2", "risk", risk="Deleting the only retry test.")])
by_id = {c["id"]: c for c in human}
h.test_result("prior decisions on a shared theme are on the card", by_id["e1"]["before"]["decisions"] == [
    {"date": "2026-02-03", "issue": 9, "answer_text": "Extend the shared helper", "reason": "I do not want two copies", "source": "human"}],
    repr(by_id["e1"]["before"]))
md = (ctx.flow.path / "questions.md").read_text()
section = md.split("## [k2]")[1].split("## [")[0]
h.test_result("a risk card's markdown starts with the risk", section.startswith(" RISK\n\n**Risk:** Deleting the only retry test.\n"), repr(section[:120]))
e1 = md.split("## [e1]")[1].split("## [")[0]
h.test_result("markdown: what we decided before shows date and reason, context only",
              "2026-02-03" in e1 and "I do not want two copies" in e1 and "Extend the shared helper" in e1
              and "nothing is pre-selected" in e1 and by_id["e1"]["answer"] is None)
order = ["**Ticket:**", "**Decision:**", "**Why it has come up:**", "**What changes under each option:**",
         "**What the experts think:**", "**What we decided before**", "**Recommendation:**"]
idx = [e1.index(s) for s in order]
h.test_result("markdown sections are in the plan's order", idx == sorted(idx))
h.test_result("render_md of a batch with no cards still renders", "Questions waiting" in questions.render_md({"stage": "plan", "cards": [], "auto": {}}))

# ---- replies ------------------------------------------------------------------------------------

def fresh_batch(extra=None):
    c = make_ctx()
    script([{"role": "resolver", "label": "t1", "output": verdict("t1", "none")}, {"role": "resolver", "label": "t2", "output": verdict("t2", "none")}]
           + (extra or []), default={"output": verdict("zz", "none")})
    qs = [q("t1", "architecture"), q("t2", "behaviour"), q("t3", "detail", rec="b")]
    auto, human = questions.resolve(c, "plan", qs)
    assert sorted(auto) == ["t3"] and len(human) == 2
    return c


# The verbatim file is written before the voice call (make the call fail and check the file exists).
ctx = fresh_batch()
script([], default={"mode": "api_error", "status": 404})
reply = "I think the first one should be the second option, and for the other, Option A. Also keep it small."
stopped = None
try:
    questions.apply_reply(ctx, reply)
except FlowStop as exc:
    stopped = exc
saved = sorted((ctx.flow.path / "human").glob("*-reply.md"))
h.test_result("the verbatim reply is saved before anything interprets it", stopped is not None and len(saved) == 1
              and saved[0].read_text() == reply)
h.test_result("a failed interpretation leaves every card open", all(c["status"] == "open" for c in questions.open_batch(ctx.flow)["cards"]))

# The fast path makes zero model calls.
ctx = fresh_batch()
script([], default={"mode": "api_error", "status": 404})
res = questions.apply_reply(ctx, "t1: a\n\nt2=b\n")
batch = questions.open_batch(ctx.flow)
h.test_result("fast path: both answered with no model call", res["answered"] == ["t1", "t2"] and res["still_open"] == []
              and res["complete"] is True and res["directions"] == [] and invocations() == [])
h.test_result("fast path: answers carry source human, empty reason, verbatim path",
              all(c["answer"]["source"] == "human" and c["answer"]["reason"] == "" and c["answer"]["verbatim_path"].startswith("human/")
                  for c in batch["cards"]) and [c["answer"]["option_id"] for c in batch["cards"]] == ["a", "b"])
h.test_result("fast path: decision files and decisions.jsonl rows",
              (ctx.flow.path / "decisions" / "t1.json").exists() and (ctx.flow.path / "decisions" / "t2.json").exists()
              and sorted(r["id"] for r in decision_rows(ctx) if r["source"] == "human") == ["t1", "t2"])
interp = list((ctx.flow.path / "human").glob("*.interpretation.json"))
h.test_result("fast path: an interpretation is saved beside the reply", len(interp) == 1 and json.loads(interp[0].read_text())["fast_path"] is True)

# Not a fast path when an id or option is unknown, or two answers disagree: the model is asked.
ctx = fresh_batch()
spoken = {"answers": [], "directions": [], "unplaced": ["could not tell"]}
script([{"role": "voice", "output": spoken}])
res = questions.apply_reply(ctx, "t1: zzz")
h.test_result("an unknown option id is not the fast path", len(invocations("voice")) == 1 and res["complete"] is False
              and res["still_open"] == ["t1", "t2"] and "could not tell" in res["summary"])

# A spoken reply covering two cards plus a direction.
ctx = fresh_batch()
spoken = {"answers": [
    {"question_id": "t1", "option_id": "b", "answer": "Option B", "reason_quote": "the second option is cleaner", "confidence": "high"},
    {"question_id": "t2", "option_id": "", "answer": "Do neither; split the ticket", "reason_quote": "it is two jobs", "confidence": "high"}],
    "directions": [{"text": "Stop adding wrappers around everything.", "quote": "stop adding wrappers"}], "unplaced": []}
script([{"role": "voice", "output": spoken}])
res = questions.apply_reply(ctx, "dictated: second option for the first. For the other, neither, split it. And stop adding wrappers.")
batch = questions.open_batch(ctx.flow)
ans = {c["id"]: c["answer"] for c in batch["cards"]}
h.test_result("a spoken reply answers both cards, quoting the human's words",
              res["answered"] == ["t1", "t2"] and res["complete"] is True and ans["t1"]["reason"] == "the second option is cleaner"
              and ans["t2"]["option_id"] == "" and ans["t2"]["text"] == "Do neither; split the ticket" and ans["t2"]["source"] == "human")
h.test_result("the direction is recorded in state and directions_text",
              res["directions"] == ["Stop adding wrappers around everything."]
              and [d["text"] for d in ctx.flow.get("directions")] == ["Stop adding wrappers around everything."]
              and ctx.flow.get("directions")[0]["source"] == "reply"
              and "1. Stop adding wrappers around everything." in voice.directions_text(ctx.flow))
h.test_result("the summary shows the answers, the quote and the direction",
              "the second option is cleaner" in res["summary"] and "Direction noted" in res["summary"])
rows = [r for r in decision_rows(ctx) if r["id"] == "t1"]
h.test_result("a spoken answer's row has the human's words and the verbatim path", rows[0]["reason"] == "the second option is cleaner"
              and rows[0]["verbatim"].startswith("human/") and rows[0]["source"] == "human")
h.test_result("record.why shows the human's words for a spoken answer", "the second option is cleaner" in record.why(ctx.flow, "t1"))
h.test_result("the interpretation is saved beside the reply", len(list((ctx.flow.path / "human").glob("*.interpretation.json"))) == 1)

# A low-confidence item stays open; a second reply closes it.
ctx = fresh_batch()
spoken = {"answers": [
    {"question_id": "t1", "option_id": "a", "answer": "Option A", "reason_quote": "go with A", "confidence": "high"},
    {"question_id": "t2", "option_id": "b", "answer": "Option B", "reason_quote": "", "confidence": "low"}],
    "directions": [], "unplaced": []}
script([{"role": "voice", "output": spoken}])
res = questions.apply_reply(ctx, "Go with A for the first. Hmm, maybe B on the other, not sure.")
h.test_result("a low-confidence item stays open and the reply is not complete", res["answered"] == ["t1"]
              and res["still_open"] == ["t2"] and res["complete"] is False and "Still open: t2" in res["summary"])
raised = False
try:
    questions.take_answers(ctx, "plan")
except RuntimeError:
    raised = True
h.test_result("take_answers with open cards raises", raised and questions.open_batch(ctx.flow) is not None)
res = questions.apply_reply(ctx, "t2: b")
h.test_result("a second reply closes it", res["answered"] == ["t2"] and res["still_open"] == [] and res["complete"] is True)

h.test_result("take_answers for another stage returns nothing and consumes nothing",
              questions.take_answers(ctx, "review") == {} and questions.open_batch(ctx.flow) is not None)
got = questions.take_answers(ctx, "plan")
h.test_result("take_answers returns auto and human answers", sorted(got) == ["t1", "t2", "t3"]
              and got["t3"].source == "assumed" and got["t3"].option_id == "b" and got["t1"].source == "human"
              and got["t1"].reason == "go with A" and got["t2"].option_id == "b")
h.test_result("take_answers consumes the batch", questions.open_batch(ctx.flow) is None
              and json.loads((ctx.flow.path / "questions.json").read_text())["status"] == "consumed"
              and questions.take_answers(ctx, "plan") == {})

text = questions.answers_for_prompt(got)
h.test_result("answers_for_prompt names id, option, who and why",
              "Question t1" in text and "Chosen option: a" in text and "Decided by: human" in text and "go with A" in text
              and "Decided by: assumed" in text and "Question t3" in text, text)
h.test_result("answers_for_prompt on nothing", questions.answers_for_prompt({}) == "")

# No open batch at all: the reply is still saved first, then refused.
ctx = make_ctx()
raised = False
try:
    questions.apply_reply(ctx, "hello")
except RuntimeError:
    raised = True
h.test_result("a reply with no open batch is refused after being saved", raised and len(list((ctx.flow.path / "human").glob("*-reply.md"))) == 1)

# ---- only a question a person alone can answer becomes a card (#570) ---------------------------

OWNER = {"unit": "u3", "files": ["src/mod.py"], "other_files": ["src/other.py"], "unit_ids": ["u1", "u3", "u7"]}
OWNERSHIP_OPTIONS = [
    {"id": "a", "label": "Leave src/other.py to its owner", "consequence": "u7 makes the edit later."},
    {"id": "b", "label": "Add src/other.py to this unit's file list", "consequence": "u3 edits both files now."}]


def driver_verdict(qid, driver_kind, reason="it is about how the work is split"):
    return {"results": [{"question_id": qid, "outcome": "none", "principle_id": "", "option_id": "",
                         "reason": reason, "who": "driver", "driver_kind": driver_kind}]}


# An ownership question from a unit's worker: the resolver says it is the driver's, so no card.
ctx = make_ctx(with_principles=False)
script([{"role": "resolver", "label": "o1", "output": driver_verdict("o1", "ownership")}])
auto, human = questions.resolve(ctx, "implement", [q("o1", "architecture")], owner=OWNER)
a = auto.get("o1")
h.test_result("ownership question from a unit is settled by the driver without a card",
              a is not None and a.source == "driver" and a.option_id == "" and "elsewhere" in a.text
              and human == [] and questions.open_batch(ctx.flow) is None
              and not (ctx.flow.path / "questions.json").exists(), repr(auto))
h.test_result("the settlement is journalled and recorded as the driver's",
              "the driver (not a question for a person)" in (ctx.flow.path / "journal.md").read_text()
              and decision_rows(ctx)[0]["source"] == "driver")
h.test_result("the resolver was asked who should answer, with no principles to go on",
              len(invocations("resolver")) == 1 and "no written principles" in invocations("resolver")[0]["prompt"])

# An option the driver cannot honour marks a bookkeeping question: settled with no model call at all.
ctx = make_ctx(with_principles=False)
script([])
auto, human = questions.resolve(ctx, "implement", [q("o2", "architecture", options=OWNERSHIP_OPTIONS)], owner=OWNER)
h.test_result("a question offering an option the driver cannot carry out is settled, not shown",
              auto["o2"].source == "driver" and "Option b" in auto["o2"].reason and human == []
              and invocations() == [] and questions.open_batch(ctx.flow) is None, repr(auto))

# Ordering and tooling get their own rule; a checkable fact goes to the scout.
ctx = make_ctx(with_principles=False)
script([{"role": "resolver", "label": "o3", "output": driver_verdict("o3", "tooling")},
        {"role": "resolver", "label": "o4", "output": driver_verdict("o4", "fact")},
        {"role": "fact_scout", "label": "o4", "output": {"answers": [
            {"question_id": "o4", "found": True, "option_id": "b", "answer": "B", "evidence": "README.md line 1"}]}}])
auto, human = questions.resolve(ctx, "implement", [q("o3", "behaviour"), q("o4", "behaviour")], owner=OWNER)
h.test_result("a tooling question gets the tooling rule; a checkable fact goes to the scout",
              auto["o3"].source == "driver" and "cannot run commands" in auto["o3"].text
              and auto["o4"].source == "fact" and auto["o4"].option_id == "b" and human == [], repr(auto))

# A real product question from a unit still reaches the person, with driver words explained.
ctx = make_ctx(with_principles=False)
script([{"role": "resolver", "label": "o5", "output": verdict("o5", "none")}])
auto, human = questions.resolve(ctx, "implement", [
    q("o5", "behaviour", question="Should unit u7 keep the old error text?")], owner=OWNER)
md = (ctx.flow.path / "questions.md").read_text()
h.test_result("a human question from a unit is still a card", auto == {} and [c["id"] for c in human] == ["o5"])
h.test_result("a card that uses the word unit says what a unit is",
              human[0]["glossary"].startswith(questions.GLOSSARY) and "**Terms:**" in md and "one slice of this ticket" in md,
              human[0].get("glossary", ""))

# Without an owner nothing changes: a plan question with no principles makes no resolver call.
ctx = make_ctx(with_principles=False)
script([])
auto, human = questions.resolve(ctx, "plan", [q("o6", "architecture")])
h.test_result("no owner and no principles: no resolver call, the card has no glossary",
              invocations() == [] and [c["id"] for c in human] == ["o6"] and human[0]["glossary"] == "")

# A detail from a unit is not worth a model call: it is assumed as before.
ctx = make_ctx(with_principles=False)
script([])
auto, human = questions.resolve(ctx, "implement", [q("o7", "detail", rec="b")], owner=OWNER)
h.test_result("a detail from a unit is assumed with no resolver call",
              auto["o7"].source == "assumed" and auto["o7"].option_id == "b" and invocations() == [])

# A risk card is never settled by the driver. One with an option the driver cannot carry out is
# rejected before it reaches the inbox.
RISK = "The spec test for mod would no longer be protected."
h.test_result("unhonourable names the options that ask to change how the work is split",
              questions.unhonourable({"options": OWNERSHIP_OPTIONS}, OWNER) == ["b"]
              and questions.unhonourable({"options": [
                  {"id": "x", "label": "Run the migration by hand", "consequence": "You run it manually."},
                  {"id": "y", "label": "Reorder the units", "consequence": "u7 goes first."},
                  {"id": "z", "label": "Keep the old name", "consequence": "Callers are unchanged."}]}, OWNER) == ["x", "y"]
              and questions.unhonourable({"options": q("n")["options"]}, OWNER) == []
              and questions.unhonourable({"options": OWNERSHIP_OPTIONS}) == [])  # no owner: never checked
ctx = make_ctx(with_principles=False)
script([])
stop = None
try:
    questions.resolve(ctx, "implement", [q("k1", "risk", risk=RISK, options=OWNERSHIP_OPTIONS)], owner=OWNER)
except FlowStop as exc:
    stop = exc
h.test_result("a card with an option the driver cannot honour is rejected before the inbox",
              stop is not None and stop.cls == "runner" and "an option the driver cannot carry out (b)" in stop.reason
              and questions.open_batch(ctx.flow) is None and not (ctx.flow.path / "questions.json").exists()
              and invocations("resolver") == [], str(stop))

# ... unless the step that asked can restate it with options the driver can carry out.
ctx = make_ctx(with_principles=False)
script([])
asked = []


def restate(incomplete):
    asked.extend(incomplete)
    return [q("k2", "risk", risk=RISK)]


auto, human = questions.resolve(ctx, "implement", [q("k2", "risk", risk=RISK, options=OWNERSHIP_OPTIONS)],
                                repair=restate, owner=OWNER)
h.test_result("the asking step gets one chance to restate the card with options that can be carried out",
              len(asked) == 1 and "cannot carry out" in asked[0]["missing"][0]
              and [c["id"] for c in human] == ["k2"] and questions.unhonourable(human[0]) == [])

# ---- F2, F3, F5: Driver settlement requires owner gate and valid driver_kind ----

# F2: Driver answer should NOT apply to plan questions without an owner
ctx_plan_no_owner = make_ctx(with_principles=False)
script([{"role": "resolver", "label": "plan_q", "output": driver_verdict("plan_q", "ownership")}])
auto, human = questions.resolve(ctx_plan_no_owner, "plan", [q("plan_q", "architecture")], owner=None)
h.test_result("F2: driver verdict on plan question without owner does not settle (goes to human)",
              auto == {} and [c["id"] for c in human] == ["plan_q"],
              f"auto={auto}, human_ids={[c['id'] for c in human]}")

# F5: Missing driver_kind should fail closed to 'human' (no settlement)
ctx_no_kind = make_ctx(with_principles=False)
script([{"role": "resolver", "label": "no_kind", "output": {"results": [
    {"question_id": "no_kind", "outcome": "none", "principle_id": "", "option_id": "",
     "reason": "test missing kind", "who": "driver"}  # Missing driver_kind
]}}])
auto, human = questions.resolve(ctx_no_kind, "implement", [q("no_kind", "architecture")], owner=OWNER)
h.test_result("F5: missing driver_kind defaults to human (not settled by driver)",
              auto == {} and [c["id"] for c in human] == ["no_kind"],
              f"auto={auto}, human_ids={[c['id'] for c in human]}")

# F5: Driver kinds must match vocabulary
h.test_result("F5: driver_kind vocabulary matches DRIVER_KINDS",
              all(kind in schemas.DRIVER_KINDS for kind in ["ownership", "ordering", "tooling", "fact"]),
              f"DRIVER_KINDS: {schemas.DRIVER_KINDS}")

# F3: _REORGANISE vocabulary - ownership questions should use driver's vocabulary, not product words
ctx_vocab = make_ctx(with_principles=False)
script([{"role": "resolver", "label": "own_vocab", "output": driver_verdict("own_vocab", "ownership")}])
auto, human = questions.resolve(ctx_vocab, "implement", [q("own_vocab", "architecture")], owner=OWNER)
# When driver settles an ownership question, it should be settled, not passed to human
settled = auto.get("own_vocab") is not None and auto["own_vocab"].source == "driver"
h.test_result("F3: ownership questions settled by driver are not marked for human review",
              settled and human == [],
              f"settled={settled}, human={human}")

h.summarize_and_exit()
