"""
The ship stage (PLAN.md section 7.7): commit, check, push, and open the pull request.

The push is a normal push. The PR text comes from one cheap call, with a plain rendering of the plan as
the fallback, so shipping never fails for want of prose. A re-ship after a send-back finds the PR that
already exists and reuses it.
"""

from typing import Any, Dict, List, Optional

from scripts.flow3 import gitops, heal, prompts, record, schemas
from scripts.flow3.context import Ctx
from scripts.flow3.provider import PullRequest
from scripts.flow3.types import CallSpec, FlowStop, StageResult

_MAX_TITLE = 100


def _plan(ctx: Ctx) -> Dict[str, Any]:
    plan = ctx.flow.data("plan").get("plan")
    return plan if isinstance(plan, dict) else {}


def _units(plan: Dict[str, Any]) -> List[str]:
    return [str(u.get("title") or u.get("id") or "") for u in plan.get("units") or [] if isinstance(u, dict)]


def _review_summary(ctx: Ctx) -> str:
    rounds = ctx.flow.data("review").get("rounds") or []
    if not rounds:
        return "No review record."
    last = rounds[-1] if isinstance(rounds[-1], dict) else {}
    triage = last.get("triage")
    triage = triage if isinstance(triage, dict) else {}
    fixes = ctx.flow.data("fix").get("rounds") or []
    fixed = sum(len(f.get("fixed") or []) for f in fixes if isinstance(f, dict))
    return ("%d review round(s). Last round: %d to do, %d deferred, %d needing a person. "
            "%d finding(s) fixed in %d fix round(s)." % (
                len(rounds), len(triage.get("doing_it") or []), len(triage.get("deferred") or []),
                len(triage.get("needs_you") or []), fixed, len(fixes)))


def _diff_stat(ctx: Ctx) -> str:
    try:
        return gitops.git(["diff", "--stat", "origin/%s...HEAD" % ctx.cfg.base], ctx.worktree) or "(no changes)"
    except gitops.GitError as exc:
        return "(diff stat unavailable: %s)" % exc


def _fallback(ctx: Ctx, plan: Dict[str, Any], diff_stat: str) -> Dict[str, str]:
    title = str(ctx.flow.get("title") or "Issue #%d" % ctx.issue)
    lines = [str(plan.get("goal") or title), ""]
    units = [u for u in _units(plan) if u]
    if units:
        lines += ["Changes:"] + ["- " + u for u in units] + [""]
    lines += ["Files changed:", "", "```", diff_stat, "```"]
    return {"title": title, "body": "\n".join(lines)}


def _write_text(ctx: Ctx, plan: Dict[str, Any], diff_stat: str) -> Dict[str, Any]:
    """The title and body, from one pr_text call; the rendered fallback on any failure."""
    try:
        units = _units(plan)
        prompt = prompts.render(
            "pr_text", issue_number=str(ctx.issue), issue_title=str(ctx.flow.get("title") or ""),
            goal=str(plan.get("goal") or "(no plan recorded)"),
            units="\n".join("- " + u for u in units) if units else "(no units recorded)",
            diff_stat=diff_stat, review_summary=_review_summary(ctx))
        spec = CallSpec(role="pr_text", prompt=prompt, schema=schemas.PR_TEXT, cwd=ctx.worktree,
                        label="pr-text")
        out = heal.call(ctx, spec).output or {}
        title, body = str(out.get("title") or "").strip(), str(out.get("body") or "").strip()
        if not title or not body:
            raise ValueError("the pr_text call returned an empty title or body")
        return {"title": title, "body": body, "source": "model"}
    except Exception as exc:  # noqa: BLE001 - shipping must not fail for want of prose
        ctx.flow.event("pr_text_fallback", reason=str(exc)[:300])
        record.journal(ctx.flow, "The PR text call failed (%s); the PR text is rendered from the plan." % exc)
        fb = _fallback(ctx, plan, diff_stat)
        fb["source"] = "fallback"
        return {k: v for k, v in fb.items()}


def _closes(body: str, issue: int) -> str:
    line = "Closes #%d" % issue
    return body if line.lower() in body.lower() else body.rstrip() + "\n\n" + line + "\n"


def run(ctx: Ctx) -> StageResult:
    """Ship the branch. Safe to re-enter and to repeat after a send-back."""
    wt = ctx.worktree
    branch = str(ctx.flow.get("branch"))
    base = ctx.cfg.base
    gitops.fetch(wt)
    synced = False
    if gitops.is_behind(wt, "origin/" + base):
        synced = heal.sync_base(ctx)

    committed = gitops.commit_all(wt, "Issue #%d: %s" % (ctx.issue, ctx.flow.get("title")))
    check = heal.check(ctx, "fast")
    gitops.push(wt, branch)
    head = gitops.head_sha(wt)

    pr: Optional[PullRequest] = ctx.provider.find_pr(branch)
    reused = pr is not None
    title, source = "", ""
    if pr is None:
        plan = _plan(ctx)
        text = _write_text(ctx, plan, _diff_stat(ctx))
        title, source = text["title"][:_MAX_TITLE], text["source"]
        pr = ctx.provider.open_pr(branch, base, title, _closes(text["body"], ctx.issue))
    else:
        if pr.state != "open":
            raise FlowStop("merge", "the pull request #%d for %s is already %s" % (pr.number, branch, pr.state),
                           tried=["looked the branch's pull request up through the provider"])
        title = str((ctx.flow.data("ship")).get("title") or ctx.flow.get("title") or "")
        source = "existing"

    ctx.flow.set("pr", pr.number)
    ctx.flow.set_data("ship", {"pr": pr.number, "url": pr.url, "title": title, "head": head})
    record.write_step_record(ctx.flow, "ship", [
        ("Given", "Branch `%s` at `%s`, base `%s`." % (branch, head[:12], base)),
        ("Did", "\n".join([
            "- Base merged in first: %s." % ("yes" if synced else "not needed"),
            "- Committed leftovers: %s." % (committed[:12] if committed else "nothing to commit"),
            "- Fast check: passed%s." % (" (reused a recorded pass)" if check.reused else ""),
            "- Pushed `%s` (a normal push)." % branch,
            "- Pull request #%d %s (%s): %s" % (pr.number, "reused" if reused else "opened", source, pr.url),
        ])),
    ])
    record.journal(ctx.flow, "Shipped: %s PR #%d (%s)." % ("reused" if reused else "opened", pr.number, pr.url))
    return StageResult("done", {})
