---
description: Search prior Claude Code conversation transcripts across all projects. Use when the user says /search-claude or wants to find and resume a past Claude conversation by topic, keyword, or remembered phrase.
allowed-tools: Bash(rg:*), Bash(find:*), Bash(jq:*), Bash(stat:*), Bash(date:*), Bash(sort:*), Bash(head:*), Bash(awk:*), Bash(sed:*), Bash(wc:*), Bash(basename:*), Bash(dirname:*), Bash(printf:*), Bash(cut:*), Bash(tr:*)
---

# Search Claude Transcripts

Search prior Claude Code conversation transcripts and return copy-pasteable resume commands.

Run this bash script, then output a one-line preamble like "Searched last 30 days across N project dirs:" followed by the script output verbatim. Nothing after.

```bash
#!/usr/bin/env bash
set -euo pipefail

ARGS="$ARGUMENTS"

# --- Parse arguments ---
QUERY=""
DAYS=30

args_array=($ARGS)
i=0
while [ $i -lt ${#args_array[@]} ]; do
  arg="${args_array[$i]}"
  case "$arg" in
    --all)
      DAYS=10950  # ~30 years
      ;;
    --days)
      i=$((i + 1))
      DAYS="${args_array[$i]}"
      ;;
    --*)
      # unknown flag, skip
      ;;
    *)
      QUERY="${QUERY:+$QUERY }$arg"
      ;;
  esac
  i=$((i + 1))
done

QUERY="${QUERY# }"

# --- Usage guard ---
if [ -z "$QUERY" ]; then
  echo "Usage: /search-claude <query> [--days N] [--all]"
  echo ""
  echo "  <query>    Keywords to search for (literal string match)"
  echo "  --days N   Search last N days (default: 30)"
  echo "  --all      Search full history (no time limit)"
  echo ""
  echo "Example: /search-claude staging --all"
  exit 0
fi

PROJECTS_DIR="$HOME/.claude/projects"

if [ ! -d "$PROJECTS_DIR" ]; then
  echo "No Claude projects directory found at $PROJECTS_DIR"
  exit 1
fi

# --- Grep for matching files ---
# NOTE: rg is a shell function in Claude Code (not a PATH binary), so
# `find ... | xargs rg` fails silently ("xargs: rg: No such file or directory").
# Use rg's own recursive walk + globs instead, then filter by mtime/size in bash.
mapfile -t RAW_MATCHES < <(rg -i -l -F \
  --max-filesize 50M \
  -g '*.jsonl' \
  -g '!*/subagents/*' \
  -- "$QUERY" "$PROJECTS_DIR" 2>/dev/null || true)

# Apply time window (mtime) in bash, since rg can't filter by age.
declare -a MATCHES
if [ "$DAYS" -lt 10000 ]; then
  now=$(date +%s)
  cutoff=$(( now - DAYS * 86400 ))
else
  cutoff=0
fi
for f in "${RAW_MATCHES[@]}"; do
  [ -n "$f" ] || continue
  mt=$(stat -f %m "$f" 2>/dev/null || echo 0)
  if [ "$mt" -ge "$cutoff" ]; then
    MATCHES+=("$f")
  fi
done

MATCH_COUNT=${#MATCHES[@]}

if [ "$MATCH_COUNT" -eq 0 ]; then
  if [ "$DAYS" -lt 10000 ]; then
    echo "No matches in last ${DAYS} days. Try --all or a different query."
  else
    echo "No matches found for: $QUERY"
  fi
  exit 0
fi

# --- Extract metadata and rank by mtime ---
declare -a RESULTS

for f in "${MATCHES[@]}"; do
  mtime=$(stat -f %m "$f" 2>/dev/null || echo 0)

  session_id=$(basename "$f" .jsonl)

  # cwd: first line with a .cwd field
  cwd=$(jq -r 'select(.cwd != null) | .cwd' "$f" 2>/dev/null | head -1)
  if [ -z "$cwd" ]; then
    # decode from directory name: ~/.claude/projects/-Users-foo-bar -> /Users/foo/bar
    dir_encoded=$(basename "$(dirname "$f")")
    cwd=$(printf '%s' "$dir_encoded" | sed 's|^-|/|; s|-|/|g')
  fi

  # first user prompt (string content only)
  first_prompt=$(jq -r '
    select(.type == "user") |
    .message.content |
    if type == "string" then .
    elif type == "array" then
      (map(select(.type == "text") | .text) | first) // ""
    else "" end
  ' "$f" 2>/dev/null | head -1 | head -c 80)
  if [ -z "$first_prompt" ]; then
    first_prompt="(no user prompt yet)"
  fi

  # match snippet: first line containing query, flattened to text
  match_snippet=$(rg -i -m 1 --max-filesize 50M -F -- "$QUERY" "$f" 2>/dev/null | \
    jq -r '(.message.content // .) | if type == "string" then . elif type == "array" then (map(select(.type == "text") | .text) | join(" ")) else tostring end' 2>/dev/null | \
    head -c 120 || echo "")
  if [ -z "$match_snippet" ]; then
    match_snippet="(match in non-text field)"
  fi

  # ISO-8601 timestamp from mtime
  ts=$(date -r "$mtime" '+%Y-%m-%d %H:%M' 2>/dev/null || date -d "@$mtime" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "unknown")

  RESULTS+=("${mtime}|${ts}|${cwd}|${session_id}|${first_prompt}|${match_snippet}")
done

# --- Sort by mtime descending, take top 10 ---
mapfile -t SORTED < <(printf '%s\n' "${RESULTS[@]}" | sort -t'|' -k1 -rn | head -10)

SHOWN=${#SORTED[@]}
WINDOW_DESC="${DAYS} days"
if [ "$DAYS" -ge 10000 ]; then
  WINDOW_DESC="full history"
fi

echo "Found ${MATCH_COUNT} match(es) (showing top ${SHOWN}, ${WINDOW_DESC}). Use --all for full history."
echo ""

rank=1
for entry in "${SORTED[@]}"; do
  IFS='|' read -r _mtime ts cwd session_id first_prompt match_snippet <<< "$entry"

  printf '%d. %s  %s\n' "$rank" "$ts" "$cwd"
  printf '   Prompt: "%s"\n' "$first_prompt"
  printf '   Match:  "%s"\n' "$match_snippet"
  printf '   Resume: cd %s && claude --resume %s\n' "$cwd" "$session_id"
  echo ""

  rank=$((rank + 1))
done
```
