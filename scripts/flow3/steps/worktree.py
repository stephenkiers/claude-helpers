"""
The worktree stage (PLAN.md section 7.1).

Order: dependencies named in the issue are merged, the ticket worktree and branch are cut from the
fetched base, and the install runs. The base is assumed green (FEEDBACK.md entry 1). A repo that sets
`check.base: true` also runs its fast check once on the untouched base, and a red base is then a
stop before any model is spent.
"""

from typing import Any, Dict, List

from scripts.flow3 import checks, gitops, heal, record
from scripts.flow3.context import Ctx
from scripts.flow3.provider import ProviderError
from scripts.flow3.types import FlowStop, StageResult

_DONE_STATES = ("closed", "merged")


def _check_dependencies(ctx: Ctx) -> List[int]:
    """Raise FlowStop("dependency") for the first dependency that is not merged. Returns those checked."""
    issue = ctx.provider.get_issue(ctx.issue)
    for dep in issue.depends_on:
        try:
            state = ctx.provider.get_issue(dep).state
        except ProviderError as exc:
            raise FlowStop("dependency", "issue #%d depends on #%d, which could not be read (%s)"
                           % (ctx.issue, dep, exc), tried=["read issue #%d from the provider" % dep])
        if state.lower() not in _DONE_STATES:
            raise FlowStop("dependency", "issue #%d depends on #%d, which is not merged yet (state: %s)"
                           % (ctx.issue, dep, state or "unknown"),
                           tried=["read issue #%d from the provider" % dep])
    return list(issue.depends_on)


def run(ctx: Ctx) -> StageResult:
    """Prepare the ticket worktree. Safe to re-enter: a finished stage returns at once."""
    done = ctx.flow.data("worktree")
    if done.get("base_check") in ("passed", "skipped") and ctx.flow.get("worktree"):
        return StageResult("done", {})

    deps = _check_dependencies(ctx)

    base = ctx.cfg.base
    base_ref = "origin/%s" % base
    slug = str(ctx.flow.get("slug") or "flow")
    gitops.fetch(ctx.repo_root)
    path = gitops.worktree_path_for(ctx.repo_root, ctx.issue, slug)
    branch = "flow3/%d-%s" % (ctx.issue, slug)
    try:
        gitops.create_worktree(ctx.repo_root, path, branch, base_ref)
    except gitops.GitError as exc:
        raise FlowStop("environment", "could not create the ticket worktree: %s" % exc,
                       tried=["git worktree add %s from %s" % (path, base_ref)])
    ctx.flow.set("worktree", str(path))
    ctx.flow.set("branch", branch)
    ctx.flow.set("base", base)
    base_sha = gitops.git(["merge-base", base_ref, "HEAD"], path)

    installed = False
    if ctx.cfg.install:
        out = record.check_output_path(ctx.flow, "install", gitops.tree_id(path))
        run = checks.run(ctx.cfg.install, path, "install", ctx.cfg.check_timeout_secs, out)
        ctx.flow.event("install", ok=run.ok, exit_code=run.exit_code, timed_out=run.timed_out,
                       output=run.output_path)
        if not run.ok:
            raise FlowStop("environment", "the install command failed (%s)" % ctx.cfg.install,
                           evidence=run.output_path, tried=["ran `%s` (exit %d)" % (ctx.cfg.install, run.exit_code)])
        installed = True

    # The base is assumed green. Only a repo that sets `check.base: true` pays for a check here.
    base_line = "- Check on the untouched base: not run (the base is assumed green)."
    if ctx.cfg.check_base:
        check = heal.check(ctx, "fast", fixer=None, raise_on_fail=False)
        if not check.ok:
            raise FlowStop("base-red", "%s fails its own check before any change" % base,
                           evidence=check.output_path or None,
                           tried=["ran `%s` on the untouched base" % check.command]
                           + (["ran the install command first"] if installed else []))
        base_line = "- Fast check on the untouched base: passed%s." % (
            " (reused a recorded pass)" if check.reused else "")

    value: Dict[str, Any] = {"base_sha": base_sha, "installed": installed,
                             "base_check": "passed" if ctx.cfg.check_base else "skipped"}
    ctx.flow.set_data("worktree", value)
    sections = [
        ("Given", "Issue #%d: %s" % (ctx.issue, ctx.flow.get("title"))),
        ("Did", "\n".join([
            "- Dependencies: %s" % (", ".join("#%d (merged)" % d for d in deps) if deps else "none"),
            "- Fetched origin and created branch `%s` at `%s` from `%s` (%s)." % (branch, path, base_ref, base_sha[:12]),
            "- Install: %s." % ("ran `%s`" % ctx.cfg.install if installed else "none configured"),
            base_line,
        ])),
    ]
    record.write_step_record(ctx.flow, "worktree", sections)
    record.journal(ctx.flow, "Worktree ready at %s on branch %s; %s." % (
        path, branch, "the base passes its own check" if ctx.cfg.check_base else "the base was not checked"))
    return StageResult("done", {})
