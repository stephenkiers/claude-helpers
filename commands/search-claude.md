---
description: Search prior Claude Code conversation transcripts across all projects. Use when the user says /search-claude or wants to find and resume a past Claude conversation by topic, keyword, or remembered phrase.
allowed-tools: Bash(~/.claude/scripts/search-claude.sh:*), Bash(grep:*), Bash(find:*), Bash(stat:*), Bash(date:*), Bash(sort:*), Bash(head:*), Bash(wc:*), Bash(basename:*), Bash(dirname:*), Bash(printf:*), Bash(cut:*)
---

# Search Claude Transcripts

Search prior Claude Code conversation transcripts and return copy-pasteable resume commands.

Invokes `~/.claude/scripts/search-claude.sh` to run a deterministic, portable search (no `rg`
dependency, works in bash and zsh), then presents the results with judgment about what to surface.

## Usage

```
/search-claude <query-words...> [--days N] [--all]
```

- `<query-words>` — Search for all these words (AND matching, case-insensitive). Wrap in quotes for
  exact phrase: `/search-claude "quoted phrase"` matches only the exact phrase verbatim.
- `--days N` — Search only the last N days (default: 30). Must be a positive integer.
- `--all` — Search full history (no time limit).

## Examples

```
/search-claude tray icon color          # Find sessions with all three words
/search-claude "exact phrase" --all     # Find exact phrase in all history
/search-claude bug --days 7             # Find "bug" in last week
```

## How it works

The script emits pipe-delimited records with: mtime, timestamp, cwd, session_id, kind
(session|subagent), first_prompt, snippet. Present top-level session hits as primary results;
include subagent hits only when they add signal for the query (this is a judgment call — feel free
to omit noisy subagent rows). Always include the parent session id so the user can resume.

When a query returns nothing, say plainly why (too narrow? too old?) and suggest ways to widen the
search.

```bash
#!/usr/bin/env bash

ARGS="$ARGUMENTS"
PROJECTS_DIR="${HOME}/.claude/projects"

# --- Parse arguments ---
QUERY_WORDS=()
DAYS_FLAG=()
ALL_FLAG=()

for arg in $ARGS; do
  case "$arg" in
    --all)
      ALL_FLAG=( "--all" )
      ;;
    --days)
      # The next arg is the value; we'll pass both through
      DAYS_FLAG=( "--days" )
      ;;
    *)
      QUERY_WORDS+=( "$arg" )
      ;;
  esac
done

# Handle --days value extraction (the next word after --days in the original args)
if [ ${#DAYS_FLAG[@]} -gt 0 ]; then
  # Find --days in ARGS and get the next token
  next_is_value=0
  for arg in $ARGS; do
    if [ "$next_is_value" -eq 1 ]; then
      DAYS_FLAG+=( "$arg" )
      next_is_value=0
    fi
    if [ "$arg" = "--days" ]; then
      next_is_value=1
    fi
  done
fi

# --- Invoke the script ---
if ! ~/.claude/scripts/search-claude.sh "${QUERY_WORDS[@]}" "${DAYS_FLAG[@]}" "${ALL_FLAG[@]}" > /tmp/search-results.$$ 2>&1; then
  # Script failed (e.g., bad --days value)
  cat /tmp/search-results.$$
  rm -f /tmp/search-results.$$
  exit 1
fi

SCRIPT_OUTPUT=$(cat /tmp/search-results.$$)
rm -f /tmp/search-results.$$

# --- Present results ---
if [ -z "$SCRIPT_OUTPUT" ]; then
  # No matches found
  if echo "$ARGS" | grep -q "\--all"; then
    echo "No matches found for: $(echo "${QUERY_WORDS[@]}" | head -c 60)"
    echo ""
    echo "Try a different search term or check the transcript directory:"
    echo "  ls ~/.claude/projects/"
  else
    echo "No matches in last 30 days for: $(echo "${QUERY_WORDS[@]}" | head -c 60)"
    echo ""
    echo "Try one of:"
    echo "  /search-claude ${QUERY_WORDS[@]} --all         (search full history)"
    echo "  /search-claude ${QUERY_WORDS[0]} --days 7    (search last week instead)"
  fi
  exit 0
fi

echo "Search results for: $(echo "${QUERY_WORDS[@]}" | head -c 60)"
if ! echo "$ARGS" | grep -q "\--all"; then
  if ! echo "$ARGS" | grep -q "\--days"; then
    echo "(last 30 days; use --all to search full history)"
  fi
fi
echo ""

# Parse and present each result
rank=1
last_session=""
while IFS='|' read -r mtime ts cwd session_id kind first_prompt snippet; do
  # Only show subagent results if they're notably different from the parent
  if [ "$kind" = "subagent" ]; then
    if [ "$session_id" = "$last_session" ]; then
      continue  # Skip duplicate parent in output (subagent already shown)
    fi
  fi
  last_session="$session_id"

  printf '%d. %s  %s\n' "$rank" "$ts" "$cwd"
  printf '   Session: %s (%s)\n' "$session_id" "$kind"
  printf '   Prompt:  "%.60s"\n' "$first_prompt"
  printf '   Match:   "%.60s"\n' "$snippet"
  printf '   Resume:  cd %s && claude --resume %s\n' "$cwd" "$session_id"
  echo ""

  rank=$((rank + 1))
done <<< "$SCRIPT_OUTPUT"

if [ "$rank" -eq 1 ]; then
  echo "(No results to display.)"
fi
```
