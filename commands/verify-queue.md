---
name: verify-queue
description: Drain the batched verification queue — the pending measurements and decisions accumulated across expert-review action plans. Run with no args to see the checklist; `done|defer|ignore <id>` to disposition an item; `sync` to refresh from action plans.
---

# Verify Queue — Batched Post-Review Verification

Every `/expert-review` triage writes an `action-plan.md` whose *Needs measurement* and *Needs you*
items carry `**Ruling**: _(pending …)_` lines — concrete things a human still has to run or rule on.
Left alone, those lines are scattered across dozens of review directories, and `/cleanup` ends up
interrogating you about them one merge at a time.

This command turns that per-merge friction into **one drainable queue**. Items accumulate as you
work; you knock out a batch when you're in the headspace for it — not when you're trying to close a
worktree.

## The queue file

`~/.claude/reviews/verify-queue.jsonl` — one JSON object per line. Cheap to append, cheap to tick
off. Each row is **self-sufficient** (carries its own summary + command) so it stays actionable even
if the source action-plan is later removed; the `plan` path is the link back for full detail
(options, resolves-via thresholds).

```json
{"id":"<repo-key>/<review-id>::<finding-slug>","plan":"/abs/path/action-plan.md","type":"measurement","summary":"one-line what to check","command":"<runnable cmd, or empty for decisions>","added":"YYYY-MM-DD","status":"open","result":""}
```

- **`id`** — stable: `<repo-key>/<review-id>::<kebab-slug-of-finding-title>`. Sync dedups on it, so
  re-running never double-enqueues.
- **`type`** — `measurement` (needs a run/number) or `decision` (needs your call).
- **`status`** — `open` (default, shows in the drain view) · `done` (resolved) · `ignored`
  (dispositioned, never surfaces again).

## Dispatch on `$ARGUMENTS`

- **empty** → **Drain view** (below)
- **`sync`** → **Sync** (below), then report how many rows were added
- **`done <id> [--result "…"]`** → set that row's `status` to `done`; if `--result` given, write it
  into both the JSONL `result` field and the source action-plan's `Ruling:` line (see *Write-back*)
- **`defer <id>`** → set `status` to `open` (explicit "keep it in the batch" — the no-op that stops
  cleanup from re-asking)
- **`ignore <id>`** → set `status` to `ignored`
- **`done|ignore all`** → apply to every currently-open row (use sparingly)

Always run **Sync** first (it is idempotent and cheap) so the view/disposition acts on current data.

## Sync

Find every pending ruling across all reviews and enqueue any not already tracked.

**Step 1 — list candidate plans (bash):**

```bash
QUEUE="$HOME/.claude/reviews/verify-queue.jsonl"
touch "$QUEUE"
REVIEWS="$HOME/.claude/reviews"

# action-plans that still have unfilled rulings, newest first
find "$REVIEWS" -name action-plan.md 2>/dev/null -print0 \
  | xargs -0 ls -t 2>/dev/null \
  | while read -r f; do
      grep -qE 'pending (your call|measurement)' "$f" && echo "$f"
    done
```

**Step 2 — for each listed plan, extract its pending items (you, reading the file):**

Read each plan and pull one queue row per unfilled ruling. Do **not** grep-and-dump — the summary and
command need judgment:

- **`id`** = `<repo-key>/<review-id>` (the two path segments under `~/.claude/reviews/`) + `::` +
  a kebab slug of the finding's title.
- **`type`** = `measurement` if the ruling reads `pending measurement`, else `decision`.
- **`summary`** = the finding title compressed to one actionable line ("confirm p95 drop >20% after
  cache change"), not the whole paragraph.
- **`command`** = for a *measurement*, the concrete command from the finding's `Command:` field,
  verbatim; for a *decision*, empty string.
- **`added`** = today's date (`date +%Y-%m-%d`).
- **`status`** = `open`, **`result`** = `""`.

**Step 3 — append only new ids (bash, per row):**

```bash
add_row() {  # args: id plan type summary command added
  local id="$1" plan="$2" typ="$3" summ="$4" cmd="$5" added="$6"
  # dedup: skip if this id already exists in the queue (any status)
  if grep -qF "\"id\":\"$id\"" "$QUEUE"; then return 0; fi
  jq -cn --arg id "$id" --arg plan "$plan" --arg type "$typ" \
        --arg summary "$summ" --arg command "$cmd" --arg added "$added" \
        '{id:$id, plan:$plan, type:$type, summary:$summary, command:$command, added:$added, status:"open", result:""}' \
    >> "$QUEUE"
}
```

Dedup is by exact `id` substring; because ids are stable, a re-sync of an already-tracked finding is
a no-op — including one you previously `ignored` (it stays ignored, never resurfacing).

Report: `synced | new: {n} | open total: {m}`.

## Drain view (no args)

```bash
QUEUE="$HOME/.claude/reviews/verify-queue.jsonl"
[ -s "$QUEUE" ] || { echo "Verify queue empty — nothing to check."; exit 0; }
echo "=== NEEDS A RUN (measurement) ==="
jq -r 'select(.status=="open" and .type=="measurement") | "• [\(.id)]\n    \(.summary)\n    $ \(.command)\n    plan: \(.plan)"' "$QUEUE"
echo ""
echo "=== NEEDS A CALL (decision) ==="
jq -r 'select(.status=="open" and .type=="decision") | "• [\(.id)]\n    \(.summary)\n    plan: \(.plan)"' "$QUEUE"
echo ""
echo "open: $(jq -rs 'map(select(.status=="open")) | length' "$QUEUE") | done: $(jq -rs 'map(select(.status=="done")) | length' "$QUEUE") | ignored: $(jq -rs 'map(select(.status=="ignored")) | length' "$QUEUE")"
```

Present the two groups as a checklist. For each item offer the natural next step: run the command
(measurement) or make the call (decision), then `done`. If a listed `plan` file no longer exists,
show the row's own `summary`/`command` and tag it `(plan archived)` — the row is still actionable.

## Disposition (`done` / `defer` / `ignore`)

Update a row's status in place. `jq` can't edit JSONL streams atomically, so rewrite via a temp file.

```bash
QUEUE="$HOME/.claude/reviews/verify-queue.jsonl"
set_status() {  # args: id new_status [result]
  local id="$1" st="$2" res="${3:-}"
  local tmp; tmp=$(mktemp "${QUEUE}.XXXXXX")
  jq -c --arg id "$id" --arg st "$st" --arg res "$res" '
    if .id == $id then .status = $st | (if $res != "" then .result = $res else . end) else . end
  ' "$QUEUE" > "$tmp" && mv "$tmp" "$QUEUE" || rm -f "$tmp"
}
```

- `done <id> [--result "…"]` → `set_status "$id" done "$result"`
- `defer <id>` → `set_status "$id" open` (keeps it batched; the point is it won't re-prompt at cleanup)
- `ignore <id>` → `set_status "$id" ignored`

### Write-back (done with --result)

When `done` carries `--result`, also update the source action-plan so the plan and queue stay
consistent. Find the finding by its title in the `plan` file and replace its
`**Ruling**: _(pending …)_` line with `**Ruling**: <result>`. If the plan file is gone, skip
write-back silently — the JSONL `result` is the record of last resort.

## Notes

- **Idempotent.** `sync` and every disposition are safe to re-run. Ids are stable, dedup is by id.
- **Persistence assumption.** Rows link to action-plans but never depend on them — the summary and
  command are copied in, so an archived review doesn't orphan a queue item.
- **Paired with `/cleanup`.** Cleanup runs `sync` at the end and, if the just-merged review left new
  items, asks a single `done | defer | ignore` for the batch — non-blocking. This command is where
  you drain the accumulated `defer`red items later.
