---
description: List or load a saved handoff from ~/.handoff/. With no args, shows the 5 most recent. With a folder name (exact or prefix), loads it into this conversation. With any other string, greps across handoff prompts and metadata. Use when the user says /handoff-resume or wants to pick up a prior /handoff snapshot.
allowed-tools: Bash(ls:*), Bash(stat:*), Bash(date:*), Bash(sort:*), Bash(head:*), Bash(jq:*), Bash(rg:*), Bash(find:*), Bash(printf:*), Bash(basename:*), Bash(dirname:*), Bash(test:*), Bash(cat:*), Bash(awk:*), Bash(sed:*), Read
---

# Handoff Resume

List or load a saved handoff. Modeled on `/search-claude`'s shape.

Run this bash script first, then act on its output:

```bash
#!/usr/bin/env bash
set -euo pipefail

ARGS="$ARGUMENTS"
HANDOFF_DIR="$HOME/.handoff"

if [ ! -d "$HANDOFF_DIR" ] || [ -z "$(ls -A "$HANDOFF_DIR" 2>/dev/null)" ]; then
  echo "MODE=empty"
  exit 0
fi

# --- Helper: list-mode output for an array of folder paths ---
list_folders() {
  local folders=("$@")
  for f in "${folders[@]}"; do
    [ -d "$f" ] || continue
    local name created desc plan_marker
    name=$(basename "$f")
    if [ -f "$f/metadata.json" ]; then
      created=$(jq -r '.created_at // ""' "$f/metadata.json" 2>/dev/null)
      desc=$(jq -r '.description // ""' "$f/metadata.json" 2>/dev/null)
    else
      created=""
      desc=""
    fi
    if [ -z "$created" ]; then
      mt=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null || echo 0)
      created=$(date -r "$mt" '+%Y-%m-%d %H:%M' 2>/dev/null || date -d "@$mt" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "unknown")
    fi
    [ -f "$f/plan.md" ] && plan_marker=" [+plan]" || plan_marker=""
    printf '  %s%s\n' "$name" "$plan_marker"
    printf '    created: %s\n' "$created"
    printf '    desc:    %s\n' "${desc:-(none)}"
    printf '    load:    /handoff-resume %s\n\n' "$name"
  done
}

# --- No args: list 5 most recent ---
if [ -z "$ARGS" ]; then
  echo "MODE=list"
  mapfile -t RECENT < <(
    for d in "$HANDOFF_DIR"/*/; do
      [ -d "$d" ] || continue
      mt=$(stat -f %m "$d" 2>/dev/null || stat -c %Y "$d" 2>/dev/null || echo 0)
      printf '%s|%s\n' "$mt" "${d%/}"
    done | sort -t'|' -k1 -rn | head -5 | awk -F'|' '{print $2}'
  )
  echo "5 most recent handoffs (of $(ls -1 "$HANDOFF_DIR" | awk 'END{print NR}')):"
  echo ""
  list_folders "${RECENT[@]}"
  exit 0
fi

# --- Try exact or prefix match against folder name ---
EXACT="$HANDOFF_DIR/$ARGS"
if [ -d "$EXACT" ]; then
  MATCH="$EXACT"
else
  mapfile -t PREFIX < <(find "$HANDOFF_DIR" -maxdepth 1 -mindepth 1 -type d -name "${ARGS}*" 2>/dev/null)
  if [ "${#PREFIX[@]}" -eq 1 ]; then
    MATCH="${PREFIX[0]}"
  elif [ "${#PREFIX[@]}" -gt 1 ]; then
    echo "MODE=ambiguous"
    echo "Multiple folders match prefix '$ARGS':"
    echo ""
    list_folders "${PREFIX[@]}"
    exit 0
  fi
fi

if [ -n "${MATCH:-}" ]; then
  echo "MODE=load"
  echo "FOLDER=$MATCH"
  echo "NAME=$(basename "$MATCH")"
  if [ -f "$MATCH/metadata.json" ]; then
    cwd=$(jq -r '.cwd // ""' "$MATCH/metadata.json" 2>/dev/null)
    echo "CWD=$cwd"
  fi
  [ -f "$MATCH/plan.md" ] && echo "HAS_PLAN=1" || echo "HAS_PLAN=0"
  echo "---PROMPT---"
  cat "$MATCH/prompt.md" 2>/dev/null || echo "(prompt.md missing)"
  exit 0
fi

# --- No folder match: literal grep across prompts + metadata ---
echo "MODE=search"
mapfile -t HITS < <(
  rg -i -l -F --max-filesize 5M -- "$ARGS" "$HANDOFF_DIR" 2>/dev/null \
    | while IFS= read -r match; do dirname "$match"; done \
    | sort -u | head -20
)
if [ "${#HITS[@]}" -eq 0 ]; then
  echo "No handoffs matched '$ARGS'. Run /handoff-resume with no args to list recent ones."
  exit 0
fi

# Rank by mtime, take 5
mapfile -t RANKED < <(
  for d in "${HITS[@]}"; do
    mt=$(stat -f %m "$d" 2>/dev/null || stat -c %Y "$d" 2>/dev/null || echo 0)
    printf '%s|%s\n' "$mt" "$d"
  done | sort -t'|' -k1 -rn | head -5 | awk -F'|' '{print $2}'
)
echo "${#HITS[@]} handoff(s) matched '$ARGS' (showing top ${#RANKED[@]}):"
echo ""
list_folders "${RANKED[@]}"
```

Then respond based on the first `MODE=...` line:

- **MODE=empty** → Output: "No handoffs saved yet. Use `/handoff <description>` to create one."
- **MODE=list** or **MODE=search** or **MODE=ambiguous** → Output the script output verbatim after a one-line preamble. Nothing else.
- **MODE=load** → A handoff is being loaded into *this* conversation. Do the following:
  1. One-line preamble: `Resuming handoff: <NAME>`.
  2. Output the `---PROMPT---` section verbatim — treat it as if the user had pasted it. The rest of this conversation should pick up from there.
  3. If `CWD` is set and differs from the current `pwd`, append a note: `Note: original cwd was <CWD>. Run cd <CWD> if you need to be there.`
  4. If `HAS_PLAN=1`, append: `An active plan was saved at <FOLDER>/plan.md — read it if you plan to continue that work.`
  5. Then act on the handoff: orient yourself to where the work left off, and either ask the user how they want to proceed or pick up the next step from the "Open questions / next steps" section.
