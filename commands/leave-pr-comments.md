---
description: Post an expert-review's curated findings to a GitHub PR as a pending (draft) review with inline comments. Use when the user says "/leave-pr-comments", "post the review", "post these as draft comments", or "leave the PR comments" after running /expert-review-coworker-beta or /expert-review-coworker and completing the walk-through.
argument-hint: [<github-pr-url> | <review-dir-path>]
allowed-tools: Bash(gh api:*), Bash(gh pr view:*), Bash(gh auth status:*), Bash(cat:*), Bash(jq:*), Bash(ls:*), Bash(echo:*), Read, Glob, Grep, Write, AskUserQuestion
model: sonnet
---

# Leave PR Comments

Take the walk-through's `selected-comments.json` — the reviewer's curated **kept** set, produced at the
end of `/expert-review-coworker[-beta]` — and post it as a **single PENDING GitHub review** with inline
comments. Two guardrails, both load-bearing:

1. **Pending only — never auto-submit.** The review is posted in the draft/pending state. The user
   clicks "Submit review" in GitHub. This command never calls the submit event.
2. **Human-in-the-loop before posting.** The user confirms the comment set before anything reaches
   GitHub (the spirit of ADR-0009, as amended by ADR-0013).

Artifacts stay under `~/.claude/reviews/`; nothing is written into the reviewed repo. Only the
curated kept set is posted — if the walk-through wrote no `selected-comments.json` (nothing kept),
this command stops before touching GitHub.

**Arguments:** $ARGUMENTS (optional — a PR URL or a review-dir path; if omitted, falls back to
`$REVIEW_DIR` in the environment).

## Step 0: Locate the review directory

Resolve `$ARGUMENTS` in priority order:

1. **No arg + `REVIEW_DIR` in env** → use `$REVIEW_DIR`.
2. **Arg is a directory** containing `selected-comments.json` (or `pr-comment-guide.md`) → use it
   directly.
3. **Arg is a PR URL** (`https://github.com/{owner}/{repo}/pull/{N}`) → compute the reviews-dir key
   with a hyphen, not a slash, then pick the newest matching review directory by mtime:
   ```bash
   REPO_KEY=$(echo "${owner}/${repo}" | tr '/' '-')
   ls -dt "$HOME/.claude/reviews/${REPO_KEY}"/pr-${N}-* 2>/dev/null | head -1
   ```
4. **No arg and no `REVIEW_DIR`** → error and stop: "No review directory specified. Pass a PR URL or
   review-dir path, or run /expert-review-coworker[-beta] first."

Set `REVIEW_DIR`. Then **error and stop if it lacks `selected-comments.json`**:

> No `selected-comments.json` in ${REVIEW_DIR}. Complete the /expert-review-coworker[-beta]
> walk-through and keep at least one finding first.

This enforces the core invariant: only the curated kept set is posted.

## Step 1: Preflight

Read the two source artifacts:

```bash
cat "${REVIEW_DIR}/selected-comments.json"
cat "${REVIEW_DIR}/pr-context.md"
```

`selected-comments.json` holds the kept findings (`pr_url`, `head_sha`, and a `findings[]` array of
`path`/`start`/`end`/`severity`/`body`/`permalink`). `pr-context.md` carries the PR URL, repo,
number, and the reviewed `Head sha` (short).

Run a soft auth preflight — warn but do not stop if the token is missing scope; the eventual POST is
the hard gate (a 403/422 surfaces a clear message there):

```bash
gh auth status
```

Extract `PR_URL`, `OWNER`, `REPO`, and `N` (the PR number) from `pr-context.md`. If `pr-context.md`
is missing the URL, fall back to `selected-comments.json`'s `pr_url`.

## Step 2: Resolve the full SHA and check for staleness

Fetch the PR's current head SHA:

```bash
FULL_SHA="$(gh pr view "$PR_URL" --json headRefOid -q '.headRefOid')"
```

Read the short `Head sha` from `pr-context.md` and compare it against `FULL_SHA` by prefix — the
first 7 chars of `FULL_SHA` must equal the reviewed short SHA. If `pr-context.md` is missing or lacks a
`Head sha` line, fall back to the `head_sha` field in `selected-comments.json` (Step 1 already read
it) — the walk-through writes that field from the same reviewed SHA, so it is an equivalent source.

**If they differ → STOP. No override:**

> PR was updated since this review (reviewed ${SHORT_SHA}, now ${FULL_SHA:0:7}). Re-run
> /expert-review-coworker[-beta] to refresh the review, then re-run /leave-pr-comments.

Posting inline comments against a stale commit anchors them to lines that may no longer exist, so a
stale review is never partially posted.

## Step 3: Build the payload (Write tool — no shell interpolation)

Author `${REVIEW_DIR}/pending-review-payload.json` with the **Write tool**, not by interpolating
comment bodies through the shell (the CLAUDE.md escaping rule: never build a JSON object by
interpolating shell variables into a string literal — let `jq` or the Write tool own the escaping).
Shape:

```json
{
  "body": "<review summary markdown>\n\n*Review is pending — not yet submitted. Posted as draft for discussion.*",
  "comments": [
    { "path": "<path>", "line": <start>, "side": "RIGHT", "body": "<draft comment>" },
    ...
  ],
  "commit_id": "<FULL_SHA>"
}
```

- **Anchor each comment at the finding's `start` line.** Step 2 confirms the head commit is current,
  so the line exists in that commit's file; the hunk scan below confirms it is in the *diff* (a `+`
  or context line within a hunk), which is what GitHub needs to attach an inline comment. If `start`
  is not a `+` or context line in `${REVIEW_DIR}/full-diff.patch`, scan the `[start, end]` range for
  one that is; if none of the lines in that range appear in the diff, **drop that comment and warn**
  — do not half-post a finding whose anchor is gone.
- **Do not include an `event` field.** Omitting it is what creates a draft review; setting the
  event to the pending state 422s. The payload above has only `body`, `comments`, and `commit_id`.
- Bodies already have the outer markdown fence stripped (the walk-through does this when writing
  `selected-comments.json`); strip a stray outer fence only if one remains.
- **Review-summary body** — compose a brief summary from the kept findings (one line each is fine).
  Optionally read `${REVIEW_DIR}/pr-comment-guide.md`'s "Reviewer's Note — Items Needing the
  Author's Judgment" bullets and append them to `body` as context. These are collegial questions
  without file:line anchors — review-summary content, not inline comments — so appending them does
  not violate "only the curated set is posted."

## Step 4: Check for an existing pending review

GitHub allows one pending review per user per PR. Check for one:

```bash
gh api repos/${OWNER}/${REPO}/pulls/${N}/reviews \
  --jq '[.[] | select(.state=="PENDING") | .id]'
```

Record any returned IDs in `EXISTING_REVIEW_IDS`. Do **not** delete yet — the delete is deferred to
Step 5 so it only happens as part of the confirmed post. If the list is non-empty, note the id(s) for
the confirmation prompt in the next step.

## Step 5: Confirm before posting

Present the comment set to the user — `file:line` plus a one-line summary for each finding — then
`AskUserQuestion`. The prompt reflects whether Step 4 found an existing draft:

- **No existing pending review:** "Post these N comments as a pending draft review?"
  - **Post all (Recommended)**
  - **Abort**

- **Existing pending review found (id …):** "Post these N comments as a pending draft review? This
  will delete the existing pending review (id …) first."
  - **Post all (Recommended)** — replaces the existing draft
  - **Abort** — leaves the existing pending review untouched

Do not post — and do not delete anything — until the user picks "Post all."

**On "Post all" with an existing review:** delete each id in `EXISTING_REVIEW_IDS` *now*, then
continue to Step 6:
```bash
gh api -X DELETE repos/${OWNER}/${REPO}/pulls/${N}/reviews/${ID}
```

The delete is placed here — after confirmation, immediately before the post — so choosing "Abort"
never destroys the existing draft. GitHub permits only one pending review per user per PR, so the old
draft must be removed before the new one can be posted; this means a 422 on Step 6 still leaves the
old draft gone (the one residual window, inherent to GitHub's one-review constraint). Step 6's 422
handler calls this out when it happens.

## Step 6: Post the pending review

```bash
gh api repos/${OWNER}/${REPO}/pulls/${N}/reviews \
  --input "${REVIEW_DIR}/pending-review-payload.json"
```

Capture `id` and `html_url` from the response.

**On 422** → report which comment/line failed and **leave the review un-posted** (do not retry a
partial post). If Step 5 deleted an existing pending review to make room, say so — that old draft is
gone and, if the user wants it back, must be recreated manually (GitHub's one-pending-review-per-user
rule forced the delete-before-post order). Tell the user to check the anchor line — it likely falls
outside the diff for the current commit, which the staleness check in Step 2 should have caught but a
multi-line hunk edge can still slip through.

## Step 7: Write the receipt

Write `${REVIEW_DIR}/posted-review-receipt.md` (distinct filename from the walk-through's
`posted-comments.md` copy-paste artifact, so neither clobbers the other). Contents:

- Header: **Review ID**, **State: PENDING (draft — not yet submitted)**, PR URL, commit SHA.
- A numbered list of the posted findings, each with `file:line` and its permalink.
- A one-line next step: "To submit, open the PR files view and click 'Submit review'. To discard,
  delete the pending review."

## Step 8: Report

Print the review `html_url`, the state (PENDING), and the inline-comment count. Remind the user it
is a draft: submit it from the PR files view, or discard it by deleting the pending review. Nothing
else posts or submits automatically.
