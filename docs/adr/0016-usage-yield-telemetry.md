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
must independently guard against. This feature's own mitigation for re-fires distinguishes re-fire (same
hook firing twice) from duplicate classification (second call with matching status `path_not_a_file` and
no begin entry is classified as `agent.internal_stop`, not a duplicate real subagent) — the ADR records
the hazard class for future reference, not just this feature's workaround.

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

## Amendment — Committed baseline snapshot for routing metrics

**Observation-only artifact, not a decision input.**
A committed baseline snapshot (`docs/metrics/routing-baseline.json`) is now generated manually at epic/phase
boundaries via `reviewer-yield.py --snapshot`. The baseline is a cross-repository, sanitized aggregate —
no repo names, paths, or per-run identifiers — containing per-(stratum, bucket) metrics (run counts, reviewer
counts, verified findings, token coverage) and solo findings per reviewer. Like all output from `reviewer-yield.py`,
the baseline remains **observation-only** (ADR-0016 carve-out: never fed into routing, model selection, or
triage decisions). Regeneration is manual and deliberate, documented in `docs/metrics/README.md`, with explicit
`--until` window specification to preserve corpus consistency across snapshots.

## Amendment — Harness-internal SubagentStop firings (agent.internal_stop)

**New event type and classification rule.**
A new event type `agent.internal_stop` classifies SubagentStop firings that are harness-internal model calls with no matching spawn event. These are identified by two signals: (1) transcript status `path_not_a_file` in the session meta file lookup, AND (2) no matching `agent.begin` entry for that `agent_id` in the session meta. This two-signal approach is necessary because `agent_type` is not a reliable signal — many legitimate spawned agents are untyped (carry blank or unknown `agent_type`), so label-based classification would incorrectly flag real agents as internal.

**Skipped from usage accounting.**
Classified internal firings emit `agent.internal_stop` events (carrying session_id, timestamp, agent_id, agent_type, and default metric fields, but no tokens, token_confidence, or outcome) and are never written to the session-state `usage.agents` dict. They do not participate in token counting or the usage gate. This is distinct from `path_not_a_file` status on a **recorded** agent in `usage.agents`, which represents a real spawned agent whose transcript was simply missing.

**`agent.begin` is authoritative for spawn counts.**
When analyzing agent-spawn data, count from `agent.begin` events, not `agent.end`. Before the cutoff date (the merge timestamp of the PR that introduced `agent.internal_stop`, `2026-10-13T18:58:25Z`, #267), `agent.end` rows with `agent_type in ("", "unknown")` are untyped and include harness-internal firings; after the cutoff, these firings emit `agent.internal_stop` instead.

**No history rewrite.**
The ~58k pre-fix blank `agent.end` rows in existing logs are not rewritten (the log is append-only). Queries spanning pre- and post-cutoff periods will observe the transition directly. The jq token-sum pattern (summing input + output + cache_creation from `agent.end` events) is unaffected because the blank rows carried only `unknown` tokens, contributing nothing to the sum.

## Amendment — `/implement-with-haiku` failure-class vocabulary and ledger-gated success

**Silently dropped failure events, now fixed.** `/implement-with-haiku` emitted `--failure-class` values in
kebab-case (e.g. `plan-not-found`). `telemetry_schema.outcome_failure` accepts only the snake_case members of
`FAILURE_CLASSES`, so every such failure event was rejected with a `ValueError` and silently dropped. The
schema now adds the snake_case members `steps_skipped`, `plan_not_found`, `all_units_failed`,
`gate_not_converged`, `resume_format_invalid`, `resume_sha_not_found`, `ancestry_invalid`,
`ancestry_invalid_after_drift`, `working_tree_dirty`, `detached_head`, `outstanding_worktree`, and
`resume_cancelled`. The kebab-case forms are not aliased. `outcome_failure` and the validator are unchanged.

**Success is ledger-gated.** `/implement-with-haiku` success is emitted only by the helper
`scripts/implement-ledger.py finish`, which records `command-end --outcome failure --failure-class steps_skipped`
for steps that were not completed and `command-end --outcome success` only when the ledger shows every step
done. The command doc never emits success directly.

The ledger is a control input, not just observational telemetry — it directly gates whether success is recorded, not merely reported after the fact. Changes to the ledger structure or the finish logic are architectural changes, not telemetry implementation details.

**Scope, stated honestly.** The ledger covers *omission*: a step that was never recorded as done cannot be
reported as a success. It does not cover *fabrication*. The ledger is written by the same orchestrator that
runs the command, so a deliberate false entry is not technically prevented. Non-empty artifact-file evidence
(the files each step must produce) makes casual fabrication a deliberate act rather than an easy default, but
it is not a cryptographic or independent check.

**Dated discontinuity (as of 2026-10-09).** Failure events for `/implement-with-haiku` appear for the first
time with this change, because the previously emitted classes were dropped. The `/implement-with-haiku` success
rate steps down, because successes now require the ledger gate. Pre-change successes are an unknown mix of real
and false successes and cannot be backfilled. Any comparison across this date must treat the two periods as
different measurements.

**Helper cannot run.** If `implement-ledger.py` itself cannot run, nothing is recorded for that command. The
command stays open, and the session-end sweep later closes it as `interrupted`. An `interrupted` outcome therefore
means "the helper did not record a terminal state", not "the run was known to be incomplete".

**Permission grant (narrow, pending verification).** The command doc's allowed-tools grant for the helper is
`Bash(python3 "$HOME/.claude/scripts/implement-ledger.py":*)`, which is narrow to that one script. It has not yet
been verified in a non-bypass permission mode. If it has to widen to `Bash(python3:*)` to run, this amendment
will be updated to say so.

## Amendment — Agent data guard in `diagnose` (#143 PR 2)

The agent data guard is an extra health-check PASS condition on `diagnose`: `diagnose` exits 0 only when the match rate, the unknown-field rate, and the guard all pass. It is **not** part of the usage gate, so the "single threshold" consequence above still refers only to the `/implement-with-haiku` gate. The guard's cutoff is a timestamp (`2026-10-13T18:58:25Z`, #267), and it judges only rows written after that time. A timestamp cannot express "events were written by fixed code," because a stale worktree's `/setup-local` can reintroduce the old hook wiring after the cutoff. The `Hook target` provenance line covers that gap; event-level provenance is a possible follow-up.

## Amendment — Known test-fixture rows excluded from `diagnose` (#280)

- `diagnose` drops rows whose `session_id` is in a closed, documented set of 8 known test-fixture ids (`floor-latch-sess`, `next-ask-sess`, `fresh-budget-sess`, `smoketest1` to `smoketest5`). It does so once, after the window filter, so the match rate, the unknown-field rate and the agent guard all judge real data only. This also changes the agent guard's input: it now judges post-cutoff rows minus fixture ids. `Excluded fixture rows: N` is printed on every run, including N = 0.
- The log stays append-only and untouched (#143). The rows are hidden at read time, never removed.
- Measured effect on one machine's log at decision time: unknown-token 63.2% to 63.8%, match rate 97.1% unchanged, agent guard 575 violations unchanged. The exclusion is about population purity. It is **not** a fix for the unknown-token rate.
- The list is closed. Recurrence is prevented at the source by the `--log` fix in `run_agent_lifecycle` and by the temp-HOME guard in `tests/run_all.py`, which fails the run when any suite writes under `<tmp HOME>/.claude/telemetry/`. A new leak is never added to the list.
