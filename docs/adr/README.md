# Architecture Decision Records

This directory documents the key design decisions behind claude-helpers. They serve two purposes:

1. **For humans** — understand *why* the system is shaped the way it is before you fork and change it.
2. **For North Star Nick** — the `north-star-nick` reviewer reads these ADRs (wired via
   `.claude/reviewers/north-star-nick-local.yaml`) to check changes against documented decisions.

ADRs are decisions, not law. If you fork this repo and want to go a different way, that's the whole
point — but update or supersede the relevant ADR so your fork stays legible to the next reader (and
to North Star Nick).

## Index

- [ADR-0001: Progressive disclosure of experts](0001-progressive-disclosure.md)
- [ADR-0002: Blind-first, two-pass review](0002-blind-first-two-pass-review.md)
- [ADR-0003: Tagger-based reviewer routing](0003-tagger-routing.md) — amended by ADR-0003.2
  (single-judgment Router replaces the keyword tagger + confirm-gate; see the Amendment section
  in that file)
- [ADR-0004: Model cost routing (Haiku for mechanical work)](0004-model-cost-routing.md)
- [ADR-0005: Context cascade](0005-three-layer-context-cascade.md) — amended by ADR-0007 (a fourth
  layer; the heading dropped "Three-layer" but the filename is kept for inbound links)
- [ADR-0006: Reviewer output-format carve-outs](0006-reviewer-output-format-carve-outs.md) — amended
  by ADR-0007 (additive fields are not carve-outs)
- [ADR-0007: Triage and decision memory](0007-triage-and-decision-memory.md) — amends ADR-0004
  (Triage Chief tier), ADR-0005 (fourth context layer), and ADR-0006 (additive fields); carries its
  own Amendment recording the dogfooded rulings

## Format

Each ADR uses: **Status**, **Context**, **Decision**, **Consequences**.

**Amending an ADR.** A later decision that revises an earlier one adds an `## Amendment — <summary>
(ADR-NNNN)` section to **both** ADRs — a pointer forward on the amended ADR and the substance on the
amending one — rather than rewriting the original Decision. The original stays legible as what was
decided *then*; the amendment records what changed and why. A cross-reference in this index's entry
flags that an ADR has been amended. Retitle an ADR's heading if a superseded word becomes misleading
(as ADR-0005's "Three-layer" did), but never rename the file — inbound links depend on it.
