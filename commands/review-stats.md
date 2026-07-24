---
name: review-stats
description: Stub — the ledger machinery was removed in chore/29, so this command is currently non-functional. Retained as a marker for the follow-up ticket that will reintroduce disposition tracking.
model: haiku
---

# Review Stats — currently non-functional

This command previously aggregated a per-repo ledger to report per-reviewer signal rates. That
machinery was removed in `chore/29-remove-decisions-yaml-and-ledger-machinery-from-ex` because it
silently suppressed findings based on past decisions, which the user explicitly ruled out.

No follow-up ticket has been opened yet. If disposition tracking is reintroduced, see the
`chore/29` amendment in `docs/adr/0007-triage-and-decision-memory.md` — specifically the
escape-hatch clause — for the design constraints any replacement must satisfy (prompt-the-human,
not silent suppression). Until then, `/review-stats` is a no-op.

Tell the user this command is currently non-functional and point them at the chore/29 amendment in
`docs/adr/0007-triage-and-decision-memory.md` for the full context.
