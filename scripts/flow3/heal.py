"""
Classify every failure and apply its bounded automatic response (PLAN.md section 5).

Every response is bounded exactly as the table says, recorded as a `heal` event plus a journal line, and
ends either in success or in a `FlowStop` that carries the class, a reason, the evidence file and what
was already tried. No module but runner.py launches `claude`; everything here goes through `runner.run`.
"""

import dataclasses
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from scripts.flow3 import checks, config, gitops, record, runner, schemas
from scripts.flow3.context import Ctx
from scripts.flow3.types import (STOP_FOR, CallResult, CallSpec, CheckRun, Failure,
                                 FlowStop)

# Replaced in tests so waits do not take real time.
_sleep = time.sleep

_OK_LIMIT_STATUSES = ("allowed", "allowed_warning")
_LIMIT_WORDING = re.compile(r"usage limit|rate limit|overloaded", re.IGNORECASE)
_BACKOFF_FIRST_SECS = 60.0
_BACKOFF_CAP_SECS = 1800.0

_MISSING_DEP = re.compile(
    r"ModuleNotFoundError|No module named|command not found|Cannot find module|ERR_MODULE_NOT_FOUND"
    r"|node_modules[^\n]*(?:not found|missing|no such)|(?:missing|no such)[^\n]*node_modules",
    re.IGNORECASE)
_FAILED_TEST_LINES = (
    re.compile(r"^\s*(?:FAILED|FAIL|ERROR)[: ]+(\S.*?)\s*$"),
    re.compile(r"^\s*(?:✗|✘|×)\s*(\S.*?)\s*$"),
    re.compile(r"^\s*not ok \d+\s*-?\s*(\S.*?)\s*$"),
)
_MAX_NAMES = 10
_BLOCKED_TEXT_CAP = 300


class Shortfall(Exception):
    """An implementer or fixer did not finish its unit (`unit-shortfall`). `write_call` handles it."""

    def __init__(self, result: CallResult) -> None:
        super().__init__("unit shortfall: %s call ran out of turns" % result.role)
        self.result = result


# ---------------------------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------------------------

def _denied_commands(result: CallResult) -> List[str]:
    names: List[str] = []
    for denial in result.permission_denials:
        tool_input = denial.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if isinstance(command, str) and command.strip():
            names.append(command.strip())
        else:
            names.append(str(denial.get("tool_name") or "unknown tool"))
    return names


def _is_limit(result: CallResult) -> bool:
    info = result.rate_limit
    if isinstance(info, dict) and info.get("status") is not None \
            and info.get("status") not in _OK_LIMIT_STATUSES:
        return True
    if result.api_error_status in (429, 529):
        return True
    return bool(_LIMIT_WORDING.search(result.result_text or "") or _LIMIT_WORDING.search(result.stderr or ""))


def classify_call(result: CallResult, spec: CallSpec) -> Optional[Failure]:
    """The failure a call shows, or None when it is good. Reads is_error and terminal_reason before subtype."""
    if result.ok:
        return None
    if result.permission_denials:
        cmds = _denied_commands(result)
        return Failure("denied", "a step needed a tool outside its allowlist: %s" % "; ".join(cmds),
                       detail={"commands": cmds})
    if _is_limit(result):
        return Failure("limit", "usage or rate limit reached (%s)" % (
            result.api_error_status or (result.rate_limit or {}).get("status") or "limit wording"))
    if not result.parsed and (result.stderr or "").lstrip().startswith("Error: --"):
        return Failure("transient", "the driver passed a bad argument to claude: %s"
                       % result.stderr.strip()[:200], detail={"no-retry": True})
    status = result.api_error_status
    if status is not None and 400 <= status < 500 and status != 429:
        return Failure("transient", "claude reported an API error %d: %s"
                       % (status, (result.result_text or "").strip()[:200]), detail={"no-retry": True})
    if result.subtype == "error_max_turns" or result.terminal_reason == "max_turns":
        if spec.writes:
            return Failure("unit-shortfall", "the %s call ran out of turns" % spec.role)
        return Failure("transient", "the %s call ran out of turns" % spec.role)
    if result.timed_out:
        return Failure("transient", "the %s call timed out" % spec.role)
    if result.exit_code < 0 or result.exit_code > 128:
        return Failure("transient", "the claude process was killed (exit %d)" % result.exit_code)
    if not result.parsed:
        if "No conversation found" in (result.stderr or ""):
            return Failure("transient", "the session to resume no longer exists")
        return Failure("transient", "claude returned no parseable result (exit %d)" % result.exit_code)
    if status is not None and status >= 500:
        return Failure("transient", "claude reported a server error %d" % status)
    if not result.is_error and result.schema_errors:
        return Failure("schema", "output did not match the schema: %s" % "; ".join(result.schema_errors),
                       detail={"errors": list(result.schema_errors)})
    return Failure("transient", "the %s call failed (%s)" % (result.role, result.subtype or "unknown"))


def _read_output(run: CheckRun) -> str:
    try:
        text = Path(run.output_path).read_text(encoding="utf-8", errors="replace")
        return text[-200000:]
    except (OSError, ValueError):
        return run.trimmed


def _failing_tests(output: str) -> List[str]:
    names: List[str] = []
    for line in output.splitlines():
        for pattern in _FAILED_TEST_LINES:
            match = pattern.match(line)
            if match:
                name = match.group(1)[:200]
                if name not in names:
                    names.append(name)
                break
        if len(names) >= _MAX_NAMES:
            break
    return names


def classify_check(first: CheckRun, rerun: Optional[CheckRun]) -> Failure:
    """Classify a failed check. `rerun` is the immediate second run, when one was made."""
    if first.timed_out:
        return Failure("environment", "the check timed out", evidence=first.output_path,
                       detail={"kind": "timeout"})
    output = _read_output(first)
    if first.exit_code == 127 or _MISSING_DEP.search(output):
        return Failure("environment", "the check could not find a dependency or tool",
                       evidence=first.output_path, detail={"kind": "missing-dependency"})
    if rerun is not None and rerun.ok:
        return Failure("check-flaky", "the check failed once and passed on an immediate re-run",
                       evidence=first.output_path, detail={"tests": _failing_tests(output)})
    return Failure("check-work", "the check fails", evidence=first.output_path,
                   detail={"tests": _failing_tests(output)})


# ---------------------------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------------------------

def _heal(ctx: Ctx, cls: str, action: str, **fields: Any) -> None:
    ctx.flow.event("heal", cls=cls, action=action, **fields)
    record.journal(ctx.flow, "Heal (%s): %s" % (cls, action))


def _call_evidence(ctx: Ctx, result: Optional[CallResult]) -> Optional[str]:
    if result is not None and result.call_dir:
        return str(ctx.flow.path / "calls" / result.call_dir)
    return None


class UnitUnfinished(FlowStop):
    """The unit-shortfall stop: the work so far is committed. `remaining` is what the worker left."""

    remaining: List[str] = []


# What a blocked worker may say it needs -> (stop class, how the stop names the need).
# F14: Derived from schemas.BLOCKED_NEEDS with import-time check to prevent drift.
BLOCKED_NEEDS: Dict[str, Tuple[str, str]] = {
    "shell": ("environment", "a shell command to be run, and it has no shell"),
    "other_file": ("implement", "a change in a file it may not edit"),
    "environment": ("environment", "a change to the environment (a tool, a dependency or a setting)"),
    "human": ("human", "a decision from a person"),
}

# F14: Import-time check that BLOCKED_NEEDS keys match schemas.BLOCKED_NEEDS
assert set(BLOCKED_NEEDS.keys()) == set(schemas.BLOCKED_NEEDS), (
    "BLOCKED_NEEDS keys %s do not match schemas.BLOCKED_NEEDS %s; they must stay in sync"
    % (sorted(BLOCKED_NEEDS.keys()), sorted(schemas.BLOCKED_NEEDS))
)


class Blocked(FlowStop):
    """
    A worker reported that it cannot finish from where it is. This is not a shortfall: the same call
    on a stronger model would say the same thing, so nothing is retried. Work it did inside its
    bounds is committed. `needs` is a key of BLOCKED_NEEDS, `what` names the thing needed (may be
    empty) and `worker_reason` is the worker's own sentence, which the stop text quotes.
    """

    needs: str = "human"
    what: str = ""
    worker_reason: str = ""


def blocked_stop(ctx: Ctx, role: str, output: Dict[str, Any], evidence: Optional[str], tried: List[str]) -> Blocked:
    """Build the stop for a worker whose status is `blocked`. Missing fields fall back to a human need."""
    raw = output.get("blocked")
    info: Dict[str, Any] = raw if isinstance(raw, dict) else {}
    needs = str(info.get("needs") or "human")
    if needs not in BLOCKED_NEEDS:
        needs = "human"

    # F18: Compute formatted strings once and cap their length
    def _cap_text(text: str) -> str:
        if len(text) > _BLOCKED_TEXT_CAP:
            return text[:_BLOCKED_TEXT_CAP] + "…"
        return text

    what = _cap_text(" ".join(str(info.get("what") or "").split()))
    said = _cap_text(" ".join(str(info.get("reason") or output.get("summary") or "").split()) or "(it gave no reason)")
    cls, phrase = BLOCKED_NEEDS[needs]

    # Construct the message once
    what_phrase = " (%s)" % what if what else ""
    full_reason = "the %s cannot finish from here: it needs %s%s. It said: \"%s\"" % (
        role, phrase, what_phrase, said)
    reason = _cap_text(full_reason)

    stop = Blocked(cls, reason, evidence=evidence, tried=tried)
    stop.needs, stop.what, stop.worker_reason = needs, what, said
    ctx.flow.event("heal", cls="blocked", action="no retry: the worker is blocked", role=role, needs=needs,
                   what=what, reason=said)
    record.journal(ctx.flow, "A %s reported it is blocked and was not retried on a stronger model. It needs %s%s. "
                             "It said: \"%s\"" % (role, phrase, what_phrase, said))
    return stop


def _stop(ctx: Ctx, failure_cls: str, reason: str, evidence: Optional[str], tried: List[str]) -> FlowStop:
    kind = UnitUnfinished if failure_cls == "unit-shortfall" else FlowStop
    stop = kind(STOP_FOR[failure_cls], reason, evidence=evidence, tried=tried)
    ctx.flow.event("heal", cls=failure_cls, action="stop", stop=stop.cls, reason=stop.reason)
    return stop


# ---------------------------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------------------------

def _budget_hard(ctx: Ctx) -> None:
    total = ctx.flow.total_cost()
    if total >= ctx.cfg.hard_usd:
        raise FlowStop("budget", "spend %.2f USD reached the hard ceiling of %.2f USD"
                       % (total, ctx.cfg.hard_usd), evidence=str(ctx.flow.path / "events.jsonl"))


def _budget_soft(ctx: Ctx) -> None:
    total = ctx.flow.total_cost()
    if total >= ctx.cfg.soft_usd and not ctx.flow.get("budget_soft_noted"):
        ctx.flow.set("budget_soft_noted", True)
        ctx.flow.event("budget_soft", total_usd=total, soft_usd=ctx.cfg.soft_usd)
        record.journal(ctx.flow, "Spend reached the soft budget: %.2f of %.2f USD" % (total, ctx.cfg.soft_usd))


# ---------------------------------------------------------------------------------------------
# The healed call
# ---------------------------------------------------------------------------------------------

def _limit_ceiling() -> float:
    raw = os.environ.get("FLOW3_LIMIT_CEILING_SECS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(config.LIMIT_WAIT_CEILING_SECS)


def _limit_wait(result: CallResult, attempt: int) -> float:
    resets = (result.rate_limit or {}).get("resetsAt")
    if isinstance(resets, (int, float)) and not isinstance(resets, bool):
        remaining = float(resets) - time.time()
        if remaining > 0:
            return remaining
    return min(_BACKOFF_FIRST_SECS * (2 ** attempt), _BACKOFF_CAP_SECS)


def call(ctx: Ctx, spec: CallSpec) -> CallResult:
    """
    One model call with its heal budget: wait out a limit, retry a transient failure once fresh, resume
    once on a schema mismatch. Raises FlowStop, or Shortfall for an implementer that ran out of turns.
    """
    original_prompt = spec.prompt
    current = spec
    waited = 0.0
    limit_retries = 0
    transient_used = False
    schema_used = False
    while True:
        _budget_hard(ctx)
        result = runner.run(current, ctx.flow, ctx.cfg.models)
        _budget_soft(ctx)
        failure = classify_call(result, current)
        if failure is None:
            if result.permission_denials:
                cmds = _denied_commands(result)
                ctx.flow.event("denied-tolerated", role=current.role, commands=cmds)
                record.journal(ctx.flow, "A %s call was refused a tool and finished anyway: %s"
                               % (current.role, "; ".join(cmds)))
            return result
        evidence = _call_evidence(ctx, result)

        if failure.cls == "limit":
            wait = _limit_wait(result, limit_retries)
            if waited + wait > _limit_ceiling():
                tried = ["waited %d s over %d retries" % (waited, limit_retries)] if limit_retries else []
                raise _stop(ctx, "limit", "%s; the wait would pass the %d s ceiling"
                            % (failure.reason, _limit_ceiling()), evidence, tried)
            runner.note_limit(time.time() + wait)
            _heal(ctx, "limit", "waiting then retrying the same call", wait_secs=wait, role=current.role)
            _sleep(wait)
            waited += wait
            limit_retries += 1
            continue

        if failure.cls == "transient":
            if failure.detail.get("no-retry"):
                raise _stop(ctx, "transient", failure.reason, evidence, [])
            if transient_used:
                raise _stop(ctx, "transient", failure.reason, evidence,
                            ["retried once in a fresh session"])
            transient_used = True
            _heal(ctx, "transient", "retrying once in a fresh session", role=current.role,
                  reason=failure.reason)
            current = dataclasses.replace(current, resume_session=None)
            continue

        if failure.cls == "schema":
            if schema_used:
                raise _stop(ctx, "schema", failure.reason, evidence,
                            ["resumed once with the validation errors"])
            schema_used = True
            if result.session_id:
                _heal(ctx, "schema", "resuming the same session with the validation errors",
                      role=current.role)
                current = dataclasses.replace(
                    current, resume_session=result.session_id,
                    prompt="Your last output did not match the required schema:\n- %s\n\n"
                           "Return output matching the schema." % "\n- ".join(result.schema_errors))
            else:
                _heal(ctx, "schema", "no session id; retrying once in a fresh session", role=current.role)
                current = dataclasses.replace(current, resume_session=None, prompt=original_prompt)
            continue

        if failure.cls == "denied":
            raise _stop(ctx, "denied", failure.reason, evidence, [])

        if failure.cls == "unit-shortfall":
            raise Shortfall(result)

        raise _stop(ctx, "transient", failure.reason, evidence, [])  # unreachable by classify_call


# ---------------------------------------------------------------------------------------------
# Writing calls: protected and unit-shortfall
# ---------------------------------------------------------------------------------------------

def path_violations(changed: Sequence[str], allowed: Sequence[str], protected: Sequence[str]) -> List[str]:
    """Changed paths that are protected, or (when `allowed` is not empty) not within `allowed`."""
    bad: List[str] = []
    for path in changed:
        if gitops.match_any(path, protected):
            bad.append(path)
        elif allowed and not gitops.match_any(path, allowed):
            bad.append(path)
    return bad


def _rule_text(paths: List[str], allowed: Sequence[str], protected: Sequence[str]) -> str:
    where = ("You may touch only: %s. " % ", ".join(allowed)) if allowed else "You may touch any file except protected ones. "
    return ("These changes were reverted because they are protected or outside your unit: %s. %s"
            "Never edit protected paths (%s)." % (", ".join(paths), where, ", ".join(protected) or "none"))


def commit(ctx: Ctx, message: str, paths: Optional[List[str]] = None) -> Optional[str]:
    """
    Commit the worktree for the driver. When the repo declares a `format` command it runs first, so a
    commit hook that checks formatting does not reject files a model wrote. A failed format command
    is recorded and the commit is still tried; the hook or the next check then names the problem.
    A repo that sets `commit_hooks: false` has the driver's commits skip its commit hooks.
    """
    wt = ctx.worktree
    cmd = ctx.cfg.format_cmd
    if cmd and not _is_clean(wt):
        out = record.check_output_path(ctx.flow, "format", _tree(wt))
        run = checks.run(cmd, wt, "format", ctx.cfg.check_timeout_secs, out)
        ctx.flow.event("format", ok=run.ok, exit_code=run.exit_code, timed_out=run.timed_out,
                       duration_secs=round(run.duration_secs, 1))
    return gitops.commit_all(wt, message, paths, hooks=ctx.cfg.commit_hooks)


def write_call(ctx: Ctx, spec: CallSpec, allowed: Sequence[str], protected: Sequence[str],
               message: str) -> CallResult:
    """
    A healed call that edits files in the worktree. Out-of-bounds changes are reverted and retried once
    with the rule restated; a shortfall keeps the partial work and retries once on the stronger model.
    A worker that reports `blocked` is not retried: its in-bounds work is committed and `Blocked` is raised.
    """
    wt = ctx.worktree
    current = spec
    reverted: List[str] = []
    protected_used = False
    shortfall_used = False
    while True:
        shortfall = False
        try:
            result = call(ctx, current)
        except Shortfall as exc:
            result = exc.result
            shortfall = True
        else:
            if isinstance(result.output, dict) and result.output.get("status") == "partial":
                shortfall = True
        evidence = _call_evidence(ctx, result)

        violations = path_violations(gitops.changed_files(wt), allowed, protected)
        rule_note = ""
        if violations:
            gitops.revert_paths(wt, violations)
            if protected_used:
                raise _stop(ctx, "protected", "changed protected or out-of-unit paths twice: %s"
                            % ", ".join(violations), evidence,
                            ["reverted %s and retried once with the rule restated" % ", ".join(reverted)])
            protected_used = True
            reverted = list(violations)
            rule_note = _rule_text(violations, allowed, protected)
            _heal(ctx, "protected", "reverted out-of-bounds paths and retrying once", paths=violations)

        if isinstance(result.output, dict) and result.output.get("status") == "blocked" and not shortfall:
            # Not a shortfall: keep what it did inside its bounds and say what it needs. No retry.
            sha = commit(ctx, message + " (partial)")
            raise blocked_stop(ctx, spec.role, result.output, evidence,
                               ["kept the work it did (%s)" % (sha or "nothing to commit")])

        if shortfall:
            sha = commit(ctx, message + " (partial)")
            if shortfall_used:
                stop = _stop(ctx, "unit-shortfall",
                             "the unit is still unfinished after a retry on %s" % result.model,
                             evidence, ["kept the partial work (%s)" % (sha or "nothing new to commit"),
                                        "retried once on a stronger model"])
                if isinstance(stop, UnitUnfinished) and isinstance(result.output, dict):
                    stop.remaining = [str(r) for r in result.output.get("remaining") or []]
                raise stop
            shortfall_used = True
            stronger = config.stronger(result.model)
            remaining = []
            if isinstance(result.output, dict):
                remaining = [str(r) for r in result.output.get("remaining") or []]
            note = ("\n\nPartial work from an earlier attempt is already committed. Continue from it; "
                    "do not redo it.")
            if remaining:
                note += " Still to do: %s." % "; ".join(remaining)
            if rule_note:
                note += "\n" + rule_note
            _heal(ctx, "unit-shortfall", "kept partial work, retrying once on %s" % stronger,
                  model=stronger, committed=sha)
            current = dataclasses.replace(spec, model=stronger, resume_session=None, prompt=spec.prompt + note)
            continue

        if violations:
            if result.session_id:
                current = dataclasses.replace(current, resume_session=result.session_id, prompt=rule_note)
            else:
                current = dataclasses.replace(current, resume_session=None, prompt=spec.prompt + "\n\n" + rule_note)
            continue

        commit(ctx, message)
        return result


# ---------------------------------------------------------------------------------------------
# The driver-owned check
# ---------------------------------------------------------------------------------------------

def _tree(wt: Path) -> str:
    try:
        return gitops.tree_id(wt)
    except (gitops.GitError, OSError):
        return ""


def _is_clean(wt: Path) -> bool:
    return not gitops.changed_files(wt)


def _names_spec_test(ctx: Ctx, run: CheckRun) -> bool:
    """True when the check output names one of the flow's protected spec test files."""
    spec_files = [str(p) for p in ctx.flow.get("protected") or []]
    if not spec_files:
        return False
    output = _read_output(run)
    return any(f in output for f in spec_files)


_NO_MODULE = re.compile(r"No module named ['\"]?([A-Za-z_][\w.]*)")


def _external_missing(ctx: Ctx, run: CheckRun) -> List[str]:
    """
    Python modules the check could not import that are not this project's own: no top-level file or
    directory in the worktree carries the name and no planned file does. A spec test that imports
    one of these needs a tool the environment lacks; no unit of work will ever make it importable.
    """
    planned: List[str] = []
    for unit in (ctx.flow.data("plan").get("plan") or {}).get("units") or []:
        if isinstance(unit, dict):
            for f in unit.get("files") or []:
                parts = str(f).split("/")
                planned.extend(parts[:-1] + [parts[-1].rsplit(".", 1)[0]])
    out: List[str] = []
    for name in _NO_MODULE.findall(_read_output(run)):
        top = name.split(".")[0]
        local = (ctx.worktree / top).exists() or (ctx.worktree / (top + ".py")).exists() or top in planned
        if not local and top not in out:
            out.append(top)
    return out


def _run_check(ctx: Ctx, mode: str, command: str) -> CheckRun:
    wt = ctx.worktree
    out = record.check_output_path(ctx.flow, mode, _tree(wt))
    run = checks.run(command, wt, mode, ctx.cfg.check_timeout_secs, out)
    ctx.flow.event("check", mode=mode, ok=run.ok, exit_code=run.exit_code, timed_out=run.timed_out,
                   tree=run.tree, output=run.output_path)
    return run


def _passed(ctx: Ctx, run: CheckRun) -> CheckRun:
    # The recorded tree is HEAD's, so a pass on a dirty worktree must not be recorded against it.
    if _is_clean(ctx.worktree):
        checks.record_pass(ctx.repo, run.tree, run.command)
    return run


def _stop_check(ctx: Ctx, cls: str, run: CheckRun, reason: str, tried: List[str]) -> FlowStop:
    return _stop(ctx, cls, "%s (%s)%s" % (reason, run.command, failing_text(run)), run.output_path, tried)


def failing_text(run: CheckRun) -> str:
    """A clause naming the tests a red run reports as failed, for a stop reason. Empty when none are named."""
    text = run.trimmed
    if run.output_path:
        try:
            with open(run.output_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            pass
    names = checks.failures(text)
    return ". Failing: %s" % "; ".join(names) if names else ""


def _measure(ctx: Ctx, mode: str, command: str) -> CheckRun:
    """
    The result of `command` on the current tree: a recorded one when the worktree is clean and this
    tree already has a result (pass or fail), else a fresh run. Every reuse is journalled with the tree.
    """
    wt = ctx.worktree
    tree = _tree(wt)
    if tree and _is_clean(wt):
        if checks.passed_before(ctx.repo, tree, command):
            ctx.flow.event("check_reused", mode=mode, tree=tree, ok=True)
            record.journal(ctx.flow, "Check (%s) not re-run: tree %s already passed `%s`." % (mode, tree[:12], command))
            return CheckRun(ok=True, command=command, mode=mode, exit_code=0, timed_out=False,
                            duration_secs=0.0, tree=tree, output_path="", trimmed="", reused=True)
        red = checks.red_before(ctx.flow.path, tree, command)
        reused = checks.reused_red(red, mode) if red is not None else None
        if reused is not None:
            ctx.flow.event("check_reused", mode=mode, tree=tree, ok=False)
            record.journal(ctx.flow, "Check (%s) not re-run: tree %s already failed `%s`; the recorded "
                                     "result is used." % (mode, tree[:12], command))
            return reused
    return _run_check(ctx, mode, command)


def _record_red(ctx: Ctx, run: CheckRun) -> None:
    # Like a pass, a red is only true of HEAD's tree when the worktree is clean and has not moved.
    if not run.reused and not run.timed_out and run.tree and _is_clean(ctx.worktree) \
            and run.tree == _tree(ctx.worktree):
        checks.record_red(ctx.flow.path, run)


def _heal_environment(ctx: Ctx, run: CheckRun, mode: str, command: str, tried: List[str]) -> Tuple[CheckRun, bool]:
    """
    F6: Heal an environment failure: re-run after timeout, or run install command once if dependency
    is missing and no external modules are involved. Returns (result, install_was_called) tuple, or raises
    FlowStop if healing is not applicable or fails. install_was_called is True if _install was invoked.
    """
    failure = classify_check(run, None)
    if failure.cls == "environment" and failure.detail.get("kind") == "missing-dependency" \
            and _names_spec_test(ctx, run) and not _external_missing(ctx, run):
        # A spec test that cannot import code a later unit has yet to write is red work, not a missing tool.
        failure = Failure("check-work", "a spec test fails", evidence=run.output_path,
                          detail={"tests": _failing_tests(_read_output(run))})
    if failure.cls != "environment":
        return run, False

    kind = failure.detail.get("kind")
    if kind == "timeout":
        tried.append("re-ran the check once after the timeout")
        _heal(ctx, "environment", "re-running the check once after a timeout", mode=mode)
        new_run = _run_check(ctx, mode, command)
        if new_run.ok:
            return new_run, False
        raise _stop_check(ctx, "environment", new_run, "the check timed out again", tried)

    if not ctx.cfg.install:
        missing = _external_missing(ctx, run)
        raise _stop_check(ctx, "environment", run,
                          "the check needs a dependency%s and no install command is configured"
                          % (" (%s is not installed)" % ", ".join(missing) if missing else ""), tried)

    irun = _install(ctx, tried)
    if not irun.ok:
        raise _stop_check(ctx, "environment", irun, "the install command failed", tried)
    new_run = _run_check(ctx, mode, command)
    if new_run.ok:
        return new_run, True
    tried.append("re-ran the check after the install")
    if classify_check(new_run, None).cls == "environment":
        raise _stop_check(ctx, "environment", new_run, "the check still fails the same way after install", tried)
    return new_run, True


def _install_for_blocked_fixer(ctx: Ctx, blocked: Blocked, tried: List[str],
                               installed: bool) -> Optional[CheckRun]:
    """
    F6, F19: When a fixer reports environment need, attempt install once if not already done and if
    dependency manifests have not changed against the base. Returns True if install succeeded and
    check is now green (the passing run is returned), None to raise Blocked unchanged. F19: check
    manifests/lockfiles against base before installing; if they changed, do not install.
    """
    if blocked.needs != "environment" or not ctx.cfg.install or installed:
        return None

    # F19: Check if manifests/lockfiles changed against base; if so, don't install
    if _manifests_changed(ctx):
        return None

    irun = _install(ctx, tried)
    if not irun.ok:
        return None
    current = _run_check(ctx, "fast", ctx.cfg.check_fast)
    if current.ok:
        _heal(ctx, "environment", "check green after the install a blocked fixer asked for", mode="fast")
        return current
    return None


_MANIFEST_NAMES = frozenset([
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "setup.py", "setup.cfg", "pyproject.toml", "Gemfile", "Gemfile.lock",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "pom.xml",
    "pubspec.yaml", "pubspec.lock", ".ruby-version", ".nvmrc", ".python-version",
])


def _is_manifest(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (name in _MANIFEST_NAMES or name.startswith(("requirements", "Pipfile"))
            or name.startswith("build.gradle"))


def _manifests_changed(ctx: Ctx) -> bool:
    """
    F19: True when a dependency manifest or lockfile differs between the branch and its base.

    Fails closed: when the diff cannot be read, it reports True so no install runs unseen.
    """
    try:
        gitops.fetch(ctx.worktree)
        out = gitops.git(["diff", "--name-only", "origin/" + ctx.cfg.base + "...HEAD"], ctx.worktree)
    except (gitops.GitError, OSError):
        return True
    return any(_is_manifest(f) for f in out.splitlines() if f)


def check(ctx: Ctx, mode: str = "fast", fixer: Optional[Callable[[CheckRun, int], None]] = None,
          raise_on_fail: bool = True) -> CheckRun:
    """
    Run the repo's check and heal what the table allows: reuse a recorded result for this tree (pass
    or fail), install a missing dependency once, re-run a timeout once, re-run a failure once (flaky,
    unless `check.confirm_red` is off), then two fixer rounds. A fixer that reports it is blocked
    ends the rounds: see `Blocked`.

    Raises:
        Blocked: When a fixer's worker reports it cannot finish from where it is. Recorded check
            failure results (reds) for this tree are cleared if an environment install was run;
            otherwise they remain valid for reuse. No further fixer rounds are attempted when
            Blocked is raised.
        FlowStop: When an unrecoverable check failure occurs (timeout, missing dependency with no
            install command, unresolved conflicts in a base merge, or check still failing after
            all healing attempts).

    Precondition: The worktree must be in a clean or committable state; Git operations and check
        execution require a valid repository and tree. The base branch reference must be fetchable.
    """
    command = ctx.cfg.check_fast if mode == "fast" else ctx.cfg.check_full
    wt = ctx.worktree
    tried: List[str] = []
    installed = False
    run = _measure(ctx, mode, command)
    if run.ok:
        return run if run.reused else _passed(ctx, run)

    current = run
    if run.reused:
        # F12: the red is keyed by tree, and the base can move with the tree unchanged. A stale branch
        # can fail for that reason alone, so sync the base before spending fixer rounds on a reused red.
        if _is_clean(wt) and _behind_base(ctx) and sync_base(ctx):
            tried.append("merged the base into the branch")
            rerun = _run_check(ctx, mode, command)
            if rerun.ok:
                return _passed(ctx, rerun)
            current = rerun
            _record_red(ctx, current)
        else:
            tried.append("used the failing result already recorded for this tree")
    else:
        # F6: Use helper to handle environment failures
        current, install_called = _heal_environment(ctx, run, mode, command, tried)
        installed = install_called  # Track whether install was actually called
        if current.ok:
            return current if current.reused else _passed(ctx, current)

        # A branch that fell behind the base can fail for that reason alone (some repos' checks refuse a
        # stale branch outright). Bring the base in once before spending re-runs or fixer calls on it.
        synced = bool(_is_clean(wt) and _behind_base(ctx) and sync_base(ctx))
        if synced:
            tried.append("merged the base into the branch")
        if synced or ctx.cfg.check_confirm_red:
            rerun = _run_check(ctx, mode, command)
            if rerun.ok:
                found = classify_check(run, rerun)
                _heal(ctx, "check-flaky", "re-ran once and it passed", mode=mode, tests=found.detail.get("tests", []))
                return _passed(ctx, rerun)
            current = rerun
            tried.append("re-ran the check once; it failed again")
        _record_red(ctx, current)

    if fixer is not None:
        for rnd in (1, 2):
            _heal(ctx, "check-work", "fixer round %d" % rnd, mode=mode, round=rnd)
            try:
                fixer(current, rnd)
            except Blocked as blocked:
                # F6, F19: Use helper to handle blocked fixer's environment need
                healed = _install_for_blocked_fixer(ctx, blocked, tried, installed)
                if healed is not None:
                    return _passed(ctx, healed)
                raise
            tried.append("fixer round %d" % rnd)
            current = _measure(ctx, mode, command)
            if current.ok:
                _heal(ctx, "check-work", "check green after fixer round %d" % rnd, mode=mode, round=rnd)
                return current if current.reused else _passed(ctx, current)
            _record_red(ctx, current)
    if raise_on_fail:
        raise _stop_check(ctx, "check-work", current, "the %s check still fails" % mode, tried)
    return current


def _install(ctx: Ctx, tried: List[str]) -> CheckRun:
    """Run the install command once. It changes the environment, so this flow's recorded reds are dropped."""
    wt = ctx.worktree
    install = ctx.cfg.install or ""
    ipath = record.check_output_path(ctx.flow, "install", _tree(wt))
    _heal(ctx, "environment", "running the install command once", install=install)
    irun = checks.run(install, wt, "install", ctx.cfg.check_timeout_secs, ipath)
    tried.append("ran `%s` (exit %d)" % (install, irun.exit_code))
    checks.clear_reds(ctx.flow.path)
    return irun


# ---------------------------------------------------------------------------------------------
# Base moved
# ---------------------------------------------------------------------------------------------

_MARKER = re.compile(r"^(<<<<<<< |>>>>>>> |=======$)", re.MULTILINE)


def _has_markers(wt: Path, files: List[str]) -> bool:
    for rel in files:
        try:
            if _MARKER.search((wt / rel).read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False


def _undo_merge(wt: Path, pre: str) -> None:
    try:
        gitops.abort_merge(wt)
    except gitops.GitError:
        pass
    gitops.git(["reset", "--hard", pre], wt)


def _behind_base(ctx: Ctx) -> bool:
    """True when the base has commits the branch lacks. Fetches; a fetch failure counts as not behind."""
    try:
        gitops.fetch(ctx.worktree)
        return gitops.is_behind(ctx.worktree, "origin/" + ctx.cfg.base)
    except gitops.GitError:
        return False


def sync_base(ctx: Ctx) -> bool:
    """
    Merge base into the branch when it is behind. True when something was merged in, False when not
    behind. Conflicts get one resolver call and a check; otherwise the merge is undone and the flow
    stops with class `conflict`.
    """
    wt = ctx.worktree
    gitops.fetch(wt)
    base_ref = "origin/" + ctx.cfg.base
    if not gitops.is_behind(wt, base_ref):
        return False
    pre = gitops.head_sha(wt)
    clean, conflicted = gitops.merge_in(wt, base_ref)
    if clean:
        _heal(ctx, "base-stale", "merged %s into the branch" % base_ref)
        return True

    names = ", ".join(conflicted)
    prompt = ("The branch is being merged with %s and these files have conflict markers: %s.\n"
              "Resolve every conflict marker, keeping the intent of both sides. Edit only those files. "
              "Do not run git." % (base_ref, "; ".join(conflicted)))
    rspec = CallSpec(role="conflict_resolver", prompt=prompt, schema=schemas.CONFLICT_RESOLVER, cwd=wt,
                     tools=("Read", "Edit", "Grep", "Glob"), allowed_tools=("Read", "Edit", "Grep", "Glob"),
                     writes=True, label="base-stale")
    _heal(ctx, "base-stale", "conflicts in %s; one resolver call" % names, files=conflicted)
    tried = ["merged %s: conflicts in %s" % (base_ref, names), "one conflict_resolver call"]
    resolved = False
    result: Optional[CallResult] = None
    try:
        result = call(ctx, rspec)
        resolved = (isinstance(result.output, dict) and result.output.get("status") == "done"
                    and not _has_markers(wt, conflicted))
    except Shortfall as exc:
        result = exc.result
    except FlowStop:
        _undo_merge(wt, pre)
        raise
    evidence = _call_evidence(ctx, result)
    if not resolved:
        _undo_merge(wt, pre)
        raise _stop(ctx, "base-stale", "could not resolve the conflicts in %s" % names, evidence, tried)

    commit(ctx, "Merge %s into the branch (conflicts resolved)" % base_ref)
    run = check(ctx, "fast", raise_on_fail=False)
    if run.ok:
        _heal(ctx, "base-stale", "conflicts resolved and the check is green", files=conflicted)
        return True
    tried.append("check after the merge failed")
    _undo_merge(wt, pre)
    raise _stop(ctx, "base-stale", "conflicts in %s were resolved but the check fails" % names,
                run.output_path or evidence, tried)


# ---------------------------------------------------------------------------------------------
# After the merge
# ---------------------------------------------------------------------------------------------

def after_merge(ctx: Ctx, what: str, fn: Callable[[], None]) -> Optional[str]:
    """
    Run a post-merge step. A failure is retried once; a second failure becomes a warning string. Never
    raises: the flow finishes as merged.
    """
    first_error = ""
    for attempt in (1, 2):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - nothing after a confirmed merge may fail the flow
            if attempt == 1:
                first_error = str(exc) or type(exc).__name__
                _heal(ctx, "cleanup", "%s failed; retrying once" % what, error=first_error)
                continue
            warning = "%s failed after a retry: %s" % (what, str(exc) or type(exc).__name__)
            _heal(ctx, "cleanup", "warning: %s" % warning, warning=warning)
            return warning
        else:
            if attempt == 2:
                _heal(ctx, "cleanup", "%s succeeded on retry" % what, error=first_error)
            return None
    return None  # pragma: no cover

