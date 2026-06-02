---
name: expert-implement-with-haiku-and-ship
description: Run the full lifecycle in one shot — implement with Haiku, ship the PR, then expert-review. Stops automatically if a phase fails, and hands the final review back to you. Use when the user says "/expert-implement-with-haiku-and-ship" or wants to implement, ship, and review in a single chained run.
allowed-tools: Read, Bash(git branch:*), Bash(git rev-parse:*), Bash(git log:*), Bash(gh pr view:*), Skill
model: opus
---

# Implement with Haiku → Ship → Review

A thin orchestrator that runs three existing commands back-to-back, with a hard gate between
each phase:

```
/implement-with-haiku  →  /shipit  →  /expert-review
```

It exists to replace the manual chain the user runs by hand (including a `/compact` between each
heavy step). **You do not need to `/compact` between phases** — the harness auto-compacts as
context grows, and each sub-command already pushes its heavy work into sub-agents and returns
only a terse summary to this thread. (The user may still `/compact` manually if they want.)

## Operating principles

- **Thin controller.** Add no implement/ship/review logic of your own. Delegate entirely to the
  three commands below; your only job is to decide whether to proceed to the next phase.
- **Stop on failure.** If a phase fails its gate, **halt immediately**, surface that phase's
  report, and do not run later phases.
- **The human is the final reviewer.** The cycle implements and ships autonomously, then hands
  the review back to the user. Never auto-resolve conflicts, uncertainties, or review findings,
  and never loop back to auto-fix — those are the user's decisions.
- **Auto-answer only mechanical prompts** (e.g. pass `--force` to skip the prior-review re-run
  confirmation). Any genuine decision raised by a sub-command, or any failed gate, halts and
  surfaces to the user.

## Phase 0 — Preflight

Run, in this thread:

```bash
git branch --show-current
git rev-parse HEAD
```

Remember the SHA as `START_SHA`. Capture any args passed to this command (e.g. an issue number,
or a reviewer subset) to forward downstream.

Tell the user in one sentence: the cycle is starting, it will run implement → ship → review, and
it will halt at the first failure and hand the review back to them.

## Phase 1 — Implement

Invoke the `implement-with-haiku` skill via the `Skill` tool, forwarding any issue-number arg.

**Wait for the full 3-round summary before doing anything else.** `implement-with-haiku` launches
background rounds and reports across several notifications — the round-3 final summary block is
the completion signal. Do not advance while rounds are still running.

**Gate** — read the round-1 `COMMITTED:` line and round-3 `Final test status`:

- If round 1 reported `COMMITTED: no` (nothing was implemented) → **halt**, surface the report.
  There is nothing to ship.
- If the final test status shows failing tests → **halt**, surface the report. Don't ship a red
  build. Tell the user to fix and re-run `/expert-implement-with-haiku-and-ship`.
- Otherwise → continue.

## Phase 2 — Ship

Invoke the `shipit` skill via the `Skill` tool.

`shipit` runs CI checks itself and stops on the first failing check without committing.

**Gate** — confirm a commit was pushed and a PR exists:

- Check `shipit`'s output, the `pr.url` it writes to `.claude/github-cache.json`, or run
  `gh pr view` on the current branch.
- If `shipit` halted on a failed check → **halt** the cycle and surface which check failed.
- Otherwise → continue.

## Phase 3 — Review (hand back to the human)

Invoke the `expert-review` skill via the `Skill` tool. Pass `--force` so it doesn't pause to ask
about re-running a prior review (we just created fresh commits, so a fresh review is always
intended). Forward any reviewer-subset arg the user passed through.

This is the terminal phase. Surface `expert-review`'s short verdict + top findings **and stop**.
Make **no** decisions about the findings — do not loop, do not auto-fix, do not resolve conflicts
or uncertainties. Those are the user's call.

## Final summary

Close with a short block that makes the three gate outcomes legible at a glance:

```
PHASE 1 — Implement   ✅ committed, tests <P passed, F failed>
PHASE 2 — Ship        ✅ PR <url>
PHASE 3 — Review      <verdict line: Critical/High/Medium/Low counts>
```

(Use ⛔ for any phase that halted the cycle, and omit later phases that didn't run.) End by
pointing the user at the review for their decision.
