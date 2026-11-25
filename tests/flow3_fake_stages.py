"""
Fake stage functions for the CLI, inbox and fleet tests. Not a test file: select it with
FLOW3_STAGES=tests.flow3_fake_stages (the registry's test seam).

What each stage does comes from a JSON file named by FLOW3_FAKE_PLAN:
    {"<issue>": {"<stage>": {"mode": "done" | "needs_human" | "stop", "secs": 0.5}}}
A missing entry means mode "done", no delay. FLOW3_FAKE_LOG, when set, gets one line per stage start and
end ("<time> <issue> <stage> start|end") so a test can check how many tickets ran at once.
"""

import json
import os
import time
from typing import Any, Callable, Dict

from scripts.flow3 import questions
from scripts.flow3.types import FlowStop, StageResult

STAGE_NAMES = ("worktree", "plan", "spec_tests", "implement", "review", "fix", "ship", "merge")


def _plan(issue: int, stage: str) -> Dict[str, Any]:
    path = os.environ.get("FLOW3_FAKE_PLAN")
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        doc = json.load(handle)
    row = (doc.get(str(issue)) or {}).get(stage) or {}
    return row if isinstance(row, dict) else {}


def _log(issue: int, stage: str, what: str) -> None:
    path = os.environ.get("FLOW3_FAKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%.3f %d %s %s\n" % (time.time(), issue, stage, what))


def _question(issue: int, stage: str) -> Dict[str, Any]:
    return {"id": "q-%d-%s" % (issue, stage), "kind": "risk", "reversible": True, "themes": ["fake"],
            "question": "Ship the fake %s step for #%d?" % (stage, issue),
            "context": "The fake %s step wants a human to choose." % stage,
            "risk": "none: this is a test",
            "options": [{"id": "go", "label": "Go ahead", "consequence": "The flow continues"},
                        {"id": "wait", "label": "Hold", "consequence": "The flow stays parked"}],
            "recommended": "go"}


def _make(stage: str) -> Callable[[Any], StageResult]:
    def run(ctx: Any) -> StageResult:
        issue = int(ctx.flow.get("issue"))
        spec = _plan(issue, stage)
        _log(issue, stage, "start")
        try:
            time.sleep(float(spec.get("secs") or 0))
            mode = str(spec.get("mode") or "done")
            if mode == "stop":
                raise FlowStop("runner", "fake stop at %s for #%d" % (stage, issue))
            if mode == "needs_human":
                batch = questions.open_batch(ctx.flow)
                if batch is not None and batch.get("stage") == stage:
                    questions.take_answers(ctx, stage)  # answered: carry on
                else:
                    _, cards = questions.resolve(ctx, stage, [_question(issue, stage)])
                    if cards:
                        return StageResult("needs_human")
            return StageResult("done", {"fix_needed": False} if stage == "review" else {})
        finally:
            _log(issue, stage, "end")
    return run


REGISTRY: Dict[str, Callable[[Any], StageResult]] = {name: _make(name) for name in STAGE_NAMES}
