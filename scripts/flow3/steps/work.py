"""
Shared machinery for stages that have a model edit files (implement now, fix later).

The split of duties for a worker's questions: `run_unit` and `run_unit_checked` make ONE write call and
(unless the worker asked questions, or the stage passed `check=False` because it checks once after its
last unit) run the check. They never swallow a `needs_answers` result. The
returned CallResult still carries it, the check is not run (the tree is mid-unit), and the calling
stage owns what happens next: resolve the questions through `questions.resolve`, park for the human if
a card is open, and resume the worker with `resume_worker`. After the answers are in, the stage calls
`check_unit` to run the check for the finished unit.
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, cast

from scripts.flow3 import config, heal, prompts, schemas, voice
from scripts.flow3.context import Ctx
from scripts.flow3.types import CallResult, CallSpec, CheckRun

# Tools a worker that edits files may use. Reads and edits only: the driver runs the check itself.
WRITE_TOOLS: Tuple[str, ...] = ("Read", "Edit", "Write", "Grep", "Glob")
# Tools for a call that only looks.
READ_TOOLS: Tuple[str, ...] = ("Read", "Grep", "Glob")


def protected_paths(ctx: Ctx) -> List[str]:
    """The repo's protected paths plus the spec tests recorded by the spec_tests stage."""
    out: List[str] = list(ctx.cfg.protected_paths)
    for path in ctx.flow.get("protected") or []:
        if str(path) not in out:
            out.append(str(path))
    return out


def list_text(items: Sequence[Any], empty: str = "(none)") -> str:
    """A dash list, or `empty` when there is nothing to list."""
    lines = ["- %s" % str(i) for i in items if str(i).strip()]
    return "\n".join(lines) if lines else empty


def interfaces_text(plan: Dict[str, Any]) -> str:
    """The plan's interface signatures, one per line."""
    rows: List[str] = []
    for item in plan.get("interfaces") or []:
        if not isinstance(item, dict):
            continue
        where = " (in %s)" % item["file"] if item.get("file") else ""
        rows.append("- %s: %s%s" % (item.get("name"), item.get("signature"), where))
    return "\n".join(rows) if rows else "(none)"


def worker_spec(ctx: Ctx, role: str, prompt: str, label: str, model: Optional[str] = None,
                resume_session: Optional[str] = None) -> CallSpec:
    """A CallSpec for a call that edits files in the ticket worktree."""
    return CallSpec(role=role, prompt=prompt, schema=schemas.BY_ROLE[role], cwd=ctx.worktree, model=model,
                    tools=WRITE_TOOLS, allowed_tools=WRITE_TOOLS, resume_session=resume_session,
                    writes=True, label=label)


def needs_answers(result: CallResult) -> bool:
    """True when the worker stopped to ask questions instead of finishing."""
    return isinstance(result.output, dict) and result.output.get("status") == "needs_answers"


def check_fixer(ctx: Ctx, allowed: Sequence[str]) -> Callable[[CheckRun, int], None]:
    """
    The fixer heal.check calls on a red check. Round 1 is one `fixer` call on the role's configured
    model with the trimmed failure; round 2 is the same on the next model up. Both edit only `allowed`
    and never protected paths, and are committed by heal.write_call.
    """
    allowed_list = list(allowed)

    def fix(run: CheckRun, rnd: int) -> None:
        base = config.model_for("fixer", ctx.cfg.models)
        model = base if rnd <= 1 else config.stronger(base)
        task = ("The %s check failed. Fix the code so it passes. This is fixer round %d."
                "\n\nCheck output (trimmed):\n%s" % (run.mode, rnd, run.trimmed or "(no output was captured)"))
        prompt = fixer_prompt(ctx, task, allowed_list, protected_paths(ctx))
        spec = worker_spec(ctx, "fixer", prompt, "fix-r%d" % rnd, model=model)
        heal.write_call(ctx, spec, allowed_list, protected_paths(ctx),
                        "flow3 #%d: fix the %s check (round %d)" % (ctx.issue, run.mode, rnd))

    return fix


def fixer_prompt(ctx: Ctx, task: str, allowed: Sequence[str], protected: Sequence[str]) -> str:
    """The fixer prompt for a task written by the driver."""
    return prompts.render("fixer", task=task, allowed=list_text(allowed), protected=list_text(protected),
                          directions=voice.directions_text(ctx.flow))


def check_unit(ctx: Ctx, fixer_allowed: Sequence[str], raise_on_fail: bool = True,
               use_fixer: bool = True) -> CheckRun:
    """Run the fast check for a unit with the two fixer rounds (or none when `use_fixer` is false)."""
    fixer = check_fixer(ctx, fixer_allowed) if use_fixer else None
    return heal.check(ctx, "fast", fixer=fixer, raise_on_fail=raise_on_fail)


def check_unit_or_blocked(ctx: Ctx, fixer_allowed: Sequence[str], raise_on_fail: bool = True,
                          use_fixer: bool = True) -> Tuple[CheckRun, Optional["heal.Blocked"]]:
    """
    check_unit with a blocked check fixer handled in one place. A fixer that reports it cannot finish
    from here is not retried: the red check is returned as it stands (never raised), together with the
    Blocked that ended the fixing. The caller decides what the red means: implement sends a red spec
    test to the judge and otherwise raises the Blocked; fix leaves the finding open.
    """
    run, blocked = guard_blocked(check_unit, ctx, fixer_allowed, raise_on_fail=raise_on_fail,
                                 use_fixer=use_fixer)
    if blocked is not None:
        run = heal.check(ctx, "fast", fixer=None, raise_on_fail=False)
    return cast(CheckRun, run), blocked


def run_unit_checked(ctx: Ctx, unit: Dict[str, Any], role: str, prompt: str, message: str,
                     fixer_allowed: Optional[Sequence[str]] = None, raise_on_fail: bool = True,
                     use_fixer: bool = True, resume_session: Optional[str] = None,
                     check: bool = True) -> Tuple[CallResult, Optional[CheckRun]]:
    """
    One write call restricted to the unit's files, then the check. Returns the worker's result and the
    CheckRun. The CheckRun is None in two cases with different meanings:
    - Worker asked questions: no check is run until the stage has the answers (see module docstring).
    - check=False: the stage checks once after all units, not per unit.
    When a unit needs no check at all, use run_unit directly instead of this function.
    With raise_on_fail false a red check is returned, not raised.
    """
    files = [str(f) for f in unit["files"]]
    spec = worker_spec(ctx, role, prompt, "unit-%s" % unit.get("id"), resume_session=resume_session)
    result = heal.write_call(ctx, spec, files, protected_paths(ctx), message)
    if needs_answers(result) or not check:
        return result, None
    run = check_unit(ctx, fixer_allowed or files, raise_on_fail=raise_on_fail, use_fixer=use_fixer)
    return result, run


def run_unit(ctx: Ctx, unit: Dict[str, Any], role: str, prompt: str, message: str,
             fixer_allowed: Optional[Sequence[str]] = None, raise_on_fail: bool = True) -> CallResult:
    """
    One heal.write_call (allowed = the unit's files, protected = protected_paths), then the fast check
    healed by check_fixer. Returns the worker's CallResult. A `needs_answers` result is returned as it
    is, without a check, for the stage to handle. With raise_on_fail false, a red check does not stop;
    use run_unit_checked to get the red CheckRun.
    """
    return run_unit_checked(ctx, unit, role, prompt, message, fixer_allowed, raise_on_fail)[0]


def guard_blocked(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Tuple[Optional[Any], Optional["heal.Blocked"]]:
    """
    Call a function that may raise heal.Blocked and return either the result or the caught exception.

    The function's normal return value is returned as the first tuple element. If heal.Blocked is raised,
    None is returned as the first element, and the exception is returned as the second element. Otherwise,
    the second element is None.

    This centralizes Blocked handling in one place: workers and check fixers that raise Blocked are routed
    through a single handler instead of being caught in multiple places (answered-card resume, check fixer
    paths, etc.). Use it like:

        result, blocked = guard_blocked(work.resume_worker, ctx, ...)
        if blocked:
            return self._blocked(unit, blocked)  # or similar handler
    """
    try:
        return fn(*args, **kwargs), None
    except heal.Blocked as exc:
        return None, exc


def resume_worker(ctx: Ctx, role: str, session_id: Optional[str], original_prompt: str, answers_text: str,
                  allowed: Sequence[str], message: str, label: str) -> CallResult:
    """
    Continue a worker that asked questions, with the answers. Resumes its session when one was kept;
    otherwise restates the original prompt. Same bounds as the first call. No check is run here.
    """
    body = prompts.render("work_answers", answers=answers_text, directions=voice.directions_text(ctx.flow))
    prompt = body if session_id else original_prompt + "\n\n" + body
    spec = worker_spec(ctx, role, prompt, label, resume_session=session_id)
    return heal.write_call(ctx, spec, list(allowed), protected_paths(ctx), message)


def read_text(path: str, fallback: str = "") -> str:
    """A file's text, or `fallback` when it cannot be read."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fallback


def cap(text: str, limit: int = 30000) -> str:
    """Cut long text for a prompt, keeping the head and saying so."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (cut: %d more characters)" % (len(text) - limit)
