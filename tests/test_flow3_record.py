#!/usr/bin/env python3
"""Tests for scripts/flow3/record.py plus the "no stop without a reason" rule."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.mkdtemp(prefix="flow3-test-")
os.environ["FLOW3_HOME"] = _TMP

from _test_harness import Harness  # noqa: E402

import json  # noqa: E402

from scripts.flow3 import record  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.types import Answer, FlowStop, make_stop  # noqa: E402

h = Harness("flow3 record")
flow = FlowDir.create("proj", 1, "Rec", Path(_TMP) / "proj")

record.journal(flow, "first")
record.journal(flow, "second")
j = (flow.path / "journal.md").read_text()
h.test_result("journal format", j.count("## ") == 2 and "\n\nfirst\n" in j and j.index("first") < j.index("second"))

p = record.write_step_record(flow, "plan", [("Given", "the ticket"), ("Produced", "a plan")])
h.test_result("step record name and body", p.name == "02-plan.md" and p.read_text().startswith("# plan\n\n## Given\n\nthe ticket\n"))
p2 = record.write_step_record(flow, "plan", [("Healed", "nothing")])
t = p2.read_text()
h.test_result("step record appends", p2 == p and t.count("# plan") == 1 and "## Healed" in t and t.index("Produced") < t.index("Healed"))
h.test_result("step number from STAGES", record.write_step_record(flow, "ship", [("a", "b")]).name == "07-ship.md")

text = "Line one\r\n  weird   spacing é—\n\nno trailing newline"
rel = record.save_human(flow, text)
h.test_result("human saved byte for byte", (flow.path / rel).read_bytes() == text.encode("utf-8") and rel.startswith("human/") and rel.endswith("-reply.md"))
rel2 = record.save_human(flow, "x", kind="direction")
h.test_result("human kind and uniqueness", rel2.endswith("-direction.md") and rel2 != rel)
record.save_interpretation(flow, rel, {"answers": []})
interp = (flow.path / rel).with_name((flow.path / rel).stem + ".interpretation.json")
h.test_result("interpretation beside reply", json.loads(interp.read_text()) == {"answers": []})

card = {"id": "q1", "question": "Use a queue?", "options": [{"id": "a", "label": "Yes", "consequence": "c"}, {"id": "b", "label": "No", "consequence": "d"}]}
sources = {
    "q1": Answer("q1", "a", "Yes", "human", reason="'because'", verbatim_path=rel),
    "q2": Answer("q2", "b", "No", "principle", principle_id="P-strong-types", reason="types win"),
    "q3": Answer("q3", "a", "Yes", "assumed", reason="safest default"),
    "q4": Answer("q4", "", "found in docs", "fact", reason="README says so"),
}
for qid, ans in sources.items():
    c = dict(card, id=qid, question=f"Question {qid} about scripts/foo.py" if qid == "q2" else card["question"])
    r = record.write_decision(flow, c, ans, "plan")
    h.test_result(f"write_decision {qid}", r == f"decisions/{qid}.json" and json.loads((flow.path / r).read_text())["downstream"] == "")
record.note_downstream(flow, "q1", "Added src/queue.py")
record.note_downstream(flow, "q1", "and tests")
h.test_result("note_downstream appends", "Added src/queue.py\nand tests" == record.read_decisions(flow)[0]["downstream"])
h.test_result("read_decisions", [d["question_id"] for d in record.read_decisions(flow)] == ["q1", "q2", "q3", "q4"])

w = record.why(flow, "q1")
h.test_result("why human", all(s in w for s in ("Use a queue?", "a: Yes (chosen)", "b: No", "human", "because", rel, "Added src/queue.py")), w)
w = record.why(flow, "q2")
h.test_result("why principle", "principle (P-strong-types)" in w and "types win" in w, w)
w = record.why(flow, "q3")
h.test_result("why assumed", "assumed" in w and "safest default" in w and "(none recorded)" in w, w)
w = record.why(flow, "q4")
h.test_result("why fact", "fact" in w and "README says so" in w, w)
record.write_step_record(flow, "implement", [("Files", "touched src/queue.py")])
w = record.why(flow, "src/queue.py")
h.test_result("why file path: decision and step", "Decision q1" in w and "steps/04-implement.md" in w and "Decision q3" not in w, w)
w = record.why(flow, "scripts/foo.py")
h.test_result("why file path via card text", "Decision q2" in w, w)
w = record.why(flow, "nope/none.py")
h.test_result("why no match", "No decision is recorded" in w and "nope/none.py" in w, w)
h.test_result("why unknown id", "No decision is recorded" in record.why(flow, "q99"))

cp = record.check_output_path(flow, "fast", "abcdef0123456789")
cp2 = record.check_output_path(flow, "full", "abcdef0123456789")
h.test_result("check_output_path", cp.name == "001-fast-abcdef012345.log" and cp2.name == "002-full-abcdef012345.log" and cp.parent.name == "checks")

# stop screen
h.test_result("no stop", "no stop recorded" in record.stop_screen(flow))
record.write_stop(flow, make_stop("check", "lint is red", "fix", "python3 -m scripts.flow3 resume proj 1", evidence="checks/001.log", tried=["fixer", "stronger model"]))
s = record.stop_screen(flow)
h.test_result("stop screen has all six parts", all(x in s for x in ("check", "lint is red", "fix", "checks/001.log", "- fixer", "- stronger model", "python3 -m scripts.flow3 resume proj 1")), s)
record.write_stop(flow, make_stop("human", "held", "merge_gate", "cmd"))
s = record.stop_screen(flow)
h.test_result("stop screen: nothing tried", "nothing" in s and "none recorded" in s, s)
record.clear_stop(flow)
record.clear_stop(flow)
h.test_result("clear_stop idempotent", record.read_stop(flow) is None)

# No stop without class and reason
def rejects(fn: object) -> bool:
    try:
        fn()  # type: ignore[operator]
    except ValueError:
        return True
    return False


h.test_result("FlowStop empty reason", rejects(lambda: FlowStop("check", "")) and rejects(lambda: FlowStop("check", "   ")))
h.test_result("FlowStop unknown class", rejects(lambda: FlowStop("nonsense", "reason")))
h.test_result("make_stop empty reason / unknown class / empty resume",
              rejects(lambda: make_stop("check", "", "plan", "rc")) and rejects(lambda: make_stop("bogus", "r", "plan", "rc"))
              and rejects(lambda: make_stop("check", "r", "plan", "")))

h.summarize_and_exit()
