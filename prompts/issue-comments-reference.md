# Issue Comments Helper Reference

This document describes the shared helper for fetching and filtering GitHub issue comments, and where each command uses it. Governance is in ADR-0023 (2026-10-30 amendment) and ADR-0008 (2026-10-30 amendment).

## Invocation

Every consumer that reads issue comments uses this explicit-cwd form:

```bash
CALLER_DIR=$(pwd)
source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh"
COMMENTS_JSON=$(cd "$CLAUDE_HELPERS_DIR" && PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli issue-comments fetch --issue "$ISSUE_REF" --cwd "$CALLER_DIR" [--trusted-only] [--flow-dir "$FLOW_DIR"] [--format json|markdown])
printf '%s' "$COMMENTS_JSON" | jq '...'
```

**Key points:**
- `ISSUE_REF` is the issue number (or issue URL), named the same in every command.
- `CALLER_DIR=$(pwd)` — the directory to resolve the repo against (the worktree being planned or implemented)
- `--cwd "$CALLER_DIR"` is **mandatory**: it is what the provider uses to pin the repo identity
- `--trusted-only` — passed by implement, fix, ship, review step, pre-merge check and `/expert-is-it-done`; omitted by planning
- `--flow-dir "$FLOW_DIR"` — passed inside a flow; source of the plan fetch time (`plan-issue-snapshot.json`) and of the acknowledged comment ids (see below)
- Never use `echo` when piping JSON; use `printf '%s'` to avoid escape-sequence corruption
- Exit codes:
  - `0`: JSON emitted, with `comments_status` one of `ok|unavailable|unsupported`
  - `2`: usage or validation error
  - `3`: the issue itself could not be fetched

## Receipt and Visibility

- `comments_status` and the counts line appear in every step's receipt
- When status is `unavailable`, show `COMMENTS UNAVAILABLE` in the receipt and any Ask summary

## Acknowledgement (no separate store)

A comment is **acknowledged** when the flow has a question for it whose answer was `CONTINUE_LABEL`. The helper resolves this from existing artifacts only:

- `${FLOW_DIR}/questions/<STEP>-<SEQ>.json` — the question entry with `comment_id` gives the question `id` → comment id mapping
- `${FLOW_DIR}/answers/<STEP>-<SEQ>.json` — `answers[<question id>] == "Continue — the plan stands"` acknowledges that comment

There is no `answers.jsonl` and no dedicated acknowledgement file. Comments are never ledger records; the only ledger entry is the human's answer to a "new comment since plan" question, written by the orchestrator as a background `answer`. Comments listed in `plan-comment-ids.json` (shown to the planner) are not raised either.

Because acknowledgement is read from the question and answer files, a comment answered Continue is raised once per flow, including across Retry, `--resume`, and `--fleet` runs. A Stop answer leaves the comment unacknowledged, so the next attempt raises it again.

## "New Comment Since Plan" Ask Procedure

Post-plan consumers (implement, fix, review, ship, and the orchestrator's pre-merge check) raise new comments one at a time through the Ask block:

1. Fetch with `--trusted-only --flow-dir "$FLOW_DIR"`. The helper marks each comment `acknowledged`.
2. Take the first trusted comment with `acknowledged == false` and `kind != "implementation_plan"`. If there is none, the step continues. Otherwise, do steps 3–6 for that one comment and stop.
3. Pick `SEQ` and write the question file with `Write` at `${FLOW_DIR}/questions/<STEP_NAME>-<SEQ>.json` (Ask block of `flow-reference.md`, steps 1–2). The question entry carries `comment_id: "<GitHub comment id>"`, and the file's `summary`/`brief` show the comment fenced, with its author, `created_at`, and the body section it touches:

   ```json
   {
     "step": "<STEP_NAME>",
     "command": "<command>",
     "context_path": null,
     "attempt": <k>,
     "summary": "New comment on the issue since the plan was written. Fenced comment: ... Body section it touches: ...",
     "questions": [
       {
         "id": "q1",
         "brief": null,
         "comment_id": "<GitHub comment id>",
         "question": "A new comment on the issue arrived after the plan. Does the plan stand?",
         "header": "New comment",
         "options": [
           {"label": "Continue — the plan stands", "description": "The comment does not change the plan. Proceed."},
           {"label": "Stop for re-plan", "description": "Stop this run so the comment can be folded into a re-plan."}
         ],
         "multiSelect": false
       }
     ]
   }
   ```

4. Record the awaiting marker in the step receipt (Ask block, step 3), close telemetry as interrupted (step 4), and END THE TURN (step 5).
5. On resume (`answers/<STEP>-<SEQ>.json` written), re-run this procedure from step 1. The answered comment is now acknowledged, so the next comment is raised or the step continues.
6. A `Stop for re-plan` answer, or any free-text answer, is a hard stop: the step writes `Decision: FAILED` with `Reason: stopped on new issue comment <comment id>`. The comment stays unacknowledged.

The option labels are exact: `CONTINUE_LABEL` = `"Continue — the plan stands"` (em dash). A `multiSelect` answer or a label other than these two is treated as Stop.

**Under `--auto-merge`** the pre-merge check still raises this Ask, and a Stop halts the merge. **Under `--fleet`** the Ask parks as `fleet-ask.json` instead of being asked; the fleet resumes the ticket with the answer, so each comment is raised once (ack-from-artifacts). A repeated ask under `--fleet` parks again only when the previous answer was not Continue.

**Outside a flow** (`FLOW_DIR` empty), do not ask. Show the new comments and counts in the step's output.

## Relevance Scouts (plan step only)

Planning reads unverified comments. When the fetch output has `relevance_filter.required == true` (more than `RELEVANCE_FILTER_THRESHOLD` = 5 unverified comments survive filtering), the plan step filters them with scouts. Otherwise the planner receives the unverified comments unfiltered.

1. The fetch output carries `relevance_filter.batches`: a list of id lists. The plan step writes one batch file per list under `comment-scout/batches/` (`{schema_version: 1, issue_title, issue_body, comments: [{id, author_login, created_at, body}]}`), using `jq --argjson`.
2. Dispatch one `expert-scout` Task per batch file, at most `MAX_SCOUTS` = 6 in parallel, with the text of `prompts/comment-relevance-scout.md` and the batch path as the prompt. The reply is the verdict JSON only.
3. Write each reply verbatim to `comment-scout/verdicts/verdict-<N>.json`. Invalid JSON is written as-is.
4. Run `issue-comments finalize --fetched "$SESSION_DIR/issue-comments.json" --verdicts "$SESSION_DIR/comment-scout/verdicts" --format json`. Use its stdout as the comments JSON. If it exits non-zero, the plan step sets comments to unavailable and sends no unverified text to the planner.

**Fail closed.** An unverified comment is kept only by an explicit, valid `include` verdict for its id. A missing verdict file, invalid JSON, a missing id, an unknown verdict value, or an unknown reason is a drop. Trusted comments are never subject to verdicts. Finalize re-applies the budgets after dropping. Re-running `finalize` on the same inputs gives the same output.

The planner's markdown is rendered from the finalized result (`finalize --format markdown`) when the filter ran. When it did not run, the markdown comes from a separate `fetch --format markdown` call (the CLI has no render-from-file subcommand).

## Consumers

| Consumer | Role | Where it sits |
|---|---|---|
| `/expert-plan` Step 1 (in-flow; `expert-plan.md`) | reasoning reader (planning) | Helper, all tiers, no `--trusted-only`; scouts filter unverified comments only when `relevance_filter.required` (fail closed); output goes into `context.md` `## Issue comments`; the body is the plan of record; records the fetch time and original ticket in `plan-issue-snapshot.json` and `plan-comment-ids.json` |
| `/implement-with-haiku` (implement, fix; in-flow) | reasoning reader (code-writing); "new comment since plan" Ask | Helper `--trusted-only` + `--flow-dir`; coordinator only; body from the helper's output; the Ask procedure above, one comment per turn |
| `/expert-review` (in-flow; post-plan) | "new comment since plan" Ask | Helper `--trusted-only` + `--flow-dir`; the Ask procedure above with `STEP_NAME` = `review` |
| `/shipit` (in-flow; post-plan) | "new comment since plan" Ask + reasoning reader ("Why this PR exists") | Helper `--trusted-only` (`--flow-dir` for the Ask); body-only use for PR body synthesis; the Ask runs before the PR is created |
| `/expert-is-it-done` | reasoning reader + human report | Helper `--trusted-only` to agents; unverified shown to the human as counts and authors |
| `/expert-flow` orchestrator (pre-merge check) | metadata reader + pre-merge check | Metadata reads stay as they are; pre-merge helper check with `--trusted-only` + `--flow-dir`; the Ask procedure above; the relay strips `comment_id`; a Stop halts the merge even under `--auto-merge` |
| `/expert-plan-deprecated`, `/expert-plan-deprecated-v2` | (none) | Deprecated planners do not read issue comments at all (title, body and labels only); not in-flow |
| `/track-and-start` `:463` (worktree-only), `:1230` (pivot body fetch), `:981`, `:1024`, `:1258` | mechanical reader (branch name, verbatim archival, state and URL validation) | Body/metadata only, deliberately. These reads never feed reasoning, and archiving must copy the body verbatim |
| `/track-and-start` Step 4d (`:1060-1120`) | plan-comment **writer** (`## Superseded Plan`) | Unchanged; keeps writing. The marker literal must equal `SUPERSEDED_PLAN_MARKER` (test-pinned). Its comments set the watermark outside a flow and are dropped read-side; inside a flow, post-plan steps use the plan fetch time instead |
| `/track` `:24-25`, `:170-177` | plan-comment **writer** (`## Implementation Plan`) | Unchanged. The literal must equal `IMPLEMENTATION_PLAN_MARKER` (test-pinned); only the newest trusted post-lower-bound one is read, and it is never raised as "new since plan" |
| `/track` `:101-146`, `:210` | mechanical reader (cache, duplicate detection) | Body/metadata only; cache stays body-only |
| `/cleanup` `:611` | retro summary input, 8k cap | Deliberately body-only (its own comment: "comment threads can be huge"); not a flow input; listed so the exclusion is explicit |
| `.claude/github-cache.json` / `scripts/workflow/cache.py` | cache | Unchanged, body-only; no schema bump |
| Flow ledger `human.jsonl` (`scripts/flow-ledger.py`) | human-input record | Comments are never ledger records. Only the human's answer to a "new comment since plan" question is written, as an `answer` (background only) |
| ADR-0008 standing exception | governance | Amended 2026-10-30 to cover the general-purpose steps that read comments in flows (plan, implement, fix, ship) |
| ADR-0023 2026-10-30 amendment | governance | Specifies which steps raise the "new comment since plan" Ask and under what conditions |
