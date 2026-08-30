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
`validate_event` in the writer (`scripts/run-metrics.py`), and any new call site must use a fixed,
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
investigating the original ~38% match rate found (predominantly a pre-#107 correlation bug, not
session abandonment). This is a health check on the log's trustworthiness, not a production
alerting system.

**No built-in cost-optimization reports.** `/usage-report` deliberately stays a thin wrapper
around `diagnose` rather than growing aggregation views (cost-per-finding, retry/zero-yield
detection, concurrency-band breakdowns). The raw log is made legible via documented query patterns
(`docs/metrics.md`) so a future Claude session can read `events.jsonl` directly and answer
whatever ad hoc question is actually being asked — including "is this expensive thing worth it,"
which no fixed report can anticipate.

**Yield fields, not just cost fields.** Findings-produced/accepted/unique/rejected/acted-upon and
checks-executed/outcome are part of the allowlisted event model precisely so a stage's *output*
can be weighed against its *cost* — matching the `/implement-with-haiku` example above.

## Consequences

- Telemetry is opt-in and additive; a repo or fork that doesn't run `install.sh --with-telemetry`
  is unaffected.
- No dashboards or automated thresholds exist to game — the design deliberately resists producing
  a single number a future change could be tempted to minimize at the expense of value.
- Concurrent-subagent correlation safety is an open verification gap, not yet resolved; instrumenting
  `/expert-review` or `/implement-with-haiku` before resolving it risks cross-attributed stage
  records under the session-state-file mechanism.
- `/usage-report` will likely never grow deterministic aggregation views under this design; treat
  requests for "a report that shows X" as a cue to write a documented query pattern instead, not a
  new subcommand.
