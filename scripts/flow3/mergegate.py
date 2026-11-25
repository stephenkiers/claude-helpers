"""
The merge gate (PLAN.md section 7.8): the screen the human sees, and the merge stage that runs after
the human chose merge.

The merge runs under a per-repo lock so merges happen one at a time. Base is merged into the branch,
never rebased, so there is no force push anywhere. The exact tree that will merge gets the full check;
the provider's own report, not a command's exit code, says whether it merged.
"""

import contextlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from scripts.flow3 import checks, config, gitops, heal, record
from scripts.flow3.context import Ctx
from scripts.flow3.paths import repo_dir
from scripts.flow3.provider import BaseMoved, ProviderError
from scripts.flow3.types import FlowStop, StageResult
from scripts.workflow.fsutil import LockHeld, atomic_write_text, nonblocking_flock, read_json_object

def _d(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


# Replaced in tests so waits do not take real time.
_sleep = time.sleep

LOCK_POLL_SECS = 5.0
LOCK_WAIT_SECS = 30 * 60.0
CONFIRM_TRIES = 5
CONFIRM_POLL_SECS = 2.0


# ---------------------------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------------------------

def _section(title: str, lines: List[str]) -> List[str]:
    return ["", title] + (lines if lines else ["  (nothing recorded)"])


def _plan_units(ctx: Ctx) -> List[str]:
    plan = ctx.flow.data("plan").get("plan")
    units = plan.get("units") if isinstance(plan, dict) else None
    return ["  - %s" % (u.get("title") or u.get("id")) for u in units or [] if isinstance(u, dict)]


def _diff_stat(ctx: Ctx) -> List[str]:
    try:
        text = gitops.git(["diff", "--stat", "origin/%s...HEAD" % ctx.cfg.base], ctx.worktree)
    except (gitops.GitError, RuntimeError, OSError):
        return ["  (diff stat unavailable)"]
    return ["  " + ln for ln in text.splitlines()] or ["  (no changes against %s)" % ctx.cfg.base]


def _last_check(ctx: Ctx) -> List[str]:
    for ev in reversed(ctx.flow.events()):
        if ev.get("kind") == "check":
            return ["  %s check %s on tree %s" % (ev.get("mode"), "passed" if ev.get("ok") else "FAILED",
                                                  str(ev.get("tree") or "")[:12] or "(unknown)")]
        if ev.get("kind") == "check_reused":
            return ["  %s check passed (recorded pass reused) on tree %s"
                    % (ev.get("mode"), str(ev.get("tree") or "")[:12])]
    return []


def _finding_titles(ctx: Ctx) -> Dict[str, str]:
    titles: Dict[str, str] = {}
    for rnd in ctx.flow.data("review").get("rounds") or []:
        for f in (rnd.get("findings") or []) if isinstance(rnd, dict) else []:
            if isinstance(f, dict):
                titles[str(f.get("id"))] = str(f.get("title") or "")
    return titles


def _named(ids: List[Any], titles: Dict[str, str]) -> str:
    return ", ".join(("%s (%s)" % (i, titles[str(i)])) if titles.get(str(i)) else str(i) for i in ids)


def _findings(ctx: Ctx) -> List[str]:
    """
    Fixed and open findings. Open is every *doing it* finding no fix round fixed, from every review round:
    that includes the last allowed round, whose fixes are never run, and a finding the human answered
    `fix` there. Those are marked, because the human was told the answer was recorded.
    """
    titles = _finding_titles(ctx)
    rounds = [r for r in ctx.flow.data("fix").get("rounds") or [] if isinstance(r, dict)]
    review = [r for r in ctx.flow.data("review").get("rounds") or [] if isinstance(r, dict)]
    lines: List[str] = []
    fixed: List[Any] = [i for r in rounds for i in r.get("fixed") or []]
    if fixed:
        lines.append("  Fixed: " + _named(fixed, titles))
    if not rounds and not review:
        return lines
    asked = {str(k) for r in review for k, a in _d(r.get("answers")).items()
             if _d(a).get("source") == "human" and _d(a).get("option_id") == "fix"}
    done = {str(i) for i in fixed}
    still: List[str] = []
    capped = 0
    for rnd in review:
        for f in rnd.get("findings") or []:
            fid = str(_d(f).get("id"))
            if _d(f).get("bucket") == "doing_it" and fid not in done and fid not in still:
                still.append(fid)
                capped += 1 if rnd.get("last_round") else 0
    for i in (rounds[-1].get("open") or []) if rounds else []:
        if str(i) not in still and str(i) not in done:
            still.append(str(i))
    if not still:
        lines.append("  Open: none")
        return lines
    lines.append("  Open (%d), not fixed:" % len(still))
    for fid in still:
        lines.append("  - %s (%s)%s" % (fid, titles.get(fid, ""),
                                        ": YOU ANSWERED `fix`, and it was not done" if fid in asked else ""))
    if capped:
        lines.append("  %d of these came from the last review round the flow allows, so no fix was run for them. "
                     "Send back with notes to have them fixed." % capped)
    return lines


def _deferred(ctx: Ctx) -> List[str]:
    lines: List[str] = []
    titles = _finding_titles(ctx)
    for rnd in ctx.flow.data("review").get("rounds") or []:
        for d in _d(_d(rnd).get("triage")).get("deferred") or []:
            if isinstance(d, dict):
                lines.append("  - %s: %s" % (_named([d.get("finding_id")], titles), d.get("reason")))
    return lines


def _measurements(ctx: Ctx) -> List[str]:
    lines: List[str] = []
    for rnd in ctx.flow.data("review").get("rounds") or []:
        for m in (rnd.get("measurements") or []) if isinstance(rnd, dict) else []:
            if isinstance(m, dict) and not m.get("ran"):
                lines.append("  - %s: `%s`" % (m.get("finding_id"), m.get("command")))
    return lines


def _test_edits(ctx: Ctx) -> List[str]:
    edits = ctx.flow.data("implement").get("test_edits") or []
    return ["  - %s (%s): %s" % (e.get("file"), e.get("verdict"), e.get("reason"))
            for e in edits if isinstance(e, dict) and e.get("applied")]


def _decisions(ctx: Ctx) -> List[str]:
    lines: List[str] = []
    for doc in record.read_decisions(ctx.flow):
        ans = _d(doc.get("answer"))
        source = str(ans.get("source") or "")
        if source not in ("assumed", "principle", "fact"):
            continue
        who = "principle %s" % ans.get("principle_id") if source == "principle" else source
        card = _d(doc.get("card"))
        lines.append("  - [%s] %s -> %s (%s). Reason: %s" % (
            doc.get("question_id"), card.get("decision") or card.get("question") or "", ans.get("text")
            or ans.get("option_id") or "", who, ans.get("reason") or "none given"))
    if lines:
        lines.append("  To overrule one: choose \"send back with notes\" and name its id in the notes.")
    return lines


def _elapsed(ctx: Ctx) -> str:
    try:
        start = datetime.fromisoformat(str(ctx.flow.get("created")).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - start).total_seconds())
    except (TypeError, ValueError):
        return "unknown"
    return "%dh %02dm" % (secs // 3600, secs % 3600 // 60)


def screen(ctx: Ctx) -> str:
    """The merge gate text, rendered only from the record. Missing records show as `(nothing recorded)`."""
    ship = ctx.flow.data("ship")
    out = ["MERGE GATE  #%s  %s" % (ctx.flow.get("issue"), ctx.flow.get("title")),
           "Pull request: %s" % (ship.get("url") or "(none recorded)")]
    out += _section("What changed", _plan_units(ctx) + _diff_stat(ctx))
    out += _section("Check", _last_check(ctx))
    out += _section("Findings", _findings(ctx))
    out += _section("Deferred, with reasons", _deferred(ctx))
    out += _section("Measurements not run", _measurements(ctx))
    out += _section("Existing tests edited without asking you", _test_edits(ctx))
    out += _section("Decisions made without you (assumed, by principle, or by a checked fact)", _decisions(ctx))
    out += ["", "Cost: %.2f USD    Elapsed: %s" % (ctx.flow.total_cost(), _elapsed(ctx)), "",
            "Choose one:  merge  |  send back with notes  |  stop"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextlib.contextmanager
def _merge_lock(ctx: Ctx) -> Iterator[None]:
    """One merge per repo at a time. Waits for a live holder; the OS frees the lock when a holder dies."""
    base = repo_dir(ctx.repo)
    lock_path, info_path = base / "merge.lock", base / "merge.lock.json"
    waited = 0.0
    with contextlib.ExitStack() as stack:
        while True:
            try:
                stack.enter_context(nonblocking_flock(lock_path))
                break
            except LockHeld:
                holder = read_json_object(info_path) or {}
                if waited >= LOCK_WAIT_SECS:
                    raise FlowStop("merge-contended",
                                   "another merge held the lock for %d s (flow %s, pid %s)"
                                   % (waited, holder.get("flow", "unknown"), holder.get("pid", "unknown")),
                                   evidence=str(info_path), tried=["waited %d s for the merge lock" % waited])
                if waited == 0:
                    record.journal(ctx.flow, "Waiting for the merge lock held by flow %s (pid %s)."
                                   % (holder.get("flow", "unknown"), holder.get("pid", "unknown")))
                _sleep(LOCK_POLL_SECS)
                waited += LOCK_POLL_SECS
        old = read_json_object(info_path)
        if old and old.get("pid") != os.getpid() and not _pid_alive(int(old.get("pid") or 0)):
            record.journal(ctx.flow, "Took over the merge lock from a dead process (pid %s, flow %s)."
                           % (old.get("pid"), old.get("flow")))
        atomic_write_text(info_path, json.dumps({"pid": os.getpid(), "flow": ctx.flow.name,
                                                 "since": datetime.now(timezone.utc).isoformat()}) + "\n")
        try:
            yield
        finally:
            try:
                info_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------------------------

def _run_command(ctx: Ctx, command: str, cwd: Path, mode: str, env: Optional[Dict[str, str]] = None) -> Any:
    out = record.check_output_path(ctx.flow, mode, "")
    run = checks.run(command, cwd, mode, ctx.cfg.check_timeout_secs, out, env=env)
    ctx.flow.event("check", mode=mode, ok=run.ok, exit_code=run.exit_code, timed_out=run.timed_out,
                   tree=run.tree, output=run.output_path, command=command)
    return run


def _expand(command: str, values: Dict[str, str]) -> str:
    for key, value in values.items():
        command = command.replace("{%s}" % key, value)
    return command


def _base_moved(ctx: Ctx) -> bool:
    gitops.fetch(ctx.worktree)
    return gitops.is_behind(ctx.worktree, "origin/" + ctx.cfg.base)


def _merge_once(ctx: Ctx, pr: int, branch: str, head: str, title: str) -> bool:
    """Hand the PR to the provider or the repo's command. False when base moved (go round again)."""
    if ctx.cfg.merge_method == "command":
        values = {"pr": str(pr), "branch": branch, "base": ctx.cfg.base, "head": head}
        env = {"FLOW3_PR": values["pr"], "FLOW3_BRANCH": branch, "FLOW3_BASE": ctx.cfg.base,
               "FLOW3_HEAD": head}
        command = _expand(ctx.cfg.merge_command or "", values)
        run = _run_command(ctx, command, ctx.worktree, "merge", env=env)
        if not run.ok:
            raise FlowStop("merge", "the repo's merge command failed (exit %d): %s" % (run.exit_code, command),
                           evidence=run.output_path, tried=["ran the merge command under the merge lock"])
        return True
    try:
        ctx.provider.merge_pr(pr, head, title)
    except BaseMoved as exc:
        ctx.flow.event("merge_base_moved", reason=str(exc)[:300])
        return False
    except ProviderError as exc:
        raise FlowStop("merge", "the provider refused the merge: %s" % exc,
                       tried=["asked the provider to squash-merge PR #%d pinned to %s" % (pr, head[:12])])
    return True


def _confirm_merged(ctx: Ctx, pr: int) -> None:
    said = "nothing"
    for attempt in range(CONFIRM_TRIES):
        try:
            said = ctx.provider.pr_state(pr)
        except ProviderError as exc:
            said = "an error: %s" % exc
        else:
            if said == "merged":
                return
        if attempt < CONFIRM_TRIES - 1:
            _sleep(CONFIRM_POLL_SECS)
    raise FlowStop("merge", "the provider does not report PR #%d as merged; it said %s" % (pr, said),
                   tried=["asked the provider %d times" % CONFIRM_TRIES])


def _merge_locked(ctx: Ctx) -> Dict[str, Any]:
    wt = ctx.worktree
    branch = str(ctx.flow.get("branch"))
    ship = ctx.flow.data("ship")
    pr = int(ctx.flow.get("pr") or ship.get("pr") or 0)
    if not pr:
        raise FlowStop("merge", "no pull request is recorded for this flow; ship first")
    title = str(ship.get("title") or ctx.flow.get("title") or "Issue #%d" % ctx.issue)
    attempts = 0
    for attempts in range(1, config.MAX_MERGE_ATTEMPTS + 1):
        if heal.sync_base(ctx):
            record.journal(ctx.flow, "Merge attempt %d: base had moved and was merged into the branch." % attempts)
        heal.check(ctx, "full")
        for command in ctx.cfg.pre_merge:
            run = _run_command(ctx, command, wt, "pre_merge")
            if not run.ok:
                raise FlowStop("check", "the pre-merge command failed (exit %d): %s" % (run.exit_code, command),
                               evidence=run.output_path, tried=["ran it on the exact tree that would merge"])
        gitops.push(wt, branch)
        if _base_moved(ctx):  # moved during the check
            record.journal(ctx.flow, "Merge attempt %d: base moved during the check; going round again." % attempts)
            continue
        head = gitops.head_sha(wt)
        if _merge_once(ctx, pr, branch, head, title):
            return {"attempts": attempts, "head": head}
        record.journal(ctx.flow, "Merge attempt %d: the provider reported base moved; going round again." % attempts)
    raise FlowStop("merge-contended", "base moved during the check %d times" % config.MAX_MERGE_ATTEMPTS,
                   evidence=str(ctx.flow.path / "events.jsonl"),
                   tried=["merged base in and re-ran the full check %d times" % attempts])


def _post_merge(ctx: Ctx, wt: Path, branch: str) -> List[str]:
    warnings: List[str] = []

    def add(what: str, fn: Any) -> None:
        warning = heal.after_merge(ctx, what, fn)
        if warning:
            warnings.append(warning)

    for command in ctx.cfg.post_merge:
        def hook(command: str = command) -> None:
            run = _run_command(ctx, command, ctx.repo_root, "post_merge")
            if not run.ok:
                raise RuntimeError("exit %d (output: %s)" % (run.exit_code, run.output_path))
        add("post-merge command `%s`" % command, hook)
    add("removing the worktree", lambda: gitops.remove_worktree(ctx.repo_root, wt))
    add("deleting the local branch", lambda: gitops.git(["branch", "-D", branch], ctx.repo_root))
    return warnings


def run(ctx: Ctx) -> StageResult:
    """The merge stage: runs only after the human chose merge."""
    with _merge_lock(ctx):
        wt = ctx.worktree
        branch = str(ctx.flow.get("branch"))
        pr = int(ctx.flow.get("pr") or ctx.flow.data("ship").get("pr") or 0)
        won = _merge_locked(ctx)
        _confirm_merged(ctx, pr)
        merged_sha = ""
        try:
            gitops.fetch(wt)
            merged_sha = gitops.git(["rev-parse", "origin/" + ctx.cfg.base], wt)
        except (gitops.GitError, OSError):
            pass
        warnings = _post_merge(ctx, wt, branch)
        ctx.flow.set_data("merge", {"attempts": won["attempts"], "merged": True, "merged_sha": merged_sha,
                                    "warnings": warnings})
        record.write_step_record(ctx.flow, "merge", [
            ("Given", "PR #%d from `%s` into `%s`, merge method `%s`." % (
                pr, branch, ctx.cfg.base, ctx.cfg.merge_method)),
            ("Did", "\n".join([
                "- Took the merge lock; attempts: %d." % won["attempts"],
                "- Full check on the exact tree, pre-merge commands: %d." % len(ctx.cfg.pre_merge),
                "- Merged head `%s`; the provider reports the PR merged." % won["head"][:12],
                "- Base is now at `%s`." % (merged_sha[:12] or "unknown"),
            ] + ["- Warning: " + w for w in warnings])),
        ])
        record.journal(ctx.flow, "Merged PR #%d after %d attempt(s)%s." % (
            pr, won["attempts"], " with %d warning(s)" % len(warnings) if warnings else ""))
        return StageResult("done", {"warnings": warnings})
