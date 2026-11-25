#!/usr/bin/env python3
"""Tests for scripts/flow3/voice.py. No real model is called: the fake claude binary answers."""

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

from scripts.flow3 import context, repoconfig, voice  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.types import FlowStop  # noqa: E402


def sh(args, cwd):
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


def make_ctx(issue=1, title="Voice ticket"):
    origin = TMP / ("o%d.git" % issue)
    clone = TMP / ("c%d" % issue)
    sh(["git", "init", "--bare", "-q", "-b", "main", str(origin)], TMP)
    sh(["git", "clone", "-q", str(origin), str(clone)], TMP)
    sh(["git", "config", "user.name", "t"], clone)
    sh(["git", "config", "user.email", "t@example.invalid"], clone)
    (clone / ".claude").mkdir()
    (clone / ".claude" / "flow.yaml").write_text("provider: local\ncheck: {full: 'true'}\n")
    (clone / "README.md").write_text("hi\n")
    sh(["git", "add", "-A"], clone)
    sh(["git", "commit", "-q", "-m", "init"], clone)
    sh(["git", "push", "-q", "origin", "main"], clone)
    issues = origin / "flow3" / "issues"
    issues.mkdir(parents=True)
    (issues / ("%d.md" % issue)).write_text("# %s\n\nDo the thing.\n" % title)
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


CARDS = [
    {"id": "q1", "decision": "Use a queue?", "recommended": "a",
     "options": [{"id": "a", "label": "Yes", "consequence": "c1"}, {"id": "b", "label": "No", "consequence": "c2"}]},
    {"id": "q2", "decision": "Name it?", "recommended": "x",
     "options": [{"id": "x", "label": "Foo", "consequence": "c1"}, {"id": "y", "label": "Bar", "consequence": "c2"}]},
]

h = Harness("Flow v3 voice")
ctx = make_ctx()

REPLY = "Queue yes, because I hate polling. Call it Bar. And stop adding wrappers around everything."
out_doc = {"answers": [
    {"question_id": "q1", "option_id": "a", "answer": "Yes", "reason_quote": "I hate polling", "confidence": "high"},
    {"question_id": "q2", "option_id": "y", "answer": "Bar", "reason_quote": "", "confidence": "low"}],
    "directions": [{"text": "Do not add wrappers", "quote": "stop adding wrappers around everything"}],
    "unplaced": []}
script([{"role": "voice", "output": out_doc}])
got = voice.interpret(ctx, REPLY, CARDS)
calls = invocations("voice")
h.test_result("interpret returns the validated output", got == out_doc, repr(got))
h.test_result("interpret makes one voice call with no tools", len(calls) == 1 and calls[0]["argv"][calls[0]["argv"].index("--tools") + 1] == "")
h.test_result("interpret prompt carries the reply and the cards",
              REPLY in calls[0]["prompt"] and "q1" in calls[0]["prompt"] and "Use a queue?" in calls[0]["prompt"]
              and "{{" not in calls[0]["prompt"])
h.test_result("interpret runs on the voice role's model", calls[0]["model"] == "sonnet")

script([{"role": "voice", "mode": "api_error", "status": 404}], default={"mode": "api_error", "status": 404})
try:
    voice.interpret(ctx, "anything", CARDS)
    stopped = False
except FlowStop as exc:
    stopped = exc.cls == "runner"
h.test_result("a failed voice call is a runner stop", stopped)

h.test_result("no directions yet", voice.directions_text(ctx.flow) == "")
voice.say(ctx.flow, "Keep it small.")
voice.say(ctx.flow, "No new dependencies.", source="reply")
dirs = ctx.flow.get("directions")
h.test_result("say adds directions with source, ts and verbatim path",
              [d["text"] for d in dirs] == ["Keep it small.", "No new dependencies."]
              and [d["source"] for d in dirs] == ["say", "reply"] and all(d["ts"] for d in dirs)
              and all((ctx.flow.path / d["verbatim"]).read_text() == d["text"] for d in dirs)
              and dirs[0]["verbatim"].endswith("-direction.md"), repr(dirs))
text = voice.directions_text(ctx.flow)
h.test_result("directions_text is a numbered list",
              text == "Directions from the human, binding on this step:\n1. Keep it small.\n2. No new dependencies.", text)

h.summarize_and_exit()
