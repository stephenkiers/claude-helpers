---
description: Edit a document using expert writing personas and accumulated prose rules. Drafts revised version to /tmp/ for diffing.
argument-hint: <file> [--add-rule]
allowed-tools: Bash(diff:*), Bash(cp:*), Bash(date:*), Bash(wc:*), Bash(ls:*), Bash(mkdir:*), Read, Glob, Grep, Write, AskUserQuestion
---

# Expert Write

A document editing system that applies accumulated prose rules through specialized editor personas. Produces a revised draft to `/tmp/expert-write/` so you can diff and cherry-pick changes.

**Two modes:**
- **Edit mode** (default): `$ARGUMENTS` is a file path — runs all editors against the document
- **Add rule mode**: `$ARGUMENTS` contains `--add-rule` — adds a new writing rule from feedback

---

## Instructions

### Detect Mode

Check `$ARGUMENTS`:
- Contains `--add-rule` → go to **Add Rule Mode**
- Otherwise → go to **Edit Mode**

---

## Edit Mode

### Step 1: Load Rules

Read `~/.claude/prompts/writing-rules.yaml`. Parse all rules — each has: name, description, detect, fix, example (before/after), source.

If the file doesn't exist, proceed with no rules loaded (editors will still apply their built-in principles).

### Step 2: Load Editors

Glob `~/.claude/reviewers/editor-*.yaml`. Read each file — extract: name, priority, summary (character + voice), principles, editReview.focusAreas.

Sort editors: `high` priority first, then `medium`.

### Step 3: Read the Document

Read the file at `$ARGUMENTS`. Assess:
- **Audience**: who is this written for? (executives, engineers, mixed)
- **Purpose**: inform, persuade, instruct, or document?
- **Length**: word count via `wc -w`

### Step 4: Editor Reviews

For each editor (in priority order), scan the document through their lens combined with the loaded rules:

For each flagged passage:
1. **Quote the exact text** (3–5 sentences of context)
2. **Classify**: `REWRITE` | `CUT` | `RESTRUCTURE`
3. **Name the pattern** (e.g., "Aphoristic stacking", "Hedging", "Buried lede")
4. **Draft replacement text** (for REWRITE/RESTRUCTURE) or explain what to cut (for CUT)

Each editor should flag 3–8 passages. More is noise; fewer means they weren't looking hard enough.

### Step 5: Build Revised Document

Apply all edits to produce a revised version of the document. Where editors conflict on the same passage, prefer the higher-priority editor's suggestion. For `CUT` classifications with no replacement, remove the passage entirely.

### Step 6: Write Revised Document

```bash
mkdir -p /tmp/expert-write
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
FILENAME=$(basename "$ARGUMENTS" .md)
OUTPUT="/tmp/expert-write/${FILENAME}-${TIMESTAMP}.md"
```

Write the revised document to `$OUTPUT`.

### Step 7: Show Summary

Output a summary in this format:

```
## Expert Write Summary

**Document**: {filename}  
**Audience**: {assessed audience}  
**Original**: {word count} words → **Revised**: {word count} words

---

### Shakespeare (cadence) — {N} passages flagged
{2–3 most impactful: show before/after excerpts, label the pattern}

### Strunk (signal) — {N} passages flagged
{2–3 most impactful: show before/after excerpts, label the pattern}

### Demosthenes (audience) — {N} passages flagged
{2–3 most impactful: show before/after excerpts, label the pattern}

---

**Revised draft**: {OUTPUT path}

To review changes:
  diff -u {original path} {OUTPUT}
```

---

## Add Rule Mode

### Step 1: Get Feedback

Ask the user: "Describe the feedback — what prose pattern was flagged, and what did the reader say?"

(If the feedback is already present in the conversation context, use it directly without asking.)

### Step 2: Extract Rule

From the feedback, extract:
- **name**: A short label for the pattern (e.g., "Aphoristic stacking")
- **description**: What the pattern is and why it undermines readability
- **detect**: How to spot it in a document (specific signals)
- **fix**: How to rewrite passages that exhibit this pattern
- **example.before**: A short example of the pattern
- **example.after**: The improved version
- **source**: Attribution (e.g., "Peer feedback on X doc, YYYY-MM-DD")

### Step 3: Append to Rules File

Append the new rule to `~/.claude/prompts/writing-rules.yaml`. If the file doesn't exist, create it with the standard header first.

### Step 4: Confirm

Show the user the rule as it was saved. Confirm the file path and total rule count.
