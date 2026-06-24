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
- [ADR-0003: Tagger-based reviewer routing](0003-tagger-routing.md)
- [ADR-0004: Model cost routing (Haiku for mechanical work)](0004-model-cost-routing.md)
- [ADR-0005: Three-layer context cascade](0005-three-layer-context-cascade.md)
- [ADR-0006: Reviewer output-format carve-outs](0006-reviewer-output-format-carve-outs.md)

## Format

Each ADR uses: **Status**, **Context**, **Decision**, **Consequences**.
