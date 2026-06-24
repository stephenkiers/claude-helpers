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
(mechanical reviewers skip two-pass). There is no single place that states the contract, the bar a
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

2. **The output sits outside the parse path that depends on the canonical format.** That is:
   - It does **not** participate in Pass 2 re-evaluation (mechanical reviewers skip two-pass per
     [ADR-0002](0002-blind-first-two-pass-review.md), or the persona's output is consumed by humans
     or a different step rather than the severity-counting amalgamation).

3. **The override is documented here,** explaining why the persona qualifies.

### Justification of existing carve-outs

- **Code Rot Cody** — Mechanical grep-based symbol connectivity analysis. Output is a symbol-inventory
  table with status cells (`CONNECTED / DEAD / TEST-ONLY`), not severity findings. Runs mechanical
  (`model: haiku`, `runOrder: after-pass1`). Participates in Pass 2 re-evaluation only if findings
  exist (dead code might be intentionally staged for follow-up). Passes all three criteria.

- **Contrarian Carl** — Runs last (`runOrder: last`) and sees all prior findings
  (`requiresPriorFindings: true`). Output is contrastive ("What Everyone Else Covered" vs "What
  Everyone Missed") and is read by humans as context, not fed back through Pass 2 re-evaluation or
  the severity-counting amalgamation. Passes all three criteria.

- **Consistency Checker** — Mechanical pattern-matching (Haiku model, `runOrder: after-tagger`,
  requires PR description for cross-reference). Output is a side-by-side table comparing Location A
  vs Location B, not architectural findings. Participates in Pass 2 re-evaluation if findings
  exist (pattern inconsistency might be intentional). Passes all three criteria.

## Consequences

- **Good:** A single place to point a would-be fourth carve-out. The bar is explicit and high by
  default, so the parse contract stays intact. A new persona must clear all three criteria;
  proposers must explain why the canonical format is insufficient.
- **Cost:** The canonical format and the three exceptions now share a doc to keep in sync if the
  format evolves. If the canonical schema changes, check the three carve-outs to see if they still
  satisfy criterion 1.
- **Dogfooding:** This repo wires [North Star Nick](../north-star-nick-local.yaml) to review
  changes against `docs/adr/` (see [ADR-0005](0005-three-layer-context-cascade.md)). Future
  carve-out proposals will be flagged against this ADR on code review.
