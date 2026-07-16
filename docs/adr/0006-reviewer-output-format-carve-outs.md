# ADR-0006: Reviewer output-format carve-outs

**Status:** Accepted

## Context

The canonical output format for every standard reviewer is defined once in
[`prompts/expert-framework.md`](../../prompts/expert-framework.md). This single source of truth is
load-bearing for downstream steps:

1. **Pass 2 re-evaluation** ([ADR-0002](0002-blind-first-two-pass-review.md)) re-reads Pass 1 findings
   in this canonical shape to debias them with project context.
2. **Amalgamation** (Step 8 of `/expert-review`) parses every reviewer's output file the same way,
   grouping findings by severity and deduping them for the final roll-up.

A persona that defines its own `OUTPUT FORMAT` block breaks this contract. If one reviewer emits a
different shape, the amalgamator's parser fails or misreads findings, and Pass 2 cannot re-evaluate
findings it cannot parse.

Three personas deliberately opt out of the canonical format:

- **Code Rot Cody** — `reviewers/code-rot-cody.yaml`
- **Contrarian Carl** — `reviewers/contrarian-carl.yaml`
- **Consistency Checker** — `reviewers/consistency-checker.yaml`

The rationale for these carve-outs is scattered: partial mentions in `reviewers/README.md`, an aside
in `expert-framework.md`, and a one-line note in [ADR-0002](0002-blind-first-two-pass-review.md)
on two-pass scope. There is no single place that states the contract, the bar a
persona must clear to opt out, and why each existing carve-out qualifies.

## Decision

**Default rule: Use the canonical output format. Do not add a custom `OUTPUT FORMAT` block.**

A persona may define its own `OUTPUT FORMAT` block only if **all three of the following hold**:

1. **Output is structurally incompatible with the canonical Decision / Files / Findings / Severity
   schema.** The canonical schema genuinely cannot express the persona's output. Examples that pass
   this test:
   - A symbol-inventory table (Code Rot Cody)
   - A contrastive "what others missed" structure (Contrarian Carl)
   - A side-by-side pattern-location table (Consistency Checker)

   Examples that fail: "I want more bullet points" or "I prefer emojis" — these are style preferences,
   not structural incompatibility.

2. **The persona's distinctive output is supplementary context, not the canonical severity payload
   the amalgamation parses.** Step 8 of `/expert-review` groups and severity-counts the canonical
   Findings block from every reviewer file. A carve-out qualifies when its custom shape carries
   information the roll-up does not grade for severity — a connectivity inventory, a contrastive
   "what others missed" list, or a Location-A-vs-B comparison — surfaced for humans or a
   cross-reference step rather than as graded findings.

   Pass 2 re-evaluation ([ADR-0002](0002-blind-first-two-pass-review.md)) is a **separate axis**: it
   re-weighs a reviewer's findings against project context and is independent of output shape. A
   carve-out may still participate in Pass 2 — Code Rot Cody and Consistency Checker do when they
   have findings, Contrarian Carl does not — so Pass-2 participation neither earns nor forfeits a
   format carve-out.

3. **The override is documented here,** explaining why the persona qualifies.

### Justification of existing carve-outs

- **Code Rot Cody** — Mechanical grep-based symbol connectivity analysis (`model: haiku` per
  [ADR-0004](0004-model-cost-routing.md), `runOrder: after-pass1`). Distinctive output is a
  symbol-inventory table with status cells (`CONNECTED / DEAD / TEST-ONLY`) — connectivity metadata
  the severity roll-up does not grade, not findings. (He participates in Pass 2 only when he raises
  findings, per the orthogonal-axis note above — dead code may be intentionally staged for a
  follow-up PR.) Passes all three criteria.

- **Contrarian Carl** — Runs last (`runOrder: last`) and sees all prior findings
  (`requiresPriorFindings: true`). Output is contrastive ("What Everyone Else Covered" vs "What
  Everyone Missed"), read by humans as context rather than parsed into the severity roll-up; he also
  skips Pass 2 entirely. Passes all three criteria.

- **Consistency Checker** — Mechanical pattern-matching (`model: haiku` per
  [ADR-0004](0004-model-cost-routing.md), `runOrder: after-tagger`, requires the PR description for
  cross-reference). Distinctive output is a side-by-side table comparing Location A vs Location B
  rather than graded findings. (It participates in Pass 2 when it has findings, per the
  orthogonal-axis note — a pattern inconsistency may be intentional.) Passes all three criteria.

## Consequences

- **Good:** A single place to point a would-be fourth carve-out. The bar is explicit and high by
  default, so the parse contract stays intact. A new persona must clear all three criteria;
  proposers must explain why the canonical format is insufficient.
- **Cost:** The canonical format and the three exceptions now share a doc to keep in sync if the
  format evolves. If the canonical schema changes, check the three carve-outs to see if they still
  satisfy criterion 1.
- **Dogfooding:** This repo wires [North Star Nick](../../reviewers/north-star-nick.yaml) to review
  changes against `docs/adr/` (see [ADR-0005](0005-three-layer-context-cascade.md)). Future
  carve-out proposals will be flagged against this ADR on code review.

## Amendment — additive fields (ADR-0007)

[ADR-0007](0007-triage-and-decision-memory.md) introduced fields that ride *alongside* the canonical
block rather than replacing it: North Star Nick's `**Category**` tag, and the optional `**Human
Call**` nomination available to every reviewer. These are **not** format carve-outs and do not need
to clear the three-criteria bar above.

The distinction is precise: a **carve-out** replaces the canonical Decision / Files / Findings /
Severity schema with an incompatible shape (and must satisfy all three criteria). An **additive
field** leaves the canonical block fully intact and appends an extra line. An additive field is
permitted when — and only when — it has a **named downstream consumer** that reads it; a field no
component reads is not additive, it is noise, and is removed rather than left dangling. The three
additive fields in play and their consumers: `**Category**` (Triage's escalation test 6 and the gut
check), `**Human Call**` (the Triage Chief, which decides whether it reaches the human), and
`## Suppressed by decision` (Triage's *Already settled* bucket and `/review-stats`' suppression
audit). Because the canonical block is untouched, Pass 2, amalgamation, and `/review-stats` parse
these reviewers exactly as they parse any other — which is why North Star Nick is not, and does not
need to be, a carve-out.
