#!/usr/bin/env python3
"""Tests for scripts/flow3/runner.py, driven by tests/flow3_fake_claude.py. No real model is called.

Run with: python3 tests/test_flow3_runner.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))
from scripts.flow3 import runner  # noqa: E402
from scripts.flow3.types import CallResult, CallSpec  # noqa: E402

FAKE = REPO_ROOT / "tests" / "flow3_fake_claude.py"
_fspec = importlib.util.spec_from_file_location("_flow3_fake", FAKE)
assert _fspec is not None and _fspec.loader is not None
fake = importlib.util.module_from_spec(_fspec)
_fspec.loader.exec_module(fake)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["done", "stuck"]}, "n": {"type": "number"},
                   "tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["status", "n"],
}


class Sink:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.events: List[Dict[str, Any]] = []
        self.count = 0

    def next_call_dir(self, role: str) -> Path:
        self.count += 1
        d = self.root / "calls" / ("%03d-%s" % (self.count, role))
        d.mkdir(parents=True)
        return d

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append(dict(kind=kind, **fields))


def spec(cwd: Path, **kw: Any) -> CallSpec:
    base: Dict[str, Any] = dict(role="judge", prompt="do the thing", schema=SCHEMA, cwd=cwd, label="L1")
    base.update(kw)
    return CallSpec(**base)


def main() -> None:
    h = Harness("Flow v3 runner")
    tmp = Path(tempfile.mkdtemp(prefix="flow3-runner-"))
    home = tmp / "home"
    work = tmp / "work"
    work.mkdir()
    os.environ["FLOW3_HOME"] = str(home)
    os.environ["FLOW3_CLAUDE_BIN"] = str(FAKE)
    os.environ.pop("FLOW3_SLOTS", None)
    log = tmp / "log.jsonl"
    os.environ["FLOW3_FAKE_LOG"] = str(log)
    script_path = tmp / "script.json"
    os.environ["FLOW3_FAKE_SCRIPT"] = str(script_path)
    counter = [0]

    def script(calls: List[Dict[str, Any]], default: Optional[Dict[str, Any]] = None) -> None:
        counter[0] += 1
        path = tmp / ("script%d.json" % counter[0])
        doc: Dict[str, Any] = {"calls": calls}
        if default is not None:
            doc["default"] = default
        path.write_text(json.dumps(doc))
        os.environ["FLOW3_FAKE_SCRIPT"] = str(path)

    def last_log() -> Dict[str, Any]:
        return json.loads(log.read_text().strip().splitlines()[-1])

    # success with schema, call dir, event
    script([{"output": {"status": "done", "n": 3}, "cost": 0.5, "session_id": "S1"}])
    sink = Sink(tmp / "flow")
    r = runner.run(spec(work), sink)
    h.test_result("success with schema", r.ok and r.output == {"status": "done", "n": 3} and r.session_id == "S1"
                  and r.cost_usd == 0.5 and r.parsed and r.num_turns == 2 and r.exit_code == 0
                  and r.model == "sonnet" and r.rate_limit is not None, repr(r))
    cd = tmp / "flow" / "calls" / "001-judge"
    names = sorted(p.name for p in cd.iterdir())
    h.test_result("call dir files", names == ["argv.json", "meta.json", "output.json", "prompt.md", "raw.json",
                                              "stderr.txt"], str(names))
    h.test_result("prompt and argv recorded", (cd / "prompt.md").read_text() == "do the thing"
                  and json.loads((cd / "argv.json").read_text())[-1] == "<prompt.md>")
    meta = json.loads((cd / "meta.json").read_text())
    h.test_result("meta has label and fields", meta["label"] == "L1" and meta["ok"] is True
                  and meta["model"] == "sonnet" and meta["cost_usd"] == 0.5, str(meta))
    ev = sink.events[0]
    h.test_result("call event", ev["kind"] == "call" and ev["role"] == "judge" and ev["label"] == "L1"
                  and ev["ok"] is True and ev["call_dir"] == "001-judge" and ev["session_id"] == "S1"
                  and ev["cost_usd"] == 0.5 and ev["subtype"] == "success", str(ev))
    h.test_result("result carries call_dir", r.call_dir == "001-judge")

    # argv
    script([])
    runner.run(spec(work, tools=(), schema=SCHEMA, model="opus", max_turns=7))
    argv = last_log()["argv"]
    h.test_result("argv: empty tools, schema, model, turns, no resume",
                  argv[argv.index("--tools") + 1] == "" and argv[argv.index("--model") + 1] == "opus"
                  and argv[argv.index("--max-turns") + 1] == "7" and "--resume" not in argv
                  and "--allowedTools" not in argv and json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
                  and "--safe-mode" in argv and argv[argv.index("--permission-mode") + 1] == "dontAsk"
                  and argv[-1] == "do the thing", str(argv))
    runner.run(spec(work, tools=("Read", "Edit"), allowed_tools=("Edit", "Bash(git status:*)"),
                    resume_session="SESS", schema={}))
    line = last_log()
    argv = line["argv"]
    h.test_result("argv: tools, allowed, resume, no schema",
                  argv[argv.index("--tools") + 1] == "Read,Edit" and line["resume"] == "SESS"
                  and [argv[i + 1] for i, a in enumerate(argv) if a == "--allowedTools"]
                  == ["Edit", "Bash(git status:*)"] and "--json-schema" not in argv
                  and os.path.realpath(line["cwd"]) == os.path.realpath(str(work)), str(line))
    h.test_result("default model and turns from config", runner.build_argv(spec(work), "haiku")[
        runner.build_argv(spec(work), "haiku").index("--max-turns") + 1] == "20")
    argv = runner.build_argv(spec(work), "haiku")
    h.test_result("the prompt follows `--` so a variadic option cannot swallow it",
                  argv[-2] == "--" and argv[-1] == spec(work).prompt)
    r = runner.run(spec(work, role="implementer"), overrides={"*": "haiku"})
    h.test_result("overrides pick model", r.model == "haiku" and last_log()["model"] == "haiku")

    # no schema
    script([{"output": {"anything": 1}}])
    r = runner.run(spec(work, schema={}))
    h.test_result("no schema: ok, output None", r.ok and r.output is None and r.schema_errors == ())

    # max turns
    script([{"mode": "max_turns"}])
    r = runner.run(spec(work))
    h.test_result("max turns", not r.ok and r.subtype == "error_max_turns" and r.is_error
                  and r.terminal_reason == "max_turns" and r.exit_code == 1 and r.output is None, repr(r))

    # api error
    script([{"mode": "api_error", "status": 404}])
    r = runner.run(spec(work))
    h.test_result("api error 404", not r.ok and r.api_error_status == 404 and r.subtype == "success"
                  and r.is_error and r.terminal_reason == "api_error" and "404" in r.result_text, repr(r))

    # limit
    resets = time.time() + 5000
    script([{"mode": "limit", "resets_at": int(resets)}])
    r = runner.run(spec(work))
    h.test_result("limit result", not r.ok and r.api_error_status == 429 and r.rate_limit is not None
                  and r.rate_limit["status"] == "rejected", repr(r))
    remaining = runner.limit_wait_remaining()
    h.test_result("limit noted", 4900 < remaining <= 5001, str(remaining))

    # sleeping out the back-off uses _sleep (replaced)
    slept: List[float] = []
    real_sleep = runner._sleep
    runner._sleep = lambda s: slept.append(s)  # type: ignore[assignment]
    script([{"output": {"status": "done", "n": 1}}])
    runner.run(spec(work))
    runner._sleep = real_sleep
    h.test_result("waits out shared back-off before launching", len(slept) == 1 and 4900 < slept[0] <= 5001,
                  str(slept))
    (home / "limit.json").unlink()

    # note_limit round trip
    h.test_result("no limit file: zero", runner.limit_wait_remaining() == 0.0)
    runner.note_limit(time.time() + 100)
    h.test_result("note_limit round trip", 90 < runner.limit_wait_remaining() <= 100)
    runner.note_limit(time.time() + 10)
    h.test_result("note_limit keeps the later", runner.limit_wait_remaining() > 90)
    runner.note_limit(time.time() + 500)
    h.test_result("note_limit extends", runner.limit_wait_remaining() > 400)
    runner.note_limit(time.time() - 5)
    (home / "limit.json").write_text("garbage")
    h.test_result("corrupt limit file is zero", runner.limit_wait_remaining() == 0.0)
    (home / "limit.json").unlink()

    # empty stdout
    script([{"mode": "empty", "exit": 1, "stderr": "No conversation found with session ID: X"}])
    r = runner.run(spec(work, resume_session="X"))
    h.test_result("empty stdout with stderr", not r.ok and not r.parsed and r.exit_code == 1
                  and "No conversation found" in r.stderr and r.output is None, repr(r))
    sink = Sink(tmp / "flow2")
    script([{"mode": "empty", "exit": 1, "stderr": "boom"}])
    runner.run(spec(work), sink)
    d2 = tmp / "flow2" / "calls" / "001-judge"
    h.test_result("raw.txt for non-JSON, stderr saved", (d2 / "raw.txt").exists() and not (d2 / "raw.json").exists()
                  and (d2 / "stderr.txt").read_text() == "boom" and json.loads((d2 / "output.json").read_text()) is None)

    # crash
    script([{"mode": "crash"}])
    r = runner.run(spec(work))
    h.test_result("crash exit 143", not r.ok and r.exit_code == 143 and not r.parsed)

    # denied
    script([{"mode": "denied", "output": {"status": "done", "n": 1}}])
    r = runner.run(spec(work))
    h.test_result("denials present, still ok", r.ok and len(r.permission_denials) == 1
                  and r.permission_denials[0]["tool_input"]["command"] == "rm -rf x", repr(r))

    # no structured output
    script([{"mode": "no_output"}])
    r = runner.run(spec(work))
    h.test_result("no structured output is not ok", not r.ok and r.parsed and r.output is None
                  and len(r.schema_errors) == 1, repr(r))

    # schema-invalid
    script([{"output": {"status": "weird", "n": "x"}}])
    r = runner.run(spec(work))
    h.test_result("schema-invalid output", not r.ok and len(r.schema_errors) >= 1 and r.exit_code == 0
                  and r.output == {"status": "weird", "n": "x"}, repr(r))

    # timeout
    script([{"mode": "hang", "seconds": 30}])
    t0 = time.time()
    r = runner.run(spec(work, timeout_secs=1))
    h.test_result("timeout kills the process", r.timed_out and not r.ok and time.time() - t0 < 8,
                  "%s %r" % (time.time() - t0, r))

    # write_files and run
    script([{"write_files": {"sub/a.txt": "hi"}, "run": "echo ran > ran.txt"}])
    runner.run(spec(work))
    h.test_result("write_files and run happen in cwd", (work / "sub" / "a.txt").read_text() == "hi"
                  and (work / "ran.txt").read_text().strip() == "ran")

    # slots
    os.environ["FLOW3_SLOTS"] = "2"
    log.write_text("")
    script([], default={"sleep": 0.4})
    threads = [threading.Thread(target=runner.run, args=(spec(work, label="t%d" % i),)) for i in range(4)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    points = sorted([(x["start"], 1) for x in rows] + [(x["end"], -1) for x in rows])
    peak = cur = 0
    for _, delta in sorted(points, key=lambda p: (p[0], p[1])):
        cur += delta
        peak = max(peak, cur)
    h.test_result("slot cap respected", len(rows) == 4 and peak <= 2 and time.time() - t0 >= 0.75,
                  "peak=%d rows=%d" % (peak, len(rows)))
    h.test_result("slot lock files created", len(list((home / "slots").glob("slot-*.lock"))) == 2)
    os.environ.pop("FLOW3_SLOTS")

    # parse_events edge cases
    res = {"type": "result", "subtype": "success"}
    h.test_result("parse: empty", runner.parse_events("") is None and runner.parse_events("  \n") is None)
    h.test_result("parse: non-JSON", runner.parse_events("not json") is None)
    h.test_result("parse: array without result", runner.parse_events(json.dumps([{"type": "system"}])) is None)
    h.test_result("parse: single object", runner.parse_events(json.dumps(res)) == res)
    h.test_result("parse: last result wins", runner.parse_events(json.dumps(
        [{"type": "result", "n": 1}, {"type": "system"}, {"type": "result", "n": 2}]))["n"] == 2)
    h.test_result("parse: junk elements tolerated", runner.parse_events(json.dumps([1, "x", None, res])) == res)
    h.test_result("parse: non-event JSON", runner.parse_events("42") is None and runner.parse_events("null") is None)
    rl = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}
    h.test_result("rate_limit_info", runner.rate_limit_info(json.dumps([rl, res])) == {"status": "allowed"}
                  and runner.rate_limit_info(json.dumps([res])) is None and runner.rate_limit_info("") is None)

    # fake: role/label matching and schema default
    script([{"role": "reviewer", "output": {"status": "stuck", "n": 9}},
            {"role": "judge", "label": "B", "output": {"status": "done", "n": 2}}])
    r1 = runner.run(spec(work, label="A"))
    r2 = runner.run(spec(work, label="B"))
    r3 = runner.run(spec(work, role="reviewer", label="C"))
    h.test_result("fake matches role and label, in order", r1.output == {"status": "done", "n": 0}
                  and r2.output == {"status": "done", "n": 2} and r3.output == {"status": "stuck", "n": 9},
                  "%r %r %r" % (r1.output, r2.output, r3.output))
    h.test_result("fake generates from schema", fake.generate({
        "type": "object", "required": ["a", "b", "c", "d", "e"],
        "properties": {"a": {"type": "string"}, "b": {"enum": ["z", "y"]}, "c": {"type": "boolean"},
                       "d": {"type": "array", "minItems": 1, "items": {"type": "number"}},
                       "e": {"type": "object", "required": ["f"], "properties": {"f": {"type": "array"}}},
                       "g": {"type": "string"}}}) == {"a": "", "b": "z", "c": False, "d": [0], "e": {"f": []}})
    script([{"output": {"status": "done", "n": 1}}], default={"output": {"status": "stuck", "n": 5}})
    outs = [runner.run(spec(work)).output for _ in range(3)]
    h.test_result("fake default after entries run out", outs == [{"status": "done", "n": 1}] + [
        {"status": "stuck", "n": 5}] * 2, str(outs))

    # missing binary never raises
    os.environ["FLOW3_CLAUDE_BIN"] = str(tmp / "nope")
    r = runner.run(spec(work))
    h.test_result("missing binary returns a failed result", isinstance(r, CallResult) and not r.ok
                  and r.exit_code == 127 and "could not launch" in r.stderr)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
