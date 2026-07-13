# ADR-0003: Tagger-based reviewer routing

**Status:** Accepted

## Context

Most diffs are relevant to only a few reviewers. Running every persona against every section wastes
tokens and dilutes results (see [ADR-0001](0001-progressive-disclosure.md)). We need a cheap way to
decide *which* reviewers see *which* parts of a diff.

## Decision

A dedicated **tagger** step (a Haiku subagent, prompt in `prompts/tagger.md`) runs after the
summarizer and before the reviewers. It maps each section of the diff to the reviewers whose
`triggers` in `reviewers/index.yaml` match, and writes the routing to `tagged-sections.md`.

Reviewers then receive only their tagged sections. Two deliberate exceptions bypass routing and always
receive the full diff because their value is cross-cutting:

- **Sam System** — traces composition/data-flow across files.
- **Code Rot Cody** — greps the whole repo for orphaned/uncalled symbols.

`--all` overrides routing to force every reviewer; `--full` reviews the whole codebase, not just the
diff. Naming reviewers explicitly (`/expert-review rachel,security-sage`) *is* the routing decision:
only they run, and the gate below is skipped.

## Amendment — the confirm-gate (supersedes "keep triggers generous")

The original position was: *keep triggers generous and rely on Pass 2 to drop noise*, with un-routed
reviewers still running a full-diff QUICK-SCAN as the safety net. That made the tagger a depth knob,
not a participation knob — every persona ran on every diff, at panel-model cost.

Triggers are now deliberately **narrow**, and the safety net is a **haiku confirm-gate** instead:
each un-routed reviewer gets a cheap subagent that sees the diff, its own domain, and the tagger's
stated skip reason *as a hypothesis to check*. It answers AGREE-SKIP (recorded in `skipped.md`) or
ESCALATE, and an escalation is promoted to a full Pass 1 review on the panel model.

This keeps a second opinion per persona — the tagger is a keyword heuristic and has been observed
inventing matches — while paying haiku prices for it rather than panel prices.

**The metric is escalation rate**, tracked per reviewer by `/review-stats`:

- Frequent escalations with confirmed findings → the triggers are too narrow; widen them.
- Many gate-skips and zero escalations ever → either a correctly narrow persona, or a gate that
  rubber-stamps the tagger. Spot-check a `skipped.md` reason against the diff to tell them apart.

## Consequences

- **Good:** Cheap routing (Haiku) gates expensive expert passes. Triggers live in one lightweight
  index, easy to tune. Narrow triggers no longer risk silently dropping a reviewer, because the gate
  can overrule them — and every override is logged as a tagger error.
- **Cost:** Two known failure modes. The tagger can invent matches (mitigated by the literal-triggers
  rule in `prompts/tagger.md`). The gate can anchor on the tagger's stated reason and rubber-stamp it
  (mitigated by framing that reason as a rebuttable hypothesis, requiring the gate to report what it
  actually scanned, and watching escalation rate).
- **Constraint on contributors:** New personas must declare `triggers` in `index.yaml`. A persona with
  no triggers is never routed — it will only ever reach the gate, and only escalate on the gate's own
  judgment.
