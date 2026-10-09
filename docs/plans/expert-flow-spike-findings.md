# Expert-flow Phase 0 spike findings (#246)

Epic: #245 (`/expert-flow`). Ticket: #246. Plan: `~/.claude/plans/expert-flow-spike-prove-subagent-skill-nested-subagents-20261008T162516-02869.md`.

> **Pre-registration.** Everything in the "Pre-registered rules" section was committed and pushed
> before any probe ran. Verdicts below are applied mechanically from it. Pre-registration commit:
> `PRE_REG_SHA` / URL: `PRE_REG_URL` (filled in by the commit that follows the push).

## Environment (pinned)

| Item | Value |
|---|---|
| Harness | `claude --version` = 2.1.296 (Claude Code) |
| Date (UTC) | 2026-10-09 |
| Driver model | Sonnet 5.5 (`claude-sonnet-5-5`), main-thread session |
| Step / probe agent models | step: `general-purpose` (inherits); probe: cheap model (recorded at Step 3) |
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

| Item | Where | Created | Removed |
|---|---|---|---|
| _(filled during the run)_ | | | |

## Q1: subagent invokes a slash command via `Skill`

### Evidence
_pending_

## Q2: nested subagents and the join barrier

### Evidence
_pending_

## Q3: `SendMessage` resume after a sentinel

### Evidence
_pending_

## Q4: zero-prompt `/track-and-start --issue N --plan-file P`

### Evidence
_pending_

## Engine decision

_pending (apply the D1 table above mechanically)_

## Confirmed / contradicted proposal sentences

_pending_

## Follow-ups

- Proposal §2/§11 edit (kill-rule wording and confirmed or contradicted facts)
- ADR-0022 records the engine decision (not written here)
- `/track-and-start` fix item if Q4 failed
- #245 constraint note if Q2 is a soft fail
- Deferred Phase-1 probes (idle duration, cancellation, lowered cap, recursive depth, stale/duplicate/send-after-finish matrix)
