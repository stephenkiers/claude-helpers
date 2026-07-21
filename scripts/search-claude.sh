#!/bin/bash
# shellcheck shell=bash
set -u

# Search Claude Code transcripts for matching sessions.
# Emits pipe-delimited records: mtime|timestamp|cwd|session_id|kind|first_prompt|snippet

# Environment variables (all optional, with sensible defaults):
#   CLAUDE_PROJECTS_DIR   Root directory for session transcripts (default: ~/.claude/projects)
#   CLAUDE_CODE_SESSION_ID Session to exclude from results (default: empty, no exclusion)
#   SEARCH_CLAUDE_LIMIT   Max results to return (default: 10)

# --- Configuration ---
PROJECTS_DIR="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"
EXCLUDE_SESSION="${CLAUDE_CODE_SESSION_ID:-}"
LIMIT="${SEARCH_CLAUDE_LIMIT:-10}"

# --- Parse arguments ---
QUERY_WORDS=()
DAYS=30
ALL_MODE=0

# Process all arguments without using indirect expansion (zsh-compatible)
while [ $# -gt 0 ]; do
  arg="$1"
  case "$arg" in
    --all)
      ALL_MODE=1
      shift
      ;;
    --days)
      shift
      if [ $# -eq 0 ]; then
        echo "error: --days requires a value" >&2
        exit 1
      fi
      DAYS="$1"
      # Validate that DAYS is a positive integer
      if ! [[ "$DAYS" =~ ^[0-9]+$ ]] || [ "$DAYS" -eq 0 ]; then
        echo "error: --days must be a positive integer" >&2
        exit 1
      fi
      shift
      ;;
    --*)
      # Unknown flag, skip silently
      shift
      ;;
    *)
      QUERY_WORDS+=("$arg")
      shift
      ;;
  esac
done

# --- Validate query ---
if [ ${#QUERY_WORDS[@]} -eq 0 ]; then
  echo "usage: search-claude.sh <query> [--days N] [--all]" >&2
  exit 1
fi

# --- Validate projects directory ---
if [ ! -d "$PROJECTS_DIR" ]; then
  exit 0
fi

# --- Determine time cutoff ---
if [ "$ALL_MODE" -eq 1 ]; then
  CUTOFF_TS=0
else
  NOW=$(date +%s)
  CUTOFF_TS=$((NOW - DAYS * 86400))
fi

# --- Helper: extract file mtime ---
get_mtime() {
  local filepath="$1"
  stat -f %m "$filepath" 2>/dev/null || stat -c %Y "$filepath" 2>/dev/null || echo 0
}

# --- Helper: extract cwd ---
extract_cwd() {
  local filepath="$1"
  local cwd
  cwd=$(grep -m 1 '"cwd":"[^"]*"' "$filepath" 2>/dev/null | grep -o '"cwd":"[^"]*"' | cut -d'"' -f4)
  if [ -n "$cwd" ]; then
    printf '%s' "$cwd"
    return
  fi
  # Fallback: decode from directory name
  local dir_encoded
  dir_encoded=$(basename "$(dirname "$filepath")")
  cwd=$(printf '%s' "$dir_encoded" | sed 's|^-|/|; s|-|/|g')
  printf '%s' "$cwd"
}

# --- Helper: extract first prompt ---
extract_first_prompt() {
  local filepath="$1"
  local prompt
  prompt=$(grep '"type":"user"' "$filepath" 2>/dev/null | head -1 | grep -o '"text":"[^"]*"' | head -1 | cut -d'"' -f4)
  if [ -z "$prompt" ]; then
    prompt="(no user prompt yet)"
  fi
  printf '%.80s' "$prompt"
}

# --- Helper: extract snippet ---
extract_snippet() {
  local line="$1"
  local snippet

  # Try text-type content block's .text field
  snippet=$(printf '%s' "$line" | jq -r '.message.content[]? | select(.type=="text") | .text' 2>/dev/null | head -1)
  if [ -n "$snippet" ] && [ "$snippet" != "null" ]; then
    printf '%.120s' "$snippet"
    return
  fi

  # Try tool_use block's .input (convert to string since it's often an object)
  snippet=$(printf '%s' "$line" | jq -r '.message.content[]? | select(.type=="tool_use") | .input | tostring' 2>/dev/null | head -1)
  if [ -n "$snippet" ] && [ "$snippet" != "null" ] && [ "$snippet" != "{}" ]; then
    printf '%.120s' "$snippet"
    return
  fi

  # Try tool_result block's .content (may be string or array)
  snippet=$(printf '%s' "$line" | jq -r '.message.content[]? | select(.type=="tool_result") | .content | if type=="array" then tostring else . end' 2>/dev/null | head -1)
  if [ -n "$snippet" ] && [ "$snippet" != "null" ]; then
    printf '%.120s' "$snippet"
    return
  fi

  # Try thinking block's .thinking field
  snippet=$(printf '%s' "$line" | jq -r '.message.content[]? | select(.type=="thinking") | .thinking' 2>/dev/null | head -1)
  if [ -n "$snippet" ] && [ "$snippet" != "null" ]; then
    printf '%.120s' "$snippet"
    return
  fi

  # Fallback: try plain string content (older-style messages)
  snippet=$(printf '%s' "$line" | grep -o '"content":"[^"]*"' | head -1 | cut -d'"' -f4)
  if [ -n "$snippet" ]; then
    printf '%.120s' "$snippet"
    return
  fi

  # No match found
  printf '%s' "(match in non-text field)"
}

# --- Process each JSONL file ---
find "$PROJECTS_DIR" -name "*.jsonl" -print0 2>/dev/null | while IFS= read -r -d '' filepath; do
  # Skip self-session if EXCLUDE_SESSION is set
  if [ -n "$EXCLUDE_SESSION" ]; then
    base=$(basename "$filepath" .jsonl)
    if [ "$base" = "$EXCLUDE_SESSION" ]; then
      continue
    fi
  fi

  # Determine if this is a subagent file and extract parent session id
  IS_SUBAGENT=0
  PARENT_SESSION_ID=""
  if [[ "$filepath" =~ /subagents/ ]]; then
    IS_SUBAGENT=1
    dir_part=$(dirname "$(dirname "$filepath")")
    PARENT_SESSION_ID=$(basename "$dir_part")
  else
    PARENT_SESSION_ID=$(grep -m 1 '"sessionId":"[^"]*"' "$filepath" 2>/dev/null | cut -d'"' -f4)
    if [ -z "$PARENT_SESSION_ID" ]; then
      PARENT_SESSION_ID=$(basename "$filepath" .jsonl)
    fi
  fi

  # Search for the first pattern in the file to pre-filter
  # Use a safe way to get the first element that works in both bash and zsh
  if [ ${#QUERY_WORDS[@]} -eq 0 ]; then
    continue
  fi
  first_pattern=
  for pw in "${QUERY_WORDS[@]}"; do
    first_pattern="$pw"
    break
  done
  if ! grep -qiF "$first_pattern" "$filepath" 2>/dev/null; then
    continue
  fi

  # Extract file-level metadata once (before the loop to avoid shellcheck SC2094)
  file_mtime=$(get_mtime "$filepath")
  cwd=$(extract_cwd "$filepath")
  first_prompt=$(extract_first_prompt "$filepath")

  # Determine kind (session or subagent)
  if [ "$IS_SUBAGENT" -eq 1 ]; then
    kind="subagent"
  else
    kind="session"
  fi

  # Now process each line of the file
  while IFS= read -r line; do
    # Skip empty lines
    if [ -z "$line" ]; then
      continue
    fi

    # Check if all patterns match this line (case-insensitive AND matching)
    matches_all=1
    for pattern in "${QUERY_WORDS[@]}"; do
      if ! printf '%s' "$line" | grep -qiF "$pattern"; then
        matches_all=0
        break
      fi
    done

    if [ $matches_all -eq 0 ]; then
      continue
    fi

    # Extract timestamp from the JSON line
    line_ts=$(printf '%s' "$line" | grep -o '"timestamp":"[^"]*"' | head -1 | cut -d'"' -f4)

    # Convert timestamp to seconds (ISO 8601 format like 2024-01-15T10:30:45)
    if [ -n "$line_ts" ]; then
      line_ts_sec=$(date -j -f "%Y-%m-%dT%H:%M:%S" "${line_ts:0:19}" "+%s" 2>/dev/null || echo 0)
    else
      line_ts_sec=0
    fi

    # Filter by time window
    if [ "$line_ts_sec" -lt "$CUTOFF_TS" ]; then
      continue
    fi

    # Extract snippet from this line
    snippet=$(extract_snippet "$line")

    # Output result
    if [ -n "$line_ts" ]; then
      printf '%s|%s|%s|%s|%s|%s|%s\n' "$file_mtime" "$line_ts" "$cwd" "$PARENT_SESSION_ID" "$kind" "$first_prompt" "$snippet"
    fi
  done < "$filepath"
done | LC_ALL=C sort -t'|' -k2 -r | head -n "$LIMIT"

exit 0
