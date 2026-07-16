# ADR-0007: Triage and decision memory

**Status:** Accepted

## Context

The review panel got good, and that turned out to have a cost.

A `/expert-review` run now routinely produces twenty or thirty confirmed findings, most of them
correct. The pipeline ended at the Amalgamator: deduplicate, severity-rank, resolve conflicts, write
`final-report.md`, stop. The report is organized **by severity** — an author's concept, and the right
one for a document of record.

But the reader is not asking "what is most severe." The reader is asking "what do I have to decide."
Roughly 85% of findings are ones they would accept exactly as written, and the severity ordering
gives them no way to tell those apart from the few that need judgment. So they re-derived that
distinction by hand, finding by finding, every single review. The better the panel got, the more it
cost to use — which is a strange and self-defeating property for a review system to have.

Two smaller problems compounded it:

1. **No memory.** A judgment call made in one review — "yes, unbounded memory is fine in the offline
   importer" — was invisible to the next one. The same finding came back, and back, and the human
   re-answered it every time. The system could not learn.
2. **No cross-cutting view.** Every reviewer has exactly one lens, and the Amalgamator dedupes and
   ranks but never asks whether the findings *mean* something together. So the most valuable signal
   in a long report — *these six findings all trace to one wrong assumption* — was something the
   reader could only catch by feel, if they were paying enough attention, which is precisely what a
   long report exhausts.

Two seams in the code had been quietly waiting for this. `final-report.md`'s `## Sign-off Checklist`
table carried a `| Decision |` column that nothing in the repo ever filled in. And North Star Nick
emitted a `QUESTION` severity meaning "alignment unclear, needs clarification" — a literal
*needs-human-input* signal with nowhere to go.

## Decision

**The review pipeline ends in triage, not synthesis.**

A new role, the **Triage Chief** (`prompts/triage.md`), runs after the Amalgamator at the panel model
tier. It adds no findings and overturns no severities. It reads the finished report and answers one
question per finding: *does a person need to rule on this, or is it just work?* It sorts into **doing
it** (the default), **needs you**, and **deferred**, and it runs the cross-cutting **gut check** —
shared premise, drift, panel disagreement, recurrence — that no single-lens reviewer can perform.

The escalation test is deliberately narrow, and the prompt states plainly that **over-escalation is
the failure mode, not the safe default**: a *needs you* list long enough to skim is one nobody reads,
which rebuilds the original problem with extra steps. When uncertain, a finding does not escalate.
The full report is unchanged, one click away, and remains the gut-check instrument — triage sits in
front of it, not over it.

Escalations are then put to the human as `AskUserQuestion` choices, recommendation first, pros and
cons attached. A handful of questions replaces adjudicating thirty findings.

**Rulings are recorded, and recorded rulings are settled law.**

Answers land in `{project}/.claude/decisions.yaml` — the **fourth context layer**, amending
[ADR-0005](0005-three-layer-context-cascade.md)'s three-layer cascade. Reviewers read it at Pass 1
(`prompts/expert-framework.md`) and **must not re-raise a finding a recorded decision already
answers**. This is the loop that closes: every ruling makes the *next* report shorter. The system
gets quieter as the project's judgment accumulates, rather than louder as the panel improves.

The bar is **patterns and the spirit behind them, never nits** — the schema's `spirit` field is
required and load-bearing, because a rule without its intent gets cargo-culted into cases it was
never meant to cover. When a ruling changes how the system is *shaped* rather than how it is written,
it becomes an ADR instead.

Finally, an append-only `~/.claude/reviews/{project}/ledger.jsonl` records every finding's
disposition, and `/review-stats` reads it for recurring themes — on the principle that **a theme on
its third appearance is not three bugs, it is one missing decision.**

Supporting changes: the Amalgamator's mandate and report template were extracted to
`prompts/amalgamator.md` (it was the only agent role with its prompt inlined in the orchestrator);
North Star Nick now emits canonical severities with his strategic category as an additional tag,
since he was never an [ADR-0006](0006-reviewer-output-format-carve-outs.md) carve-out and his
non-canonical output had been silently breaking receipts, Pass 2 eligibility, and `/review-stats`
parsing.

## Consequences

- **Good:** The human reads a decision list, not a severity list. The panel can keep getting more
  thorough without the reader paying for it linearly — which removes the perverse incentive to keep
  the panel dumb.
- **Good:** Judgment compounds. A decision recorded once suppresses that finding forever, so review
  noise falls over a project's life instead of rising.
- **Good:** The gut check makes the "is an underlying assumption wrong?" question explicit and
  routine, rather than something a careful reader occasionally notices.
- **Cost: a fourth place a fact can live.** ADR-0005's convention extends: generic truth in the
  persona, project truth in `project.yaml`, single-persona project truth in the local override, and
  now **decided truth** in `decisions.yaml`. The line: `project.yaml` is hand-authored and describes
  what the project *is*; `decisions.yaml` is machine-appended (with approval) and records what a human
  *ruled*. Keeping rulings out of `project.yaml` keeps a hand-curated file from being churned by a
  tool.
- **Risk: over-escalation** silently rebuilds the load this removes. Guarded by a narrow test, an
  explicit anti-thoroughness instruction in the prompt, and a ≥20% warning in the orchestrator.
- **Risk: `decisions.yaml` becomes a junk drawer.** This is the dangerous one, because reviewers treat
  the file as settled law — a nit recorded there will *suppress real findings*. Guarded by the
  patterns-only bar, the required `spirit` field, and per-entry human approval. Prune it.
- **The command is no longer purely read-only.** `/expert-review` may now write `decisions.yaml` and
  draft an ADR — never source code, never without approval, and never from a subagent. Reviewer agents
  still have no `Edit` tool at all; that control is unchanged and remains technical, not conventional.
