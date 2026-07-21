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
# Stored as an ISO-8601 string (not epoch seconds) so the hot per-line path
# below can filter with a plain lexicographic string comparison instead of
# forking `date` once per matching line — timestamps in this format sort
# the same lexicographically as chronologically.
CUTOFF_ISO=""
if [ "$ALL_MODE" -ne 1 ]; then
  NOW=$(date +%s)
  CUTOFF_SEC=$((NOW - DAYS * 86400))
  CUTOFF_ISO=$(date -j -f %s "$CUTOFF_SEC" +"%Y-%m-%dT%H:%M:%S" 2>/dev/null || date -d "@$CUTOFF_SEC" +"%Y-%m-%dT%H:%M:%S" 2>/dev/null)
fi

# --- Helper: extract file mtime ---
get_mtime() {
  local filepath="$1"
  stat -f %m "$filepath" 2>/dev/null || stat -c %Y "$filepath" 2>/dev/null || echo 0
}

# --- Reverse-line-order helper (checked once, not per file) ---
# Used so the first grep stage can scan from the end of a file and stop
# after $LIMIT matches, instead of collecting every match in a large file
# (some transcripts are tens of MB) only to discard all but the last few.
HAVE_TAC=0
if command -v tac >/dev/null 2>&1; then
  HAVE_TAC=1
fi
reverse_stdin() {
  if [ "$HAVE_TAC" -eq 1 ]; then
    tac
  else
    tail -r
  fi
}

# --- Helper: extract cwd, first prompt, and sessionId in one pass ---
# Combines what used to be three separate forked lookups (extract_cwd,
# extract_first_prompt, and a standalone grep+cut for sessionId) into a
# single awk pass over the file. With thousands of candidate files,
# per-file fork count dominates runtime far more than the actual
# byte-scanning does.
extract_file_metadata() {
  local filepath="$1"
  awk '
    cwd == "" && match($0, /"cwd":"[^"]*"/) {
      cwd = substr($0, RSTART + 7, RLENGTH - 8)
    }
    prompt == "" && /"type":"user"/ && match($0, /"text":"[^"]*"/) {
      prompt = substr($0, RSTART + 8, RLENGTH - 9)
    }
    sid == "" && match($0, /"sessionId":"[^"]*"/) {
      sid = substr($0, RSTART + 13, RLENGTH - 14)
    }
    cwd != "" && prompt != "" && sid != "" { exit }
    END { print cwd "\x1f" prompt "\x1f" sid }
  ' "$filepath" 2>/dev/null
}

# --- Helper: fallback cwd decode from directory name (rare path) ---
decode_cwd_from_path() {
  local filepath="$1"
  local dir_encoded
  dir_encoded=$(basename "$(dirname "$filepath")")
  printf '%s' "$dir_encoded" | sed 's|^-|/|; s|-|/|g'
}

# --- jq filter: parse EVERY matched line from the ENTIRE corpus in a single
# jq invocation at the very end, instead of forking jq per matching file.
# jq's own per-record processing is fast; what's expensive at corpus scale
# (thousands of matching files) is jq's process-startup cost paid again and
# again. So the per-file loop below only emits lightweight intermediate
# records (file metadata + raw JSON line, unit-separator delimited); this
# filter runs exactly once over the whole accumulated stream. Same snippet
# field-priority as before (text > tool_use.input > tool_result.content >
# thinking > plain string content > sentinel).
# The $-vars below are jq variables, not bash -- must stay single-quoted.
# shellcheck disable=SC2016
JQ_BATCH_FILTER='
def snippet_of($obj):
  ( first($obj.message.content[]? | select(.type=="text") | .text | select(. != null)) ) //
  ( first($obj.message.content[]? | select(.type=="tool_use") | .input | select(. != null and . != {}) | tostring) ) //
  ( first($obj.message.content[]? | select(.type=="tool_result") | .content | select(. != null) | (if type=="array" then tostring else . end)) ) //
  ( first($obj.message.content[]? | select(.type=="thinking") | .thinking | select(. != null)) ) //
  ( if (($obj.message.content // null)|type)=="string" then $obj.message.content else empty end ) //
  "(match in non-text field)";
split("\u001f") as $parts
| $parts[0] as $mtime
| $parts[1] as $cwd
| $parts[2] as $sid
| $parts[3] as $kind
| $parts[4] as $prompt
| ($parts[5:] | join("\u001f")) as $rawjson
| (try ($rawjson | fromjson) catch {}) as $obj
| ($obj.timestamp // "") as $ts
| select($ts != "")
| select($cutoff == "" or ($ts[0:19] >= $cutoff))
| (snippet_of($obj) | gsub("\n"; " ") | .[0:120]) as $snip
| "\($mtime)|\($ts)|\($cwd)|\($sid)|\($kind)|\($prompt)|\($snip)"
'

# --- Process each JSONL file ---
# When a cutoff is active, let find itself skip files whose mtime already
# proves every line in them is too old (no line's timestamp can be newer
# than its own file's mtime) — this is the realistic default case
# (30-day window, no --all), and it means the expensive per-file grep/awk
# work below never even starts for files outside the window, instead of
# running on the whole corpus and filtering the result at the very end.
FIND_EXTRA_ARGS=()
if [ -n "$CUTOFF_ISO" ]; then
  FIND_EXTRA_ARGS=(-newermt "$CUTOFF_ISO")
fi
find "$PROJECTS_DIR" -name "*.jsonl" "${FIND_EXTRA_ARGS[@]}" -print0 2>/dev/null | while IFS= read -r -d '' filepath; do
  # Skip self-session if EXCLUDE_SESSION is set
  if [ -n "$EXCLUDE_SESSION" ]; then
    base=$(basename "$filepath" .jsonl)
    if [ "$base" = "$EXCLUDE_SESSION" ]; then
      continue
    fi
  fi

  # Determine if this is a subagent file (cheap: a bash regex match on the
  # path string, no fork). The rest of subagent/session-id resolution needs
  # forks (dirname/basename or a file-level lookup), so it's deferred below
  # until a match is confirmed.
  IS_SUBAGENT=0
  if [[ "$filepath" =~ /subagents/ ]]; then
    IS_SUBAGENT=1
  fi

  # Select only lines matching ALL query words, using grep itself (not a
  # bash read-loop) so the expensive per-line work below only ever runs on
  # genuine matches. Chain one grep per word, each stage narrowing the last.
  # Done before metadata extraction so multi-word queries that fail on a
  # later word skip metadata extraction entirely.
  #
  # The FIRST stage scans from the end of the file (reverse_stdin) and stops
  # after $LIMIT matches (grep -m), instead of collecting every match in the
  # file. Session files are append-only chronological logs, so this yields
  # exactly the $LIMIT most recent matches — and no single file can
  # contribute more than $LIMIT results to a global top-$LIMIT anyway, so
  # this drops no line that could ever reach the final output. Without this,
  # a common word can match every line of a large transcript and force the
  # whole file through a bash variable before ever getting trimmed. Later
  # stages (additional query words) only ever narrow this already-small set
  # further, so they use a plain grep.
  matched_lines=""
  is_first_word=1
  for pattern in "${QUERY_WORDS[@]}"; do
    if [ "$is_first_word" -eq 1 ]; then
      matched_lines=$(reverse_stdin < "$filepath" 2>/dev/null | grep -m "$LIMIT" -iF -- "$pattern" 2>/dev/null | reverse_stdin)
      is_first_word=0
    else
      matched_lines=$(printf '%s' "$matched_lines" | grep -iF -- "$pattern" 2>/dev/null)
    fi
    if [ -z "$matched_lines" ]; then
      break
    fi
  done

  if [ -z "$matched_lines" ]; then
    continue
  fi

  # Extract file-level metadata once, only for files with a confirmed match
  # (before the loop to avoid shellcheck SC2094)
  file_mtime=$(get_mtime "$filepath")
  file_metadata=$(extract_file_metadata "$filepath")
  cwd="${file_metadata%%$'\x1f'*}"
  rest="${file_metadata#*$'\x1f'}"
  first_prompt="${rest%%$'\x1f'*}"
  sid_from_file="${rest#*$'\x1f'}"
  if [ -z "$cwd" ]; then
    cwd=$(decode_cwd_from_path "$filepath")
  fi
  if [ -z "$first_prompt" ]; then
    first_prompt="(no user prompt yet)"
  fi
  first_prompt=$(printf '%.80s' "$first_prompt")

  # Determine kind and parent session id (deferred until now: both need a
  # fork, either dirname/basename or the file-metadata lookup above).
  if [ "$IS_SUBAGENT" -eq 1 ]; then
    kind="subagent"
    dir_part=$(dirname "$(dirname "$filepath")")
    PARENT_SESSION_ID=$(basename "$dir_part")
  else
    kind="session"
    PARENT_SESSION_ID="$sid_from_file"
    if [ -z "$PARENT_SESSION_ID" ]; then
      PARENT_SESSION_ID=$(basename "$filepath" .jsonl)
    fi
  fi

  # Emit one lightweight intermediate record per matched line: file metadata
  # plus the raw JSON line, unit-separator delimited. No jq/date fork here —
  # timestamp extraction and snippet parsing happen once, below, for the
  # whole corpus at once.
  while IFS= read -r line; do
    if [ -z "$line" ]; then
      continue
    fi
    printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\n' "$file_mtime" "$cwd" "$PARENT_SESSION_ID" "$kind" "$first_prompt" "$line"
  done <<< "$matched_lines"
done | jq -R -r --arg cutoff "$CUTOFF_ISO" "$JQ_BATCH_FILTER" 2>/dev/null | LC_ALL=C sort -t'|' -k2 -r | head -n "$LIMIT"

exit 0
