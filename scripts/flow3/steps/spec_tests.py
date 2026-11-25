"""
The spec_tests stage (PLAN.md section 7.3): before any implementation exists, a test author gets the
test spec and the interface signatures only, and writes the tests. The driver commits them and records
their paths as protected. The check is not run here: the tests are meant to be red until the units land.
"""

from typing import Any, Dict, List, Optional

from scripts.flow3 import gitops, heal, prompts, questions, record, voice
from scripts.flow3.context import Ctx
from scripts.flow3.steps import work
from scripts.flow3.types import FlowStop, StageResult

STAGE = "spec_tests"
MAX_QUESTION_ROUNDS = 3


def _plan(ctx: Ctx) -> Dict[str, Any]:
    plan = ctx.flow.data("plan").get("plan")
    return plan if isinstance(plan, dict) else {}


def _test_files(plan: Dict[str, Any]) -> List[str]:
    files: List[str] = []
    for item in plan.get("test_spec") or []:
        path = str(item.get("test_file") or "") if isinstance(item, dict) else ""
        if path and path not in files:
            files.append(path)
    return files


def _spec_text(plan: Dict[str, Any]) -> str:
    rows = []
    for item in plan.get("test_spec") or []:
        if isinstance(item, dict):
            rows.append("- %s: %s (write it in %s)" % (item.get("id"), item.get("behaviour"), item.get("test_file")))
    return "\n".join(rows) or "(none)"


def _prompt(ctx: Ctx, plan: Dict[str, Any], files: List[str]) -> str:
    # Spec-blind by construction: only the test spec, interfaces, how tests run, and the human's
    # directions. Never units, steps, the goal, or expert advice.
    return prompts.render(
        "test_author", test_spec=_spec_text(plan), interfaces=work.interfaces_text(plan),
        test_command=ctx.cfg.check_fast, files=work.list_text(files),
        directions=voice.directions_text(ctx.flow))


def _finish(ctx: Ctx, files: List[str], commit: Optional[str], skipped: str, summary: str) -> StageResult:
    existing = [str(p) for p in ctx.flow.get("protected") or []]
    if files:
        ctx.flow.set("protected", sorted(set(existing + files)))
    ctx.flow.set_data(STAGE, {"files": files, "commit": commit, "skipped": skipped})
    record.write_step_record(ctx.flow, STAGE, [
        ("Spec tests", summary),
        ("Files", work.list_text(files)),
        ("Commit", commit or "(none)"),
    ])
    record.journal(ctx.flow, "Spec tests: %s" % summary)
    return StageResult("done", {})


def run(ctx: Ctx) -> StageResult:
    """Write and commit the spec tests, or record why there are none. Safe to re-enter."""
    data = ctx.flow.data(STAGE)
    if "skipped" in data:
        return StageResult("done", {})
    plan = _plan(ctx)
    files = _test_files(plan)
    if not plan.get("test_spec"):
        reason = str(plan.get("untestable_reason") or "").strip() or "the plan has no test spec"
        return _finish(ctx, [], None, reason, "skipped, the plan says it cannot be tested: %s" % reason)

    wt = ctx.worktree
    prompt = _prompt(ctx, plan, files)
    message = "flow3 #%d: spec tests" % ctx.issue
    start = str(data.get("start_head") or gitops.head_sha(wt))
    protected = list(ctx.cfg.protected_paths)
    rounds = int(data.get("rounds") or 0)

    if data.get("phase") == "waiting":
        answers = questions.take_answers(ctx, STAGE)
        result = work.resume_worker(ctx, "test_author", data.get("session"), prompt,
                                    questions.answers_for_prompt(answers), files, message, "spec-tests-answers")
    else:
        spec = work.worker_spec(ctx, "test_author", prompt, "spec-tests")
        result = heal.write_call(ctx, spec, files, protected, message)

    while work.needs_answers(result):
        rounds += 1
        if rounds > MAX_QUESTION_ROUNDS:
            raise FlowStop("implement", "the test author kept asking questions instead of writing tests",
                           tried=["answered %d rounds of questions" % (rounds - 1)])
        qs = [q for q in (result.output or {}).get("questions") or [] if isinstance(q, dict)]
        auto, cards = questions.resolve(ctx, STAGE, qs)
        if cards:
            ctx.flow.set_data(STAGE, {"phase": "waiting", "session": result.session_id,
                                      "start_head": start, "rounds": rounds})
            return StageResult("needs_human", {})
        result = work.resume_worker(ctx, "test_author", result.session_id, prompt,
                                    questions.answers_for_prompt(auto), files, message, "spec-tests-answers")

    head = gitops.head_sha(wt)
    commit = head if head != start else None
    written = [f for f in files if (wt / f).is_file()]
    if not written:
        raise FlowStop("implement", "the test author wrote none of the spec test files: %s" % ", ".join(files),
                       tried=["one test author call"])
    summary = str((result.output or {}).get("summary") or "tests written")
    return _finish(ctx, written, commit, "", summary)

