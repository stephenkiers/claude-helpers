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

# --- Check for tac availability (checked once, passed to workers) ---
HAVE_TAC=0
if command -v tac >/dev/null 2>&1; then
  HAVE_TAC=1
fi

# --- Setup for parallel worker execution ---
WORKER_SCRIPT="$(dirname "$0")/search-claude-worker.sh"
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

# --- Determine worker count ---
# Auto-detect from system if not set, but clamp to max of 8
WORKERS="${SEARCH_CLAUDE_WORKERS:-}"
if [ -z "$WORKERS" ] || ! [[ "$WORKERS" =~ ^[0-9]+$ ]]; then
  # Fall back to auto-detect if unset or non-numeric
  WORKERS=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)
fi
# Clamp to [1, 8]
if [ "$WORKERS" -lt 1 ]; then
  WORKERS=1
fi
if [ "$WORKERS" -gt 8 ]; then
  WORKERS=8
fi

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

# --- Process each JSONL file via parallel workers ---
# When a cutoff is active, let find itself skip files whose mtime already
# proves every line in them is too old (no line's timestamp can be newer
# than its own file's mtime) — this is the realistic default case
# (30-day window, no --all), and it means the expensive per-file grep/awk
# work below never even starts for files outside the window, instead of
# running on the whole corpus and filtering the result at the very end.
#
# Each worker writes to its own unique temp file in $WORKDIR, avoiding
# concurrent-write corruption that would occur if all workers wrote to
# a single shared stdout or file.
FIND_EXTRA_ARGS=()
if [ -n "$CUTOFF_ISO" ]; then
  FIND_EXTRA_ARGS=(-newermt "$CUTOFF_ISO")
fi
find "$PROJECTS_DIR" -name "*.jsonl" "${FIND_EXTRA_ARGS[@]}" -print0 2>/dev/null | \
  xargs -0 -P "$WORKERS" -I {} bash "$WORKER_SCRIPT" {} "$WORKDIR" "$LIMIT" "$EXCLUDE_SESSION" "$HAVE_TAC" "${QUERY_WORDS[@]}"

# Collect results from all worker output files and pipe through jq + sort + limit
find "$WORKDIR" -type f -exec cat {} + 2>/dev/null | jq -R -r --arg cutoff "$CUTOFF_ISO" "$JQ_BATCH_FILTER" 2>/dev/null | LC_ALL=C sort -t'|' -k2 -r | head -n "$LIMIT"

exit 0
