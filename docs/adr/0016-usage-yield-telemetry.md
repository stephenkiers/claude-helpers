# ADR-0016: Usage and yield telemetry

**Status:** Accepted

## Context

By the time this ADR was written, usage/yield telemetry had already shipped across 7+ merged PRs
(`a18fecc`, `ed7c822`, `ea5e1e5`, `f35c012`, `b2d9aaf`, `fa29ef8`, `ca8b1c0`, and others) with no
ADR of record — an undocumented decision, per this repo's own convention (see `docs/adr/README.md`).
This ADR captures the design retroactively.

The motivating question was never "how do we cut cost" — it was "we don't actually know where
cost and value go." `/implement-with-haiku` is a good example: it's known to consume a large share
of token usage, but it also delivers some of the best-value output in the toolkit. A telemetry
system built to minimize percentages would push toward cutting exactly the wrong thing. The goal
is **self-measurement that enables a human + Claude discussion about value vs. cost**, not an
automated optimizer.

## Decision

**Event model.** An append-only local JSONL log
(`~/.claude/telemetry/events.jsonl`, opt-in via `install.sh --with-telemetry`) of
`session.begin/end`, `command.begin/end`, `stage.begin/end`, and `agent.begin/end` events, each
carrying a versioned schema. Instrumentation is observational only — it must never alter routing,
model selection, or command behavior.

**Privacy allowlist.** Only metadata is recorded: timestamps, correlation IDs (session/command/
stage/agent, all UUIDs), repo basename, command/stage/agent/model names, token and cache counts,
turn count, elapsed time, retries, peak concurrency, transcript/artifact size, outcome and failure
class. Prompts, source code, diffs, issue bodies, and credentials are never recorded — enforced by
`build_event`'s closed parameter list in the writer (`scripts/run-metrics.py`), and any new call site must use a fixed,
enumerated vocabulary for `--stage`/`--command` values rather than interpolating PR/diff-derived
strings.

**Session-scoped state-file correlation.** Command docs call `run-metrics.py`'s `*-begin`/`*-end`
subcommands from separate, isolated Bash tool invocations, and Claude Code's Bash tool does not
persist shell variables across those invocations. The original design (threading IDs through
shell variables across call sites) was unreliable for exactly this reason. The fix (PR #107,
`ca8b1c0`) persists correlation state to a session-scoped state file on disk, which — unlike a
shell variable — is visible across isolated subprocess calls; explicit `--command-id`/`--stage-id`
flags still win over the state file where a call site passes them. This mechanism has been
verified safe for cross-process, sequential invocations. It has **not** been verified safe under
truly concurrent subagents (e.g. `/implement-with-haiku`'s parallel workers, `/expert-review`'s
subagent fan-out) — that verification is required before instrumenting either.

**Data-quality self-check, not a hard alert.** The `diagnose` subcommand reconciles `*.begin`/
`*.end` pairs and reports a match rate and unknown-field rate against thresholds (≥95% match,
<15% unknown), plus (added alongside this ADR) a stale-vs-recent split for unmatched begins — see
`docs/metrics.md`'s "Interpreting a low match rate" section for why that split exists and what
investigating the original ~37% match rate found (predominantly a pre-#107 correlation bug, not
session abandonment). This is a health check on the log's trustworthiness, not a production
alerting system.

**No built-in cost-optimization reports.** `/usage-report` deliberately stays a thin wrapper
around `diagnose` rather than growing aggregation views (cost-per-finding, retry/zero-yield
detection, concurrency-band breakdowns). The raw log is made legible via documented query patterns
(`docs/metrics.md`) so a future Claude session can read `events.jsonl` directly and answer
whatever ad hoc question is actually being asked — including "is this expensive thing worth it,"
which no fixed report can anticipate.

**Yield fields, not just cost fields.** Findings-produced/accepted/unique/rejected/acted-upon and
checks-executed/passed — plus the separate, sibling `outcome` field — are part of the allowlisted
event model precisely so a stage's *output* can be weighed against its *cost* — matching the
`/implement-with-haiku` example above.

## Consequences

- Telemetry is opt-in and additive; a repo or fork that doesn't run `install.sh --with-telemetry`
  is unaffected. *(Amended: see Amendment section below on opted-out behavior under the usage gate.)*
- No dashboards or automated thresholds exist to game — the design deliberately resists producing
  a single number a future change could be tempted to minimize at the expense of value. *(Amended:
  see Amendment section below on the single threshold that now exists.)*
- Concurrent-subagent correlation safety is an open verification gap, not yet resolved; instrumenting
  `/expert-review` or `/implement-with-haiku` before resolving it risks cross-attributed stage
  records under the session-state-file mechanism. *(Amended: see Amendment section below on
  resolved state-file concurrency and remaining re-fire hazard.)*
- `/usage-report` will likely never grow deterministic aggregation views under this design; treat
  requests for "a report that shows X" as a cue to write a documented query pattern instead, not a
  new subcommand.

## Amendment — Per-subagent token accounting and the `/implement-with-haiku` usage gate

**Concurrent-subagent correlation: partially resolved, with one hazard class remaining.**
The original Consequences section flagged a race where concurrent subagent writes could clobber the
session-state file. This specific race is now resolved: `load_and_update_state` already holds an
`fcntl.flock` exclusive lock for the duration of its read-modify-write cycle, and the in-file shape
already keys per-agent data by `agent_id` inside a shared dict, preventing per-agent state clobbering.
A DIFFERENT hazard class remains open: a `SubagentStop` hook firing more than once for the same agent
(a "re-fire") is a possible failure mode that any future new hook on an event with a blocking hook chain
must independently guard against. This feature's own mitigation for re-fires (idempotent-on-status
folding; see (d) below) is a specific fix, not a general principle — the ADR records the hazard class
for future reference, not just this feature's workaround.

**First production consumer of telemetry's usage data.**
`/implement-with-haiku` is the first consumer of this ADR's telemetry data for any decision beyond
passive observation. Prior to this, all telemetry was observational only — the log existed to answer
"what happened and why," not to alter flow control. This feature marks the boundary: `agent.end` events
now carry populated `tokens` and `token_confidence` fields (previously present in the event schema's
allowlist but never populated by any writer) and are read by an automated usage gate.

**Carve-out: usage data gates `/implement-with-haiku` fan-outs only.**
The rule is narrow: *"usage totals may gate `/implement-with-haiku`'s round fan-out, and nothing else;
the 'observational only' rule stands everywhere else, and usage data must not silently touch routing or
model selection."* This carve-out is explicit and bounded — a future change to route or select models
based on usage must negotiate an amendment to this ADR, not infer permission from the existence of this
gate. Routing and model selection remain off-limits for usage data.

**Counted metrics and token-confidence caveat.**
The usage gate's counted metric is: input tokens + output tokens + cache-creation tokens, **excluding**
cache-read tokens (rationale: a single real verified subagent transcript had cache-read alone already
exceeding the 130k-token threshold, so counting it would trigger the gate on literally every run's first
checkpoint, making it useless as a decision signal). `token_confidence` is a hardcoded `"low"` string
constant in `claude-transcript-metrics.py`, not a computed assessment of any particular transcript's
quality — it must never be read as a signal about a specific transcript's trustworthiness, only as a
blanket disclaimer on the whole mechanism (per-message output tokens in Claude Code transcripts are
unreliable placeholders, so all per-transcript token totals carry inherently low confidence, whether the
metric "looks good" or not).

**Opted-out telemetry: proceed with notice, not blocking ask.**
The original Consequences section stated telemetry is opt-in and unaffected repos are unaffected. This
is partially falsified by this feature: a session without telemetry installed now gets `state=unavailable`
at `/implement-with-haiku`'s seams, which should render as `DECISION: proceed` with a one-line notice
pointing to install instructions, not as a blocking `ask` interrupt. The design intent is "this is
optional, proceed by default when unavailable; install for real tracking." The command doc for
`/implement-with-haiku` is the authority on the actual behavior (see USAGE GATE LOG block in Final
summary section), so if the `usage-check` implementation differs from this ADR's stated intent,
surface that discrepancy as a handoff item.

**Single threshold now exists; it's a stop-and-ask, never an optimizer.**
The original Consequences section stated "no dashboards or automated thresholds exist." This is
partially falsified: `/implement-with-haiku` now ships exactly one automated threshold (130k tokens by
default, overridable via `--threshold` flag). This threshold is never fed back into routing, model
selection, or any optimization loop — it surfaces as a stop-and-ask to the human, with options to
proceed, stop, or defer the interrupt until the next increment. The threshold is a gate control surface,
not an optimizer input.

## Amendment — expert-review command/stage shape fields (effort, model, mode, reviewer_count)

These four fields exist specifically to make `/expert-review`'s subagent spawns (`expert-reviewer` and
`expert-scout` agent types) joinable to their run's shape — effort level, model tier, run mode, reviewer
count — via `command_id`. Before this amendment, 14% of Claude Code usage (the full span of subagent
spawns) was previously unattributable to any run shape, preventing any analysis that answered questions
like "how does cost-per-finding vary across effort levels?" or "which mode (local/PR/coworker) produces
the best ROI?" This amendment closes that gap.

This does NOT change the "observational only" rule established elsewhere in this ADR — these fields record
already-resolved values (`EFFORT`, `PANEL_MODEL`, resolved mode, resolved reviewer count) from the
command's own resolution logic. They are never read back by `/expert-review` itself and never feed into
routing or model selection. Telemetry remains a read-only observation layer.

One known, accepted consequence: the deprecated-but-functional `/expert-review-coworker` and
`/expert-review-coworker-beta` commands share the underlying panel logic but never call `command-begin`
themselves, so their runs will emit `stage.*` events with `command_id` resolving to `"unknown"`. This is
accepted, not a defect — future readers of `diagnose` output shouldn't be confused by a nonzero count of
unexplained `expert-reviewer`/`expert-scout` stages without a parent `command_id` for `expert-review`.
