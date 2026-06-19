---
description: Snapshot the current conversation into ~/.handoff/<timestamp>-<slug>/ so it can be resumed in a fresh window via /handoff-resume. Use when the user says /handoff or wants to fork/checkpoint a session before rewinding.
allowed-tools: Bash(mkdir:*), Bash(date:*), Bash(cp:*), Bash(test:*), Bash(ls:*), Bash(printf:*), Bash(pwd:*), Bash(echo:*), Bash(jot:*), Bash(head:*), Bash(tr:*), Write
---

# Handoff

Snapshot the live conversation to disk so it can be resumed in another window. After `/handoff` runs, the user typically presses Esc-Esc to rewind the conversation — the on-disk folder persists either way. *This* is the fork.

Do the following:

1. **Parse `$ARGUMENTS`** as the freeform handoff description.
   - If empty: infer a short purpose statement from the conversation context yourself. Then proceed using your inferred description (don't block on confirmation — the user can always Esc-Esc and re-run).

2. **Compute the slug**: kebab-case, ≤40 chars, derived from the description (or inferred purpose). Strip punctuation, lowercase, hyphen-separated.

3. **Compute the folder path**:
   ```bash
   STAMP=$(date +%Y-%m-%d_%H%M)
   BASE="$HOME/.handoff/${STAMP}-<slug>"
   DIR="$BASE"
   # collision: if it exists, append a 2-char random suffix
   if [ -d "$DIR" ]; then
     SUFFIX=$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 2)
     DIR="${BASE}-${SUFFIX}"
   fi
   mkdir -p "$DIR"
   ```

4. **Synthesize `prompt.md`** directly from this live conversation (you have the context — no agent needed). Use exactly these sections:

   ```markdown
   # Handoff: <description>

   Created: <ISO timestamp>
   From cwd: <pwd>

   ## Task / goal
   <One paragraph: what we were trying to accomplish.>

   ## Current state
   <Where we left off. What's done. What's in flight. What's blocked.>

   ## Key decisions & rationale
   <Bulleted list of non-obvious choices already made, so the resumer doesn't re-litigate them. Include the *why* for each.>

   ## Open questions / next steps
   <What the resumer should pick up. Concrete, ordered if possible.>

   ## Referenced files
   <Bulleted list of file paths touched or discussed. Paths only — git tracks the content.>

   ## Active plan
   <"See plan.md in this folder." if a plan was copied, else "(none)">
   ```

   Write it with the `Write` tool to `$DIR/prompt.md`. Be substantive — this is the *only* thing the resumer will see. Don't pad with conversational filler; do capture context the resumer can't reconstruct from the codebase.

5. **Write `metadata.json`** to `$DIR/metadata.json`:
   ```json
   {
     "created_at": "<ISO-8601 with timezone>",
     "cwd": "<output of pwd>",
     "slug": "<slug>",
     "description": "<$ARGUMENTS verbatim, or your inferred description>",
     "source_plan_path": "<absolute path to active plan file, or null>",
     "referenced_files": ["<paths from §4 Referenced files>"]
   }
   ```

6. **Copy the active plan** if one was referenced in this conversation:
   ```bash
   test -f "<plan path>" && cp "<plan path>" "$DIR/plan.md"
   ```
   If no plan, skip silently.

7. **Print confirmation** — a single block like:
   ```
   Handoff saved: <DIR>
     prompt.md     (<N> lines)
     metadata.json
     plan.md       (if copied)

   Resume in any window:
     /handoff-resume <basename of DIR>

   Esc-Esc to roll back this conversation; the handoff on disk persists.
   ```

Nothing else.
