# Expert-flow Phase 0 spike findings (#246)

Epic: #245 (`/expert-flow`). Ticket: #246. Plan: `~/.claude/plans/expert-flow-spike-prove-subagent-skill-nested-subagents-20261008T162516-02869.md`.

> **Pre-registration.** Everything in the "Pre-registered rules" section was committed and pushed
> before any probe ran. Verdicts below are applied mechanically from it. Pre-registration commit:
> `d0335ed775e1fb9ef4eed8931f35d513e98ff902` on PR https://github.com/stephenkiers/claude-helpers/pull/260.

## Environment (pinned)

| Item | Value |
|---|---|
| Harness | `claude --version` = 2.1.296 (Claude Code) |
| Date (UTC) | 2026-10-09 |
| Driver model | Sonnet 5.5 (`claude-sonnet-5-5`), main-thread session |
| Step / probe agent models | step: `general-purpose` (inherits); probes: `general-purpose` with `model: haiku` (see deviations) |
| `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` | unset in the driver shell (documented default 20); not lowered |
| Harness location | `~/spike-246/` only (no pushed harness branch); user-level installs listed in the inventory |
| Effort-2 spend ceiling (Open Item 1) | **None, by user decision** ("no token / max cost … lots of room"). Cost is observed and reported, not enforced. Manual abort if the run exceeds about 2x expected duration. |

The engine decision below is conditional on this harness version.

## Pre-registered rules

### D1 engine kill rule

Primary engine: subagent-per-step. Fallback: `claude --bg` session-per-step (proposal §2).

| Question | Outcome | Consequence |
|---|---|---|
| Q1 (subagent `Skill`) | fail | **kill** (switch to `claude --bg`) |
| Q3 (`SendMessage` resume) | fail | **kill** |
| Q2 (nesting) | *works* | none |
| Q2 | *soft fail* | recorded constraint on #245, **not** a kill |
| Q2 | *hard fail* | **kill** |
| Q4 (zero-prompt pivot) | fail | `/track-and-start` fix item, not a kill |

The §5 question protocol proceeds in every case.

**Q2 classification (exhaustive; every observed result maps to exactly one row):**

- *works*: background launches at depth 2 all start, overlap, and all four conditions hold: the step is
  re-invoked by notifications, children outlive the step's end-turn, exactly one hand-back reaches the
  orchestrator after the barrier closes, and probes overlapped.
- *soft fail*: background nesting is broken or only partly works (spawns but serialized, partially
  rejected below the cap, or the step is not re-invoked) **and** foreground nested launches run and overlap.
- *hard fail*: no nested spawn at any depth, foreground or background.
- Launches 21–22 being queued, rejected or dropped at the cap is scored **separately** as at-cap
  behavior. It never changes the three-way classification by itself.

**Source disagreement:** proposal §2 says the kill criterion is "1–3", §11 says "1 *and* 3", the ticket
says "1 or 3". This spike applies the D1 rule above (Q1, Q3, or hard Q2). Which wording wins is a
follow-up edit to the proposal.

### Repeat rule (terminal)

Q1 and Q3 each run 3 times. 3/3 = pass, 0/3 = fail. 1/3 or 2/3 = ambiguous, which triggers the matching
deferred probe (Step 8). **After Step 8, any result still not 3/3 on Q1 or Q3 counts as a fail for the D1
kill rule.** Q2 and Q4 are single runs by decision.

### Q3 stub background-probe wake rule

The stub's background probe is a second possible wake source. The driver waits for the probe's `end_ts`
before `SendMessage`, and records whether the step was re-invoked by the probe's notification first. A
probe-wake before `SendMessage` is **not** a Q3 failure if the step then ends its turn again on the
sentinel and the resume still passes the Q3 criteria. It is an **invalid trial (repeat it)** if the step
treats the wake as completion, removes the sentinel, or writes `q3-resume` before the answers file exists.

### Invalid-run rule

A run is invalid if a probe's `start_ts` lags the `launch_ts` the step wrote to disk by more than a few
seconds (set at 5 s), or if a permission dialog appeared. Invalid runs are repeated, never counted.

### Per-question pass criteria

- **Q1**: `RUN_DIR/q1-nonce` == `N_cmd`; transcript shows a `Skill` tool_use for the stub; transcript
  shows **no** Read/Grep/Glob of the command file; after resume `RUN_DIR/q1-complete` exists and contains
  both `N_cmd` and `N_ans`. Q1-neg (nonexistent command) must error and produce no nonce file.
- **Q3**: resumed reply recites **both** `N_ctx` (conversation only, never on disk) and `N_ans`; the agent
  writes both to `RUN_DIR/q3-resume`; the step's agent id is unchanged across the resume. Q3-neg
  (`SendMessage` with no answers file) must report the file missing and must not fabricate `N_ans`.
  If a control "passes", the matching positive results are invalid.
- **Q2 stub**: Step 5 observations (i)–(vii) and the classification above.
- **Q2 real path**: `/expert-review --effort 2` under a step agent; review dir has pod checkpoints,
  `final-report.md`, `claude-action-plan.md` with mtimes after the step started; exactly one hand-back
  after the final file appears. An escalation prompt the step could not ask is an observation and does
  not fail the nesting verdict; an improvised choice or a stall on a prompt is a constraint for #245.
- **Q4**: issue body after the run reflects plan `P`; transcript audit finds zero `AskUserQuestion`
  attempts, tool-unavailable errors, improvised choices and Bash permission dialogs; static grep of the
  `--issue` path finds no reachable `AskUserQuestion`. `IN_PLAN_MODE=0` pinned.

### Evidence standard

Evidence is inline: nonces, timestamps, agent ids, transcript paths, harness version, date. Completion is
decided from artifacts on disk, never from an agent's self-report. Decisive transcript excerpts are
quoted inline because transcripts may be purged.

## Side-effect inventory

| Item | Where | Created (UTC 2026-10-09) | Removed |
|---|---|---|---|
| Stub commands `spike-246-stub{,-t2b,-t2c,-t3,-t3b}.md` | `~/.claude/commands/` | 20:12:11 to 20:15:16 | stale ones during run; rest 20:25:37 |
| Probe agent `spike-246-probe.md` (bypassPermissions) | `~/.claude/agents/` | Step 3 | removed same step (never discovered mid-session) |
| Review worktree + branch `spike/246-review-target` | `~/spike-246/review-wt` | 20:21:13 | 20:25:37 |
| Scratch issue #261 (body replaced, plan comment) | GitHub | 20:24:13 | comment deleted 20:25:30; closed 20:25:32 (issues cannot be deleted) |
| Worktree + branch `feature/261-scratch-plan-p` | `worktrees/261-scratch-plan-p` | Q4 run | 20:25:37 |
| Review dir `spike-246-review-target-a1a28e3-20261009T132127-31836` | `~/.claude/reviews/stephenkiers-claude-helpers/` | Q2 real run | 20:26:43 |
| Scratch root | `~/spike-246/` | start | removed after evidence was quoted here |

Not inspected: any reviewer-yield or review-cache entry the nested `/expert-review` run may have appended. Treat as an unremoved side effect. Background agents: user confirmed none remain.

### Deviations from the plan

- Probe agent: the auto-mode classifier blocked creating a `bypassPermissions` agent definition. After user approval it was created, but custom agent types are not discovered mid-session, so probes were `general-purpose` with `model: haiku`.
- A command body is served from its first load, so edits are ignored: one command name per trial (`t2c`, `t3`). Trial 2 (stale cache) and trial 3's first attempt ("Unknown skill", not yet registered) were invalid and doubled as Q3-neg and Q1-neg controls.
- Q2 stub run 1 was invalid (stamp taken in a prior call: 38 to 46 s start lag; 14 of 19 probe files invalid JSON). Run 2 (`q2-bg2`) is the scored run.
- **Start-lag ruling (user, 2026-10-09, "finish this up"):** run 2's max start lag was 6.13 s, over the pre-registered 5 s invalid-run threshold. The run is accepted as scored and not repeated. Rationale: every Q2 observation (19 overlapping probes, 60.4 s outlive, 19 re-invocations, one hand-back) is far from any threshold, so a rerun cannot change the verdict. Recorded as a deliberate deviation from the strict rule.

## Q1: subagent invokes a slash command via `Skill`

### Evidence
**Verdict: PASS (3/3).** Valid trials t1, t2c, t3. Each stub wrote `q1-nonce` equal to `N_cmd` (present only in the command body), and after resume `q1-complete` held both nonces:

| Trial | N_cmd | N_ans |
|---|---|---|
| t1 | cmd-a94b821b | ans-7ea8e2ff |
| t2c | cmd-8e60dfa8 | ans-1da50a08 |
| t3 | cmd-c894d9cf | ans-73cac065 |

Transcript audits found a `Skill` tool_use for the stub and no Read/Grep/Glob of the command file. Q1-neg: invoking the unregistered `spike-246-stub-t3` returned "Unknown skill" and produced no nonce file.

## Q2: nested subagents and the join barrier

### Evidence
**Verdict: WORKS (stub fan-out) and PASS (real path). No kill.**

Stub fan-out (`q2-bg2`): the step launched background depth-2 probes (`launch_ts` 1791577165.492, `endturn_ts` 1791577167.719; 19 valid probe JSONs). 19 started and 3 were rejected with an explicit error at the cap (the step itself counts toward the 20, so a step can launch 19). Peak overlap 19; children outlived the step's end-turn by 60.4 s; the step was re-invoked by 19 completion notifications; exactly one hand-back reached the orchestrator, after the barrier closed. Max probe start lag 6.13 s (over the 5 s threshold; accepted, see deviations).

At-cap behavior (scored separately): explicit rejection, not queueing or silent drop.

Real path: `/expert-review --effort 2` invoked via `Skill` inside a step agent against `spike/246-review-target`. The review dir held pod checkpoints, `final-report.md`, `claude-action-plan.md` and `findings.json`, all with mtimes after the step started; 7 depth-2 agents ran; no prompt attempts; one hand-back. Cost was not observable (null metrics; step usage 83.5k tokens). Nested `Skill`, `Agent` and background launches all work inside a `general-purpose` subagent.

## Q3: `SendMessage` resume after a sentinel

### Evidence
**Verdict: PASS (3/3).** The resumed agent (same agent id, via `SendMessage`) wrote both `N_ctx` (conversation only) and `N_ans` (answers file) to `q3-resume`:

| Trial | N_ctx | N_ans |
|---|---|---|
| t1 | ctx-5545d942 | ans-7ea8e2ff |
| t2c | ctx-43da06dd | ans-1da50a08 |
| t3 | ctx-9b27c1e5 | ans-73cac065 |

Q3-neg: a resume sent before the answers file existed (a chained driver command aborted) reported the file missing and did not fabricate `N_ans`. Step 8 skipped (nothing ambiguous). Lesson: verify the answers file exists before sending the resume.

## Q4: zero-prompt `/track-and-start --issue N --plan-file P`

### Evidence
**Verdict: PASS on the pre-registered criteria.** Scratch issue #261: the step ran `/track-and-start --issue 261 --plan-file <plan-P>` with `IN_PLAN_MODE=0`; the body was replaced with plan P, the old body archived as a "Superseded Plan" comment, worktree `feature/261-scratch-plan-p` created. Step notes: no `AskUserQuestion` attempts, no permission dialogs, no improvised choices. Static check: `commands/track-and-start.md` line 757 skips Duplicate Detection when `$ISSUE_FLAG` is set, and the "Pivot to existing" flow has no reachable prompt.

Caveat: the `track plan/apply` CLI has no existing-issue option (it would create a new issue), so the step did the pivot and `git worktree add` by hand. Tracked as #262.

## Engine decision

Applying the D1 table mechanically: Q1 pass, Q3 pass, Q2 *works*, Q4 pass-with-caveat. No kill condition fired.

**Engine: subagent-per-step (primary). The `claude --bg` fallback is not needed.** Conditional on harness 2.1.296 and the effort-2 path observed. #251 is written against subagent-per-step.

## Confirmed / contradicted proposal sentences

- Confirmed: subagents can invoke slash commands via `Skill` (Q1).
- Confirmed: `SendMessage` resumes a step agent with context intact and the same agent id (Q3).
- Confirmed: nested `Agent` and background launches work, and children outlive the step's end-turn (Q2).
- Confirmed with refinement: the concurrency cap of 20 includes the step agent, so a step can launch 19 children; overflow is rejected with an explicit error, not queued.
- New: background children's completion notifications re-invoke the **step**, and one hand-back reaches the orchestrator after the barrier closes.
- New: a new command registers for `Skill` only after a turn boundary and its body is served from first load; custom agent types are not discovered mid-session.
- Contradicted: nothing in the proposal was contradicted outright.

## Follow-ups

- Proposal §2/§11 edit (kill-rule wording and confirmed or contradicted facts)
- ADR-0022 records the engine decision; written in #251 per the epic, citing this file
- `/track-and-start` CLI existing-issue pivot: #262
- #245 constraint: a step launches at most 19 children; batch larger fan-outs
- Deferred Phase-1 probes (idle duration, cancellation, lowered cap, recursive depth, stale/duplicate/send-after-finish matrix)
