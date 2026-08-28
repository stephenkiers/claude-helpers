# ADR-0009: Peer review and shared panel

**Status:** Accepted

## Context

The `/expert-review` command is designed to support an **author reviewing their own code** — it loads the full expert panel, runs the Triage Chief to sort findings into decision buckets, and writes review artifacts into the working tree. This is appropriate for code you own, where depth and completeness matter most.

But peer code review — reading a **coworker's PR** checked out in a shared worktree — has different constraints:
- The reviewer is collegial, not exhaustive; high bar for escalations, not comprehensive coverage.
- The author is present and responsive (in the same PR), so findings surface as questions for them, not solo decisions.
- The reviewed repo is not "yours" — it is the coworker's; a reviewer should not write artifacts into it.

The panel evaluation logic (the two-pass blind review, routing, amalgamation) remains unchanged and valuable for peer review too. But the surrounding orchestration differed enough to warrant a second command, and the opportunity to factor out the shared panel.

## Decision

**Decision 1: A second command, `/expert-review-coworker`, for peer PR review.**

Created alongside `/expert-review`, configured specifically for collegial peer review:
- Runs the same blind-first two-pass panel as `/expert-review` (Steps 4–10; see Decision 2 below).
- Deliberately **omits the Triage Chief**: peer review surfaces escalations as collegial questions for the author to rule on, inline in PR comments (via `prompts/pr-comment-guide.md`), rather than collecting them into a decision list and asking a single judgment call per item.
- The PR Comment Guide agent applies a high bar for escalation — only `**Human Call**` / `DRIFT` / `QUESTION` severities surface as actual questions, filtering out findings the author likely anticipated.
- Review artifacts live in `~/.claude/reviews/{owner}-{repo}/` (outside the reviewed repo), not in the working tree.
- Contrasts with `/expert-review` (author-centric, exhaustive, your own code, Triage Chief included, decision list output).

**Decision 2: Extract the shared panel into `prompts/expert-review-panel.md`.**

The blind-first two-pass panel (Summarizer → Router → Pass 1 → Contrarian Carl → Q&A → Pass 2 → Amalgamator, Steps 4–10 of the original pipeline) is consumed by both `/expert-review` and `/expert-review-coworker`. Extracted into a single shared-prompt file, `prompts/expert-review-panel.md`, loaded by both commands to avoid duplication and drift.

This supersedes the single-consumer design shape described in [ADR-0001](0001-progressive-disclosure.md) and [ADR-0002](0002-blind-first-two-pass-review.md); the panel logic is unchanged, the consumer count is now two.

**Decision 3: Write boundary — never write to a repo you don't own.**

A principle introduced by the peer-review command: **artifacts produced by a reviewer belong under `~/.claude/reviews/`, not in the repo being reviewed.** The coworker command never writes `github-cache`, checkpoints, or any review artifact into the reviewed repo; all writes live outside it, keyed by repository identity.

This principle generalizes across the repo: it is a guardrail against a reviewer injecting state into a project it does not own, and a reminder that review is an observer role, not an author role.

## Consequences

- **Good:** Peer review has a dedicated command tuned to collegial, high-bar escalation, separate from author review.
- **Good:** The panel's Two-Pass isolation and progressive-disclosure logic serve both use cases without reimplementation.
- **Good:** Review artifacts respect ownership boundaries — a coworker's repo stays clean.
- **Cost:** A second command means maintenance of two orchestrations. Guarded by shared-panel extraction — changes to the panel itself land in one place, and either command picking up the change.
- **Cost:** The PR Comment Guide is a new role with its own persona and thresholds. It runs post-panel, not integrated into the panel itself, adding a sequential step.
- **Design note:** The omission of Triage Chief from `/expert-review-coworker` is deliberate, not an oversight or a future plan. Triage sorts findings into buckets for the author to decide on in series; peer review surfaces escalations as collegial questions for the author to answer inline in the PR. Different output shapes for different contexts.

## Amendment — Draft review posting (ADR-0015)

The no-auto-post principle recorded above has one narrow exception, added by
[ADR-0015](0015-leave-pr-comments-draft-review.md): posting a PENDING (draft) review via the GitHub
API is permitted, because the human still gates submission. The draft is invisible to the PR author
until the reviewer clicks "Submit review" in GitHub's UI, so the human-in-the-loop gate survives at
the submit layer — only the copy-paste friction at the draft layer is removed. See ADR-0015 for the
full decision.
