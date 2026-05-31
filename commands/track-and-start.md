---
name: track-and-start
description: Use when user says "/track-and-start" to create a GitHub issue (or local plan), branch, and worktree in one step. Requires plan mode.
---

# Track and Start - Combined Issue, Branch, and Worktree Workflow

Creates a GitHub issue (or local plan file) from the plan, generates a branch name, and sets up a worktree in one step. When the project root has a `plans/` directory and an array-format `issues.json`, uses local plan tracking instead of GitHub issues.

## Requirements

- **Must be in plan mode** with a valid plan file
- Current directory must be within a git repository with a GitHub remote

## Behavior

1. **Validate** plan mode and plan file exist
2. **Read original plan** content (preserve for issue - no modifications)
3. **Detect project** from git remote and worktree layout
4. **Check for local plan mode** — if `plans/` directory and array-format `issues.json` exist at the project root, use [Local Plan Mode](#local-plan-mode) (replaces steps 5-7 with local equivalents)
5. **Pivot detection** — if current worktree has a linked issue overlapping with the new plan, offer to pivot (see [Pivot Detection](#pivot-detection)). If pivot is accepted, skip steps 6-10.
6. **Check for overlapping issues** in the issue cache (see [Duplicate Detection](#duplicate-detection))
7. **Create GitHub issue** with ORIGINAL plan as body, assign to current GitHub user
8. **Generate branch name** from issue type and title
9. **Create worktree** in correct location
10. **Output handoff commands** for user to start implementation in the new worktree

**Note:** This skill does NOT call ExitPlanMode or continue implementation, **except** in the pivot flow where the user is already in the correct worktree — in that case, ExitPlanMode is called so the user can approve and begin implementing immediately.

## Project Detection

```bash
# 1. Repo identity
REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner')

# 2. Main worktree (first entry is always main)
MAIN_WORKTREE=$(git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2)

# 3. Detect worktree parent from existing worktrees
SECOND_WORKTREE=$(git worktree list --porcelain | grep '^worktree ' | sed -n '2p' | cut -d' ' -f2)
if [ -n "$SECOND_WORKTREE" ]; then
  WORKTREE_PARENT=$(dirname "$SECOND_WORKTREE")
else
  WORKTREE_PARENT="${MAIN_WORKTREE}/.claude/worktrees"
fi

# 4. Issue cache at worktree parent level
CACHE_FILE="${WORKTREE_PARENT}/issues.json"

# 5. Current GitHub user for assignment
ASSIGNEE=$(gh api user -q '.login' 2>/dev/null || echo "")
```

```bash
# 6. Detect project root (parent of worktrees/ structure, or main worktree itself)
PARENT_BASENAME=$(basename "$(dirname "$MAIN_WORKTREE")")
if [ "$PARENT_BASENAME" = "worktrees" ]; then
  PROJECT_ROOT=$(dirname "$(dirname "$MAIN_WORKTREE")")
else
  PROJECT_ROOT="$MAIN_WORKTREE"
fi
```

### Graft Detection

After resolving `MAIN_WORKTREE`, check if graft is installed and the current repo is registered with it.

```bash
USE_GRAFT=false
GRAFT_REPO_NAME=""
if command -v graft >/dev/null 2>&1; then
  GRAFT_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/graft/config.json"
  if [ -f "$GRAFT_CONFIG" ]; then
    GRAFT_REPO_NAME=$(jq -r --arg path "$MAIN_WORKTREE" '
      .repos // {} | to_entries[] |
      select(.value.path == $path) | .key
    ' "$GRAFT_CONFIG" 2>/dev/null | head -1)
    if [ -n "$GRAFT_REPO_NAME" ]; then
      USE_GRAFT=true
    fi
  fi
fi
```

**If not in a git repo or no GitHub remote:** Error with message about needing to be in a git repository with a GitHub remote.

## Local Plan Mode

When the project root has both a `plans/` directory and an array-format `issues.json`, skip GitHub issue creation and use local plan tracking instead. This replaces steps 5-7 (pivot detection, duplicate detection, issue creation) with a local workflow.

### Detection

```bash
PLANS_DIR="${PROJECT_ROOT}/plans"
PROJECT_ISSUES="${PROJECT_ROOT}/issues.json"

LOCAL_MODE=false
if [ -d "$PLANS_DIR" ] && [ -f "$PROJECT_ISSUES" ]; then
  IS_ARRAY=$(jq 'type == "array"' "$PROJECT_ISSUES" 2>/dev/null)
  if [ "$IS_ARRAY" = "true" ]; then
    LOCAL_MODE=true
  fi
fi
```

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
cat > "${WORKTREE_PATH}/.claude/github-cache.json" <<CACHEEOF
{
  "branch": "${BRANCH}",
  "localPlan": {
    "id": ${ISSUE_NUM},
    "title": "${ISSUE_TITLE}",
    "plan": "${PLAN_FILE}",
    "status": "in_progress"
  }
}
CACHEEOF
```

### Handoff (Local Mode)

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

## Pivot Detection

When `/track-and-start` is called from a worktree that's already linked to an issue, and the new plan overlaps with that issue, offer to **pivot** — replace the issue's scope with the new plan instead of creating a new issue and worktree.

### Step 4a: Detect Current Worktree's Linked Issue

First, check if we're in a non-main worktree:

```bash
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
GIT_DIR=$(git rev-parse --git-dir 2>/dev/null)
if [ "$COMMON_DIR" = "$GIT_DIR" ]; then
  # We're in the main worktree — skip pivot detection
fi
```

**If in main worktree:** Skip pivot detection entirely, fall through to Step 5 (Duplicate Detection).

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
echo "$EXISTING" | jq --arg body "<new plan content>" \
  '.issue.body = $body' > .claude/github-cache.json
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

**6. Call `ExitPlanMode`** — since the user is already in the correct worktree, they can approve the plan and begin implementing immediately. The plan file content is the new plan (it triggered `/track-and-start`).

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
| **Link to existing** | Skip creating a new issue. Use the existing issue number — create the branch/worktree linked to that issue instead. Add the plan as a comment on the existing issue. |
| **Create new and reference** | Create the new issue but add a "Related: #N" line. Useful when the work is distinct but connected. |
| **Create new (no overlap)** | The match was a false positive. Proceed normally with no references. |

If multiple issues match, list them all and let the user pick which (if any) to link or reference.

### When No Matches Are Found

Proceed directly to issue creation — no user prompt needed.

### "Pivot to existing" Flow

If the user chooses to pivot to an existing issue (archive old body, replace with plan):

This follows the same archive-then-replace pattern as [Pivot Detection Step 4d](#step-4d-execute-pivot), but applied to an issue found during duplicate detection rather than a worktree's linked issue.

**CRITICAL: Operations must execute in this exact order.** The comment (archiving old body) MUST succeed before the edit (replacing body). This ensures no data loss.

1. **Fetch the existing issue body** if not already available:
   ```bash
   OLD_BODY=$(gh issue view "$EXISTING_ISSUE_NUM" --repo "$REPO" --json body -q '.body')
   ```

2. **Archive old body as a comment:**
   ```bash
   gh issue comment "$EXISTING_ISSUE_NUM" --repo "$REPO" --body "$(cat <<'EOF'
   ## Superseded Plan

   _This was the original issue description before it was replaced with the implementation plan on YYYY-MM-DD._

   ---

   <old issue body, verbatim>
   EOF
   )"
   ```
   **If this fails:** Abort — the old body is still on the issue. Error: "Failed to archive old issue body. Aborting pivot to avoid losing the original content."

3. **Replace issue body with the new plan:**
   ```bash
   gh issue edit "$EXISTING_ISSUE_NUM" --repo "$REPO" --body "$(cat <<'EOF'
   <new plan content - unmodified>
   EOF
   )"
   ```

4. **Skip issue creation** — use the existing issue number
5. **Use the existing issue number** for branch naming: `{type}/{existing-issue#}-{slug}`
6. **Update `$CACHE_FILE`** with the new issue body so future duplicate detection runs against the current plan
7. **Continue with worktree creation** and handoff as normal, using the existing issue's URL and number

### "Link to existing" Flow

If the user chooses to link to an existing issue instead of creating a new one:

1. **Skip issue creation** entirely
2. **Use the existing issue number** for branch naming: `{type}/{existing-issue#}-{slug}`
3. **Add the plan as a comment** on the existing issue:
   ```bash
   gh issue comment "$EXISTING_ISSUE_NUM" --body "$(cat <<'EOF'
   ## Implementation Plan

   <original plan content>
   EOF
   )"
   ```
4. **Continue with worktree creation** and handoff as normal, using the existing issue's URL and number

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

cat > "${WORKTREE_PATH}/.claude/github-cache.json" <<CACHEEOF
{
  "branch": "${BRANCH}",
  "issue": {
    "number": ${ISSUE_NUM},
    "url": "${ISSUE_URL}",
    "title": "${ISSUE_TITLE}",
    "body": $(echo "$PLAN_CONTENT" | jq -Rs .),
    "state": "open"
  }
}
CACHEEOF
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

| Condition | Action |
|-----------|--------|
| Not in plan mode | Error: "Must be in plan mode. Use `/plan` first to create a plan." |
| No plan file | Error: "No plan file found. Create a plan first." |
| Not in a git repo | Error: "Must be in a git repository with a GitHub remote" |
| No GitHub remote | Error: "No GitHub remote found. Add one with `gh repo create` or `git remote add`" |
| Overlapping issue found | Ask user: pivot to existing, link to existing, create new with reference, or create new (no overlap) |
| Pivot-to-existing: `gh issue comment` fails | Error + abort: "Failed to archive old issue body. Aborting pivot to avoid losing the original content." |
| Pivot-to-existing: `gh issue edit` fails | Error: "Failed to update issue body. Old body is preserved as a comment. Try again or update manually." |
| Pivot: `gh issue comment` fails | Error + abort: "Failed to archive old plan. Aborting pivot to avoid losing the original plan." |
| Pivot: `gh issue edit` fails | Error: "Failed to update issue body. Old plan is preserved as a comment. Try again or update manually." |
| Pivot: linked issue is closed | Skip pivot detection, proceed to Step 5 (Duplicate Detection) |
| Issue cache missing/empty | Skip duplicate detection, proceed to create issue |
| Worktree already exists | Error: "Worktree already exists at `<path>`. Use it or pick a different branch name." |
| Branch already exists | Ask: "Branch `<name>` exists. Use existing branch or create new?" |
| gh CLI not authenticated | Error: "GitHub CLI not authenticated. Run `gh auth login`" |

## Quick Reference

| Step | Command/Action |
|------|----------------|
| Read plan | Preserve original content for issue/plan file |
| Detect project | `gh repo view`, `git worktree list`, detect worktree parent and project root |
| **Local mode check** | If `plans/` + array `issues.json` exist at project root → local plan mode |
| Local: save plan | Write plan to `plans/{id}-{slug}.md`, update `issues.json` → `in_progress` |
| GitHub: pivot detection | Check if current worktree has linked issue overlapping with plan |
| GitHub: pivot (if chosen) | Archive old plan as comment, update issue body + caches, call ExitPlanMode |
| GitHub: check duplicates | Scan `$CACHE_FILE` for open issues overlapping with plan |
| GitHub: create issue | `gh issue create --assignee "$ASSIGNEE"` (or link to existing) |
| Generate branch | `{type}/{id}-{slug}` (id = local ID or GitHub issue number) |
| Create worktree | Graft repo: `graft new <dir> --no-setup -r <repo>` (fallback: `git worktree add`) |
| Finish (local) | Output handoff: `cd <worktree> && claude "Implement the plan at <plan-path>"` |
| Finish (GitHub) | Output handoff: `cd <worktree> && claude "Implement <issue-url>"` |
