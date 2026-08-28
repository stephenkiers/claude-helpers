# ADR-0015: Leave PR comments as pending draft review

**Status:** Accepted

## Context

[ADR-0009](0009-peer-review-and-shared-panel.md) established the no-auto-post / human-in-the-loop
principle for coworker review: the panel produces findings, but a human decides which surface as PR
comments and pastes them in. That is friction. After `/expert-review-coworker[-beta]` runs and the
reviewer walks the `pr-comment-guide.md` to curate a kept set, the reviewer is left holding
`selected-comments.json` and must copy-paste each kept comment into GitHub by hand, one thread at a
time. N threads become N paste operations against N different locations in the diff, and any edit
before submitting has to happen in GitHub's per-comment UI.

## Decision

**Add `/leave-pr-comments`, which posts the reviewer's curated findings as a single PENDING (draft)
review via the GitHub API.** It never sets `event` to a submit action (`APPROVE` / `REQUEST_CHANGES`
/ `COMMENT`), so the review is invisible to the PR author until the reviewer clicks "Submit review"
in GitHub's UI. The human remains the gate; the command removes only the copy-paste friction and
coalesces N threads into one draft review that can be edited in a single place before submitting.

It consumes `selected-comments.json` — the walk-through's kept set — not the full guide, so what
gets posted is exactly what the reviewer chose to keep, nothing more. Artifacts (the request payload
and the API receipt) stay under `~/.claude/reviews/{owner}-{repo}/`; nothing is written into the
reviewed repo, honoring ADR-0009's Decision 3 write boundary.

**Stale-commit refusal.** `/leave-pr-comments` refuses to post if the PR's HEAD has moved since the
review was run. Each comment in `selected-comments.json` is anchored to a commit SHA and (for inline
comments) a file path and line; if the branch tip has advanced, those anchors may no longer line up
with the diff, and GitHub would reject misanchored inline comments or silently drop them onto the
wrong location. The command surfaces the mismatch and halts rather than posting a misanchored draft.

## Consequences

- **Good:** Less manual paste — N threads become one API call, submitted as one draft.
- **Good:** A single draft is easier to edit before submitting than N independent comments pasted
  into GitHub's UI.
- **Good:** The human gate is preserved — nothing reaches the author until the reviewer clicks
  "Submit review". The command crosses ADR-0009's no-auto-post line only at the *draft* layer.
- **Good:** The posted set is exactly the curated set — `selected-comments.json`, not the full guide
  — so nothing the reviewer discarded can leak through.
- **Cost:** This is the first command that writes review state to GitHub on a coworker's PR. The
  risk is mitigated on three axes: pending-only (the author never sees it until the reviewer
  submits), explicit user confirmation before the API call, and stale-commit refusal (misanchored
  comments are never posted).

## Amendment — Draft review posting (ADR-0009)

This ADR amends [ADR-0009](0009-peer-review-and-shared-panel.md)'s no-auto-post line. ADR-0009 held
that the reviewer copy-pastes comments by hand and nothing is posted automatically. ADR-0015
permits a PENDING (draft) review to be posted via the GitHub API, because the human still gates
submission — the draft layer is invisible to the PR author until the reviewer clicks "Submit
review". The no-auto-post principle survives at the submit layer; only the copy-paste friction at
the draft layer is removed.
