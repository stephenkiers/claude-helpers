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

### Amendment: Centralized model-policy registry (2026-08-25)

Expert-review model routing is now centralized in a single registry rather than scattered across
agent frontmatter and command overrides. The per-agent `model:` pins described above were a model-
retirement bug: a dated provider ID (e.g. `claude-haiku-4-5-20251001`) baked into frontmatter
breaks silently when the provider retires that ID, and there was no single place to retune the
review system for a fork. This amendment fixes both.

**Semantic aliases are policy, not provider IDs.** `haiku`/`sonnet`/`opus`/`fable` are the only
model identifiers used in expert-review routing. Claude Code settings map these aliases to local
gateway models via `ANTHROPIC_DEFAULT_*_MODEL` environment variables; the aliases themselves never
name a provider model ID.

**`config/expert-review-models.json` is the single helper-level source of truth** for review model
policy. Its schema is `{schemaVersion, aliases, roles:{router,mechanical,panel,escalation}}`; each
role is an ordered alias list where the first available alias wins. A fork changes this one file to
retune the whole review system — no per-agent edits required.

**`scripts/resolve-expert-review-models.py` validates the registry and resolves each role** before
any review agents launch. It reads `~/.claude/settings.json` only as an optional availability source
(`enforceAvailableModels`) and never rewrites settings. `fable` is always `unchecked` (unmapped), so
it is always available and never gates another alias. The resolver emits one JSON object on stdout;
errors go to stderr with a non-zero exit.

**Ordered fallbacks are bounded and visible.** When a provider model disappears at runtime, an
`unchecked` alias that fails at spawn heals to the next configured alias exactly once, with a printed
receipt and cached metadata — never loops, never silent. If the ordered list is exhausted, the
resolver stops and names the registry entry that ran dry.

**Strict overrides never fall back.** An explicit `--model <alias>` that is unavailable **fails**
rather than silently switching to a different model. The strict override is the user's deliberate
choice; healing it would be the silent-substitution failure mode this amendment exists to prevent.

**Exact provider model IDs are prohibited** in expert-review routing. Agent frontmatter and the
registry use semantic aliases only. `agents/expert-scout.md` carries no model default at all — the
caller passes the resolved `MECHANICAL_MODEL` alias on every spawn, so there is no second hidden
default to go stale. `agents/plan-implementer.md` (outside the expert-review role registry but
sharing the same retirement bug) uses `model: haiku`.

