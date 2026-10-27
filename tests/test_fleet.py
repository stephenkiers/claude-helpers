#!/usr/bin/env python3
"""Tests for scripts/fleet.py (/expert-fleet dispatcher bookkeeping)."""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "fleet.py"
_spec = importlib.util.spec_from_file_location("fleet", SCRIPT)
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

REPO = "o/r"

# A stand-in for `claude -p`: reads the prompt (last argv), writes flow state like /expert-flow would,
# prints the json event array. Fresh run -> hand-off; resume -> park; answer -> done.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
prompt = sys.argv[-1]
flows = Path(os.environ["HOME"]) / ".claude" / "flows" / "o_r"
log = Path(os.environ["FAKE_LOG"])
with open(log, "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
issue = int(os.environ["FAKE_ISSUE"])
fd = flows / f"{issue}-x-abc"
def state(**kw):
    fd.mkdir(parents=True, exist_ok=True)
    st = {"issue": issue, "updated": "2026-10-16T00:00:00Z", "live": None, "attempts": {}}
    st.update(kw)
    (fd / "state.json").write_text(json.dumps(st))
if prompt.startswith("/expert-flow --resume"):
    state(step="plan", status="awaiting", worktree=os.getcwd(), attempts={"plan": 1})
    (fd / "fleet-ask.json").write_text(json.dumps({"status": "open", "kind": "gate", "step": "plan",
        "question_file": None, "report_file": None, "gate": {"header": "Flow stop"}}))
elif prompt.startswith("/expert-flow"):
    wt = Path(os.environ["FAKE_WT"]); wt.mkdir(exist_ok=True)
    state(step="plan", status="paused", worktree=str(wt))
else:
    (fd / "fleet-ask.json").write_text(json.dumps({"status": "answered"}))
    state(step="merge", status="done", pr="https://x/pr/1")
print(json.dumps([{"type": "system"}, {"type": "result", "session_id": "sess-1", "result": "ok", "is_error": False}]))
'''


def write_state(root, issue, **kw):
    d = root / "o_r" / f"{issue}-slug-id"
    d.mkdir(parents=True, exist_ok=True)
    st = {"issue": issue, "updated": "2026-10-16T00:00:00Z", "live": None, "attempts": {}}
    st.update(kw)
    (d / "state.json").write_text(json.dumps(st))
    return d


def main():
    h = Harness("fleet")

    # parse_dep
    h.test_result("int dep is in-repo", fl.parse_dep(5, REPO) == (REPO, 5))
    h.test_result("'#5' is in-repo", fl.parse_dep("#5", REPO) == (REPO, 5))
    h.test_result("cross-repo dep", fl.parse_dep("a/b#7", REPO) == ("a/b", 7))

    # parse_claude_output
    sid, res, err = fl.parse_claude_output(json.dumps([{"type": "system"}, {"type": "result", "session_id": "s", "result": "r", "is_error": False}]))
    h.test_result("parses event array", (sid, res, err) == ("s", "r", False))
    sid, _, err = fl.parse_claude_output(json.dumps({"type": "result", "session_id": "s2", "result": "", "is_error": True}))
    h.test_result("parses single object", sid == "s2" and err)
    h.test_result("non-json is an error", fl.parse_claude_output("boom")[2] is True)

    # claude_cmd
    h.test_result("fresh cmd", fl.claude_cmd("/x", "auto") == ["claude", "-p", "--permission-mode", "auto", "--output-format", "json", "/x"])
    h.test_result("resume cmd", fl.claude_cmd("hi", "auto", "s")[:4] == ["claude", "-p", "--resume", "s"])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "flows"
        data = {"repo": REPO, "max_parallel": 2, "tickets": [
            {"issue": 1, "deps": []},
            {"issue": 2, "deps": [1]},
            {"issue": 3, "deps": []},
            {"issue": 4, "deps": []},
            {"issue": 5, "deps": ["a/b#9"]},
            {"issue": 6, "deps": []},
        ]}
        write_state(root, 1, step="merge", status="paused", pr="https://x/pr/1")
        write_state(root, 3, step="plan", status="paused")
        d4 = write_state(root, 4, step="review", status="awaiting")
        (d4 / "fleet-ask.json").write_text(json.dumps({"status": "open", "kind": "relay", "step": "review",
                                                       "question_file": "questions/review-01.json", "report_file": None}))
        (d4 / "questions").mkdir()
        (d4 / "questions" / "review-01.json").write_text(json.dumps({"questions": [{"id": "q1"}]}))
        write_state(root, 6, step="implement", status="running")
        rows = {r["issue"]: r for r in fl.compute(data, root, closed=lambda r, n: False)}
        h.test_result("held at merge gate with PR", rows[1]["state"] == "held")
        h.test_result("dep on held ticket blocks", rows[2]["state"] == "blocked" and rows[2]["unmet"] == ["#1"])
        h.test_result("hand-off state", rows[3]["state"] == "handoff")
        h.test_result("open ask -> parked", rows[4]["state"] == "parked" and rows[4]["ask"] == "relay")
        h.test_result("cross-repo dep unmet", rows[5]["state"] == "blocked" and rows[5]["unmet"] == ["a/b#9"])
        h.test_result("running flow without process -> stalled", rows[6]["state"] == "stalled")
        h.test_result("ready lists launchable with deps met", fl.ready(data, list(rows.values())) == [3])

        rows2 = {r["issue"]: r for r in fl.compute(data, root, closed=lambda r, n: (r, n) == ("a/b", 9))}
        h.test_result("closed dep is satisfied", rows2[5]["state"] == "pending" and not rows2[5]["unmet"])
        # ready() keeps data["tickets"] order. Both candidates fit under max_parallel=2, so this line checks
        # the ordering only; the cap itself is exercised by the running-ticket assertion below.
        h.test_result("ready keeps ticket order", fl.ready(data, list(rows2.values())) == [3, 5])
        rows2[6]["state"] = "running"
        h.test_result("running ticket takes a slot", fl.ready(data, list(rows2.values())) == [3])

        (root / "o_r" / "1-slug-id" / "state.json").write_text(json.dumps({"issue": 1, "status": "done", "step": "merge"}))
        rows3 = {r["issue"]: r for r in fl.compute(data, root, closed=lambda r, n: False)}
        h.test_result("done in-fleet dep unblocks", rows3[2]["state"] == "pending")

        items = fl.inbox(data, root)
        h.test_result("inbox has the parked relay", len(items) == 1 and items[0]["issue"] == 4
                      and items[0]["question_data"] == {"questions": [{"id": "q1"}]})
        (d4 / "fleet-ask.json").write_text(json.dumps({"status": "answered"}))
        h.test_result("answered ask leaves inbox", fl.inbox(data, root) == [])

        # Regression guard: find_flow picks the newest flow by state.json `updated`, not by directory name.
        # The newer directory ("7-other-id2") sorts before the older one ("7-slug-id"), so a pick by name
        # (max or last glob match) would return the older flow and fail this assertion.
        write_state(root, 7, step="plan", status="failed")
        newer = root / "o_r" / "7-other-id2"
        newer.mkdir()
        (newer / "state.json").write_text(json.dumps({"issue": 7, "updated": "2026-10-17T00:00:00Z", "status": "done"}))
        h.test_result("find_flow newest by updated", fl.find_flow(REPO, 7, root) == newer)

    # wait: settles only on a changed receipt with a decision or awaiting marker
    h.test_result("stub is not settled", not fl._receipt_settled("STUB: x\nDecision: PASS"))
    h.test_result("decision settles", fl._receipt_settled("ATTEMPT: 1\nDecision: PASS"))
    h.test_result("awaiting marker settles", fl._receipt_settled("ATTEMPT: 1\n<!-- awaiting-answers: q -->"))
    with tempfile.TemporaryDirectory() as tmp:
        rc = Path(tmp) / "01-plan.md"
        rc.write_text("ATTEMPT: 1\n<!-- awaiting-answers: questions/plan-01.json -->")
        h.test_result("unchanged settled receipt times out", fl.wait(rc, 0.2, 0.05) == "timeout")
        import threading
        settle = threading.Timer(0.1, lambda: rc.write_text("ATTEMPT: 1\nDecision: PASS"))
        settle.start()
        h.test_result("changed receipt settles", fl.wait(rc, 2, 0.05) == "settled")
        settle.join()

    # End-to-end `run` with a fake claude, in a temp HOME: fresh -> hand-off -> resume -> park, then answer -> done.
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        main_wt = Path(tmp) / "main"
        main_wt.mkdir()
        wt = Path(tmp) / "wt-8"
        log = Path(tmp) / "log.jsonl"
        tickets = Path(tmp) / "t.json"
        tickets.write_text(json.dumps([{"issue": 8, "title": "eight"}]))
        env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               "FAKE_LOG": str(log), "FAKE_ISSUE": "8", "FAKE_WT": str(wt)}

        def cli(*args):
            return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)

        r = cli("init", "--repo", REPO, "--main-worktree", str(main_wt), "--epic", "https://g/o/r/issues/100", "--tickets", str(tickets))
        fleet_dir = Path(r.stdout.strip())
        h.test_result("init creates fleet dir under ~/.claude/fleets", r.returncode == 0 and fleet_dir.is_dir()
                      and str(fleet_dir).startswith(str(home / ".claude" / "fleets" / "o_r" / "100-")))
        h.test_result("ready lists the new ticket", cli("ready", str(fleet_dir)).stdout.split() == ["8"])

        r = cli("run", str(fleet_dir), "8")
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        h.test_result("run: fresh from main, then resume in worktree",
                      [c["argv"][-1] for c in calls] == ["/expert-flow 8 --fleet", "/expert-flow --resume 8 --fleet"]
                      and calls[0]["cwd"] == str(main_wt.resolve()) and Path(calls[1]["cwd"]).resolve() == wt.resolve())
        h.test_result("run reports parked", r.stdout.strip().startswith("FLEET #8 parked (gate at plan)"))
        fdata = json.loads((fleet_dir / "fleet.json").read_text())
        t = fdata["tickets"][0]
        h.test_result("session recorded, pid cleared", t["session"] == "sess-1" and t["pid"] is None and len(t["runs"]) == 2)
        h.test_result("parked ticket is not ready", cli("ready", str(fleet_dir)).stdout.strip() == "")

        msg = Path(tmp) / "m.txt"
        msg.write_text("Fleet answer for Flow stop:\nRetry")
        r = cli("run", str(fleet_dir), "8", "--message-file", str(msg))
        last = json.loads(log.read_text().splitlines()[-1])
        h.test_result("answer resumes the session in its cwd", last["argv"][:3] == ["-p", "--resume", "sess-1"]
                      and last["argv"][-1] == "Fleet answer for Flow stop:\nRetry" and Path(last["cwd"]).resolve() == wt.resolve())
        h.test_result("run reports done with PR", r.stdout.strip() == "FLEET #8 done at merge pr=https://x/pr/1")
        st = json.loads(cli("status", str(fleet_dir), "--json").stdout)
        h.test_result("status json shape", st["rows"][0]["state"] == "done" and st["ready"] == [])

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
