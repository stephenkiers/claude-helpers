#!/bin/bash
# shellcheck shell=bash
set -u

# Worker script for search-claude.sh, invoked via xargs -P for parallel per-file processing.
#
# Positional arguments:
#   1: filepath       - JSONL file to process
#   2: workdir        - Temporary directory for output files
#   3: limit          - Max results per search (used by grep -m)
#   4: exclude_session - Session ID to skip (or empty)
#   5: have_tac       - 1 if tac is available, 0 otherwise
#   6+: query_words   - Search terms (all must match via AND)
#
# Creates a unique output file in $workdir and writes intermediate records to it.
# Exits 0 regardless of whether any matches were found (empty output file is ok).

filepath="$1"
workdir="$2"
limit="$3"
exclude_session="$4"
have_tac="$5"
shift 5
query_words=("$@")

# --- Create unique output file for this worker ---
# Each worker needs a unique temp file. Use mktemp with appropriate pattern.
# The pattern requires X's to be replaced with random characters (6+ X's works best on macOS).
outfile=$(mktemp "$workdir/worker.XXXXXX")

# --- Helper: extract file mtime ---
get_mtime() {
  local fpath="$1"
  stat -f %m "$fpath" 2>/dev/null || stat -c %Y "$fpath" 2>/dev/null || echo 0
}

# --- Reverse-line-order helper ---
reverse_stdin() {
  if [ "$have_tac" -eq 1 ]; then
    tac
  else
    tail -r
  fi
}

# --- Helper: extract cwd, first prompt, and sessionId in one pass ---
extract_file_metadata() {
  local fpath="$1"
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
  ' "$fpath" 2>/dev/null
}

# --- Helper: fallback cwd decode from directory name (rare path) ---
decode_cwd_from_path() {
  local fpath="$1"
  local dir_encoded
  dir_encoded=$(basename "$(dirname "$fpath")")
  printf '%s' "$dir_encoded" | sed 's|^-|/|; s|-|/|g'
}

# --- Main per-file processing ---

# Skip self-session if EXCLUDE_SESSION is set
if [ -n "$exclude_session" ]; then
  base=$(basename "$filepath" .jsonl)
  if [ "$base" = "$exclude_session" ]; then
    exit 0
  fi
fi

# Determine if this is a subagent file
IS_SUBAGENT=0
if [[ "$filepath" =~ /subagents/ ]]; then
  IS_SUBAGENT=1
fi

# Select only lines matching ALL query words via grep chaining
matched_lines=""
is_first_word=1
for pattern in "${query_words[@]}"; do
  if [ "$is_first_word" -eq 1 ]; then
    matched_lines=$(reverse_stdin < "$filepath" 2>/dev/null | grep -m "$limit" -iF -- "$pattern" 2>/dev/null | reverse_stdin)
    is_first_word=0
  else
    matched_lines=$(printf '%s' "$matched_lines" | grep -iF -- "$pattern" 2>/dev/null)
  fi
  if [ -z "$matched_lines" ]; then
    break
  fi
done

if [ -z "$matched_lines" ]; then
  exit 0
fi

# Extract file-level metadata
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

# Determine kind and parent session id
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

# Emit one lightweight intermediate record per matched line to our output file
while IFS= read -r line; do
  if [ -z "$line" ]; then
    continue
  fi
  printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\n' "$file_mtime" "$cwd" "$PARENT_SESSION_ID" "$kind" "$first_prompt" "$line" >> "$outfile"
done <<< "$matched_lines"

exit 0
