---
name: track-and-start
description: Use when user says "/track-and-start" to create a GitHub issue (or local plan), branch, and worktree in one step. Requires plan mode.
---

# Track and Start - Combined Issue, Branch, and Worktree Workflow

Creates a GitHub issue (or local plan file) from the plan, generates a branch name, and sets up a worktree in one step. When the project root has a `plans/` directory and an array-format `issues.json`, uses local plan tracking instead of GitHub issues.

## Requirements

- **Must be in plan mode** with a valid plan file
- Current directory must be within a git repository with a GitHub remote
- If called with a tracker ticket ID argument (`[A-Z]+-\d+`), a GitHub remote is **not** required.

## Behavior

1. Validate plan mode and plan file exist
2. Read original plan content (preserve for cache — no modifications)
3. Check args for tracker ticket ID → if argument matches [A-Z]+-\d+, enter Tracker Ticket mode (highest priority; skips steps 4–7)
4. Detect project from git remote and worktree layout
5. Check for local plan mode
6. Pivot detection
7. Duplicate detection
8. Create GitHub issue with original plan as body
9. Generate branch name from issue type and title
10. Create worktree in correct location
11. Output handoff commands for user to start implementation

**Note:** This skill does NOT call ExitPlanMode or continue implementation, **except** in the pivot flow where the user is already in the correct worktree — in that case, ExitPlanMode is called so the user can approve and begin implementing immediately.

## Telemetry: mark command start

Telemetry is local, observational, and best-effort — it must never block or fail
`/track-and-start`. Every call below is non-fatal (see docs/metrics.md's telemetry call-site conventions for why `*-begin` uses `|| true` for non-fatal best-effort):

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" command-begin --command track-and-start 2>/dev/null || true
```

Correlation between stages and commands is automatic via a session-scoped state file keyed on the `CLAUDE_CODE_SESSION_ID` environment variable (which persists across separate Bash tool calls). Shell variables set in one Bash tool call do **not** survive into a later, separate Bash tool call, which is why this doc no longer threads `TELEMETRY_CMD_ID`/`TELEMETRY_STAGE_ID` through shell variables across call boundaries — across whichever mode (GitHub, Local Plan, or Tracker Ticket) this run takes.

## Project Detection

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage detect-project 2>/dev/null || true
```

Run the **Project Detection** and **Graft Detection** blocks from
`~/.claude/prompts/worktree-reference.md` (read that file for the bash). This sets `REPO`,
`MAIN_WORKTREE`, `WORKTREE_PARENT`, `CACHE_FILE`, `ASSIGNEE`, `PROJECT_ROOT`, `USE_GRAFT`,
and `GRAFT_REPO_NAME`.

**If not in a git repo or no GitHub remote:** Error with message about needing to be in a git repository with a GitHub remote.

**Exception:** If Tracker Ticket mode was activated in step 3, a missing GitHub remote is not an error — `REPO` will be empty and that is expected. Steps that follow must not call `gh` commands in this mode.

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage detect-project --outcome success 2>/dev/null || true
```

## Local Plan Mode

When the project root has both a `plans/` directory and an array-format `issues.json`, skip GitHub issue creation and use local plan tracking instead. This replaces steps 5-7 (pivot detection, duplicate detection, issue creation) with a local workflow.

### Detection

Run the **Local Plan Mode Detection** block from `~/.claude/prompts/worktree-reference.md`.
If `LOCAL_MODE` is false, fall through to the normal GitHub flow ([Pivot Detection](#pivot-detection) → [Duplicate Detection](#duplicate-detection) → [Creating the Issue](#creating-the-issue)).

### Local Duplicate Detection

Before generating an ID, scan `issues.json` for existing entries with overlapping titles (same semantic comparison as [Duplicate Detection](#duplicate-detection)). If a match is found with status `"todo"` or `"planned"`, present via `AskUserQuestion`:

| Option | Description |
|--------|-------------|
| **Start this entry** | Use the existing entry's ID, update its status to `"in_progress"`, save the plan file |
| **Create new entry** | Generate a new ID, add a new entry to issues.json |

### Local ID Generation

```bash
# Next available ID from the array
NEXT_ID=$(jq '[.[].id] | max + 1' "$PROJECT_ISSUES")
```

### Save Plan File

```bash
mkdir -p "$PLANS_DIR"
PLAN_FILE="${PLANS_DIR}/${ISSUE_NUM}-${SLUG}.md"
cat > "$PLAN_FILE" <<'EOF'
<original plan content>
EOF
echo "Plan saved to ${PLAN_FILE}"
```

### Update issues.json

**New entry** (no duplicate match):

```bash
jq --argjson id "$ISSUE_NUM" \
   --arg title "$TITLE" \
   --arg slug "$SLUG" \
   --arg plan "plans/${ISSUE_NUM}-${SLUG}.md" \
  '. += [{"id": $id, "title": $title, "status": "in_progress", "plan": $plan}]' \
  "$PROJECT_ISSUES" > "${PROJECT_ISSUES}.tmp" && mv "${PROJECT_ISSUES}.tmp" "$PROJECT_ISSUES"
echo "Updated issues.json: \"$TITLE\" → in_progress"
```

**Existing entry** (user chose "Start this entry"):

```bash
jq --argjson idx "$MATCH_IDX" \
   --arg plan "plans/${ISSUE_NUM}-${SLUG}.md" \
  '.[$idx].status = "in_progress" | .[$idx].plan = $plan' \
  "$PROJECT_ISSUES" > "${PROJECT_ISSUES}.tmp" && mv "${PROJECT_ISSUES}.tmp" "$PROJECT_ISSUES"
echo "Updated issues.json: \"$MATCHED_TITLE\" → in_progress"
```

### Branch and Worktree (Local Mode)

Use the local ID as the issue number for branch naming, then continue to [Branch Naming](#branch-naming) and [Creating the Worktree](#creating-the-worktree) as normal.

```bash
ISSUE_NUM=$NEXT_ID  # or existing entry's id if matched
```

### Worktree Cache (Local Mode)

Write `.claude/github-cache.json` in the new worktree with local plan data:

```bash
mkdir -p "${WORKTREE_PATH}/.claude"
jq -n --arg branch "${BRANCH}" \
      --argjson id "${ISSUE_NUM}" \
      --arg title "${ISSUE_TITLE}" \
      --arg plan "${PLAN_FILE}" \
      '{branch: $branch, localPlan: {id: $id, title: $title, plan: $plan, status: "in_progress"}}' \
      > "${WORKTREE_PATH}/.claude/github-cache.json"
```

### Handoff (Local Mode)

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome success 2>/dev/null || true
```

```
## Ready to implement!

**Plan:** `<plan-file-path>`
**Branch:** `<branch-name>`
**Worktree:** `<worktree-path>`

### Start implementation:

cd <worktree-path> && claude "/implement-with-haiku"
```

**Do NOT** call ExitPlanMode, continue implementation, or create a GitHub issue.

---

## Tracker Ticket Mode

Activates when `/track-and-start` is called with a ticket ID argument matching `[A-Z]+-\d+` (e.g., `PPS-166`). It looks up the ticket via MCP, uses the tracker's branch name, creates a worktree named from the lowercase ticket ID, writes the standard `github-cache.json` shape, and does not require a GitHub remote. Steps 1 and 2 (validate plan mode, read original plan) still run first — the plan content becomes the cache `body`. This mode is self-contained (like Local Plan Mode): its own detection, resolution, cache write, and handoff, merging back into the shared worktree creation block. Graft is explicitly skipped (the branch name comes from the tracker and must not be renamed).

#### Detection

```bash
TICKET_ID=""
if echo "${1:-}" | grep -qE '^[A-Z]+-[0-9]+$'; then
  TICKET_ID="${1}"
fi
```

If `TICKET_ID` is empty, fall through to step 4 (Project Detection) as normal.

#### Tracker Resolution

Try Linear first; if not found, try Jira.

**Linear:**
Call `mcp__linearv3__get_issue` with the ticket ID (e.g., `PPS-166`).

On success, extract:
- `BRANCH` ← `gitBranchName` field (see fallback below)
- `TICKET_URL` ← `url` field
- `TICKET_TITLE` ← `title` field
- `TICKET_BODY` ← `description` field (used only if needed; plan content is the primary body)

**Jira fallback:**
If Linear returns an error or "not found", call `mcp__atlassian__getJiraIssue` with `issueIdOrKey: "$TICKET_ID"`.

On success, extract:
- `TICKET_TITLE` ← `fields.summary`
- `TICKET_URL` ← constructed from base URL + ticket ID (e.g., `https://<workspace>.atlassian.net/browse/$TICKET_ID`)
- No native `gitBranchName` in Jira — always use slug fallback (see below)

**If both fail:** Error: `"Ticket $TICKET_ID not found in Linear or Jira. Check the ID and try again."`

#### Branch Name

Linear provides `gitBranchName` natively. Use it directly if non-empty:

```bash
BRANCH="${LINEAR_GIT_BRANCH_NAME}"
```

**Fallback** (Linear with empty `gitBranchName`, or Jira):
```bash
TICKET_ID_LOWER=$(echo "$TICKET_ID" | tr '[:upper:]' '[:lower:]')
SLUG=$(echo "$TICKET_TITLE" | \
  tr '[:upper:]' '[:lower:]' | \
  sed 's/[^a-z0-9]/-/g' | \
  sed 's/--*/-/g' | \
  sed 's/^-//' | \
  sed 's/-$//' | \
  cut -c1-40)
BRANCH="${TICKET_ID_LOWER}-${SLUG}"
```

#### Base Branch

Ask the user what branch to base the worktree on. Detect the default:

```bash
DEFAULT_BASE=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || echo "main")
```

Present via `AskUserQuestion`:
> What branch should this worktree be based on?
> Default: `<DEFAULT_BASE>` — press Enter to accept, or type a branch name (e.g., `pps-165-some-feature` to stack on a predecessor).

Set `BASE_BRANCH` to the user's answer, defaulting to `$DEFAULT_BASE` if blank.

#### Worktree Dir

Named from the lowercase ticket ID only — not the full branch slug:

```bash
WORKTREE_DIR=$(echo "$TICKET_ID" | tr '[:upper:]' '[:lower:]')
# e.g., "pps-166"
```

#### Worktree Creation

Run Project Detection and Graft Detection from `~/.claude/prompts/worktree-reference.md` to get `MAIN_WORKTREE` and `WORKTREE_PARENT`. Then create the worktree directly — **Graft is not used in Tracker Ticket mode** (the branch name comes from the tracker and must not be renamed):

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage create-worktree 2>/dev/null || true

cd "$MAIN_WORKTREE"
WORKTREE_PATH="${WORKTREE_PARENT}/${WORKTREE_DIR}"
git worktree add "$WORKTREE_PATH" -b "${BRANCH}" "${BASE_BRANCH}"
```

#### Cache Write

Write `.claude/github-cache.json` in the new worktree. `issue.number` is the ticket ID string (e.g., `"PPS-166"`). Downstream commands that use `issue.number` for GitHub issue closing (`Closes #N`) will produce `Closes #PPS-166` in PR bodies — GitHub will not recognize this as a closing reference, which is expected and acceptable (there is no GitHub issue to auto-close in this mode).

```bash
mkdir -p "${WORKTREE_PATH}/.claude"
jq -n \
  --arg branch    "${BRANCH}" \
  --arg number    "${TICKET_ID}" \
  --arg url       "${TICKET_URL}" \
  --arg title     "${TICKET_TITLE}" \
  --arg body      "${PLAN_CONTENT}" \
  '{branch: $branch, issue: {number: $number, url: $url, title: $title, body: $body, state: "open"}}' \
  > "${WORKTREE_PATH}/.claude/github-cache.json"
```

`PLAN_CONTENT` is the original plan content read in step 2 — same as GitHub mode.

#### Handoff Output

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome success 2>/dev/null || true
```

Same format as today:

```
## Ready to implement!

**Ticket:** <ticket-url>
**Branch:** `<branch-name>`
**Worktree:** `<worktree-path>`

### Start implementation:

cd <worktree-path> && claude "/implement-with-haiku"
```

**Do NOT** call `ExitPlanMode`, create a GitHub issue, or call any `gh` command.

---

## Pivot Detection

When `/track-and-start` is called from a worktree that's already linked to an issue, and the new plan overlaps with that issue, offer to **pivot** — replace the issue's scope with the new plan instead of creating a new issue and worktree.

### Step 4a: Detect Current Worktree's Linked Issue

First, run the **In-Worktree Check** from `~/.claude/prompts/worktree-reference.md`.

**If in main worktree** (`IN_WORKTREE=false`): Skip pivot detection entirely, fall through to Step 5 (Duplicate Detection).

If in a non-main worktree, look for a linked issue:

1. **Primary**: Read `.claude/github-cache.json` for `issue.number`, `issue.title`, `issue.body`, `issue.state`
2. **Fallback**: Parse issue number from branch name (`git branch --show-current`), then look up in `$CACHE_FILE` or via `gh issue view`

**Skip pivot if:**
- No linked issue found
- Linked issue is closed (`issue.state` is not `"open"`)

### Step 4b: Compare New Plan Against Existing Issue

Use the same semantic comparison as [Duplicate Detection](#duplicate-detection), but against the single linked issue only:

- **Title similarity**: Keywords in common, same feature area, same component
- **Scope overlap**: The plan addresses something the existing issue already covers (fully or partially)
- **Subset/superset**: The plan is a narrower or broader version of the existing issue

**If no overlap:** Skip pivot, fall through to Step 5 (Duplicate Detection).

### Step 4c: Present Pivot Option

If the plan overlaps with the linked issue, present the choice to the user via `AskUserQuestion`:

```
## Pivot Detected

You're in worktree `{worktree-dir}` which is linked to:
- **Issue #{number}**: {title}
- **URL**: {issue-url}

The new plan overlaps with this existing issue:
- {brief explanation of overlap}
```

| Option | Description |
|--------|-------------|
| **Pivot** | Update this issue with the new plan. The old plan is preserved as a comment. Continue working in this worktree. |
| **New issue + worktree** | Create a separate issue and worktree for the new plan. Existing issue is untouched. |

**If user chooses "New issue + worktree":** Fall through to Step 5 and the normal flow.

### Step 4d: Execute Pivot

**CRITICAL: Operations must execute in this exact order.** The comment (archiving old body) MUST succeed before the edit (replacing body). This ensures no data loss — if commenting fails, the old body is still on the issue.

**1. Archive old body as a comment:**

```bash
gh issue comment "$ISSUE_NUM" --repo "$REPO" --body "$(cat <<'EOF'
## Superseded Plan

_This was the original plan for this issue before it was updated on YYYY-MM-DD._

---

<original issue body, verbatim>
EOF
)"
```

**2. Replace issue body with the new plan:**

```bash
gh issue edit "$ISSUE_NUM" --repo "$REPO" --body "$(cat <<'EOF'
<new plan content - unmodified>
EOF
)"
```

**3. Update `.claude/github-cache.json` in the current worktree:**

Only update `issue.body` — preserve everything else (`branch`, `issue.number`, `issue.url`, `issue.title`, `issue.state`, and any `pr` section).

```bash
EXISTING=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
# Write to a temp file and mv on success so a jq failure never truncates the existing cache
# (a bare `> github-cache.json` redirect truncates the file before jq runs).
TMP=$(mktemp .claude/github-cache.json.XXXXXX)
echo "$EXISTING" | jq --arg body "<new plan content>" \
  '.issue.body = $body' > "$TMP" && mv "$TMP" .claude/github-cache.json || rm -f "$TMP"
```

**4. Update project-level `issues.json` cache:**

Update the issue's body in `$CACHE_FILE` so future duplicate detection runs against the current plan.

**5. Output pivot confirmation:**

```
## Pivot Complete!

**Issue #{number}**: {title}
**URL**: {issue-url}

- Old plan archived as comment on the issue
- Issue body updated with new plan
- Local caches updated
```

**6. Close telemetry and call `ExitPlanMode`:**

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome success 2>/dev/null || true
```

Call `ExitPlanMode` — since the user is already in the correct worktree, they can approve the plan and begin implementing immediately. The plan file content is the new plan (it triggered `/track-and-start`).

**Do NOT** proceed to Steps 5-9 after a successful pivot.

### Known Trade-offs

- **Branch name may drift**: After a pivot, the branch name (e.g., `feature/42-old-slug`) may no longer match the new plan. This is acceptable — the issue is the source of truth, and renaming branches in worktrees is disruptive.
- **Multiple pivots**: Each pivot adds a "Superseded Plan" comment, creating an audit trail. This is intentional.

## Duplicate Detection

**Note:** If pivot detection (Step 4) already resolved the overlap by updating the current issue, Steps 5-9 are skipped entirely and this section does not apply.

Before creating a new issue, check the issue cache for existing open issues that overlap with the planned work. This prevents duplicate issues and surfaces opportunities to link or extend existing work.

### How to Check

1. **Load the issue cache** at `$CACHE_FILE` (detected from worktree layout)

2. **Filter to open issues only** (`state: "open"`)

3. **Compare the plan title and content** against each open issue's title and labels. Look for:
   - **Title similarity**: Keywords in common, same feature area, same component
   - **Scope overlap**: The plan addresses something an existing issue already covers (fully or partially)
   - **Subset/superset**: The plan is a narrower or broader version of an existing issue

4. **Present matches to the user** if any are found (see below)

### When Matches Are Found

If one or more open issues look related, **stop and present them** to the user using `AskUserQuestion` before creating anything. Show:

- The issue number, title, and URL for each match
- A brief note on why it looks related (e.g., "both address transcript display")

Then offer these options:

| Option | Description |
|--------|-------------|
| **Pivot to existing** | Archive the existing issue's body as a comment, replace it with the new plan, then create branch/worktree linked to that issue. Use when the plan supersedes or refines the existing issue. |
| **Create new and reference** | Create the new issue but add a "Related: #N" line. Useful when the work is distinct but connected. |
| **Create new (no overlap)** | The match was a false positive. Proceed normally with no references. |

If multiple issues match, list them all and let the user pick which (if any) to link or reference.

### When No Matches Are Found

Proceed directly to issue creation — no user prompt needed.

### "Pivot to existing" Flow

If the user chooses to pivot to an existing issue, execute **steps 1–2 of
[Pivot Detection Step 4d](#step-4d-execute-pivot)** (archive-then-replace, same ordering guarantee,
same abort-on-comment-failure rule) against `$EXISTING_ISSUE_NUM` — fetching its body first if not
already available (`gh issue view "$EXISTING_ISSUE_NUM" --repo "$REPO" --json body -q '.body'`).

Then, instead of 4d's steps 3–6 (this pivot targets a duplicate-detection match, not the current
worktree's issue):

1. **Skip issue creation** — use the existing issue number for branch naming: `{type}/{existing-issue#}-{slug}`
2. **Update `$CACHE_FILE`** with the new issue body so future duplicate detection runs against the current plan
3. **Continue with worktree creation** and handoff as normal, using the existing issue's URL and number

## Branch Naming

Format: `{type}/{issue#}-{slug}`

- **Types**: `fix`, `feature`, `chore`
- **Slug**: Kebab-case from issue title, max 50 chars, lowercase
- **Example**: `feature/42-add-transcript-export`

### Type Inference

Scan the plan title and content for keywords:

| Pattern | Type |
|---------|------|
| "fix", "bug", "broken", "error", "crash" | `fix` |
| "add", "new", "feature", "implement", "create" | `feature` |
| "refactor", "cleanup", "update", "chore", "rename", "move" | `chore` |
| Default | `feature` |

### Slug Generation

```bash
# From issue title, generate slug
SLUG=$(echo "$TITLE" | \
  tr '[:upper:]' '[:lower:]' | \
  sed 's/[^a-z0-9]/-/g' | \
  sed 's/--*/-/g' | \
  sed 's/^-//' | \
  sed 's/-$//' | \
  cut -c1-50)

BRANCH="${TYPE}/${ISSUE_NUM}-${SLUG}"
```

## Issue Cache

After creating a new issue, append it to the local JSON cache so subsequent commands can avoid API calls.

Cache file location: `${WORKTREE_PARENT}/issues.json` (detected from worktree layout — see [Project Detection](#project-detection))

## Creating the Issue

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage create-issue 2>/dev/null || true
```

**IMPORTANT:** Use the ORIGINAL plan content. Do NOT include branch/worktree info in the issue body.
The issue body should be a clean copy of the plan that was written before /track-and-start was invoked.

```bash
# $REPO is already set from project detection above

# Create issue with ORIGINAL plan as body (no setup additions)
ISSUE_URL=$(gh issue create \
  --repo "$REPO" \
  --title "<title from plan>" \
  --body "$(cat <<'EOF'
<original plan content - unmodified>
EOF
)" \
  --assignee "$ASSIGNEE" \
  --label "<type-label>" \
  --json url -q '.url')

# Extract issue number
ISSUE_NUM=$(echo "$ISSUE_URL" | grep -oE '[0-9]+$')
```

### Cache Write-Back

After creating the issue, add it to the local cache:

```bash
# $CACHE_FILE is already set from project detection above

# Append new issue to cache (use jq or equivalent)
# .issues["$ISSUE_NUM"] = { number, title, state: "open", labels, milestone: null, assignee: "$ASSIGNEE", url }
# Update .lastSynced to current timestamp

python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-issue --outcome success 2>/dev/null || true
```

## Label Inference

Infer labels from plan content:

| Content Pattern | Label |
|-----------------|-------|
| "fix", "bug", "broken" | `bug` |
| "add", "new", "feature" | `enhancement` |
| "doc", "readme", "guide" | `documentation` |
| "refactor", "cleanup" | `chore` |

## Creating the Worktree

**Note:** Local Plan Mode's "Branch and Worktree (Local Mode)" step above also lands here via its
own "continue to Branch Naming and Creating the Worktree as normal" cross-reference — this stage
marker is unconditional and self-contained (no prior stage-end bundled into it) specifically so
that entry path stays correctly paired, rather than closing out a `create-issue` stage Local Mode
never opened.

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage create-worktree 2>/dev/null || true
```

**CRITICAL:** Must `cd` to main worktree first to ensure correct git repo.

```bash
cd "$MAIN_WORKTREE"
WORKTREE_DIR="${BRANCH#*/}"

if [ "$USE_GRAFT" = "true" ]; then
  GRAFT_OUTPUT=$(graft new "$WORKTREE_DIR" --no-setup -r "$GRAFT_REPO_NAME" 2>&1)
  GRAFT_EXIT=$?
  if [ $GRAFT_EXIT -ne 0 ]; then
    echo "WARNING: graft new failed, falling back to manual worktree creation"
    echo "$GRAFT_OUTPUT"
    USE_GRAFT=false
  else
    echo "$GRAFT_OUTPUT"
    GRAFT_BRANCH=$(echo "$GRAFT_OUTPUT" | grep "Using branch name" | sed "s/.*'\\([^']*\\)'.*/\\1/")
    [ -n "$GRAFT_BRANCH" ] && BRANCH="$GRAFT_BRANCH"
    GRAFT_PATH=$(echo "$GRAFT_OUTPUT" | grep "Created worktree" | sed "s/.* at //")
    [ -n "$GRAFT_PATH" ] && WORKTREE_PATH="$GRAFT_PATH"
    WORKTREE_PARENT=$(dirname "$WORKTREE_PATH")
    CACHE_FILE="${WORKTREE_PARENT}/issues.json"
  fi
fi
if [ "$USE_GRAFT" != "true" ]; then
  mkdir -p "$WORKTREE_PARENT"
  WORKTREE_PATH="${WORKTREE_PARENT}/${WORKTREE_DIR}"
  git worktree add "$WORKTREE_PATH" -b "${BRANCH}"
fi
```

## Worktree GitHub Cache

After creating the issue and worktree, write the issue data to `.claude/github-cache.json` **in the new worktree** (not the current directory). The issue body is the original plan content (already available — it was used as the issue body).

```bash
# Write cache into the NEW worktree
mkdir -p "${WORKTREE_PATH}/.claude"

jq -n --arg branch "${BRANCH}" \
      --argjson number "${ISSUE_NUM}" \
      --arg url "${ISSUE_URL}" \
      --arg title "${ISSUE_TITLE}" \
      --arg body "$PLAN_CONTENT" \
      '{branch: $branch, issue: {number: $number, url: $url, title: $title, body: $body, state: "open"}}' \
      > "${WORKTREE_PATH}/.claude/github-cache.json"
```

**Why the new worktree?** The current session is in main (or another worktree). The new worktree is where implementation will happen, so that's where commands like `/shipit` and `/expert-review` will read from.

## Plan Archival

**In local plan mode:** Already done during [Local Plan Mode](#local-plan-mode) — skip.

**In GitHub mode:** After creating the worktree, check if a `plans/` directory exists at the **project root**. If it does, save a copy of the plan there for permanent reference.

```bash
# $PROJECT_ROOT is already set from project detection above
PLANS_DIR="${PROJECT_ROOT}/plans"
if [ -d "$PLANS_DIR" ]; then
  PLAN_FILE="${PLANS_DIR}/${ISSUE_NUM}-${SLUG}.md"
  cat > "$PLAN_FILE" <<'EOF'
<original plan content>
EOF
  echo "Plan archived to ${PLAN_FILE}"
fi
```

## Project Issues Tracker Update

**In local plan mode:** Already done during [Local Plan Mode](#local-plan-mode) — skip.

**In GitHub mode:** After creating the issue and worktree, check if the project root contains an `issues.json` that is a **JSON array** with objects that have `id`, `title`, and `status` fields. If found, update the matching entry's `status` to `"in_progress"`.

**Matching logic** (in priority order):
1. **By id**: If the GitHub issue number matches an entry's `id` field
2. **By title**: If the GitHub issue title is a close match to an entry's `title` field

```bash
# $PROJECT_ROOT is already set from project detection above
PROJECT_ISSUES="${PROJECT_ROOT}/issues.json"
if [ -f "$PROJECT_ISSUES" ]; then
  IS_ARRAY=$(jq 'type == "array"' "$PROJECT_ISSUES" 2>/dev/null)
  if [ "$IS_ARRAY" = "true" ]; then
    MATCH_IDX=$(jq -r --arg title "$ISSUE_TITLE" \
      'to_entries[] | select(.value.title | ascii_downcase | contains($title | ascii_downcase)) | .key' \
      "$PROJECT_ISSUES" 2>/dev/null | head -1)

    if [ -n "$MATCH_IDX" ]; then
      jq --argjson idx "$MATCH_IDX" '.[$idx].status = "in_progress"' \
        "$PROJECT_ISSUES" > "${PROJECT_ISSUES}.tmp" && mv "${PROJECT_ISSUES}.tmp" "$PROJECT_ISSUES"
      MATCHED_TITLE=$(jq -r --argjson idx "$MATCH_IDX" '.[$idx].title' "$PROJECT_ISSUES")
      echo "Updated issues.json: \"$MATCHED_TITLE\" → in_progress"
    fi
  fi
fi
```

## Final Output - Handoff Commands

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome success 2>/dev/null || true
```

After creating the issue and worktree, output the following for the user to copy/paste:

```
## Ready to implement!

**Issue:** <issue-url>
**Branch:** `<branch-name>`
**Worktree:** `<worktree-path>`

### Start implementation:

cd <worktree-path> && claude "/implement-with-haiku"
```

The user will:
1. Copy the `cd ... && claude ...` command
2. Run it in their terminal
3. A new Claude session starts in the correct worktree with the issue context

**Do NOT:**
- Call ExitPlanMode
- Try to continue implementation in this session
- Update the local plan file (it stays in the original location)

## Error Handling

On any error-table exit below (before the worktree/handoff step is reached), mark the command as
failed — non-fatal, same as every other telemetry call in this doc:

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome failure --failure-class guard_block 2>/dev/null || true
```

| Condition | Action |
|-----------|--------|
| Not in plan mode | Error: "Must be in plan mode. Use `/plan` first to create a plan." |
| No plan file | Error: "No plan file found. Create a plan first." |
| Not in a git repo | Error: "Must be in a git repository with a GitHub remote" |
| No GitHub remote | Error: "No GitHub remote found. Add one with `gh repo create` or `git remote add`" |
| Overlapping issue found | Ask user: pivot to existing, create new with reference, or create new (no overlap) |
| Pivot-to-existing: `gh issue comment` fails | Error + abort: "Failed to archive old issue body. Aborting pivot to avoid losing the original content." |
| Pivot-to-existing: `gh issue edit` fails | Error: "Failed to update issue body. Old body is preserved as a comment. Try again or update manually." |
| Pivot: `gh issue comment` fails | Error + abort: "Failed to archive old plan. Aborting pivot to avoid losing the original plan." |
| Pivot: `gh issue edit` fails | Error: "Failed to update issue body. Old plan is preserved as a comment. Try again or update manually." |
| Pivot: linked issue is closed | Skip pivot detection, proceed to Step 5 (Duplicate Detection) |
| Issue cache missing/empty | Skip duplicate detection, proceed to create issue |
| Worktree already exists | Error: "Worktree already exists at `<path>`. Use it or pick a different branch name." |
| Branch already exists | Ask: "Branch `<name>` exists. Use existing branch or create new?" |
| gh CLI not authenticated | Error: "GitHub CLI not authenticated. Run `gh auth login`" |
| Ticket ID not found in Linear or Jira | Error: "Ticket $TICKET_ID not found in Linear or Jira. Check the ID and try again." |
| `gitBranchName` empty in Linear response | Fall back to `${ticket-id-lower}-${title-slug}` (same slug generation used in GitHub mode) |
| No GitHub remote (tracker-ticket mode) | Not an error — expected. `REPO` will be empty; no `gh` commands are called. |

(The numbered **Behavior** list at the top is the workflow reference — the sections above are the
detail for each step.)
