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

Answers land in a repo-keyed `decisions.yaml` — the **fourth context layer**, amending
[ADR-0005](0005-three-layer-context-cascade.md)'s three-layer cascade. It lives **outside the working
tree** at `~/.claude/reviews/{owner-repo}/decisions.yaml` (see the Amendment below for why). Reviewers
read it at Pass 1 (`prompts/expert-framework.md`) from a path the orchestrator supplies, and **must
not re-raise a finding a recorded decision already answers** — recording it instead as suppressed, so
a shrinking report never masquerades as a blinded one. This is the loop that closes: every ruling
makes the *next* report shorter. The system gets quieter as the project's judgment accumulates,
rather than louder as the panel improves.

The bar is **patterns and the spirit behind them, never nits** — the schema's `spirit` field is
required and load-bearing, because a rule without its intent gets cargo-culted into cases it was
never meant to cover. When a ruling changes how the system is *shaped* rather than how it is written,
it becomes an ADR instead.

Finally, an append-only `~/.claude/reviews/{owner-repo}/ledger.jsonl` — keyed on repo identity, next
to the decisions file — records every finding's disposition, and `/review-stats` reads it for
recurring themes on the principle that **a theme on its third appearance is not three bugs, it is one
missing decision.** The Triage Chief pre-serializes each ledger line so the orchestrator never
assembles JSON from model-authored text in a shell string.

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
  explicit anti-thoroughness instruction in the prompt, and an absolute-count warning in the
  orchestrator (see the Amendment — the original ratio-only guard measured the wrong thing).
- **Risk: `decisions.yaml` becomes a junk drawer.** This is the dangerous one, because reviewers treat
  the file as settled law — a nit recorded there will *suppress real findings*. Guarded by the
  patterns-only bar, the required `spirit` field, per-entry human approval, and a hard floor: a
  decision can never suppress a CRITICAL or a security finding (see the Amendment). Prune it.
- **The command is no longer purely read-only.** `/expert-review` may now write `action-plan.md`'s own
  ruling lines, `decisions.yaml`, and draft an ADR — never source code, never without approval, and
  never from a subagent. The `Edit` grant is a red line scoped in the orchestrator to exactly those
  three targets (see the Amendment below). Reviewer agents still have no `Edit` tool at all; that
  control is unchanged and remains technical, not conventional.

## Amendment — read semantics, floor, and store location (dogfooded rulings)

The first `/expert-review` run against this feature reviewed the feature itself and surfaced that
`decisions.yaml` had been designed as a *write path* but not as a *read contract*. Seven escalations
were ruled on; the resulting refinements amend the Decision above:

- **The decisions file and ledger live OUTSIDE the repo**, at a repo-keyed path
  (`~/.claude/reviews/{owner-repo}/`), keyed on repository identity (`gh nameWithOwner`), not on a
  directory name. Two reasons, one per store. *Decisions:* a decision suppresses findings, so an
  in-tree file lets a branch add an entry that silences the review of that same branch — a change
  licensing itself. Out of tree, no diff can contain it, and settled law is by construction what was
  settled *before* the change under review. *Ledger:* this repo's own `/track-and-start` names
  worktrees after branches and `/cleanup` deletes them, so a directory-keyed history silently resets
  to empty — indistinguishable from "nothing recurred." Repo identity is stable across worktrees.
- **A decision demotes; it never deletes.** It can lower a finding's priority or mark it accepted, but
  it can never suppress a CRITICAL or a security finding — those still surface, annotated. The
  original controls all targeted the *quality* failure (a junk drawer of nits); this floor targets the
  *adversarial* one (an entry phrased as a plausible pattern that blinds a whole domain).
- **`project.yaml` `invariants` and `redLines` outrank the decisions file.** A recorded decision that
  appears to license crossing a documented red line does not settle the finding — it *is* one.
- **Suppression is observable.** When a decision causes a reviewer to withhold a finding, the reviewer
  emits it under `## Suppressed by decision`, and triage records it as `(withheld)`. A reviewer going
  silent was the only pipeline action that otherwise left no artifact — which made "the report got
  shorter" (the success metric) indistinguishable from "a reviewer went blind" (the worst failure).
- **Every entry is live; there is no `supersedes`.** Overturning a decision means editing the entry in
  place. History lives in git and the ledger, not in ambiguous live rows.
- **The over-escalation guard trips on absolute count, not just ratio:** warn if `needs-you >= 5`, or
  if (`needs-you / confirmed > 0.2` and `confirmed >= 10`). The harm — a list too long to read — is a
  count; a pure ratio cried wolf on tidy 3-finding reviews and slept through 40-finding ones.
- **Under-escalation gets an instrument too:** a bounded *Declined nominations* list records every
  `**Human Call**` waved through, because over-escalation is visible (an extra question) while
  under-escalation is not.
- **Triage is scoped to `/expert-review` only.** `/expert-pr-comments` also loads
  `expert-framework.md` but has no Triage Chief and no suppression recorder, so the framework tells
  reviewers there to state `**Human Call**` reasoning inline and never to silently withhold.
- **Additive fields are permitted** (North Star Nick's canonical severity **plus** a `Category` tag)
  under the rule now stated in [ADR-0006](0006-reviewer-output-format-carve-outs.md)'s amendment: an
  intact canonical block plus a named-consumer field is not a format carve-out.
- **The `Edit` red line's third target is `action-plan.md` itself, scoped to the ruling line.** Step 12
  puts each escalation to the human via `AskUserQuestion`, then records the answer by editing that
  item's `- **Ruling**:` line in `{REVIEW_DIR}/action-plan.md` — not `decisions.yaml` and not an ADR,
  since most rulings don't generalize into either. This target stays inside the control's spirit: it
  is `{REVIEW_DIR}` (a file the command already writes freely in Step 11), the write is scoped to a
  single line the human just answered, and the two writes that leave the working tree — `decisions.yaml`
  and an ADR — are unchanged. Consequences above now correctly names three targets, not two.
