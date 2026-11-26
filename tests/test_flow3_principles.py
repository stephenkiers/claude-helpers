#!/usr/bin/env python3
"""Tests for scripts/flow3/principles.py: the file format, the decision log, and suggestions."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="flow3-test-")).resolve()
os.environ["FLOW3_HOME"] = str(TMP / "home")
os.environ["HOME"] = str(TMP / "userhome")

from _test_harness import Harness  # noqa: E402

from scripts.flow3 import principles  # noqa: E402

h = Harness("Flow v3 principles")

TEXT = """# Goal
Ship a small, trustworthy tool.
It should stay boring.

## P-strong-types: Prefer strong types
Statement: Make illegal states unrepresentable;
  no stringly-typed values across module boundaries.
Spirit: Catch mistakes at the boundary.
Revisit if: The type costs more than the mistake.

## P-bare: Only a title
"""

goal, plist = principles.parse(TEXT, "project")
h.test_result("parse goal", goal == "Ship a small, trustworthy tool.\nIt should stay boring.", repr(goal))
h.test_result("parse ids keep prefix", [p.id for p in plist] == ["P-strong-types", "P-bare"])
p0 = plist[0]
h.test_result("parse fields with continuation", p0.title == "Prefer strong types"
              and p0.statement == "Make illegal states unrepresentable; no stringly-typed values across module boundaries."
              and p0.spirit == "Catch mistakes at the boundary." and p0.revisit_if == "The type costs more than the mistake."
              and p0.scope == "project", repr(p0))
h.test_result("parse is tolerant of missing fields", plist[1].statement == "" and plist[1].spirit == "" and plist[1].revisit_if == "")
h.test_result("parse empty text", principles.parse("", "personal") == ("", []))

# load precedence
proj = TMP / "proj.md"
pers = TMP / "pers.md"
proj.write_text("# Goal\nProject goal.\n\n## P-a: Project A\nStatement: from project\n")
pers.write_text("# Goal\nPersonal goal.\n\n## P-a: Personal A\nStatement: from personal\n\n## P-b: Personal B\nStatement: only personal\n")
g, merged = principles.load(proj, pers)
by = {p.id: p for p in merged}
h.test_result("load: project wins on clash, goal too", g == "Project goal." and by["P-a"].statement == "from project"
              and by["P-a"].scope == "project" and by["P-b"].scope == "personal" and len(merged) == 2)
proj.write_text("## P-a: Project A\nStatement: from project\n")
g, _ = principles.load(proj, pers)
h.test_result("load: personal goal when the project has none", g == "Personal goal.")
g, merged = principles.load(None, TMP / "missing.md")
h.test_result("load: both missing", g == "" and merged == [])
g, merged = principles.load(None, pers)
h.test_result("load: only personal", len(merged) == 2)

# seeds and starter
h.test_result("six seed themes", len(principles.SEED_THEMES) == 6
              and len({p.id for p in principles.SEED_THEMES}) == 6
              and all(p.id.startswith("P-") and p.statement and p.spirit and p.revisit_if for p in principles.SEED_THEMES))
h.test_result("seed ids", principles.SEED_THEMES[0].id == "P-docs-follow-code")
text = principles.starter("Be useful.", principles.SEED_THEMES[:3])
g2, back = principles.parse(text, "project")
h.test_result("starter round trip", g2 == "Be useful." and [(p.id, p.title, p.statement, p.spirit, p.revisit_if) for p in back]
              == [(p.id, p.title, p.statement, p.spirit, p.revisit_if) for p in principles.SEED_THEMES[:3]])
h.test_result("as_text round trip without goal", principles.parse(principles.as_text("", plist), "x")[1][0].statement == p0.statement)

# decision log
REPO = "r1"
OPTS = [{"id": "a", "label": "Use the helper", "consequence": "c"}, {"id": "b", "label": "Write new", "consequence": "d"}]


def rec(i, themes, answer="a", source="human", flow="1-x", ts=None, **kw):
    row = {"ts": ts or "2026-01-%02dT00:00:00+00:00" % i, "flow": flow, "issue": 1, "stage": "plan", "id": "q%d" % i,
           "question": "Q%d" % i, "kind": "architecture", "themes": themes, "options": OPTS, "answer": answer,
           "answer_text": dict(a="Use the helper", b="Write new")[answer], "source": source, "reason": "r%d" % i,
           "verbatim": "", "overruled": False}
    row.update(kw)
    return row


h.test_result("read with no log", principles.read_decisions(REPO) == [])
for i, t in enumerate([["Reuse"], ["testing"], ["reuse", "docs"]], 1):
    principles.append_decision(REPO, rec(i, t))
rows = principles.read_decisions(REPO)
h.test_result("append and read", [r["id"] for r in rows] == ["q1", "q2", "q3"] and all(r["overruled"] is False for r in rows))
(principles.repo_dir(REPO) / "decisions.jsonl").open("a").write("not json\n")
h.test_result("read skips a bad line", len(principles.read_decisions(REPO)) == 3)
prior = principles.prior_decisions(REPO, ["REUSE"], 5)
h.test_result("prior: shared theme, case-insensitive, most recent first", [r["id"] for r in prior] == ["q3", "q1"])
h.test_result("prior: limit", [r["id"] for r in principles.prior_decisions(REPO, ["reuse"], 1)] == ["q3"])
h.test_result("prior: no match or no themes", principles.prior_decisions(REPO, ["nope"]) == [] and principles.prior_decisions(REPO, []) == [])

# overrule
principles.append_decision(REPO, rec(4, ["naming"], source="assumed", flow="2-y", id="q4"))
principles.mark_overruled(REPO, "2-y", "q4", "b", "I wanted it new")
rows = principles.read_decisions(REPO)
last = rows[-1]
h.test_result("mark_overruled appends a human record", last["overruled"] is True and last["source"] == "human"
              and last["answer"] == "b" and last["answer_text"] == "Write new" and last["reason"] == "I wanted it new"
              and last["themes"] == ["naming"], repr(last))
prior = principles.prior_decisions(REPO, ["naming"])
h.test_result("prior: the overrule replaces the overruled answer", [r["overruled"] for r in prior] == [True])

# suggestions
three = [rec(i, ["docs"], answer="a") for i in (1, 2, 3)]
c = principles.suggest_candidates(three)
h.test_result("suggest: three the same -> candidate with evidence", len(c) == 1 and c[0]["theme"] == "docs"
              and c[0]["why"] == "answered 3 times" and len(c[0]["evidence"]) == 3 and "Use the helper" in c[0]["proposed_statement"], repr(c))
h.test_result("suggest: two -> none", principles.suggest_candidates(three[:2]) == [])
mixed = [rec(1, ["docs"], "a"), rec(2, ["docs"], "b"), rec(3, ["docs"], "a")]
h.test_result("suggest: different answers do not count together", principles.suggest_candidates(mixed) == [])
by_text = [rec(i, ["docs"], "a", answer_text="Use  the HELPER!", id="t%d" % i) for i in (1, 2, 3)]
h.test_result("suggest: answer text is normalised", len(principles.suggest_candidates(by_text)) == 1)
auto = [rec(i, ["docs"], "a", source="assumed", id="z%d" % i) for i in (1, 2, 3)]
h.test_result("suggest: only human answers count towards repeats", principles.suggest_candidates(auto) == [])

assumed = rec(5, ["naming"], "a", source="assumed", flow="3-z", id="q5")
over = dict(assumed, answer="b", answer_text="Write new", source="human", overruled=True, reason="no")
c = principles.suggest_candidates([assumed, over])
h.test_result("suggest: an overruled assumed answer", len(c) == 1 and c[0]["why"] == "overruled" and c[0]["theme"] == "naming"
              and len(c[0]["evidence"]) == 2, repr(c))
pr = dict(assumed, source="principle:P-x")
c = principles.suggest_candidates([pr, dict(over)])
h.test_result("suggest: an overruled principle answer", len(c) == 1 and c[0]["why"] == "overruled")
human = dict(assumed, source="human")
h.test_result("suggest: overruling a human answer proposes nothing", principles.suggest_candidates([human, over]) == [])

h.summarize_and_exit()
