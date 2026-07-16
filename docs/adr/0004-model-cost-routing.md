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
- **Panel model** (default Opus; override with `--model`) for the judgment-heavy work where persona
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

The tiers, cheapest to dearest (per 1M tokens, input/output): **Haiku 4.5** $1/$5 · **Sonnet 5** $3/$15
· **Opus 4.8** $5/$25 · **Fable 5** $10/$50. Note the shape of that ladder: Fable is the *most
capable and most expensive* model, at 2× Opus — it is not a cheap tier, and the default (inherit the
orchestrator's Opus) is deliberately one rung below it. Verify pricing against the current model
lineup before writing it into a doc; an earlier draft of this ADR's own command had Fable labelled as
the cheap option, which was simply false.

## Consequences

- **Good:** Most token volume (Q&A, grepping, pattern matching) runs cheap; expensive models are
  reserved for judgment where they change the outcome. Routing is judgment but economical (Sonnet).
  Synthesis (Amalgamator) is the deliberately expensive step, made explicit by `--model fable`.
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
gate ran 19 Haiku calls over the same patch. 3 vs. 19 is a ~6× cost reduction for the routing step —
not a measured figure (the gate's ~1.1M-token cost above is measured; the Router's is a model, not
yet benchmarked against a real run). Routing accuracy (judgment vs. keywords) is a bonus on top.
