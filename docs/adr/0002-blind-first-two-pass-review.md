# ADR-0002: Blind-first, two-pass review

**Status:** Accepted

## Context

When a reviewer is handed the author's justification up front ("this is fine because X"), it tends to
rationalize the code instead of scrutinizing it — the same anchoring bias human reviewers fall into.
But context-free review also produces noise: findings that are technically true but irrelevant given
the project's constraints, scope, or documented decisions.

## Decision

Run each reviewer in two passes:

1. **Pass 1 — blind.** The reviewer sees the diff (and its tagged sections) but *not* the business
   context, PR narrative, or rationalizations. It records findings, open questions, and proposals
   based purely on the code.
2. **Pass 2 — re-evaluation.** The reviewer re-reads its own Pass 1 findings *with* the business
   context and answers to its open questions, then keeps, downgrades, or withdraws each finding.

Open questions raised in Pass 1 are answered by a cheap Haiku Q&A subagent before Pass 2, so the
re-evaluation is informed rather than speculative.

## Consequences

- **Good:** Catches issues a context-first reviewer would rationalize away, while Pass 2 filters out
  findings that don't survive contact with real constraints. Each pass is a separate checkpoint
  artifact, so the reasoning is inspectable.
- **Cost:** Two passes per reviewer is more expensive than one. Justified for a review tool whose value
  is catching what a single rationalizing pass misses; mechanical reviewers (e.g. Code Rot Cody) skip
  the two-pass structure since they have no judgment to debias.
