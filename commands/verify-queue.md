---
name: verify-queue
description: Per-repository batched verification queue — store pending rulings from action-plans in the repo's worktrees/ folder, sync and drain them, and record results back to action-plan.md
---

# Verify Queue — Per-Repository Verification Workflow

Batched verification queue for pending rulings — **per-repository**, stored at `<repo>/worktrees/verify-queue.jsonl`
beside `issues.json`. Collects *your-call* and *measurement* findings from `action-plan.md` files, surfaces them
for review, and records your decisions back to the plans.

**Must run from inside a git repo** (or one of its worktrees) — errors outside a git repository.

## Queue File & Helpers

Every bash step that touches the queue starts with Project Detection (from `~/.claude/prompts/worktree-reference.md`)
to set `WORKTREE_PARENT`, then defines the queue location:

```bash
# 1. Repo identity
REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || echo "")

# 2. Main worktree (first entry is always main)
MAIN_WORKTREE=$(git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2)

# 3. Detect worktree parent from existing worktrees
SECOND_WORKTREE=$(git worktree list --porcelain | grep '^worktree ' | sed -n '2p' | cut -d' ' -f2)
if [ -n "$SECOND_WORKTREE" ]; then
  WORKTREE_PARENT=$(dirname "$SECOND_WORKTREE")
else
  WORKTREE_PARENT="${MAIN_WORKTREE}/worktrees"
fi

# 4. Queue file (per-repo, lives beside issues.json)
QUEUE="${WORKTREE_PARENT}/verify-queue.jsonl"
touch "$QUEUE"
```

The queue file is **untracked** (like `issues.json`) and lives in the repo's `worktrees/` folder. It is
scoped to a single repository and cleaned up with that repo.

### Queue Row Schema (JSONL format)

Each line is a JSON object with exactly these fields:

```json
{
  "id": "<repo-key>/<review-id>::<finding-slug>",
  "status": "open",
  "kind": "your-call",
  "summary": "Short human-readable description of the finding",
  "command": "",
  "plan": "/absolute/path/to/action-plan.md",
  "result": ""
}
```

**Field definitions:**
- `id` (string): Globally unique identifier: `<repo-key>/<review-id>::<finding-slug>`. Repo-key is
  derived from `gh repo view` (e.g., `acme-inc/claude-helpers`), and review-id is the review directory
  name (e.g., `feature-x-abc123-1723456789`). Dedup key on `add_row` — if an id already exists in the
  queue, skip it (re-sync is idempotent).
- `status` (string): One of `open`, `done`, or `ignored`. Only `open` rows surface on drain.
- `kind` (string): One of `your-call` (awaiting human judgment) or `measurement` (awaiting a test run).
- `summary` (string): Human description of the finding. Copy from action-plan.md directly.
- `command` (string): For `measurement` rows, the concrete runnable command to resolve the question;
  empty string for `your-call` rows.
- `plan` (string): Absolute path to the source `action-plan.md` file.
- `result` (string): Filled when the row is marked `done` with `--result "…"`; otherwise empty string.

### Helper: `add_row` (dedup append)

Appends a new row to the queue, but skips if an id already exists (making re-sync idempotent and
allowing previously-ignored rows to stay ignored):

```bash
add_row() {
  local id="$1" status="$2" kind="$3" summary="$4" command="$5" plan="$6" result="$7"
  
  # Skip if this id already exists in queue.
  # Fixed-string match (-qF): ids contain `/`, `::`, and can carry `.` (repo owner/name),
  # all of which are regex metacharacters — grep -F sidesteps the escaping entirely.
  if grep -qF "\"id\":\"$id\"" "$QUEUE"; then
    return 0
  fi
  
  # Append new row (let jq own the escaping via --arg)
  jq -n \
    --arg id "$id" \
    --arg status "$status" \
    --arg kind "$kind" \
    --arg summary "$summary" \
    --arg command "$command" \
    --arg plan "$plan" \
    --arg result "$result" \
    '{id: $id, status: $status, kind: $kind, summary: $summary, command: $command, plan: $plan, result: $result}' \
    >> "$QUEUE"
}
```

### Helper: `set_status` (read-modify-write via temp file)

Updates the status of a row (and optionally the result field) and writes the queue back:

```bash
set_status() {
  local id="$1" new_status="$2" new_result="${3:-}"
  
  # Create temp file in same directory (ensures atomic rename)
  TMP="$(mktemp "$(dirname "$QUEUE")/verify-queue.XXXXXX")"
  
  jq -r \
    --arg id "$id" \
    --arg status "$new_status" \
    --arg result "$new_result" \
    'if .id == $id then .status = $status | .result = $result else . end' \
    "$QUEUE" > "$TMP" && mv "$TMP" "$QUEUE" || { rm -f "$TMP"; return 1; }
}
```

## Dispatch on Arguments

Every path runs **sync first** (unless sync has already run this invocation). Then:

- **No arguments** → sync + drain (view all pending rulings)
- `sync` → scan action-plans, discover new rulings, add to queue
- `done <id> [--result "…"]` → mark row done, optionally record result, write back to action-plan
- `defer <id>` → leave row open (no change to status; used for "will do this later")
- `ignore <id>` → mark row ignored (never resurfaces on re-sync)
- `done all` → mark all currently-open rows as done (no result recorded)
- `ignore all` → mark all currently-open rows as ignored

## Sync: Discover and Queue Rulings

**Step 1 — Repo-scoped candidate discovery:**

```bash
# Derive REPO_KEY (identity, not directory path)
PROJECT_ROOT=$(git rev-parse --show-toplevel)
REPO_KEY=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null | tr '/' '-')
[ -z "$REPO_KEY" ] && REPO_KEY=$(basename "$PROJECT_ROOT")

# Find all action-plans for this repo
REVIEWS="$HOME/.claude/reviews/${REPO_KEY}"
[ -d "$REVIEWS" ] || { echo "No reviews for this repo ($REPO_KEY) yet."; exit 0; }

# Candidates: action-plan.md files with pending rulings, sorted by recency
find "$REVIEWS" -name action-plan.md 2>/dev/null -print0 \
  | xargs -0 ls -t 2>/dev/null \
  | while read -r plan_file; do
      grep -qE 'pending (your call|measurement)' "$plan_file" && echo "$plan_file"
    done
```

**Step 2 — Extract rulings from each plan:**

For each `action-plan.md` file, parse the sections (e.g., "Needs Your Call" or "Needs Measurement").
For each pending ruling, extract:
- `finding-slug`: a kebab-case slug of the finding title
- `summary`: the finding description (copy from the plan, use judgment to shorten/clarify)
- `kind`: `your-call` or `measurement` (infer from section heading or the `pending …` marker)
- `command`: for `measurement` rows, draft or copy the concrete runnable command from the plan;
  for `your-call` rows, use empty string
- `review-id`: the review directory name (directory between `reviews/<repo-key>/` and `action-plan.md`)
- `plan`: the absolute path to the action-plan.md file

Build `id` as `<repo-key>/<review-id>::<finding-slug>` — globally unique.

**Step 3 — Append to queue via `add_row`:**

For each extracted ruling, call `add_row "$id" "open" "$kind" "$summary" "$command" "$plan" ""`.
The dedup logic in `add_row` ensures re-sync is idempotent and preserves prior `ignore` decisions.

## Drain View (no args after sync)

After sync, display all open rows, grouped by kind:

```bash
# Group by kind: "needs a run" (measurement) first, then "needs a call" (your-call)
if [ ! -s "$QUEUE" ]; then
  echo "✓ Nothing to verify."
  exit 0
fi

echo "# Pending Rulings"
echo ""

# Needs a run (measurement rows)
MEASUREMENTS=$(jq -r 'select(.status == "open" and .kind == "measurement") | @json' "$QUEUE")
if [ -n "$MEASUREMENTS" ]; then
  echo "## Needs a run (test/measurement)"
  echo ""
  echo "$MEASUREMENTS" | jq -r '
    "- [\(.id)](\(.plan)) — \(.summary)\n  Command: `\(.command)`"
  '
  echo ""
fi

# Needs a call (your-call rows)
YOUR_CALLS=$(jq -r 'select(.status == "open" and .kind == "your-call") | @json' "$QUEUE")
if [ -n "$YOUR_CALLS" ]; then
  echo "## Needs your call (judgment)"
  echo ""
  echo "$YOUR_CALLS" | jq -r '
    "- [\(.id)](\(.plan)) — \(.summary)"
  '
  echo ""
fi

# Summary count
TOTAL=$(jq '[select(.status == "open")] | length' "$QUEUE")
echo "Total pending: **$TOTAL**"
```

## Disposition Verbs

### `done <id> [--result "…"]`

Marks a row as `done`, optionally records a result, and writes the result back to the source action-plan:

```bash
ID="$2"
RESULT=""

# Parse --result flag if present
if [ "$3" = "--result" ] && [ -n "$4" ]; then
  RESULT="$4"
fi

# Update queue
set_status "$ID" "done" "$RESULT"

# Write result back to action-plan (read the plan, find the Ruling line, replace pending marker)
# Extract plan path from queue
PLAN=$(jq -r --arg id "$ID" 'select(.id == $id) | .plan' "$QUEUE" | head -1)
if [ -z "$PLAN" ] || [ ! -f "$PLAN" ]; then
  echo "ERROR: Could not find plan file for $ID"
  exit 1
fi

echo "Marked $ID as done"
[ -n "$RESULT" ] && echo "Result: $RESULT"
echo "Now write the ruling back to: $PLAN"
```

**Then write the ruling back into the plan (judgment step, not blind sed).** Open `$PLAN`, locate the
`Ruling:` line belonging to *this* finding (match on the finding's `summary` / slug — a plan usually
holds several rulings, so a global replace would corrupt the others), and replace its `pending your
call` / `pending measurement` marker with the decision, including `$RESULT` when present. For example
a line `Ruling: pending your call` becomes `Ruling: done — <result>`. When exactly one pending marker
of the matching kind remains in the file, a scoped replacement is safe as a fallback:

```bash
# Fallback ONLY when a single matching pending marker remains in $PLAN:
# sed -i.bak "s/Ruling: pending your call/Ruling: done — ${RESULT}/" "$PLAN" && rm -f "$PLAN.bak"
```

This write-back is what keeps `action-plan.md` (the gut-check instrument of record) in sync with the
queue — so a re-sync no longer re-discovers a ruling you've already resolved.

### `defer <id>`

Leaves the row with status `open` (i.e., no change). Use when you want to handle it later:

```bash
ID="$2"
echo "Deferred $ID (will resurface on next sync)"
```

### `ignore <id>`

Marks a row as `ignored` — it will never resurface on re-sync:

```bash
ID="$2"
set_status "$ID" "ignored" ""
echo "Ignored $ID (will not appear again)"
```

### `done all`

Marks all currently-open rows as `done`:

```bash
# Count open rows BEFORE flipping them — afterwards the select would return 0.
COUNT=$(jq -s '[.[] | select(.status == "open")] | length' "$QUEUE")
jq -r 'select(.status == "open") | .id' "$QUEUE" | while read -r id; do
  set_status "$id" "done" ""
done
echo "Marked all $COUNT rows as done"
```

### `ignore all`

Marks all currently-open rows as `ignored`:

```bash
# Count open rows BEFORE flipping them — afterwards the select would return 0.
COUNT=$(jq -s '[.[] | select(.status == "open")] | length' "$QUEUE")
jq -r 'select(.status == "open") | .id' "$QUEUE" | while read -r id; do
  set_status "$id" "ignored" ""
done
echo "Ignored all $COUNT rows (will not reappear)"
```

## Notes

- **Queue is per-repository:** Stored in `<repo>/worktrees/verify-queue.jsonl`, scoped to one repo.
  Untracked, like `issues.json`. Cleans up when the repo is removed.
- **IDs are globally unique:** Built from repo-key + review-id + finding-slug. Same finding across
  multiple reviews has different IDs (different review-id components).
- **Dedup makes re-sync idempotent:** Running `sync` multiple times against the same action-plans does
  not create duplicate rows. Previously-`ignored` rows stay ignored.
- **No migration needed:** This is the first per-repo queue design. No global state to migrate.
- **JSON safety:** All jq operations use `--arg` and `--argjson` flags; shell variables never interpolate
  into JSON string literals (ensures proper escaping of quotes, backslashes, newlines).
