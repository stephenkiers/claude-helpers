# ADR-0004: Model cost routing (Haiku for mechanical work)

**Status:** Accepted

## Context

A full expert review involves many distinct kinds of work. Some require deep judgment (domain
expertise, type design, concurrency reasoning). Others are mechanical (summarize this diff, route
sections by keyword, grep for uncalled symbols, answer a yes/no question about the code). Running
everything on the most capable model is needlessly expensive; running everything on a cheap model
loses the judgment that makes the review worth doing.

## Decision (Revised)

Route each step to the cheapest model that can do it well:

- **Haiku** for mechanical/throughput steps: Q&A (answer questions), Code Rot Cody (dead-code grep),
  and Consistency Checker (pattern matching). These are declared with `model: haiku` in
  `agents/expert-scout.md`.
- **Sonnet** for narrow judgment: the **Router** that decides which reviewers to include. This is
  judgment (not mechanical), but narrow (not deep expertise), so Sonnet is right-sized: capable at
  1/3 the Opus cost.
- **Panel model** (default Sonnet; override with `--model`) for the judgment-heavy work where persona
  expertise lives: the `expert-review` orchestrator and its Pass 1/Pass 2/Contrarian Carl subagents
  (which inherit the orchestrator's model), the single **Amalgamator** that synthesizes all findings,
  and the **Triage Chief** ([ADR-0007](0007-triage-and-decision-memory.md)) that decides what a human
  must rule on — deciding that wrong in either direction costs more than the model does, so it rides
  the panel tier deliberately.
- **Fable** is available as the deliberate expensive step: use `--model fable` when the diff is
  particularly gnarly and you want maximum synthesis capability on the Amalgamator.

Model choice is set per command/reviewer via frontmatter (`model:`), so it stays explicit and tunable.
For `/expert-review`:
- The Router is pinned to Sonnet via an explicit `model: "sonnet"` override in the Step 5 Router
  call (judgment but economical; uses expert-reviewer agent, not expert-scout)
- Haiku mechanical roles are pinned to Haiku in `agents/expert-scout.md`
- Panel roles (Pass 1, Carl, Pass 2, Amalgamator, Triage Chief) inherit from the command's model,
  overrideable via `--model`

**Per-invocation override:** `/expert-review --model <haiku|sonnet|opus|fable>` sets the model for the
**judgment panel only** — Pass 1, Contrarian Carl, Pass 2, Amalgamator, Triage Chief. The Router
(judgment but narrow, pinned to Sonnet) and the mechanical roles (Q&A, Cody, Consistency Checker) stay
at their pinned models regardless. So the flag scales the part of the bill that buys judgment expertise, and
only that part.

<!-- Pricing verified: 2026-07-17, source: https://www.anthropic.com/pricing -->
The tiers, cheapest to dearest (per 1M tokens, input/output): **Haiku 4.5** $1/$5 · **Sonnet 5** $3/$15
· **Opus 4.8** $5/$25 · **Fable 5** $10/$50. Note the shape of that ladder: Fable is the *most
capable and most expensive* model, at 2× Opus — it is not a cheap tier, and the default (inherit the
orchestrator's Sonnet) is deliberately two rungs below it. Opus is available as an explicit
escalation via `--model opus` or through a new Router-flagged human-confirmed escalation path (see
below). Verify pricing against the current model lineup before writing it into a doc; an earlier
draft of this ADR's own command had Fable labelled as the cheap option, which was simply false.

### Amendment: Sonnet as the new default panel tier (2026-08-23)

Opus had not proven significantly better than Sonnet for most reviews in practice, and defaulting to
it burned more credits than it was worth. The panel tier has flipped to Sonnet. Opus is now the
exception, not the rule — available only when explicitly requested via `--model opus` or when a new
escalation mechanism recommends it. That mechanism: the Router (which already reads the full diff and
business context) judges whether this diff's difficulty exceeds what a Sonnet-tier panel should be
trusted to review alone — signals include deep concurrency reasoning, security/crypto-critical
correctness, novel distributed-systems logic, or unusually high blast radius. If the Router flags an
escalation, the orchestrator asks the human via `AskUserQuestion` before upgrading to Opus — it
never auto-escalates, since that would silently reintroduce the cost problem this flip fixes. See
`prompts/router.md`'s new `## Escalation Recommendation` output section and `prompts/expert-review-panel.md`'s
new `Step 5.5: Opus Escalation Check (human-confirmed)` for the mechanics.

### Amendment: Haiku Effort Scout exception (2026-09-12)

**Narrow, bounded exception:** the Effort Scout ([ADR-0012 amendment](0012-effort-ladder-and-pr-mode.md#amendment-haiku-effort-scout-2026-09-12))
runs on Haiku despite making a judgment call (which effort tier a diff deserves), not a mechanical
lookup. It stays inside this ADR's spirit by being bounded: one sentence, evidence-grounded, and only
allowed to move the tier down (never up) relative to the deterministic mechanical fallback that runs
if it fails. See that amendment for the full guardrails.

## Consequences

- **Good:** Most token volume (Q&A, grepping, pattern matching) runs cheap; expensive models are
  reserved for judgment where they change the outcome. Routing is judgment but economical (Sonnet).
  Synthesis (Amalgamator) is the deliberately expensive step, made explicit by `--model fable`.
  The Triage Chief ([ADR-0007](0007-triage-and-decision-memory.md)) is a net addition to the
  pipeline — it rides the panel tier by design (deciding what a human must rule on is a judgment
  call), but it does not replace any existing step, so the panel's previous cost is a floor, not
  a comparison point.
- **Cost:** Sonnet occasionally mis-judges which reviewers to include, feeding the panel slightly
  wrong members. Acceptable because each panel member re-reads the actual diff, and the Amalgamator
  deduplicates/conflicts-resolves; a mild routing miss is caught downstream.
- **Cost:** Parallel per-reviewer subagents (ADR-0002) multiply *input* tokens — every reviewer
  re-receives the framework, its persona, and its sections. The Router narrows the initial panel to
  only truly relevant reviewers, so the multiplier is applied to fewer subagents than keyword-routing
  would produce.
- **No cross-review:** The old cross-review stage (each DEEP-DIVE reviewer reacted to others' findings)
  was quadratic in panel size (17 agents × 175k tokens each) and produced the least-valuable output.
  The Amalgamator replaces it: one expensive agent reads all findings and synthesizes (deduplicates,
  severity-ranks, conflicts-resolves) for the final report. Simpler, cheaper, and better.
- **Forking note:** If you fork, retune `model:` frontmatter to your budget — there is no global
  switch; it is intentionally per-step.

### Routing cost optimization

The Router reads the full `full-diff.patch` once, emits line ranges into it, and passes those ranges
to Pass 1 reviewers for bounded reads. One agent pays full diff price; many agents read their
sections only. The previous design (Haiku tagger + Haiku confirm-gate per unrouted reviewer) meant:

- **Tagger:** reads full patch once (unavoidable; line ranges are byte offsets)
- **Confirm-gate:** 19 parallel agents, each reading the full 44k-token patch, to render a yes/no
  routing decision. Measured: ~1.1M tokens on Haiku to produce 19 verdicts.

The new design (single Sonnet Router):

- **Router:** reads full patch + summary once, routes based on judgment + index signals
- **Pass 1 reviewers:** read their bounded sections (not the whole patch)

Sonnet is 3× the cost of Haiku per token, but the Router runs once instead of 19 times. Token math:
one Sonnet call over the full patch costs about as much as 3 Haiku calls over the same patch; the old
gate ran 19 Haiku calls over the same patch. 3 vs. 19 yields a modeled ~6× cost reduction for the
routing step (unmeasured; the gate's ~1.1M-token cost is the measured baseline, while the Router's
cost is a model estimate not yet benchmarked against a real run). Routing accuracy (judgment vs.
keywords) is a bonus on top.

### Amendment: implementer escalate-once exception (2026-10-16)

ADR-0023's decision to never auto-remediate includes one narrow exception: a single-unit implement/fix shortfall offers a fourth hard-stop option, `Retry with sonnet`, for manual escalation. An automatic escalation path is reserved for when a turns source becomes available (see #325); until then, the automatic path is dormant and the manual option is the live path. The 2026-10-25 amendment in ADR-0023 (#341) records that the turns probe was not run, so the path stays dormant. When available, the `/expert-flow` orchestrator would re-run the step once on Sonnet if turn exhaustion did not cause the failure and no escalation has been attempted for this step. This is a bounded escalation (one per step per flow, Sonnet-only, always logged), never automatic for multi-unit runs or non-shortfall failures, and Opus is never used for implementer escalation. The escalated Sonnet unit would inherit the same `maxTurns: 120` cap and partial work from the Haiku attempt's `START_SHA`. Rationale: a single-unit shortfall is a reasonable signal that the task exceeds what Haiku can reliably do in one unit, and re-running Sonnet is cheaper than escalating to higher tiers for every capacity miss. See ADR-0023's 2026-10-16 amendment for the full design.

### Amendment: flow-implement and flow-review step agents (2026-10-26)

When `/expert-flow` spawns the implement, fix, and review steps, they run on purpose-built restricted agents (`flow-implement` and `flow-review`; see ADR-0008 2026-10-26 amendment and ADR-0023 2026-10-26 amendment) rather than `general-purpose` agents. These agents carry their own model pins via agent frontmatter:

- **`flow-implement` (coordinator, Sonnet):** The agent frontmatter pins `model: sonnet`. This governs the orchestrator step that spawns Haiku `plan-implementer` units through `/implement-with-haiku`. The units themselves remain on `plan-implementer`'s own model (Haiku) and continue to respect the `--implementer-model` override flag. The #315 Sonnet escalation (a manual `Retry with sonnet` option on single-unit shortfalls, see amendment above) is unchanged.

- **`flow-review` (Sonnet unless Step 5.5 asks):** The agent frontmatter pins `model: sonnet`. This restores the `/expert-review` documented default (the command inherits the Sonnet panel tier). Inside a flow, the review runs on Sonnet unless the flow omits `--review-effort` at flow start. When `--effort` is passed at flow invocation, the flow's Step 5.5 (Opus escalation check) is skipped, so only Sonnet is available. When `--effort` is omitted, `/expert-review` in the flow runs with the session model (which defaults to Sonnet), and a human-confirmed Opus escalation may happen through Step 5.5 (documented in `/expert-review` itself). The escalation-check step is skipped only when the flow passes `--effort`.

**Model conventions:** Aliases like `sonnet` are used in agent-frontmatter `model:` fields for `flow-*` agents only (a convenience, pinning to the resolved `claude-sonnet-5-5` id at ship time); dated model ids are unchanged elsewhere. A test reconciles both conventions by asserting that every `agents/*.md` `model:` field is either an alias in `{sonnet}` (for `flow-*` only), a dated id from a documented constant set (`claude-haiku-4-5-20251001`, etc.), or absent (defaulting to the session model). The resolved id for the `sonnet` alias observed in the first flow run is `claude-sonnet-5-5` (probe result, Step 2.1).

**Task-time model override (probe result, Step 2.1):** Spawning a Task with `model: sonnet` overrides an agent whose file pins `claude-haiku-4-5-20251001`; the subagent ran on `claude-sonnet-5-5` (transcript `message.model`). This is how the #315 Sonnet escalation works (a manual override at hard-stop time) and is already depended on by the `/expert-review --model` override. The escalation is not automatic; the human confirms it at the hard-stop menu. **Probe sessions (excluded from /usage-report):** 04a8f48c, 6f4a40a1, 9edf70ee, d4642c82, d9503c8, ecc74372, ff4dabbe.
