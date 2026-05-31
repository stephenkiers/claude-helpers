---
name: track
description: Use when user says "/track", "track this", "create issue", or wants to create/link a GitHub issue (or local plan entry). In plan mode, uploads the plan. Outside plan mode, creates from current context.
---

# Track - Issue / Local Plan Workflow

Create or link GitHub issues (or local plan entries) to track work. When the project root has a `plans/` directory and an array-format `issues.json`, uses local plan tracking instead of GitHub issues.

## Behavior

### In Plan Mode

1. Read the current plan file (path from system prompt)
2. Detect if working on an existing issue:
   - Check branch name for issue number (e.g., `123-feature-name`)
   - Check recent conversation for issue references
3. **If new work:**
   - Create GitHub issue with plan as description
   - Assign to current GitHub user (`gh api user -q '.login'`)
   - Add labels based on plan content (infer type, priority)
   - Update plan file with issue link at top
4. **If existing issue:**
   - Add plan as comment with header: `## Implementation Plan`
   - If previous plans exist, note iteration number
5. Call `ExitPlanMode` to proceed with implementation

### Outside Plan Mode

1. Summarize current work/context
2. Ask user for title if not obvious
3. Create or update GitHub issue
4. Suggest creating a worktree named after the issue

## Local Plan Mode

When the project root has both a `plans/` directory and an array-format `issues.json`, use local plan tracking instead of GitHub issues.

### Detection

```bash
# Detect project root
MAIN_WORKTREE=$(git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2)
PARENT_BASENAME=$(basename "$(dirname "$MAIN_WORKTREE")")
if [ "$PARENT_BASENAME" = "worktrees" ]; then
  PROJECT_ROOT=$(dirname "$(dirname "$MAIN_WORKTREE")")
else
  PROJECT_ROOT="$MAIN_WORKTREE"
fi

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

If `LOCAL_MODE` is false, fall through to the normal GitHub flow.

### In Plan Mode (Local)

1. Read the current plan file
2. Scan `issues.json` for existing entries with overlapping titles (status `"todo"` or `"planned"`)
3. **If match found:** Ask user to update existing entry or create new
4. **If new work:**
   - Generate next ID: `NEXT_ID=$(jq '[.[].id] | max + 1' "$PROJECT_ISSUES")`
   - Generate slug from title
   - Save plan to `plans/{id}-{slug}.md`
   - Add entry to `issues.json` with `status: "planned"`
5. **If existing entry:**
   - Save plan to `plans/{id}-{slug}.md` (using existing entry's ID)
   - Update entry: set `plan` field, keep status as-is (or set to `"planned"` if `"todo"`)
6. Call `ExitPlanMode` to proceed

```bash
# New entry example
jq --argjson id "$NEXT_ID" \
   --arg title "$TITLE" \
   --arg plan "plans/${NEXT_ID}-${SLUG}.md" \
  '. += [{"id": $id, "title": $title, "status": "planned", "plan": $plan}]' \
  "$PROJECT_ISSUES" > "${PROJECT_ISSUES}.tmp" && mv "${PROJECT_ISSUES}.tmp" "$PROJECT_ISSUES"
echo "Updated issues.json: \"$TITLE\" → planned"
```

### Outside Plan Mode (Local)

1. Summarize current work/context
2. Ask user for title if not obvious
3. Add entry to `issues.json` with `status: "planned"` (no plan file — no plan content to save)
4. Suggest using `/track-and-start` to create a plan and worktree

### Worktree Cache (Local Mode)

If in a non-main worktree, write `.claude/github-cache.json` with local plan data:

```bash
mkdir -p .claude
EXISTING=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
echo "$EXISTING" | jq --arg branch "$(git branch --show-current)" \
  --argjson localPlan "{\"id\": $ISSUE_NUM, \"title\": \"$TITLE\", \"plan\": \"$PLAN_FILE\", \"status\": \"planned\"}" \
  '. + {branch: $branch, localPlan: $localPlan}' > .claude/github-cache.json
```

---

## Issue Cache

Before calling `gh issue view` or `gh issue list`, check the local JSON cache:

1. Detect issue cache from worktree layout:
   ```bash
   MAIN_WORKTREE=$(git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2)
   SECOND_WORKTREE=$(git worktree list --porcelain | grep '^worktree ' | sed -n '2p' | cut -d' ' -f2)
   if [ -n "$SECOND_WORKTREE" ]; then
     WORKTREE_PARENT=$(dirname "$SECOND_WORKTREE")
   else
     WORKTREE_PARENT="${MAIN_WORKTREE}/.claude/worktrees"
   fi
   CACHE_FILE="${WORKTREE_PARENT}/issues.json"
   ```
2. Read the cache file and look up the issue number in the `issues` object
3. If found in cache, use that data — **skip the `gh` API call**
4. Only fall back to `gh issue view` if the issue is **not in the cache**
5. After creating a new issue, append it to the cache (see "Cache Write-Back" below)

### Cache Write-Back

After creating a new issue via `gh issue create`, add it to the cache:

```bash
# After: ISSUE_URL=$(gh issue create ... --json url -q '.url')
# Extract issue number
ISSUE_NUM=$(echo "$ISSUE_URL" | grep -oE '[0-9]+$')

# Detect cache path (same project detection as above)
# Use jq or a script to add the new entry to issues.json:
# .issues["$ISSUE_NUM"] = { number, title, state: "open", labels, milestone, assignee, url }
```

Update `lastSynced` to the current timestamp when writing back.

## Detecting Existing Issues

```bash
# Check branch name for issue number
BRANCH=$(git branch --show-current)
ISSUE_NUM=$(echo "$BRANCH" | grep -oE '^[0-9]+' || echo "")

# Detect cache path from worktree layout
MAIN_WORKTREE=$(git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2)
SECOND_WORKTREE=$(git worktree list --porcelain | grep '^worktree ' | sed -n '2p' | cut -d' ' -f2)
if [ -n "$SECOND_WORKTREE" ]; then
  WORKTREE_PARENT=$(dirname "$SECOND_WORKTREE")
else
  WORKTREE_PARENT="${MAIN_WORKTREE}/.claude/worktrees"
fi
CACHE_FILE="${WORKTREE_PARENT}/issues.json"

# Check local cache first
if [ -n "$ISSUE_NUM" ]; then
  # Try cache first, fall back to API
  if [ -f "$CACHE_FILE" ]; then
    CACHED=$(jq -r ".issues[\"$ISSUE_NUM\"] // empty" "$CACHE_FILE" 2>/dev/null)
    if [ -n "$CACHED" ]; then
      echo "$CACHED"
    else
      gh issue view "$ISSUE_NUM" --json number,title,state 2>/dev/null
    fi
  else
    gh issue view "$ISSUE_NUM" --json number,title,state 2>/dev/null
  fi
fi
```

## Creating Issues

```bash
# Detect repo from remote
REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner')

# Create issue with plan as body
gh issue create \
  --repo "$REPO" \
  --title "<title>" \
  --body "$(cat <<'EOF'
<plan content>
EOF
)" \
  --assignee "$ASSIGNEE" \
  --label "<type>" \
  --label "<priority>"
```

## Adding Plan to Existing Issue

```bash
gh issue comment "$ISSUE_NUM" --body "$(cat <<'EOF'
## Implementation Plan

<plan content>
EOF
)"
```

## Label Inference

Infer labels from plan content:

| Content Pattern | Label |
|-----------------|-------|
| "fix", "bug", "broken" | `bug` |
| "add", "new", "feature" | `enhancement` |
| "doc", "readme", "guide" | `documentation` |
| "critical", "urgent", "blocking" | `P0` |
| "high priority", "important" | `P1` |
| "medium", "normal" | `P2` |
| "low priority", "nice to have" | `P3` |
| "trivial", "quick", "simple" | `effort:trivial` |
| "small", "straightforward" | `effort:low` |
| "moderate", "some work" | `effort:medium` |

## Worktree GitHub Cache

After creating or linking an issue, write the issue data to `.claude/github-cache.json` in the current worktree. This per-worktree cache gives other commands (`/shipit`, `/cleanup`, `/expert-review`) fast access to the linked issue and PR without API calls.

**Write strategy:** Read-modify-write (merge, not replace). Only write the `branch` and `issue` sections — preserve any existing `pr` section.

```bash
# Get current branch
BRANCH=$(git branch --show-current)

# Build the cache data
# If linking an existing issue (not creating), fetch the body:
#   ISSUE_BODY=$(gh issue view "$ISSUE_NUM" --json body -q '.body')
# If creating a new issue, the body is the plan content already available

# Write to .claude/github-cache.json (create .claude/ dir if needed)
mkdir -p .claude

# Read existing cache to preserve pr section (if any)
EXISTING=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')

# Merge issue data into cache
echo "$EXISTING" | jq --arg branch "$BRANCH" \
  --argjson issue "{\"number\": $ISSUE_NUM, \"url\": \"$ISSUE_URL\", \"title\": \"$ISSUE_TITLE\", \"body\": $(echo "$ISSUE_BODY" | jq -Rs .), \"state\": \"open\"}" \
  '. + {branch: $branch, issue: $issue}' > .claude/github-cache.json
```

## Workflow Integration

After creating/linking an issue:
1. Suggest creating a worktree: `git worktree add ../{issue#}-{desc} -b {issue#}-{desc}`
2. Work is tracked in issue comments
3. `/shipit` will link PR to issue with "Closes #N"
4. `/cleanup` will verify issue is closed

## Quick Reference

| Scenario | Action |
|----------|--------|
| Local mode, plan mode, new work | Save plan to `plans/`, add entry to `issues.json` with `"planned"` |
| Local mode, plan mode, existing entry | Save plan to `plans/`, update entry's `plan` field |
| Local mode, outside plan mode | Add entry to `issues.json` with `"planned"` (no plan file) |
| GitHub mode, plan mode, new work | Create issue with plan as description |
| GitHub mode, plan mode, existing issue | Add plan as comment |
| GitHub mode, outside plan mode | Summarize context, create issue |
| Branch has issue number | Link to existing issue/entry |
