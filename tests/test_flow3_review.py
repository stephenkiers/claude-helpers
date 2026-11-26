#!/usr/bin/env python3
"""
Tests for scripts/flow3/steps/review.py (PLAN.md 7.5). No real model is called: the fake claude binary
(tests/flow3_fake_claude.py) answers, scripted per scenario by role and label.

Labels the stage uses: sweep-<i>, <reviewer name>, verify-<i>, triage-<round>.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

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

from scripts.flow3 import config, context, heal, questions, repoconfig, runner  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.steps import common, review  # noqa: E402
from scripts.flow3.types import StageResult  # noqa: E402

heal._sleep = lambda s: None  # type: ignore[assignment]
runner._sleep = lambda s: None  # type: ignore[assignment]

h = Harness("Flow v3 review stage")
_COUNTER = [0]


def sh(args: List[str], cwd: Path) -> str:
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


class Box:
    """A sandbox repo (the clone is the worktree), a flow with a plan and a base sha, and its Ctx."""

    def __init__(self) -> None:
        _COUNTER[0] += 1
        n = _COUNTER[0]
        origin = TMP / ("o%d.git" % n)
        self.clone = TMP / ("c%d" % n)
        sh(["git", "init", "--bare", "-q", "-b", "main", str(origin)], TMP)
        sh(["git", "clone", "-q", str(origin), str(self.clone)], TMP)
        sh(["git", "config", "user.name", "t"], self.clone)
        sh(["git", "config", "user.email", "t@example.invalid"], self.clone)
        (self.clone / ".claude").mkdir()
        (self.clone / ".claude" / "flow.yaml").write_text("provider: local\ncheck: {full: 'true'}\n")
        (self.clone / "README.md").write_text("hi\n")
        sh(["git", "add", "-A"], self.clone)
        sh(["git", "commit", "-q", "-m", "init"], self.clone)
        sh(["git", "push", "-q", "origin", "main"], self.clone)
        self.base = sh(["git", "rev-parse", "HEAD"], self.clone)
        issues = origin / "flow3" / "issues"
        issues.mkdir(parents=True)
        (issues / ("%d.md" % n)).write_text("# Review ticket\n\nBody.\n")
        self.flow = FlowDir.create(repoconfig.repo_name(self.clone), n, "Review ticket", self.clone)
        self.flow.set("worktree", str(self.clone))
        self.flow.set_data("worktree", {"base_sha": self.base})
        self.flow.set_data("plan", {"plan": {"goal": "Add the a module.", "non_goals": ["Rewrite b"]}})
        self.ctx = context.build(self.flow)

    def commit(self, rel: str, text: str, msg: str = "work") -> str:
        p = self.clone / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        sh(["git", "add", "-A"], self.clone)
        sh(["git", "commit", "-q", "-m", msg], self.clone)
        return sh(["git", "rev-parse", "HEAD"], self.clone)


def script(calls: List[Dict[str, Any]]) -> None:
    for p in (SCRIPT, Path(str(SCRIPT) + ".consumed"), LOG):
        if p.exists():
            p.unlink()
    SCRIPT.write_text(json.dumps({"calls": calls}))


def invocations(role: Optional[str] = None) -> List[Dict[str, Any]]:
    if not LOG.exists():
        return []
    rows = [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]
    return [r for r in rows if role is None or r["role"] == role]


def counts() -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in invocations():
        out[r["role"]] = out.get(r["role"], 0) + 1
    return out


def scouts(lens_candidates: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Six identical scout answers; a lens name outside a call's group is dropped by the stage."""
    out = {"lenses": [{"lens": k, "candidates": v} for k, v in lens_candidates.items()]}
    return [{"role": "review_scout", "output": out} for _ in range(6)]


def finding(title: str, severity: str = "medium", file: str = "src/a.py", line: int = 3) -> Dict[str, Any]:
    return {"id": "model-id", "lens": "Somebody Else", "file": file, "line": line, "severity": severity,
            "title": title, "detail": "detail of " + title, "suggestion": "suggest " + title}


def reviewer(name: str, findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"role": "reviewer", "label": name, "output": {"findings": findings}}


def verdicts(rows: Dict[str, str]) -> Dict[str, Any]:
    return {"role": "verifier", "output": {"verdicts": [
        {"finding_id": k, "verdict": v, "evidence": "looked at the code", "command": ("git log" if v == "needs_measurement" else "")}
        for k, v in rows.items()]}}


def triage(doing: Optional[List[str]] = None, needs_you: Optional[List[Dict[str, Any]]] = None,
           measure: Optional[List[Dict[str, Any]]] = None, deferred: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"role": "triage", "output": {
        "doing_it": [{"finding_id": f, "fix": "do the fix for " + f, "files": ["src/a.py"]} for f in doing or []],
        "needs_you": needs_you or [],
        "needs_measurement": measure or [],
        "deferred": [{"finding_id": f, "reason": "later"} for f in deferred or []],
        "gut_check": "one shared premise: none"}}


def question(fid: str, options: Optional[List[Dict[str, str]]] = None, rec: str = "fix") -> Dict[str, Any]:
    return {"finding_id": fid, "question": {
        "id": "whatever", "question": "Should %s be fixed?" % fid, "context": "Found in src/a.py by a reviewer.",
        "options": options or [{"id": "fix", "label": "Fix it", "consequence": "The code changes"},
                               {"id": "leave", "label": "Leave as-is", "consequence": "The defect stays"}],
        "recommended": rec, "kind": "behaviour", "reversible": True}}


def data(box: Box) -> Dict[str, Any]:
    return box.flow.data("review")


def result_info(res: StageResult) -> Dict[str, Any]:
    return res.info


# ---- lens_groups ------------------------------------------------------------------------------

lenses = common.reviewers("review")
groups = review.lens_groups(lenses)
flat = [e["name"] for g in groups for e in g]
h.test_result("lens_groups: every review lens exactly once", sorted(flat) == sorted(e["name"] for e in lenses)
              and len(flat) == len(set(flat)) and len(lenses) >= 20, "%d lenses, %d placed" % (len(lenses), len(flat)))
h.test_result("lens_groups: about 6 groups of 4 or 5", len(groups) == 6 and all(4 <= len(g) <= 5 for g in groups),
              str([len(g) for g in groups]))
h.test_result("lens_groups: small and empty inputs", review.lens_groups(lenses[:3]) == [[e] for e in lenses[:3]]
              and review.lens_groups([]) == [])

# ---- is_read_only_command ---------------------------------------------------------------------

ok_cmds = ["git log --oneline", "git diff main...HEAD", "git show HEAD:src/a.py", "git status", "git grep -n foo src",
           "git ls-files", "git rev-parse HEAD", "git blame src/a.py", "grep -rn 'a b' src", "rg foo src", "ls -la src",
           "cat src/a.py", "wc -l src/a.py", "head -n 5 src/a.py", "tail -n 5 src/a.py", "find src -name '*.py'",
           "git log --pretty=format:%h"]
bad_cmds = ["", "   ", "rm -rf src", "git push", "git commit -m x", "git checkout main", "git diff --output=out.txt",
            "git grep -O less foo", "git -c core.pager=x log", "cat a; rm b", "cat a && ls", "ls | wc", "cat a > b",
            "cat < a", "echo `id`", "cat $(id)", "cat a\nrm b", "python3 -m pytest", "pytest", "python3 -c 'print(1)'",
            "find . -delete", "find . -exec rm {} +", "find . -fprint out", "rg --pre ./x foo", "cat /etc/passwd",
            "cat ../x", "cat ~/x", "cat 'unterminated", "tail -f log", "sh -c ls", "curl http://example.invalid"]
h.test_result("is_read_only_command: accepts the allowlist", all(review.is_read_only_command(c) for c in ok_cmds),
              str([c for c in ok_cmds if not review.is_read_only_command(c)]))
h.test_result("is_read_only_command: rejects writers, metacharacters and the rest",
              not any(review.is_read_only_command(c) for c in bad_cmds),
              str([c for c in bad_cmds if review.is_read_only_command(c)]))

# ---- _normalize (the fallback bucketing) ------------------------------------------------------

fs = [{"id": "a", "severity": "critical", "file": "x", "title": "t", "detail": "d", "status": "confirmed"},
      {"id": "b", "severity": "high", "file": "x", "title": "t", "detail": "d", "status": "confirmed"},
      {"id": "c", "severity": "low", "file": "x", "title": "t", "detail": "d", "status": "confirmed"},
      {"id": "d", "severity": "high", "file": "x", "title": "t", "detail": "d", "status": "refuted"},
      {"id": "e", "severity": "low", "file": "x", "title": "t", "detail": "d", "status": "needs_measurement"}]
norm = review._normalize({"doing_it": [{"finding_id": "e", "fix": "f", "files": []}, {"finding_id": "zzz", "fix": "f"}],
                          "needs_you": [{"finding_id": "e", "question": {}}],
                          "needs_measurement": [], "deferred": [{"finding_id": "c", "reason": "later"},
                                                                 {"finding_id": "d", "reason": "refuted one"}]}, fs)
ids = {b: [i["finding_id"] for i in norm[b]] for b in review.BUCKETS}
h.test_result("normalize: omitted critical/high go to doing_it, omitted low to deferred 'not triaged'",
              sorted(ids["doing_it"]) == ["a", "b"] and ids["deferred"] == ["c"]
              and norm["deferred"][0]["reason"] == "later", str(ids))
h.test_result("normalize: duplicate keeps needs_you first; unknown and refuted ids dropped",
              ids["needs_you"] == ["e"] and "zzz" not in sum(ids.values(), []) and "d" not in sum(ids.values(), []), str(ids))
norm2 = review._normalize({}, fs[2:3])
h.test_result("normalize: not triaged reason", norm2["deferred"] == [{"finding_id": "c", "reason": "not triaged"}])

# ---- a full round, no questions ---------------------------------------------------------------

box = Box()
box.commit("src/a.py", "UNIQUE_DIFF_MARK = 1\n")
script(scouts({"Uncle Bob": [{"file": "src/a.py", "line": 1, "note": "NOTE-UB"}],
               "Rachel": [{"file": "src/a.py", "line": 1, "note": "NOTE-RACHEL"}],
               "Not A Lens": [{"note": "NOTE-BOGUS"}]})
       + [reviewer("Uncle Bob", [finding("T-UB-high", "high")]),
          reviewer("Rachel", [finding("T-RACHEL-medium", "medium")]),
          reviewer("Security Sage", [finding("T-SAGE-low", "low")]),
          verdicts({"r0-1": "confirmed", "r0-2": "refuted"}),
          triage(doing=["r0-1"])])
res = review.run(box.ctx)
n = counts()
h.test_result("sweep: one scout call per group, each with the same diff, covering every lens",
              n.get("review_scout") == 6
              and all("UNIQUE_DIFF_MARK" in r["prompt"] for r in invocations("review_scout"))
              and sorted(r["label"] for r in invocations("review_scout")) == ["sweep-%d" % i for i in range(6)]
              and all(e["name"] in " ".join(r["prompt"] for r in invocations("review_scout")) for e in lenses),
              str(n))
called = sorted(r["label"] for r in invocations("reviewer"))
h.test_result("promote: exactly the lenses with candidates plus Security Sage and North Star Nick",
              called == sorted(["Uncle Bob", "Rachel", "Security Sage", "North Star Nick"]), str(called))
by_label = {r["label"]: r["prompt"] for r in invocations("reviewer")}
h.test_result("promote: blind, no reviewer prompt holds another reviewer's candidates or findings",
              "NOTE-UB" in by_label["Uncle Bob"] and "NOTE-RACHEL" not in by_label["Uncle Bob"]
              and "NOTE-RACHEL" in by_label["Rachel"] and "NOTE-UB" not in by_label["Rachel"]
              and "NOTE-BOGUS" not in "".join(by_label.values())
              and not any("T-UB" in p or "T-RACHEL" in p or "T-SAGE" in p for p in by_label.values())
              and "UNIQUE_DIFF_MARK" in by_label["Security Sage"], "")
rnd = data(box)["rounds"][0]
fnd = {f["id"]: f for f in rnd["findings"]}
h.test_result("promote: ids assigned by the driver in lens order, lens set to the reviewer name",
              list(fnd) == ["r0-1", "r0-2", "r0-3"] and fnd["r0-1"]["lens"] == "Uncle Bob"
              and fnd["r0-2"]["lens"] == "Security Sage" and fnd["r0-3"]["lens"] == "Rachel", str(list(fnd.values())))
h.test_result("verify: statuses set; an unruled finding counts as confirmed",
              fnd["r0-1"]["status"] == "confirmed" and fnd["r0-2"]["status"] == "refuted"
              and fnd["r0-3"]["status"] == "confirmed" and "did not rule" in fnd["r0-3"]["evidence"], str(fnd))
tri_prompt = invocations("triage")[0]["prompt"]
h.test_result("triage: refuted findings never reach it; goal and non-goals do",
              "T-UB-high" in tri_prompt and "T-RACHEL-medium" in tri_prompt and "T-SAGE-low" not in tri_prompt
              and "Add the a module." in tri_prompt and "Rewrite b" in tri_prompt, "")
t = rnd["triage"]
h.test_result("triage: omitted medium finding is deferred 'not triaged'",
              [i["finding_id"] for i in t["doing_it"]] == ["r0-1"]
              and t["deferred"] == [{"finding_id": "r0-3", "reason": "not triaged"}], str(t))
h.test_result("round is recorded: from base, to head, gut check, buckets on findings",
              rnd["round"] == 0 and rnd["from"] == box.base and rnd["to"] == sh(["git", "rev-parse", "HEAD"], box.clone), "")
h.test_result("done with fix_needed true when there is a doing_it item",
              res.kind == "done" and result_info(res) == {"fix_needed": True}
              and "current" not in data(box) and rnd["gut_check"] == "one shared premise: none"
              and fnd["r0-1"]["bucket"] == "doing_it" and fnd["r0-3"]["bucket"] == "deferred", str(res))
rec = (box.flow.path / "steps").glob("*review*.md")
text = "".join(p.read_text() for p in rec)
h.test_result("step record and journal are written", "T-UB-high" in text and "gut check" in text
              and (box.flow.path / "journal.md").exists(), text[:300])

# ---- no doing_it, no sendback: fix_needed false; unhandled sendback: true -----------------------

box = Box()
box.commit("src/a.py", "x = 1\n")
script(scouts({"Uncle Bob": [{"note": "n"}]}) + [reviewer("Uncle Bob", [finding("only a note")]),
       verdicts({"r0-1": "confirmed"}), triage(deferred=["r0-1"])])
res = review.run(box.ctx)
h.test_result("fix_needed is false with nothing in doing_it", res.kind == "done" and res.info == {"fix_needed": False}, str(res))

box = Box()
box.commit("src/a.py", "x = 1\n")
box.flow.set_data("sendback", {"notes": "please redo the error text", "handled": False})
script([])
res = review.run(box.ctx)
h.test_result("fix_needed is true with an unhandled sendback and no findings",
              res.info == {"fix_needed": True} and counts().get("triage", 0) == 0 and counts().get("verifier", 0) == 0, str(res))
box.flow.set_data("sendback", {"notes": "please redo the error text", "handled": True})
box2 = Box()
box2.commit("src/a.py", "x = 1\n")
box2.flow.set_data("sendback", {"notes": "done already", "handled": True})
script([])
res = review.run(box2.ctx)
h.test_result("a handled sendback does not force a fix", res.info == {"fix_needed": False}, str(res))

# ---- empty diff -------------------------------------------------------------------------------

box = Box()
script([])
res = review.run(box.ctx)
h.test_result("empty diff short-circuits: an empty round, no model calls",
              res.kind == "done" and res.info == {"fix_needed": False} and counts() == {}
              and len(data(box)["rounds"]) == 1 and data(box)["rounds"][0]["findings"] == [], str(counts()))
box = Box()
box.flow.set_data("sendback", {"notes": "n", "handled": False})
res = review.run(box.ctx)
h.test_result("empty diff with an unhandled sendback still needs a fix", res.info == {"fix_needed": True}, str(res))

# ---- round 1 reviews only the fix diff --------------------------------------------------------

box = Box()
c0 = box.commit("src/a.py", "ROUND0_CONTENT = 1\n")
box.flow.set_data("review", {"rounds": [{"round": 0, "from": box.base, "to": c0, "findings": [], "triage": {},
                                         "measurements": [], "gut_check": "", "answers": {}}]})
box.commit("src/b.py", "ROUND1_CONTENT = 2\n")
script([])
res = review.run(box.ctx)
sweeps = invocations("review_scout")
rr = data(box)["rounds"]
h.test_result("round 1 reviews rounds[0].to..HEAD only",
              len(sweeps) == 6 and all("ROUND1_CONTENT" in r["prompt"] and "ROUND0_CONTENT" not in r["prompt"] for r in sweeps)
              and len(rr) == 2 and rr[1]["round"] == 1 and rr[1]["from"] == c0, str(counts()))

# ---- last allowed round: doing_it is listed open, not sent to fix -------------------------------

box = Box()
c0 = box.commit("src/a.py", "a = 1\n")
c1 = box.commit("src/a.py", "a = 2\n")
stub = {"findings": [], "triage": {}, "measurements": [], "gut_check": "", "answers": {}}
box.flow.set_data("review", {"rounds": [dict(stub, round=0, **{"from": box.base, "to": c0}),
                                        dict(stub, round=1, **{"from": c0, "to": c1})]})
box.commit("src/a.py", "a = 3\n")
assert config.MAX_REVIEW_ROUNDS - 1 == 2
script(scouts({"Uncle Bob": [{"note": "n"}]}) + [reviewer("Uncle Bob", [finding("late high", "high")]),
        verdicts({"r2-1": "confirmed"}), triage(doing=["r2-1"])])
res = review.run(box.ctx)
last = data(box)["rounds"][2]
op = review.open_findings(box.flow)
h.test_result("last allowed round: fix_needed false, doing_it stays open",
              res.info == {"fix_needed": False} and last["last_round"] is True
              and [i["finding_id"] for i in last["triage"]["doing_it"]] == ["r2-1"]
              and [f["id"] for f in op] == ["r2-1"] and op[0]["reason"] == "not fixed", str(res) + str(op))
box = Box()
c0 = box.commit("src/a.py", "a = 1\n")
c1 = box.commit("src/a.py", "a = 2\n")
box.flow.set_data("review", {"rounds": [dict(stub, round=0, **{"from": box.base, "to": c0}),
                                        dict(stub, round=1, **{"from": c0, "to": c1})]})
box.commit("src/a.py", "a = 3\n")
script(scouts({"Uncle Bob": [{"note": "n"}]}) + [reviewer("Uncle Bob", [finding("late ask", "medium")]),
        verdicts({"r2-1": "confirmed"}), triage(needs_you=[question("r2-1")])])
res = review.run(box.ctx)
questions.apply_reply(box.ctx, "r2-1: fix")
res = review.run(box.ctx)
h.test_result("last allowed round: a human `fix` answer asks for one more fix run",
              res.kind == "done" and res.info == {"fix_needed": False, "human_fix": True}, str(res))

h.test_result("human_fix_ids reads only human `fix` answers",
              review.human_fix_ids({"answers": {"a": {"option_id": "fix", "source": "human"},
                                                "b": {"option_id": "leave", "source": "human"},
                                                "c": {"option_id": "fix", "source": "principle"}}}) == ["a"])
h.test_result("last allowed round: triage is told it is the last round", "last review round" in invocations("triage")[0]["prompt"])

# ---- needs_you: park, re-entry without redoing anything ---------------------------------------

box = Box()
box.commit("src/a.py", "x = 1\n")
script(scouts({"Uncle Bob": [{"note": "n"}]})
       + [reviewer("Uncle Bob", [finding("first thing", "medium"), finding("second thing", "medium")]),
          verdicts({"r0-1": "confirmed", "r0-2": "confirmed"}),
          triage(needs_you=[question("r0-1"),
                            question("r0-2", options=[{"id": "x", "label": "Option X", "consequence": "cx"},
                                                      {"id": "y", "label": "Option Y", "consequence": "cy"}], rec="x")])])
res = review.run(box.ctx)
batch = questions.open_batch(box.flow)
before = counts()
cards = {c["id"]: c for c in (batch or {}).get("cards", [])}
h.test_result("needs_you parks with complete cards that always offer fix and leave",
              res.kind == "needs_human" and sorted(cards) == ["r0-1", "r0-2"]
              and all({"fix", "leave"} <= {o["id"] for o in c["options"]} for c in cards.values())
              and {"x", "y"} <= {o["id"] for o in cards["r0-2"]["options"]}
              and data(box)["phase"] == "waiting_answers" and not data(box)["rounds"], str(res))
questions.apply_reply(box.ctx, "r0-1: fix\nr0-2: leave")
res = review.run(box.ctx)
after = counts()
rnd = data(box)["rounds"][0]
h.test_result("re-entry after answers: fix moves to doing_it, leave to deferred",
              res.kind == "done" and res.info == {"fix_needed": True}
              and [i["finding_id"] for i in rnd["triage"]["doing_it"]] == ["r0-1"]
              and [i["finding_id"] for i in rnd["triage"]["deferred"]] == ["r0-2"] and rnd["triage"]["needs_you"] == []
              and rnd["answers"]["r0-1"]["option_id"] == "fix" and rnd["answers"]["r0-2"]["source"] == "human"
              and "suggest first thing" in rnd["triage"]["doing_it"][0]["fix"], str(rnd["triage"]))
h.test_result("re-entry does not rerun sweep, promote, verify or triage", before == after, "%s vs %s" % (before, after))

# ---- mid-round re-entry after promote ----------------------------------------------------------

box = Box()
head = box.commit("src/a.py", "x = 1\n")
f1 = {"id": "r0-1", "lens": "Uncle Bob", "file": "src/a.py", "line": 1, "severity": "high", "title": "saved finding",
      "detail": "d", "suggestion": "s"}
box.flow.set_data("review", {"rounds": [], "phase": "promoted", "current": {
    "round": 0, "from": box.base, "to": head, "sweep": {"Uncle Bob": [{"note": "n"}]}, "findings": [f1],
    "promoted": ["Uncle Bob"]}})
script([verdicts({"r0-1": "confirmed"}), triage(doing=["r0-1"])])
res = review.run(box.ctx)
h.test_result("re-entry mid-round does not redo the sweep or promote",
              counts() == {"verifier": 1, "triage": 1} and res.info == {"fix_needed": True}
              and data(box)["rounds"][0]["findings"][0]["title"] == "saved finding", str(counts()))

# ---- measurements -----------------------------------------------------------------------------

box = Box()
box.commit("src/a.py", "x = 1\n", msg="the measured commit")
script(scouts({"Uncle Bob": [{"note": "n"}]})
       + [reviewer("Uncle Bob", [finding("m1"), finding("m2"), finding("m3")]),
          verdicts({"r0-1": "needs_measurement", "r0-2": "needs_measurement", "r0-3": "needs_measurement"}),
          triage(measure=[
              {"finding_id": "r0-1", "command": "git log --oneline", "read_only": True, "confirms_if": "two commits"},
              {"finding_id": "r0-2", "command": "python3 -m pytest", "read_only": True, "confirms_if": "red"},
              {"finding_id": "r0-3", "command": "git log --oneline", "read_only": False, "confirms_if": "x"}])])
res = review.run(box.ctx)
ms = {m["finding_id"]: m for m in data(box)["rounds"][0]["measurements"]}
h.test_result("measurement: a read-only command is run in the worktree and recorded",
              ms["r0-1"]["ran"] is True and ms["r0-1"]["exit_code"] == 0 and "the measured commit" in ms["r0-1"]["output"]
              and ms["r0-1"]["command"] == "git log --oneline", str(ms["r0-1"]))
h.test_result("measurement: a non-allowlisted or not-read-only command is not run",
              ms["r0-2"]["ran"] is False and ms["r0-3"]["ran"] is False and ms["r0-2"]["exit_code"] is None, str(ms))
h.test_result("measurement: no questions, fix_needed false", res.info == {"fix_needed": False}, str(res))
op = review.open_findings(box.flow)
h.test_result("open_findings lists unrun measurements, not ones that ran",
              sorted(f["id"] for f in op) == ["r0-2", "r0-3"] and all(f["reason"] == "measurement not run" for f in op)
              and op[0]["command"] != "", str(op))

# ---- open_findings and summary with fix rounds ---------------------------------------------------

box = Box()
box.flow.set_data("review", {"rounds": [{
    "round": 0, "from": "a", "to": "b", "gut_check": "", "answers": {}, "measurements": [],
    "triage": {"doing_it": [], "needs_you": [], "needs_measurement": [], "deferred": []},
    "findings": [
        {"id": "r0-1", "status": "confirmed", "bucket": "doing_it", "title": "fixed one"},
        {"id": "r0-2", "status": "confirmed", "bucket": "doing_it", "title": "still open"},
        {"id": "r0-3", "status": "confirmed", "bucket": "deferred", "title": "later"},
        {"id": "r0-4", "status": "refuted", "title": "wrong"},
        {"id": "r0-5", "status": "needs_measurement", "bucket": "needs_measurement", "title": "measure me"}]}]})
box.flow.set_data("fix", {"rounds": [{"review_round": 0, "fixed": ["r0-1"], "open": ["r0-2"], "head": "c"}]})
sm = review.summary(box.flow)
h.test_result("summary: counts across rounds using fix rounds",
              sm == {"rounds": 1, "findings": 4, "refuted": 1, "fixed": 1, "open": 2, "deferred": 1,
                     "needs_measurement": 1, "measurements_not_run": 1, "needs_you_pending": 0}, str(sm))
h.test_result("open_findings: the unfixed doing_it and the unrun measurement",
              [f["id"] for f in review.open_findings(box.flow)] == ["r0-2", "r0-5"])

h.summarize_and_exit()
