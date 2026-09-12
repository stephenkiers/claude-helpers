# Track-and-Start Reference Documentation

<!-- Lives in prompts/, not commands/: every .md file in ~/.claude/commands/ is registered as an
     invocable slash command regardless of frontmatter, and this is a reference doc, not a command. -->

This file contains the conditional-path sections of `track-and-start-v2.md` — each only runs under a
specific entry mode, never on every invocation. The common path (argument parsing, plan file
resolution, project detection, branch naming, issue/worktree creation, final output, error handling)
stays inline in `track-and-start-v2.md`. Read this when:
- Invoked with a Linear/Jira tracker ticket ID (see Tracker Ticket Mode)
- Running inside a worktree already linked to an open issue (see Pivot Detection)
- The project root has both a `plans/` directory and an array-format `issues.json` (see Local Plan Mode)
- On the GitHub-mode issue-creation path, checking for overlapping open issues (see Duplicate Detection)

---

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

### Plan and Apply (Local Mode)

**Print metadata confirmation** (for `ENTRY_MODE ∈ {path-arg, tracker-with-path}`):

*Note: This block also appears in Tracker Ticket Mode (Worktree Creation), Pivot Detection, and Creating the Issue sections. Keep these blocks in sync.*

```bash
if [ "$ENTRY_MODE" = "path-arg" ] || [ "$ENTRY_MODE" = "tracker-with-path" ]; then
  # Always print path + mtime line (even if CONFIRM_ECHO=0)
  PLAN_MTIME=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$PLAN_FILE" 2>/dev/null || stat -c '%y' "$PLAN_FILE" 2>/dev/null | cut -d' ' -f1,2)
  echo "**Resolved plan file:** \`$PLAN_FILE\` (modified: $PLAN_MTIME)"
  
  # Print other details only if CONFIRM_ECHO != 0
  if [ "$CONFIRM_ECHO" != "0" ]; then
    echo "**Title:** $TITLE"
    echo "**Branch:** feature/{issue_number}-<slug>"
    echo "**Worktree:** $WORKTREE_PARENT/{issue_number}-<slug>"
    if [ -n "$REPO" ]; then
      echo "**Target repo:** $REPO"
    else
      echo "**Target repo:** (no GitHub remote — local mode)"
    fi
  fi
  echo
fi
```

Use the CLI to plan the local track operation. The CLI infers all required state (slug, branch naming, collision detection):

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage create-worktree >/dev/null 2>&1 || true

source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh" || { echo "ERROR: could not resolve claude-helpers scripts directory — run /setup-local to (re)install claude-helpers symlinks" >&2; exit 1; }

TRACK_PLAN=$(PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli track plan --mode local --plan-file "$PLAN_FILE" --title "$TITLE" \
  --tracker-path "$PROJECT_ISSUES" --plans-dir "$PLANS_DIR")
PLAN_OK=$(printf '%s' "$TRACK_PLAN" | jq -r '.plan_hash // empty')
if [ -z "$PLAN_OK" ]; then
  echo "ERROR: Failed to plan track (local mode)" >&2
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome failure --failure-class other 2>/dev/null || true
  python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome failure --failure-class other 2>/dev/null || true
  exit 1
fi
```

Now check if the plan signals a duplicate. The plan's `candidate_issues` array lists existing open entries that may overlap:

```bash
if [ -n "$ISSUE_FLAG" ]; then
  # --issue was given: skip the candidate-matching guesswork entirely and use
  # the named entry directly, same intent as picking "Start this entry" below.
  echo "Using --issue $ISSUE_FLAG: starting that entry directly (skipping duplicate detection)."
else
  CANDIDATES=$(printf '%s' "$TRACK_PLAN" | jq '.candidate_issues | length')
  if [ "$CANDIDATES" -gt 0 ]; then
    # Present the matched issues to the user
    # TODO: AskUserQuestion with options: "Start this entry", "Create new entry"
    # For now, create new entry (user can handle duplicates manually)
    echo "Note: $CANDIDATES existing entries may overlap. Review before proceeding."
  fi
fi
```

Apply the plan to create the local issue entry and worktree:

```bash
source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh" || { echo "ERROR: could not resolve claude-helpers scripts directory — run /setup-local to (re)install claude-helpers symlinks" >&2; exit 1; }

TRACK_RESULT=$(printf '%s' "$TRACK_PLAN" | PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli track apply -)
TRACK_OK=$(printf '%s' "$TRACK_RESULT" | jq -r '.success // false')
if [ "$TRACK_OK" != "true" ]; then
  ERROR=$(printf '%s' "$TRACK_RESULT" | jq -r '.error // "Unknown error"')
  STEPS_COMPLETED=$(printf '%s' "$TRACK_RESULT" | jq -r '.steps_completed | join(", ")')
  STEPS_FAILED=$(printf '%s' "$TRACK_RESULT" | jq -r '.steps_failed | join(", ")')
  
  echo "ERROR: track apply failed: $ERROR" >&2
  if [ -n "$STEPS_COMPLETED" ]; then
    echo "  Completed: $STEPS_COMPLETED" >&2
  fi
  if [ -n "$STEPS_FAILED" ]; then
    echo "  Failed: $STEPS_FAILED" >&2
  fi
  
  # Handle partial success (e.g., issue created but worktree failed)
  ISSUE_NUM=$(printf '%s' "$TRACK_RESULT" | jq -r '.issue_number // empty')
  if [ -n "$ISSUE_NUM" ] && [ -n "$STEPS_FAILED" ]; then
    echo "  Issue #$ISSUE_NUM was created but is orphaned (no worktree)." >&2
    echo "  Manual cleanup or retry may be needed." >&2
  fi
  
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome failure --failure-class other 2>/dev/null || true
  python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome failure --failure-class other 2>/dev/null || true
  exit 1
fi
```

Extract the real branch and worktree path from the result (NOT from the plan, which contains placeholder `{issue_number}`):

```bash
ISSUE_NUM=$(printf '%s' "$TRACK_RESULT" | jq -r '.issue_number // empty')
BRANCH=$(printf '%s' "$TRACK_RESULT" | jq -r '.branch // empty')
WORKTREE_PATH=$(printf '%s' "$TRACK_RESULT" | jq -r '.worktree_path // empty')
# main_worktree/worktree_parent are already available as $MAIN_WORKTREE/$WORKTREE_PARENT
# from Project Detection above — do not reassign $PLAN_FILE/$PLANS_DIR here, those names
# are reused for the plan-mode markdown file and the plans/ directory elsewhere in this doc.

if [ -z "$ISSUE_NUM" ] || [ -z "$BRANCH" ] || [ -z "$WORKTREE_PATH" ]; then
  echo "ERROR: Invalid result from track apply (missing required fields)" >&2
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome failure --failure-class other 2>/dev/null || true
  exit 1
fi
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

Activates when `/track-and-start` is called with a ticket ID argument matching `[A-Z]+-\d+` (e.g., `PPS-166`). It looks up the ticket via MCP, uses the tracker's branch name, creates a worktree named from the lowercase ticket ID, writes the standard `github-cache.json` shape, and does not require a GitHub remote. For `ENTRY_MODE=tracker`, plan mode is still required and the plan content becomes the cache `body`. For `ENTRY_MODE=tracker-with-path`, the plan content comes from the resolved `$ARG2` path instead of the live session. This mode is self-contained (like Local Plan Mode): its own resolution, cache write, and handoff, merging back into the shared worktree creation block. The branch name comes from the tracker and must not be renamed.

#### Entry Mode Detection

Entry into this mode (`ENTRY_MODE=tracker` or `ENTRY_MODE=tracker-with-path`) is determined by the shared Entry Mode Dispatch section above.

```bash
TICKET_ID="$ARG1"
```

**For `ENTRY_MODE=tracker-with-path`:** Plan File Resolution's Stage A (Plan File Validation) has already resolved and validated `$ARG2` into `PLAN_FILE` and `PLAN_CONTENT`. Stage B (Title Derivation) is skipped because `TICKET_TITLE` comes from the tracker and must not be overridden.

**For `ENTRY_MODE=tracker`:** Plan mode is still required as in today's flow. `PLAN_CONTENT` is read from the live session (the model's assessment).

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

Run Project Detection from `~/.claude/prompts/worktree-reference.md` to get `MAIN_WORKTREE` and `WORKTREE_PARENT`. Then create the worktree directly (the branch name comes from the tracker and must not be renamed):

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage create-worktree >/dev/null 2>&1 || true

cd "$MAIN_WORKTREE"
WORKTREE_PATH="${WORKTREE_PARENT}/${WORKTREE_DIR}"
```

**Print metadata confirmation** (for `ENTRY_MODE=tracker-with-path`):

*Note: This block also appears in Local Plan Mode, Pivot Detection, and Creating the Issue sections. Keep these blocks in sync.*

```bash
if [ "$ENTRY_MODE" = "tracker-with-path" ]; then
  # Always print path + mtime line (even if CONFIRM_ECHO=0)
  PLAN_MTIME=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$PLAN_FILE" 2>/dev/null || stat -c '%y' "$PLAN_FILE" 2>/dev/null | cut -d' ' -f1,2)
  echo "**Resolved plan file:** \`$PLAN_FILE\` (modified: $PLAN_MTIME)"
  
  # Print other details only if CONFIRM_ECHO != 0
  if [ "$CONFIRM_ECHO" != "0" ]; then
    echo "**Ticket title:** $TICKET_TITLE"
    echo "**Branch:** $BRANCH"
    echo "**Worktree:** $WORKTREE_PATH"
  fi
  echo
fi
```

```bash
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

### Step 4-pre: Explicit `--issue` Override

If `$ISSUE_FLAG` is set, skip auto-detection (Steps 4a–4c) and the interactive Duplicate Detection
prompt entirely — the user already told you the target. This also covers the case where **no
worktree exists yet** for the target issue (auto-detected Pivot Detection only ever looks at the
*current* worktree's linked issue, which doesn't help when the target issue has no worktree at all).

1. **Validate the target exists and is open:**

   ```bash
   if [ -n "$ISSUE_FLAG" ]; then
     if printf '%s' "$ISSUE_FLAG" | grep -qE '^[A-Z]+-[0-9]+$'; then
       echo "ERROR: --issue with a tracker ticket ID ($ISSUE_FLAG) is not yet supported for pivoting — tracker tickets don't have a GitHub issue to pivot into. Use a bare GitHub issue number instead." >&2
       exit 1
     fi
     ISSUE_STATE=$(gh issue view "$ISSUE_FLAG" --repo "$REPO" --json state -q '.state' 2>/dev/null)
     if [ -z "$ISSUE_STATE" ]; then
       echo "ERROR: --issue $ISSUE_FLAG not found in $REPO." >&2
       exit 1
     fi
     if [ "$ISSUE_STATE" != "OPEN" ]; then
       echo "ERROR: --issue $ISSUE_FLAG is not open (state: $ISSUE_STATE) — cannot pivot into a closed issue." >&2
       exit 1
     fi
   fi
   ```

2. **Execute the pivot:** set `EXISTING_ISSUE_NUM="$ISSUE_FLAG"` and jump directly to
   [Duplicate Detection → "Pivot to existing" Flow](#pivot-to-existing-flow) — the same flow used
   when the user picks "Pivot to existing" from the interactive prompt, including its worktree
   creation (so a target issue with no existing worktree gets one). Do not run Steps 4a–4c, and do
   not run the normal Duplicate Detection candidate-matching/`AskUserQuestion` step — `$ISSUE_FLAG`
   already resolved that decision.

3. **If `$ISSUE_FLAG` is not set**, proceed to Step 4a as normal (unchanged).

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

**2. Print metadata confirmation** (for `ENTRY_MODE ∈ {path-arg, tracker-with-path}` and `IN_PLAN_MODE=0`):

*Note: This block also appears in Local Plan Mode, Tracker Ticket Mode, and Creating the Issue sections. Keep these blocks in sync.*

```bash
if ([ "$ENTRY_MODE" = "path-arg" ] || [ "$ENTRY_MODE" = "tracker-with-path" ]) && [ "$IN_PLAN_MODE" = "0" ]; then
  # Always print path + mtime line (even if CONFIRM_ECHO=0)
  PLAN_MTIME=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$PLAN_FILE" 2>/dev/null || stat -c '%y' "$PLAN_FILE" 2>/dev/null | cut -d' ' -f1,2)
  echo "**Resolved plan file:** \`$PLAN_FILE\` (modified: $PLAN_MTIME)"
  
  # Print other details only if CONFIRM_ECHO != 0
  if [ "$CONFIRM_ECHO" != "0" ]; then
    echo "**Title:** $TITLE"
    echo "**Branch:** feature/{issue_number}-<slug>"
    echo "**Worktree:** $WORKTREE_PARENT/{issue_number}-<slug>"
  fi
  echo
fi
```

**3. Replace issue body with the new plan:**

```bash
gh issue edit "$ISSUE_NUM" --repo "$REPO" --body "$(cat <<'EOF'
<new plan content - unmodified>
EOF
)"
```

**4. Update `.claude/github-cache.json` in the current worktree:**

Only update `issue.body` — preserve everything else (`branch`, `issue.number`, `issue.url`, `issue.title`, `issue.state`, and any `pr` section).

```bash
EXISTING=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
# Write to a temp file and mv on success so a jq failure never truncates the existing cache
# (a bare `> github-cache.json` redirect truncates the file before jq runs).
TMP=$(mktemp .claude/github-cache.json.XXXXXX)
printf '%s' "$EXISTING" | jq --arg body "<new plan content>" \
  '.issue.body = $body' > "$TMP" && mv "$TMP" .claude/github-cache.json || rm -f "$TMP"
```

**5. Update project-level `issues.json` cache:**

Update the issue's body in `$CACHE_FILE` so future duplicate detection runs against the current plan.

**6. Output pivot confirmation:**

```
## Pivot Complete!

**Issue #{number}**: {title}
**URL**: {issue-url}

- Old plan archived as comment on the issue
- Issue body updated with new plan
- Local caches updated
```

**7. Close telemetry and conditionally call `ExitPlanMode`:**

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome success 2>/dev/null || true
```

**If `IN_PLAN_MODE=1`** (user is in a live plan-mode session):
Call `ExitPlanMode` — since the user is already in the correct worktree, they can approve the plan and begin implementing immediately. The plan file content is the new plan (it triggered `/track-and-start`).

**If `IN_PLAN_MODE=0`** (user passed a path-arg or tracker-with-path, not in live plan mode):
Skip `ExitPlanMode` and print the standard handoff block instead:

```
## Ready to implement!

**Plan:** `<plan-file-path>`
**Branch:** `<branch-name>`
**Worktree:** `<worktree-path>`

### Start implementation:

cd <worktree-path> && claude "/implement-with-haiku"
```

**Do NOT** proceed to Steps 5-9 after a successful pivot.

### Known Trade-offs

- **Branch name may drift**: After a pivot, the branch name (e.g., `feature/42-old-slug`) may no longer match the new plan. This is acceptable — the issue is the source of truth, and renaming branches in worktrees is disruptive.
- **Multiple pivots**: Each pivot adds a "Superseded Plan" comment, creating an audit trail. This is intentional.


## Duplicate Detection

**Note:** If pivot detection (Step 4) already resolved the overlap by updating the current issue, Steps 5-9 are skipped entirely and this section does not apply. **Likewise, if `$ISSUE_FLAG` is set, Step 4-pre already jumped straight to the ["Pivot to existing" Flow](#pivot-to-existing-flow) below — the candidate-matching logic and `AskUserQuestion` in this section never run.**

The `track plan` CLI step has already performed duplicate detection and populated the plan's `candidate_issues` array with open issues that may overlap. This section documents how to present them to the user.

### How to Check

The plan JSON's `candidate_issues` field is already populated:

```bash
CANDIDATES=$(printf '%s' "$TRACK_PLAN" | jq '.candidate_issues')
CANDIDATE_COUNT=$(printf '%s' "$CANDIDATES" | jq 'length')
```

Each candidate issue includes:
- `number`: GitHub issue number
- `title`: Issue title
- `url`: GitHub issue URL
- `state`: `"open"` (only open issues are returned)
- `labels`: Array of label strings
- `assignee`: Assignee if set, or null

The CLI compares the plan title and content against each open issue's title and labels, looking for:
- **Title similarity**: Keywords in common, same feature area, same component
- **Scope overlap**: The plan addresses something an existing issue already covers (fully or partially)
- **Subset/superset**: The plan is a narrower or broader version of an existing issue

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

If the user chooses to pivot to an existing issue, execute **steps 1 and 3 of
[Pivot Detection Step 4d](#step-4d-execute-pivot)** (archive-then-replace — step 2 there is the
metadata confirmation print, which does not apply to this flow; same ordering guarantee, same
abort-on-comment-failure rule) against `$EXISTING_ISSUE_NUM` — fetching its body first if not
already available (`gh issue view "$EXISTING_ISSUE_NUM" --repo "$REPO" --json body -q '.body'`).

Then, instead of 4d's steps 4–7 (this pivot targets a duplicate-detection match, not the current
worktree's issue):

1. **Skip issue creation** — use the existing issue number for branch naming: `{type}/{existing-issue#}-{slug}`
2. **Update `$CACHE_FILE`** with the new issue body so future duplicate detection runs against the current plan
3. **Continue with worktree creation** and handoff as normal, using the existing issue's URL and number

