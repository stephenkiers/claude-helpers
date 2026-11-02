# Issue Comments Helper Reference

This document describes the shared helper for fetching and filtering GitHub issue comments, and where each command uses it.

## Invocation

Every consumer that reads issue comments uses this explicit-cwd form:

```bash
CALLER_DIR=$(pwd)
source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh"
COMMENTS_JSON=$(cd "$CLAUDE_HELPERS_DIR" && PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli issue-comments fetch --issue "$ISSUE_REF" --cwd "$CALLER_DIR" [--trusted-only] [--flow-dir "$FLOW_DIR"] [--format json|markdown])
printf '%s' "$COMMENTS_JSON" | jq '...'
```

**Key points:**
- `CALLER_DIR=$(pwd)` — the directory to resolve the repo against (the worktree being planned or implemented)
- `--cwd "$CALLER_DIR"` is **mandatory**: it is what the provider uses to pin the repo identity
- `--trusted-only` — passed by implement, fix, ship, review step, and the pre-merge check; omitted by planning
- `--flow-dir "$FLOW_DIR"` — passed inside a flow; source of the plan fetch time and acknowledged comment ids
- Never use `echo` when piping JSON; use `printf '%s'` to avoid escape-sequence corruption
- Exit codes:
  - `0`: JSON emitted, with `comments_status` one of `ok|unavailable|unsupported`
  - `2`: usage or validation error
  - `3`: the issue itself could not be fetched

## Receipt and Visibility

- `comments_status` and the counts line appear in every step's receipt
- When status is `unavailable`, show `COMMENTS UNAVAILABLE` in the receipt and any Ask summary

## "New Comment Since Plan" Ask Procedure

After the plan step, post-plan consumers (implement, fix, review, ship, pre-merge) raise each unacknowledged trusted comment posted after the plan fetch time, once per flow step, through the Ask block. The procedure:

1. Fetch comments with `--flow-dir "$FLOW_DIR"` to use the plan fetch time as the lower bound
2. For each unacknowledged trusted comment (one not in `plan-comment-ids.json` and not answered Continue in a prior step):
   - Write an Ask-block question with `comment_id: "<GitHub comment id>"` (allowed by the `flow-reference.md` schema amendment)
   - Show the fenced comment and the body section it touches in `summary`/`brief`
   - Offer two options:
     - **Continue — the plan stands** (the exact label `CONTINUE_LABEL` = `"Continue — the plan stands"`): the comment is now acknowledged
     - **Stop for re-plan**: the step hard-stops with `Decision: FAILED`, `Reason: stopped on new issue comment <id>`; the comment stays unacknowledged
   - Free-text answers are treated as **Stop for re-plan**
3. Record the answer in the ledger (background only); only Continue answers acknowledge the comment
4. Outside a flow, do not ask; show the new comments and counts in the step's output

## Consumers

| Consumer | Role | Where it sits |
|---|---|---|
| `/expert-plan` Step 1 (in-flow; `expert-plan.md:310`) | reasoning reader (planning) | Helper, all tiers, no `--trusted-only`; scouts filter long unverified threads via batched `comment-relevance-scout.md`; output goes into `context.md` `## Issue comments`; conflicts become open questions; body is the plan of record; records the fetch time and original ticket in `plan-issue-snapshot.json` and reads them back on a redo |
| `/implement-with-haiku` (implement, fix; in-flow) | reasoning reader (code-writing); "new comment since plan" Ask | Helper `--trusted-only` + `--flow-dir`; coordinator only; body from the helper's output; new comments raise an Ask-block question once per flow step |
| `/expert-review` (in-flow; post-plan) | "new comment since plan" Ask | Helper `--trusted-only` + `--flow-dir`; raises an Ask-block question for each unacknowledged trusted comment posted after plan |
| `/shipit` (in-flow; post-plan) | "new comment since plan" Ask + reasoning reader ("Why this PR exists") | Helper `--trusted-only` + `--flow-dir` for Ask questions; body-only fetch for PR body synthesis |
| `/expert-is-it-done` | reasoning reader + human report | Helper `--trusted-only` to agents; unverified shown to the human as counts and authors |
| `/expert-flow` orchestrator (pre-merge check) | metadata reader + pre-merge check | Metadata reads stay as they are; pre-merge helper check with `--trusted-only` + `--flow-dir`; relay strips `comment_id` |
| `/expert-plan-deprecated`, `/expert-plan-deprecated-v2` | (none) | Deprecated planners do not use the issue-comments helper; not in-flow |
| `/track-and-start` `:463` (worktree-only), `:1230` (pivot body fetch), `:981`, `:1024`, `:1258` | mechanical reader (branch name, verbatim archival, state and URL validation) | Body/metadata only, deliberately. These reads never feed reasoning, and archiving must copy the body verbatim |
| `/track-and-start` Step 4d (`:1060-1120`) | plan-comment **writer** (`## Superseded Plan`) | Unchanged; keeps writing. The marker literal must equal `SUPERSEDED_PLAN_MARKER` (test-pinned). Its comments set the watermark outside a flow and are dropped read-side; inside a flow, post-plan steps use the plan fetch time instead |
| `/track` `:24-25`, `:170-177` | plan-comment **writer** (`## Implementation Plan`) | Unchanged. The literal must equal `IMPLEMENTATION_PLAN_MARKER` (test-pinned); only the newest trusted post-lower-bound one is read, and it is never raised as "new since plan" |
| `/track` `:101-146`, `:210` | mechanical reader (cache, duplicate detection) | Body/metadata only; cache stays body-only |
| `/cleanup` `:611` | retro summary input, 8k cap | Deliberately body-only (its own comment: "comment threads can be huge"); not a flow input; listed so the exclusion is explicit |
| `.claude/github-cache.json` / `scripts/workflow/cache.py` | cache | Unchanged, body-only; no schema bump |
| Flow ledger `human.jsonl` (`scripts/flow-ledger.py`) | human-input record | Comments are never ledger records. Only the human's answer to a "new comment since plan" question is written, as an `answer` (background only) |
| ADR-0008 standing exception | governance | Amended in this ticket to cover only general-purpose steps (plan, implement, fix, ship) in flows; scouts present for planning |
| ADR-0023 2026-10-30 amendment | governance | Specifies which steps raise the "new comment since plan" Ask and under what conditions |
