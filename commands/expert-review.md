---
description: Smart expert code review with triage - works across all projects
argument-hint: [reviewers...] [--model haiku|sonnet|opus|fable] [--all] [--force]
allowed-tools: Bash(git diff:*), Bash(git branch:*), Bash(git log:*), Bash(git rev-parse:*), Bash(git show:*), Bash(git status:*), Bash(git -C:*), Bash(git worktree:*), Bash(mkdir:*), Bash(rm:*), Bash(echo:*), Bash(cat:*), Bash(jq:*), Bash(gh:*), Bash(ls:*), Bash(tr:*), Bash(mktemp:*), Bash(mv:*), Bash(BRANCH=:*), Bash(HASH=:*), Bash(PROJECT=:*), Bash(PROJECT_ROOT=:*), Bash(REPO_KEY=:*), Bash(TIMESTAMP=:*), Bash(REVIEW_DIR=:*), Read, Glob, Grep, Task, Write, Edit, AskUserQuestion
model: sonnet
---

# Expert Code Review

A checkpoint-based, parallel code review pipeline:

1. **Summarizer** analyzes the diff (subagent)
2. **Router** (sonnet) judges which reviewers meet the threshold for this diff
3. **Pass 1 blind reviews** — one **parallel subagent per selected reviewer** (incl. Sam System, Code
   Rot Cody, Consistency Checker), each writing its own checkpoint file
4. **Contrarian Carl** — after all Pass 1 files exist, sees everything, finds what was missed
5. **Haiku Q&A** — parallel haiku subagents answer each reviewer's open questions
6. **Pass 2 re-evaluations** — parallel subagents, **fresh skeptic-verifier framing**, business context
   + Q&A answers revealed. Judgment reviewers only (mechanical roles get no Pass 2).
7. **Amalgamator** (PANEL_MODEL) — one expensive agent replaces quadratic cross-review; deduplicates,
   severity-ranks, resolves conflicts, writes final-report.md
8. **Triage Chief** (PANEL_MODEL) — sorts findings into *doing it* / *needs you* / *needs measurement*
   / *deferred*, runs the cross-cutting gut check, writes action-plan.md
9. **Rulings → Record → Cache metadata** (main thread) — ask the human only what only they can answer,
   then write the answers down so the panel stops asking

**Why triage?** The Amalgamator decides *what is true*. That is not the same as *what a person has to
look at*. Ordering by severity is an author's concept; ~85% of findings are ones the reader would
accept as written, and making them re-derive that finding by finding is the cognitive tax this step
removes. The full report is unchanged and one click away — triage sits in front of it, not over it.

**Why checkpoints?** Every step writes an inspectable artifact to the review directory; if any
agent fails, the others' work is preserved and only the missing step re-runs.

**Why subagents (not main thread)?** Two reasons. *Blindness*: a Pass 1 reviewer running in the
main thread can see every earlier reviewer's output sitting in context — a fresh subagent cannot.
*Quality*: sequential main-thread review accumulates enormous context by the twentieth reviewer;
each subagent starts clean. Parallelism is the bonus, not the reason.

**Context discipline (orchestrator).** Subagents isolate *their* work from you; they do not isolate
themselves from you automatically. Three rules keep this pipeline from ballooning your context —
they are the difference between a ~180k review and a ~430k one:

1. **Pass paths, not contents.** Never read a reviewer's YAML or the expert framework yourself, and
   never paste them into a prompt. Name the file; the subagent reads it. Every prompt you write
   stays in your context for the whole run.
2. **The file is the contract.** Every panel agent Writes its output to `{REVIEW_DIR}` and returns a
   one-line **receipt**, never its report. A returned report reaches you *twice* — as the tool result
   and again in the completion notification's `<result>` block. The Amalgamator reads the files once; you never do.
3. **Never poll.** No `ScheduleWakeup`, no `sleep`. Launch a batch in one message and let it return.
4. **The diff is a file, not a string.** Write `full-diff.patch` once (Step 1);
   pass paths. Never `cat` the diff into your own context and never paste it into a prompt — a
   44k-token diff inlined into 20 prompts is 880k tokens of *your* context, re-read from cache on
   every subsequent turn. Note that passing a path only saves *your* context: the receiving subagent
   pays the same tokens the moment it calls `Read`. The Router reads the full patch once; Pass 1 reviewers read their bounded sections. Also write `diff-index.md` (Step 1) as a quick orientation artifact: file list + hunk headers only, ~1/20th the size of the full patch — useful for skimming the review directory or reconstructing scope if a step needs re-running.

You are a dispatcher: routing, review, and synthesis all happen in subagents. Review text belongs in files and in subagents, not in you.

## Arguments

- `$1...`: Reviewer selection (default: all discovered reviewers, router-selected)
  - Comma- or space-separated names matched case-insensitively against `index.yaml` — full names or
    unambiguous prefixes: `/expert-review rachel,security-sage` — error if a name doesn't match
  - Naming reviewers **bypasses the router**: only named reviewers run
  - `--all`: explicitly run all reviewers (the default; router makes the final call)
- `--model <haiku|sonnet|opus|fable>`: model for the **judgment panel** — Pass 1, Pass 2, Contrarian
  Carl, **Amalgamator**, and **Triage Chief**. Default: inherit this command's model (`sonnet`). Three
  tiers per ADR-0004: **Router** (Step 5) = sonnet (judgment, narrow, economical); **Mechanical roles**
  (Q&A, Code Rot Cody, Consistency Checker) = haiku (routing and grep are model-agnostic); **Judgment
  panel** (Pass 1, Carl, Pass 2, Amalgamator, Triage) = PANEL_MODEL (your `--model` choice, or
  inherited). Triage rides the panel tier deliberately — deciding what a human must rule on is a
  judgment call, and getting it wrong in either direction costs more than the model does.
- `--force` (alias `-y`): skip the re-run confirmation when a prior review exists for this branch

  Cost per 1M tokens (in/out), cheapest first: **haiku** $1/$5 · **sonnet** $3/$15 · **opus** $5/$25
  · **fable** $10/$50. Fable is the most capable *and* the most expensive — 2× Opus — it is the
  deliberate expensive step, used by the Amalgamator to resolve conflicts and severity-rank findings.
  Sonnet is the default panel tier.

Examples: `/expert-review --model haiku` (whole panel, cheapest — good for a smoke test) ·
`/expert-review rachel,security-sage` (two reviewers, no router) ·
`/expert-review --model fable` (use fable for the amalgamator and panel)

## Checkpoint Files

All artifacts live in `{REVIEW_DIR}` = `~/.claude/reviews/{REPO_KEY}/{branch}-{short_hash}-{timestamp}/`
(persists across reboots; one subfolder per *invocation* — the timestamp means two overlapping
invocations against the same branch/commit never collide on the same directory, and re-running an
already-reviewed commit never overwrites the prior run):

| File | Written by | When |
|------|-----------|------|
| `full-diff.patch` | Main thread | Step 1 — the full delta, ~1 char/token; large on purpose |
| `diff-index.md` | Main thread | Step 1 — `git diff --stat` + hunk headers only, ~20× smaller |
| `summary.md` | Summarizer | Step 4 — Technical Summary + Business Context |
| `tagged-sections.md` | Router (or Step 5 synthesis) | Step 5 — section → reviewer routing with Panel Decision (includes/excludes); synthesized from the user's explicit selection when `NAMED_SELECTION=true` |
| `{reviewer}-pass1.md` | Each Pass 1 subagent | Step 6 (Consistency Checker + Cody included) |
| `contrarian-carl-pass1.md` | Carl | Step 7 — no Pass 2, presented as-is |
| `{reviewer}-questions-answered.md` | Haiku Q&A | Step 8 — only reviewers with open questions |
| `{reviewer}-pass2.md` | Pass 2 subagents | Step 9 — only reviewers with findings, judgment reviewers only |
| `final-report.md` | Amalgamator | Step 10 — the complete record; the gut-check instrument |
| `action-plan.md` | Triage Chief (Step 11); ruling lines appended in place by the main thread (Step 12) | Step 11 — decision-first; **the file the human opens** |

---

## Instructions

### Step 0: Setup

1. Resolve paths and create the checkpoint directory:
   ```bash
   set -euo pipefail

   # Shell helpers — any variable that is unset or any file write that is silently truncated
   # fails loud here rather than propagating empty downstream.
   # $1 must be a valid bash identifier (no hyphens, cannot start with a digit)
   require_var() {
     [ -n "${!1:-}" ] || { echo "ERROR: $1 is unset or empty" >&2; exit 1; }
   }
   sentinel_or_fail() {
     local file=$1 sentinel=$2
     tail -1 "$file" 2>/dev/null | grep -qF "$sentinel" \
       || { echo "ERROR: sentinel '${sentinel}' not found at end of ${file} — write may be truncated" >&2; exit 1; }
   }

   BRANCH=$(git rev-parse --abbrev-ref HEAD | tr '/' '-')
   HASH=$(git rev-parse --short HEAD)
   PROJECT_ROOT=$(git rev-parse --show-toplevel)
   # Add a random suffix so two invocations in the same second never collide on the same dir.
   # $RANDOM is a bash builtin (no subshell); printf pads to 5 digits for stable sort order.
   TIMESTAMP=$(date +%Y%m%dT%H%M%S)-$(printf '%05d' $RANDOM)

   # REPO_KEY identifies the repository, NOT a directory. This repo's own /track-and-start creates
   # worktrees named after the branch and /cleanup deletes them — so `basename $PROJECT_ROOT` would
   # key cross-run memory on a path that vanishes, silently resetting history to empty. Key on repo
   # identity instead; fall back to the directory name only when gh/remote is unavailable.
   REPO_KEY=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null | tr '/' '-')
   [ -z "$REPO_KEY" ] && REPO_KEY=$(basename "$PROJECT_ROOT")

   REVIEW_DIR="$HOME/.claude/reviews/${REPO_KEY}/${BRANCH}-${HASH}-${TIMESTAMP}"
   mkdir -p "$REVIEW_DIR"
   ```

   `PROJECT_ROOT` is where the project's `.claude/project.yaml` lives (still read per-worktree).
   `REVIEW_DIR` is per-invocation, under `~/.claude/reviews/${REPO_KEY}/`.

   **Do not cache these across Bash calls in a shared-path scratch file** (e.g. a fixed
   `/tmp/*.sh` sourced by later steps). The Bash tool's working directory persists between calls but
   its shell state does not, and it's tempting to bridge that gap with a scratch file — but a
   predictable path is shared across every concurrent invocation on the machine, including ones
   running against a *different* repo. `REPO_KEY`, `BRANCH`, `HASH`, and `TIMESTAMP` are cheap to
   recompute (`git rev-parse` / `gh repo view`); recompute them in each Bash block that needs them,
   or carry the already-known literal values forward as text, rather than persisting them to disk.

2. **Read `.claude/project.yaml`** (if present in the project root). Store as `PROJECT_CONTEXT`
   and pass to all reviewer prompts. Key extractions:
   - `techStack.language` → primary language (skips detection in step 3)
   - `fragility.*` → Fragile Feynman; `docStyle` → Contract Chris;
     `typeChecker`, `propertyTestingLib` → Tara TypeSafe
   - `adrs`, `invariants`, `redLines`, `terminology` → all reviewers

3. **Detect project languages** (skip if `techStack.language` set): `Cargo.toml` → rust,
   `package.json` → typescript; otherwise majority file extension among changed files
   (`.go`, `.rb`, `.py`, …). A diff can have multiple languages; collect all that appear as
   `DETECTED_LANGUAGES`.

4. **Detect project modifiers** from CLAUDE.md or `.claude/review-config.md`: a
   `## Review Modifiers` section, or phrases like "pre-release" / "greenfield" / "backwards
   compatibility is not a concern" → `greenfield: true`; `internal: true` for internal tools.
   These are defined in the expert framework (Project Modifiers section) — pass any detected
   modifiers to every reviewer prompt.

5. **Gather plan/ticket context and review history (cache-first):**
   - Read `.claude/github-cache.json`. If `issue.body` exists → business context; `issue.title`
     → summarizer prompt; `issue.url` → report.
   - **Prior-review check:** if `review.lastRun` exists AND `review.branch` == `BRANCH`, print:
     ```
     ℹ️ Previous review found on this branch:
       Last run: {review.lastRun}
       Commit: {review.commit}{" (current)" if == HASH else " (older — current is {HASH})"}
       Reviewers: {review.reviewers joined}
       Findings: {critical}C / {high}H / {medium}M / {low}L
       Checkpoint: {review.reviewDir}
     ```
     Then — unless `--force`/`-y` — ask before proceeding: same commit → "This exact commit was
     already reviewed. Re-run anyway? (prior results in `{review.reviewDir}` are preserved — the
     timestamped `{REVIEW_DIR}` means this never overwrites them)"; different commit → "Re-run for
     the current commit? (prior results in `{review.reviewDir}` are preserved; new results go to a
     different folder)". Wait for explicit confirmation; exit cleanly if declined.
   - **Fallback (no cache):** search `~/.claude/plans/*.md` for mentions of this branch/project;
     also check for kanban files (`*-kanban.md`) in project root or docs/.
   - Plan context found → give it to the summarizer (Step 4) and to Sam System as "Known
     Integration Concerns" (Step 6); cross-reference in the final report.

### Step 1: Determine Review Scope

- `git diff --name-only main...HEAD`. If empty, inform the user and exit.
- Write both diff artifacts once, so every later step passes a path instead of re-deriving or
  inlining the diff:
  ```bash
  git diff main...HEAD > "$REVIEW_DIR/full-diff.patch"
  { echo "## Files"; git diff --stat main...HEAD;
    echo; echo "## Hunks"; git diff main...HEAD | grep -E '^(\+\+\+|@@)'; } > "$REVIEW_DIR/diff-index.md"
  ```
  `diff-index.md` is the file list plus every hunk header — each one already carries its enclosing
  function/section (`@@ -39,13 +39,16 @@ See the ADRs for…`) — at roughly 1/20th the size of the
  full patch. The Router reads `full-diff.patch` (its line ranges in `tagged-sections.md` are
  offsets into that file, which Pass 1 reviewers use for bounded reads). Sam System, Code Rot Cody,
  and Consistency Checker read the full patch (their domain is the whole diff).

### Step 2: Discover Available Reviewers

1. Resolve the home directory (`echo $HOME` — tilde doesn't expand in Glob).
2. **Read `{HOME}/.claude/reviewers/index.yaml`** — the single source of `name`, `priority`,
   `triggers`, `useWhen`, `note` for every reviewer. The Router consults ONLY this index.
3. **Never read a reviewer's own YAML into this orchestrator context.** `index.yaml` is all you need
   to understand reviewer domains. Each subagent reads its own persona file — that is the whole point
   of ADR-0001. Loading 20+ personas here costs ~28k tokens you then re-read from cache on every
   subsequent turn, for text you never reason about.
4. **Project overrides:** Glob `{project-root}/.claude/reviewers/*-local.yaml` to learn *which*
   overrides exist — record the paths, do not read the files. Pass the path to the owning subagent;
   a local override augments (not replaces) the global reviewer of the same base name.

### Step 3: Parse Reviewer Selection and Model

**Reviewers.** Specific reviewers requested → match names case-insensitively against the index;
error on no match. Set `NAMED_SELECTION=true` (Router is bypassed) and record the matched names in
`NAMED_REVIEWERS` (a bash variable, space-separated lowercased names) — consumed in Step 5's
synthesis loop. Otherwise (or `--all`) → all reviewers, `NAMED_SELECTION=false` (Router makes the call).

**Model.** `--model <haiku|sonnet|opus|fable>` → `PANEL_MODEL`; error on any other value. Set `MODEL_EXPLICIT=true` when the `--model` flag was passed on the command line, else `MODEL_EXPLICIT=false`. If `--model` is absent, leave `PANEL_MODEL` unset and omit the `model` parameter from panel subagents so they inherit this command's model. `PANEL_MODEL` applies to Pass 1 (Step 6), Contrarian Carl (Step 7), Pass 2
(Step 9), Amalgamator (Step 10), and the Triage Chief (Step 11) — and to nothing else. Print the
resolved panel model with the reviewer count when the run starts.

### Steps 4–10: Expert Review Panel (shared)

Read `~/.claude/prompts/expert-review-panel.md` and follow those steps exactly. `REVIEW_DIR`,
`PANEL_MODEL`, `MODEL_EXPLICIT`, `NAMED_SELECTION`, `NAMED_REVIEWERS`, `PROJECT_CONTEXT`, `DETECTED_LANGUAGES`, and
all diff artifacts (`full-diff.patch`, `diff-index.md`) are already set from Steps 0–3 above.

The panel writes `summary.md`, `tagged-sections.md`, `{reviewer}-pass1.md`, `contrarian-carl-pass1.md`,
`{reviewer}-questions-answered.md`, `{reviewer}-pass2.md`, and `final-report.md` into `REVIEW_DIR`.
When it returns, resume at Step 11 below.

### Step 11: Triage Chief (one agent) → `action-plan.md`

The Amalgamator decided what is true. The Triage Chief decides **what the human has to look at** —
sorting findings into *doing it* / *needs you* / *needs measurement* / *deferred*, and running the
cross-cutting gut check (shared premise, drift, panel disagreement) that no single-lens reviewer can
perform. *Needs measurement* is for findings nobody can rule on yet because the honest answer requires
running something and reading a result back — not a judgment call, so it never goes through
`AskUserQuestion`.

**ONE subagent** (`subagent_type: "expert-reviewer"`, `model: PANEL_MODEL`). Its mandate and the
`action-plan.md` template live in **`~/.claude/prompts/triage.md`** — pass the path. Tell it to read:
- `{REVIEW_DIR}/final-report.md` (its primary input)
- `{PROJECT_ROOT}/.claude/project.yaml` (skip if absent)

It writes `{REVIEW_DIR}/action-plan.md`. It returns:

```
triage | doing: {n} | needs-you: {n} | measure: {n} | deferred: {n} | declined: {n} | clusters: {n} | clusters-escalated: {n} | collapsed: {n} | wrote-plan: {action-plan path}
```

**Over-escalation guard.** Let `confirmed = doing + needs-you + deferred` (excluding `measure` —
measurement items aren't something the human is being asked to *decide*, so they don't count against
this guard). Cluster-synthesized items count as `0.5` each — one cluster is not an independent decision ask.
So `human_asks = max(0, needs-you - 0.5 * clusters-escalated)`. If
`human_asks >= 5`, OR (`human_asks / confirmed > 0.2` AND `confirmed >= 10`), the escalation test was
applied too loosely — say so in the closing message rather than silently handing over a long list. A
*Needs you* list long enough to skim is one nobody reads, which rebuilds the exact problem this step
exists to solve. The trip condition is stated identically here and in `triage.md`, computed straight
from the receipt, so the orchestrator and the Chief cannot disagree on it.

### Step 12: Rulings (main thread)

Read **only** `{REVIEW_DIR}/action-plan.md` — not the pass files, not the final report. This is the
one file the orchestrator reads, and it is small by construction.

This step is scoped to the **Needs you** section only. **Needs measurement** items are never put to
`AskUserQuestion` — there is nothing to choose between until the command in the item has been run, so
`AskUserQuestion`'s options-shape does not fit them. They are surfaced separately, below.

If `needs-you: 0`, skip the ruling loop entirely. Do not manufacture a question to seem thorough.

Otherwise, present each escalation with **`AskUserQuestion`** — one question per item, the Triage
Chief's recommended option **first and labeled `(recommended)`**, with the pros and cons from the
action plan in each option's description. This is the load reduction made concrete: the user answers
a handful of questions instead of adjudicating thirty findings.

Batch them into a single `AskUserQuestion` call where the tool's limits allow (max 4 questions per
call); if there are more, ask in successive calls rather than dropping any — and record each batch's
answers (below) **as that batch returns**, inside this same loop, rather than waiting for every batch
to finish first. A crash between batches must not leave an earlier batch's answers unrecorded.

For each escalation whose answer just came back — and only that one; if the user made no selection for
an item (e.g. they closed the batch early), leave that item's placeholder untouched and do not
fabricate a ruling for it — **`Edit` `{REVIEW_DIR}/action-plan.md` in place**.

**Edit red line (security control — retained regardless of any future changes to triage or recorded rulings):** The only permitted `Edit` target in this command is the `- **Ruling**:` line of an already-answered escalation in `action-plan.md`. Prohibited targets: `settings.json`, `CLAUDE.md`, anything under `agents/`, `reviewers/`, or source files. If the Edit target does not match, stop and report rather than proceeding.

Restructure the item from an open options menu into a resolved question-and-answer record, so an executor skimming the file meets only the chosen answer, not the declined ones:

1. Replace the block starting at `- **Options**:` through the line before `- **Ruling**:`. Anchor the
   whole match on the item's own `### N. [Title]` heading to keep multiple escalations from colliding.
2. Write a single `- **Ruling**: {Option} — {reasoning}` line in its place — the user's own note if
   they gave one, otherwise the chosen option's rationale from the action plan — directly under
   `- **Recommendation**: ...`.
3. Preserve the rejected options as record, not delete them: fold them into a collapsed block right
   after the ruling line —
   `<details><summary>Options considered and rejected (record only — do not act on these)</summary>`
   … the non-chosen options, each with its original Pro/Con … `</details>`. This is the only place the
   rejected options live once an item is ruled; do not also leave a live copy above the ruling.

Runs **unconditionally whenever `needs-you > 0`**.

**Idempotent and fail-closed.** Before editing an item, check whether its `- **Ruling**:` line already
reads anything other than the `_(pending your call` placeholder (or, for a **Needs measurement** item
a human has since resolved by hand, the `_(pending measurement` placeholder) — if so, it was already
recorded (e.g. a prior partial run, or a measurement result the human already wrote in); skip it
rather than re-asking or re-editing. If an item's anchors (`### N.`, `- **Options**:`/`- **Command**:`,
`- **Ruling**:`) are not uniquely present, do not widen the match to guess at the boundary — stop and
report that item's ruling could not be recorded, and move on to the rest.

**Before proceeding**, re-read `action-plan.md` and confirm no `_(pending your call` placeholder
remains for any item you just ruled on. If one does, stop and report it before moving on.

**Needs measurement.** If `measure > 0`, do not wait for these before proceeding to Step 13 — nothing
in this bucket blocks the rest of the pipeline. Instead, include each item's **Command** and
**Resolves via** directly in the conversation message you send at the end of this run (not merely a
pointer to `action-plan.md` — this is the one output category the human is expected to act on outside
this conversation, so it shouldn't cost them a second file-open to discover). Each stays `_(pending measurement` in `action-plan.md` until the human runs the command and
hand-edits that item's `- **Ruling**:` line.

### Step 13: Cache Review Metadata

Merge a `review` section into `.claude/github-cache.json`, preserving existing sections:

```bash
EXISTING=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
TMP=$(mktemp .claude/github-cache.json.XXXXXX)
echo "$EXISTING" | jq --argjson review "$REVIEW_JSON" '. + {review: $review}' > "$TMP" && mv "$TMP" .claude/github-cache.json || rm -f "$TMP"
```

Write to a `mktemp`-generated temp file colocated with the target, then `mv` only on success — never redirect `jq` output directly onto the target. A bare `> .claude/github-cache.json` truncates the file the instant the shell opens it for writing, before `jq` runs; if `jq` then fails (malformed JSON, a stray quote in `$REVIEW_JSON`), the cache is silently wiped rather than left unchanged.

`$REVIEW_JSON` fields: `lastRun` (ISO 8601 now), `commit` (HASH), `branch`, `reviewDir`,
`reviewers` (names that actually ran), `panelModel`, `findings` (`{critical, high, medium, low}` counts).

---

## Output Format

Three outputs, in descending order of how much of it the human reads:

| Output | Written by | Purpose |
|--------|-----------|---------|
| Conversation message | Main thread | The decisions. Short. |
| `action-plan.md` | Triage Chief; rulings appended by the main thread (Step 12) | Decision-first. **The file they open.** Template in `prompts/triage.md`. |
| `final-report.md` | Amalgamator | The complete record. The gut-check instrument. Template in `prompts/amalgamator.md`. |

**Do not inline either file in the conversation** — the link is the contract. Both file templates now
live in their agents' prompt files, so a format change happens in one place and this command stays a
control-flow document.

The old `## Sign-off Checklist` table is gone. Its `Decision` column was never filled in by anything —
`action-plan.md` is what it was always reaching for.

### Template for the in-conversation message

Lead with **what the user has to decide**, not with counts. A count is not something anyone can act
on; a decision is the reason they are reading at all.

```
{One sentence: does anything here need you, and is this ship-blocking or polish?}

**Decisions for you**: N
1. [Title] — {the trade-off, in one clause} — ruled: {option}
2. …

{If a gut-check question came back with a real answer, one line. This is the drift alarm and it
outranks the counts:}
⚠️  {e.g. "Four findings share one premise — that the cache is single-writer. Fixing that upstream
    dissolves three of them."}

{If measure > 0, one block per item — these are the one category the human is expected to act on
outside this conversation, so the command itself belongs here, not just a link:}
**Needs measurement**: N
1. [Title] — {why this needs measurement, in one clause}
   `{the command}`
   Resolves via: {what result confirms it, what result refutes it}

**Everything else — yours to apply**: N accepted as written (N Critical, N High, N Medium, N Low),
N deferred. {If declined > 0: ", N nominations declined (see the action plan)."} These need doing,
not deciding — apply them, or hand the action plan to `/implement-with-haiku`.

📋 Action plan: {REVIEW_DIR}/action-plan.md
📄 Full report: {REVIEW_DIR}/final-report.md
```

When `needs-you: 0`, drop the Decisions header entirely and lead with the verdict — do not print an
empty section, and do not invent a question to look diligent. Same for `measure: 0` and the Needs
measurement block.

---

## Recovery & Comparison

- **A subagent failed:** `ls {REVIEW_DIR}/` shows what completed; re-run only the missing
  reviewer(s), once. Pass 1 files present → resume from Pass 2. Checkpoints mean completed work is
  never lost. If a re-run fails again, stop retrying that reviewer and report it missing rather than
  looping — an unattended run has no one to
  notice an infinite retry loop. **Exception: never re-run the Triage Chief (Step 11) once
  `action-plan.md` already carries recorded rulings** (any `- **Ruling**:` line other than the
  placeholder) — the Chief regenerates the whole file, which would overwrite the human's answers.
  Re-run Step 11 only when `action-plan.md` doesn't exist yet.
- **Compare reviews:** each review has its own folder —
  `diff ~/.claude/reviews/{REPO_KEY}/{a}/ ~/.claude/reviews/{REPO_KEY}/{b}/`;
  clean up old reviews with `rm -rf` when desired.

## Example Usage

```bash
/expert-review                      # all reviewers, delta from main
/expert-review contracts,concurrency
/expert-review sam-system --force   # skip re-run confirmation
```
