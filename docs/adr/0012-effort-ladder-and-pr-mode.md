# ADR-0012: Effort ladder and PR mode for `/expert-review`

**Status:** Accepted

## Context

`/expert-review-coworker-beta` proved a fast/cheap tier works: 6 haiku scouts with CRITIC
`path:line` grounding plus one sonnet merge, end-to-end in a couple of minutes. But a side-by-side
on PR instacart/bento-cli#254 showed it misses the full panel's substantive findings — the two
approaches are complements, not competitors. Meanwhile the full pipeline (~40 subagent spawns,
~30 minutes) is overkill for small diffs, and there was no sanctioned middle ground between "six
scouts" and "everyone, twice."

Separately, peer-PR review lived in two commands (`/expert-review-coworker`, `-beta`) that duplicate
`/expert-review`'s orchestration with a different tail (pr-comment-guide instead of Triage). The
shared panel (ADR-0009, Decision 2) already made the cores identical; only the wrappers differed.

The unmerged branch `feature/74-...` (commit eb5ae9f) explored part of this space and is mooted by
this ADR; it was closed unmerged.

## Decision

**Decision 1: A manual `--effort 1–5` ladder on `/expert-review`, default 4 = today's behavior.**

| Level | Name | What runs |
|---|---|---|
| 1 | swarm | 6 fixed-lens haiku scouts (`prompts/peer-scout.md`) → 1 merge agent (`prompts/swarm-merge.md`) → `final-report.md` → Triage |
| 2 | focused pair | Router picks exactly the top 2 judgment reviewers; Carl + Cody + Consistency Checker still run; full pipeline otherwise |
| 3 | pair + Bob | Effort 2 plus `uncle-bob` pre-seated (full-patch read, like named mode) |
| 4 | normal | Current behavior, unchanged |
| 5 | everyone | All `index.yaml` reviewers, implemented as named-selection over the full index — the router is bypassed with no new code path |

Triage runs at **every** level — the output contract (`final-report.md` → `claude-action-plan.md`) is
identical regardless of how the findings were produced. `--effort` + named reviewers is an error
(the user is sizing the run twice); `--model` stays orthogonal, with the effort-1 merge agent
pinned to sonnet unless `--model` was explicit.

**Monotonic-cost rationale for the reduced always-run set at 2–3.** Sam System reads the full patch
— he is not a cheap seat — so at levels 2–3 he runs only if the router's top-2 includes him. Code
Rot Cody and the Consistency Checker stay always-run even there: they are pinned-haiku cheap, and
they are precisely the check on a 2-person panel's characteristic failure mode — claims nobody else
was routed to verify. Contrarian Carl still runs last, always: a contrarian is most valuable exactly
when the panel is smallest.

**Decision 2: PR mode — a positional `<github-pr-url>` argument folds coworker review into
`/expert-review`.**

A positional argument matching `^https://github\.com/[^/]+/[^/]+/pull/[0-9]+/?$` (checked before
reviewer-name matching) replaces the local-diff Step 1 with the existing
`scripts/setup-pr-worktree.sh` setup call; Steps 4–11 of the shared panel run unchanged against the
PR worktree, **including the Triage Chief** — unlike the old coworker command, PR mode triages.

PR mode preserves the ADR-0009 write boundary exactly: no prior-review cache check, no Step 12
rulings loop, no Step 13 cache write — never write to a repo you don't own. The *Needs you* items
(the candidate PR comments) are listed verbatim in the closing message for the user to post
themselves; the pr-comment-guide / walkthrough / posted-comments tail is not produced. PR mode is
mutually exclusive with named reviewers and `--force`.

**Decision 3: Deprecation, not deletion, for `/expert-review-coworker(-beta)`.**

Both commands get a banner pointing at the new form and remain fully functional; every prompt file
they depend on (`peer-scout.md`, `peer-merge.md`, `pr-comment-guide.md`, the shared panel) stays.
The panel's new `EFFORT` precondition is OPTIONAL with unset ≡ 4 ≡ today's behavior, so the
deprecated commands exercise the identical code path they always did. Deletion is deferred until
the new form has soaked; the pr-comment-guide flow remains the reason to reach for the old commands
in the meantime.

**Decision 4: Deliberate deviation — effort 1 is not a blind panel.**

ADR-0002's blindness rule is relaxed at effort 1: `pr-context.md` (PR title and body) reaches the
Wave 1 scouts, and no Pass 2 exists to hold the context reveal. This is acceptable because effort 1
is a cheap screen, not a verdict — its job is to catch confident, tool-verified, low-hanging
findings fast, and the anchor-verification step in `swarm-merge.md` (the CRITIC trust-gap closer)
is the control that replaces the second pass. Anyone who needs the blind guarantee runs effort 4.
The deviation is recorded here rather than discovered later.

## Consequences

- **Good:** one command spans swarm → full panel; the right-sized review is a flag, not a different
  command to learn.
- **Good:** effort 5 reuses the named-selection path — "everyone" adds zero new control flow.
- **Good:** PR mode inherits Triage, so peer review findings now land in the same decision buckets
  as author review instead of a bespoke guide format.
- **Good:** the ADR-0009 write boundary is stated once (PR mode skips Steps 0.5/12/13) instead of
  being re-encoded per command.
- **Cost:** `--effort` is manual. The beta deliberately had no auto-gate ("you are the gate"); the
  ladder keeps that — a complexity auto-gate remains future work.
- **Cost:** effort 1 has no resumability (inline scout returns) and no blindness (Decision 4). Both
  are accepted trade-offs for a ~2-minute screen, recorded here.
- **Cost:** three review commands exist until the deprecated two are deleted; banners and this ADR
  are the guard against drift.

## Amendment (2026-08-29)

**Default effort is now heuristic-derived from `~/.claude/effort-heuristic.yaml` (tiers 2–4 only); efforts 1 and 5 remain explicit-only.**

When `--effort` is not passed, `/expert-review` loads `effort-heuristic.yaml` (user-level or project-level;
template in `prompts/effort-heuristic.yaml.template`) and auto-picks 2, 3, or 4 based on diff signal:
file count, lines of code, risk keywords in paths/diffs/issues/commits. This biases toward over-review
as the baseline ("start with a somewhat over-reviewed baseline"). Efforts 1 and 5 remain explicit-only
to avoid the failure mode of auto-downsizing risky changes or auto-upsizing routine ones.

The motivation: usage data showed `expert-reviewer` subagents at 15% of weekly token spend, the largest
single line item. Most small/mechanical diffs routinely run the full panel (effort 4, default) because
users either never pass `--effort` or habitually typed `--effort 4` themselves. The heuristic lets the
default scale without the user remembering to dial it down, and captures the signal for raising it when
a diff scores high on risk despite being small. Closing message prints the run summary (code recap, effort
and source, reviewer names and reason) plus an explicit `/implement-with-haiku` handoff, so the execution
path is copy-pasteable and doesn't require re-reading the action plan.
