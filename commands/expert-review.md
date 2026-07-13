---
description: Smart expert code review with triage - works across all projects
argument-hint: [reviewers...] [--model haiku|sonnet|opus|fable] [--full] [--all] [--force]
allowed-tools: Bash(git diff:*), Bash(git branch:*), Bash(git log:*), Bash(git rev-parse:*), Bash(git show:*), Bash(git status:*), Bash(git -C:*), Bash(mkdir:*), Bash(rm:*), Bash(echo:*), Bash(gh:*), Bash(ls:*), Bash(BRANCH=:*), Bash(HASH=:*), Bash(PROJECT=:*), Bash(REVIEW_DIR=:*), Read, Glob, Grep, Task, Write
model: opus
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
8. **Verify → Cache metadata** (main thread)

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
  Carl, and **Amalgamator**. Default: inherit this command's model (`opus`). Three tiers per ADR-0004:
  **Router** (Step 5) = sonnet (judgment, narrow, economical); **Mechanical roles** (Q&A, Code Rot Cody,
  Consistency Checker) = haiku (routing and grep are model-agnostic); **Judgment panel** (Pass 1, Carl,
  Pass 2, Amalgamator) = PANEL_MODEL (your `--model` choice, or inherited).
- `--full`: review the entire codebase instead of just changed files
- `--force` (alias `-y`): skip the re-run confirmation when a prior review exists for this branch

  Cost per 1M tokens (in/out), cheapest first: **haiku** $1/$5 · **sonnet** $3/$15 · **opus** $5/$25
  · **fable** $10/$50. Fable is the most capable *and* the most expensive — 2× Opus — it is the
  deliberate expensive step, used by the Amalgamator to resolve conflicts and severity-rank findings.
  Opus is the default panel tier.

Examples: `/expert-review --model haiku` (whole panel, cheapest — good for a smoke test) ·
`/expert-review rachel,security-sage` (two reviewers, no router) ·
`/expert-review --model fable` (use fable for the amalgamator and panel)

## Checkpoint Files

All artifacts live in `{REVIEW_DIR}` = `~/.claude/reviews/{project}/{branch}-{short_hash}/`
(persists across reboots; one subfolder per review enables comparing reviews):

| File | Written by | When |
|------|-----------|------|
| `full-diff.patch` | Main thread | Step 1 — the full delta, ~1 char/token; large on purpose |
| `diff-index.md` | Main thread | Step 1 — `git diff --stat` + hunk headers only, ~20× smaller |
| `summary.md` | Summarizer | Step 4 — Technical Summary + Business Context |
| `tagged-sections.md` | Router | Step 5 — section → reviewer routing with Panel Decision (includes/excludes) |
| `{reviewer}-pass1.md` | Each Pass 1 subagent | Step 6 (Consistency Checker + Cody included) |
| `contrarian-carl-pass1.md` | Carl | Step 7 — no Pass 2, presented as-is |
| `{reviewer}-questions-answered.md` | Haiku Q&A | Step 8 — only reviewers with open questions |
| `{reviewer}-pass2.md` | Pass 2 subagents | Step 9 — only reviewers with findings, judgment reviewers only |
| `final-report.md` | Amalgamator | Step 10 |

---

## Instructions

### Step 0: Setup

1. Resolve paths and create the checkpoint directory:
   ```bash
   BRANCH=$(git rev-parse --abbrev-ref HEAD | tr '/' '-')
   HASH=$(git rev-parse --short HEAD)
   PROJECT=$(basename "$(git rev-parse --show-toplevel)")
   REVIEW_DIR="$HOME/.claude/reviews/${PROJECT}/${BRANCH}-${HASH}"
   mkdir -p "$REVIEW_DIR"
   ```

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
     already reviewed. Re-run anyway? (will overwrite prior results)"; different commit →
     "Re-run for the current commit? (prior results in `{review.reviewDir}` are preserved; new
     results go to a different folder)". Wait for explicit confirmation; exit cleanly if declined.
   - **Fallback (no cache):** search `~/.claude/plans/*.md` for mentions of this branch/project;
     also check for kanban files (`*-kanban.md`) in project root or docs/.
   - Plan context found → give it to the summarizer (Step 4) and to Sam System as "Known
     Integration Concerns" (Step 6); cross-reference in the final report.

### Step 1: Determine Review Scope

- Default (delta): `git diff --name-only main...HEAD`. If empty, inform the user and exit.
- `--full`: review the entire `src/` directory.
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
error on no match. Set `NAMED_SELECTION=true` (Router is bypassed). Otherwise (or `--all`) →
all reviewers, `NAMED_SELECTION=false` (Router makes the call).

**Model.** `--model <haiku|sonnet|opus|fable>` → `PANEL_MODEL`; error on any other value. If absent,
leave `PANEL_MODEL` unset and omit the `model` parameter from panel subagents so they inherit this
command's model. `PANEL_MODEL` applies to Pass 1 (Step 6), Contrarian Carl (Step 7), Pass 2
(Step 9), and Amalgamator (Step 10) — and to nothing else. Print the resolved panel model
with the reviewer count when the run starts.

### Step 4: Summarizer → `summary.md`

Spawn one subagent (`subagent_type: "Explore"`) with the summarizer prompt
@~/.claude/prompts/summarizer.md, pointing it at `{REVIEW_DIR}/full-diff.patch` (it needs the actual
diff text to summarize) rather than inlining `git diff` output, plus: changed-file list, commit
messages (`git log main...HEAD --format="%s%n%n%b"`), PR description if available, and any
known-issues index. Save its output to `{REVIEW_DIR}/summary.md`. The file contains
`## Technical Summary` (what), `## Business Context` (why), `## Suggested Reviewers`.

### Step 5: Router (sonnet) → `tagged-sections.md`

Spawn a subagent (`subagent_type: "expert-reviewer"`, `run_in_background: false`, `model: "sonnet"` —
model explicitly pinned to sonnet here, a narrow judgment task independent of the panel tier) with the router prompt @~/.claude/prompts/router.md. The router reads:
- `{REVIEW_DIR}/full-diff.patch` (it needs the full patch: the line ranges it emits are offsets into
  that file, which later reviewers use for bounded reads)
- The `{REVIEW_DIR}/summary.md` (Technical Summary and Business Context)
- The plan/issue context (if any)
- `reviewers/index.yaml` **ONLY** — the router must not load persona YAML files; progressive
  disclosure means routing decisions are made from each expert's declared interest (`triggers`,
  `useWhen` in the index), not their full personas

The router outputs `{REVIEW_DIR}/tagged-sections.md` with:
1. `## Panel Decision` — a summary of which reviewers were selected and why, formatted as:
   ```
   | Reviewer | Selected | Reason |
   | ... | Yes | {1-line justification} |
   | ... | No | {1-line justification} |
   ```
2. Per-reviewer sections with line ranges, exactly as today's router outputs them, so Pass 1 reviewers
   can use them for bounded reads.

**Always-run set (never routed, pre-seated):**
- Sam System, Code Rot Cody, Consistency Checker (they get the full diff by domain, not by routing),
- Contrarian Carl (runs last, always).

The router is told these four are pre-seated and to treat them as included for the decision table.

**Named reviewers:** If the user named specific reviewers (Step 3), skip the router entirely — the
user's selection *is* the decision, and all four always-run reviewers still participate.

### Step 6: Pass 1 Blind Reviews (parallel subagents) → `{reviewer}-pass1.md`

Read `tagged-sections.md` and parse which reviewers were selected by the router. Launch all selected
reviewers (routed by the router OR named by the user OR always-run) in ONE message. All run as
**`subagent_type: "expert-reviewer"`**, `run_in_background: false`, `model: PANEL_MODEL` from Step 3.

**Launch ALL Pass 1 reviewers in ONE message** (multiple Task calls in a single assistant turn).
One subagent per reviewer. They still run concurrently — the harness caps concurrency — and they have
all returned by the time you continue.

**Why a custom agent, not `general-purpose`.** Twenty concurrent subagents reading persona files and
writing checkpoints outside the working directory would produce twenty near-identical permission
dialogs — the permission system does not deduplicate across concurrent agents, and this command's
`allowed-tools` frontmatter does not propagate to subagents it spawns. `expert-reviewer` is
capability-restricted instead of dialog-gated: `permissionMode: bypassPermissions`, but **no `Edit`
tool and no write-capable Bash**, so a reviewer physically cannot modify the code it is reviewing.
It reads the repo and writes one file. Same for `expert-scout` on the mechanical roles.

**Pass paths, not contents.** A prompt is self-contained if the subagent can *reach* everything it
needs, not if you paste everything into it. Every `Agent` prompt you write stays in your context for
the rest of the run and is re-read from cache on every turn — so inlining a 11.4KB framework into 20+
prompts costs you ~50k tokens and buys the subagent nothing it couldn't have read itself. Open each
prompt with:

```
Before reviewing, read these files with the Read tool:
  1. ~/.claude/prompts/expert-framework.md  — the canonical output format, response levels,
     severity definitions, scope-expansion and when-not-to-flag rules. Follow it exactly.
  2. ~/.claude/reviewers/{name}.yaml        — your persona. Use `codeReview.prompt` as your
     review lens. If it has a `languageExtensions` key with entries matching any of
     {DETECTED_LANGUAGES}, apply those too, under "Language-Specific Checks ({language})".
  3. {path to {name}-local.yaml}            — only if one was found in Step 2; it augments (2).
```

Then supply inline **only what you alone know** — none of it is on disk for the subagent to find:

- Their tagged sections — as line ranges into `{REVIEW_DIR}/full-diff.patch`, plus:
  ```
  Your sections are line ranges into {REVIEW_DIR}/full-diff.patch. Read ONLY those ranges
  (the Read tool takes offset/limit). Reading whole source files when a finding needs
  surrounding context is expected and correct — reading the whole patch file is not.
  ```
- The **Technical Summary** from `summary.md`
- `PROJECT_CONTEXT`, project modifiers, `DETECTED_LANGUAGES`, and the strict delta-scope rule below
- `{REVIEW_DIR}` and their output path

**The file is the contract — never ask a subagent to return its report.** Instruct each reviewer to
Write its full review to `{REVIEW_DIR}/{reviewer}-pass1.md` in the framework's canonical format, and
to return **only a one-line receipt** as its final message:

```
Write your complete review to {REVIEW_DIR}/{reviewer}-pass1.md using the Write tool.

Your final message must be ONLY this receipt line — NOT the review itself:

  {reviewer} | {SKIP|QUICK-SCAN|DEEP-DIVE} | findings: {n} ({c}C/{h}H/{m}M/{l}L) | open-questions: {n} | wrote: {path}
```

A subagent's final message comes back to you *twice* — once as the `Task` tool result, and again in
the `<result>` block of its completion notification. Returning a 5KB review from 20+ reviewers puts
~130k tokens of text into your context that you do not need yet and will read from the files anyway
later. The receipt carries everything downstream steps actually branch on (decision level,
whether there are findings, whether there are open questions) — used by Step 8 (Q&A), Step 9 (Pass 2),
and Step 10 (Amalgamator). If an agent dies without writing its file, verify the file after the join
barrier and re-run that one reviewer — that is cheaper than taxing every run to insure against a rare
failure.

**Blindness rule: Pass 1 prompts must NOT include Business Context, commit messages, or the PR
description** — only the Technical Summary and the code. This is the point of running them as
fresh subagents.

Strict delta-scope rule (include in every prompt unless `--full`):
```
SCOPE: STRICT DELTA REVIEW — only report issues INTRODUCED or WORSENED by this PR.
Do NOT report pre-existing issues in unchanged code. If the PR makes an existing
issue worse, report it; if it doesn't touch it, skip it.
```

Three reviewers **always run and are never gated** (the router does not route them; their domain is
the whole diff by definition). They get special inputs but run in the same parallel batch — and they
follow the same rules as everyone else: they read their own YAML by path, and they return a receipt,
not a report. (Their output *formats* differ — those formats are defined in their own YAMLs, which
they read themselves; you do not need to know them here.)

- **Sam System** (integration): gets `{REVIEW_DIR}/full-diff.patch` (not tagged sections — his
  domain is the whole diff, so he reads the whole file), the Technical Summary, and any plan context
  as "Known Integration Concerns". He must trace data flow across files — read both ends of every
  factory/event-bus/config connection and flag parameters passed but never used. Output: canonical
  format (he is NOT an ADR-0006 carve-out); each finding's **Issue** field starts with the data-flow
  trace, e.g.
  `Flow createSession (a.ts:12) → createRecordingSession (b.ts:30): eventBus passed but never destructured`.
  Decision is always DEEP-DIVE.

- **Code Rot Cody** (`subagent_type: "expert-scout"`, ADR-0006 carve-out): gets
  `{REVIEW_DIR}/full-diff.patch` + changed-file list. He greps the ENTIRE repo to verify every
  claim — never guesses. New symbols: grep for callers (excluding definition site), flag zero-caller
  symbols DEAD. Removed symbols: grep for lingering references, flag ORPHANED. New config fields:
  verify stored, read, validated, documented. His output format (symbol-inventory table) and his
  `languageExtensions` are in his own YAML, which he reads.

- **Consistency Checker** (`subagent_type: "expert-scout"`, ADR-0006 carve-out): gets
  `{REVIEW_DIR}/full-diff.patch` + the PR description (from cache or `gh pr view --json body`); it
  reads its own persona file for the review lens, like every other subagent. Mechanical pattern
  pass: mixed error types for the same purpose, inconsistent cleanup patterns, PR-description claims
  contradicted by the code. Its output format is defined in its own YAML.

**Join barrier.** All Step 6 agents launched in one message with `run_in_background: false` means
they have all returned by the time you continue. Then verify `{REVIEW_DIR}/{reviewer}-pass1.md`
exists for every selected reviewer. If a receipt came back but its file is missing, **re-run that one
reviewer** — do not try to reconstruct the review from the receipt; the receipt is a status line, not
a report.

**Never poll.** Do not use `ScheduleWakeup`, `sleep`, or repeated status checks to wait for
subagents. A timed wakeup re-reads your *entire* context from cache and learns nothing you would not
have learned by waiting — in one observed run, 14 such wakeups each re-read ~430k tokens. If a panel
is large enough that you truly want it backgrounded, then **end your turn**: the harness re-invokes
you when the agents finish. Track per-reviewer status by checking for files, never by counting
notifications.

### Step 7: Contrarian Carl (after the barrier) → `contrarian-carl-pass1.md`

Carl runs **last** and is the one reviewer who is not blind to the panel. Spawn one subagent
(`subagent_type: "expert-reviewer"`, `model: PANEL_MODEL`) and, per "Pass paths, not contents",
point him at the files rather than inlining them: his persona (`~/.claude/reviewers/contrarian-carl.yaml`),
the other reviewers' `{REVIEW_DIR}/*-pass1.md` files (including Cody's and the Consistency Checker's).
Supply the diff scope inline. His instruction:

```
You have access to what EVERY other reviewer found. Your job is to find something
DIFFERENT. Do not repeat any finding already raised; look where others didn't look;
question assumptions everyone shared. Raise the strongest concern nobody else
mentioned — or, if after genuine effort you find none, name the strongest candidate
concern you considered and explain why you rejected it. Do NOT manufacture a finding
just to have one.
```

His contrastive output format (What Others Covered / What Everyone Missed / Assumptions I'm
Questioning / The Question Nobody Asked / Verdict) is defined in his own YAML (ADR-0006
carve-out). He writes `{REVIEW_DIR}/contrarian-carl-pass1.md` and returns a receipt only —
`contrarian-carl | findings: {n} | wrote: {path}` — like every other panel agent. He does NOT
participate in Pass 2; his findings are presented as-is.

### Step 8: Haiku Q&A (parallel) → `{reviewer}-questions-answered.md`

Runs BEFORE Pass 2 so the re-evaluation is informed rather than speculative (ADR-0002). For each
reviewer whose Pass 1 receipt reported `open-questions > 0`: spawn a subagent
(`subagent_type: "expert-scout"`) and point it at `{REVIEW_DIR}/{reviewer}-pass1.md` — it reads
the Open Questions itself, so you never have to load them. Supply the reviewer's name and role
summary (from `index.yaml`) and their tagged sections, plus: "Read the hinted files plus whatever
else is needed to answer concretely. If a question can't be settled by static analysis, say so and
name the runtime evidence needed."

It writes `{REVIEW_DIR}/{reviewer}-questions-answered.md` — **Answer** + **Evidence** (`path:line`)
per question — and returns a receipt only: `{reviewer} | answered: {n} | wrote: {path}`. Launch all
Q&A agents in one message.

### Step 9: Pass 2 Re-evaluations (parallel subagents) → `{reviewer}-pass2.md`

**Only for judgment reviewers whose Pass 1 receipt reported findings > 0.** Mechanical roles
(Code Rot Cody, Consistency Checker) skip Pass 2. Carl is not re-evaluated; his findings stand as-is.

Launch one subagent per eligible reviewer, in one message (`subagent_type: "expert-reviewer"`,
`model: PANEL_MODEL`). Pass paths, not contents — each prompt names the files to Read:

- `~/.claude/prompts/pass2-reevaluation.md` — the pass2 prompt and output format
- `{REVIEW_DIR}/{reviewer}-pass1.md` — their own Pass 1
- `{REVIEW_DIR}/{reviewer}-questions-answered.md` — if one exists
- Permission to read any file referenced in their findings, to resolve uncertainty

Supply inline only the **Business Context** section from `summary.md` — revealed now for the first
time, and the whole point of Pass 2. Also supply the plan/issue context if available.

**Reframed as skeptic-verifier (anti-anchoring).** The prompt's framing changes from "continue your
review" to "another engineer submitted this review; given the context, which findings hold up to
your standards?" — third-person, minimal context, explicitly designed to prevent sunk-cost defense.

Each re-evaluates every finding as CONFIRMED / RESOLVED / DOWNGRADED (with reason and final
severity), writes `{REVIEW_DIR}/{reviewer}-pass2.md` in the pass2 prompt's format, and returns a
receipt only:

```
{reviewer} | pass2 | confirmed: {n} | resolved: {n} | downgraded: {n} | wrote: {path}
```

### Step 10: Amalgamator (one expensive agent) → `final-report.md`

**Before spawning the Amalgamator,** verify all expected checkpoint files exist (pass1 for every
routed reviewer, pass2 where findings, questions-answered where open questions).
If any are missing, report which agents failed and re-run only those.

The Amalgamator is **ONE subagent** (`subagent_type: "expert-reviewer"`, `model: PANEL_MODEL`; this
is the step where `--model fable` earns its cost). Its job: synthesis, not review. It reads:
- All `{REVIEW_DIR}/*-pass1.md` files (including Carl's)
- All `{REVIEW_DIR}/*-pass2.md` files (re-evaluation verdicts)
- All `{REVIEW_DIR}/*-questions-answered.md` files
- The `{REVIEW_DIR}/tagged-sections.md` (router's Panel Decision)
- The plan/issue context (if any)

Its mandate (in the prompt):
```
You are synthesizing a code review from specialists who have already reported. Your job is NOT to
add new findings or re-review the code — it is to:

1. DEDUPLICATE — if multiple reviewers flagged the same issue, report it once, noting who agreed
   and why their angles differed.
2. SEVERITY-RANK — given each CONFIRMED finding and its evidence, assign final severity
   (CRITICAL > HIGH > MEDIUM > LOW) and prioritize (most critical first).
3. RESOLVE CONFLICTS — where reviewers disagree, note who is right and why. Reference their
   pass1/pass2 reasoning.
4. SEPARATE SIGNAL FROM NOISE — DOWNGRADED findings vs CONFIRMED; findings from Carl (who sees all
   priors) vs the blind panel.
5. WRITE final-report.md in the template format below.
```

It writes `{REVIEW_DIR}/final-report.md` and returns a receipt with a short verdict (ship-blocking?
polish-only?) and the finding count summary:

```
amalgamator | final-report written | critical: {n} | high: {n} | medium: {n} | low: {n} | wrote: {path}
```

### Step 11: Cache Review Metadata

Merge a `review` section into `.claude/github-cache.json`, preserving existing sections:

```bash
EXISTING=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
echo "$EXISTING" | jq --argjson review "$REVIEW_JSON" '. + {review: $review}' > .claude/github-cache.json
```

`$REVIEW_JSON` fields: `lastRun` (ISO 8601 now), `commit` (HASH), `branch`, `reviewDir`,
`reviewers` (names that actually ran), `panelModel`, `scope` (`"delta"`/`"full"`),
`findings` (`{critical, high, medium, low}` counts).

---

## Output Format

Two outputs — the file is complete, the conversation message is short. **Do not inline the full
report in the conversation**; the link is the contract.

### Template for `final-report.md`

```markdown
# Code Review Report

**Date**: YYYY-MM-DD | **Branch**: … | **Commit**: … | **Project**: …
**Scope**: Delta from main | Full codebase | **Files Reviewed**: N
**Checkpoint Directory**: ~/.claude/reviews/{project}/{branch}-{hash}/

## Executive Summary
- **Reviewers Run**: N (names, router-selected or user-named)
- **Panel model**: {PANEL_MODEL or "inherited (opus)"}
- **Total Findings**: N — Critical: N, High: N, Medium: N, Low: N
- **Context Re-evaluation**: CONFIRMED: N, RESOLVED: N, DOWNGRADED: N

## Technical Summary
[from summary.md — what changed]

## Findings by Severity

### Critical / High / Medium / Low (repeat per severity)

#### [Finding Title]
- **Reviewer**: … | **File**: path:line
- **Issue** / **Impact** / **Recommendation**
- **Context Re-evaluation**: CONFIRMED | RESOLVED | DOWNGRADED (+ notes if changed)
- **Known Issue**: #NNN (if matches)

## Reviewer Summary

| Reviewer | Decision | Findings | Confirmed | Notes |
|----------|----------|----------|-----------|-------|

Decision legend: `DEEP-DIVE` thorough investigation · `QUICK-SCAN` quick look at tagged sections ·
`ROUTED` selected by router · `ALWAYS-RUN` (Sam System, Code Rot Cody, Consistency Checker, Carl) ·
`CODE-ROT` mechanical grep verification · `CONTRARIAN` ran last with all prior findings

## Routing Accuracy

| Reviewer | Router said | Selected | Reason |
|----------|-------------|----------|--------|

The routing decision table: every reviewer in the index, marked Selected/Not Selected, with the
router's one-line justification. This table is the input to `/review-stats` for evaluating router
accuracy.

## Answered Questions
| Reviewer | Question | Answer |   (omit if none)

## Recommended Next Steps
1. [prioritized actions from CONFIRMED findings]

## Sign-off Checklist
| Item | Severity | Recommendation | Decision |
```

### Template for the in-conversation message

```
{One-paragraph verdict: ship-blocking? polish-only? regressions vs prior review?}

**Findings**: N Critical, N High, N Medium, N Low.

**Top items requiring attention**:
1. [most important] — file:line — one sentence
2. …

**Top recommended actions** (priority order):
1. …

📄 Full report: ~/.claude/reviews/{project}/{branch}-{hash}/final-report.md
```

---

## Recovery & Comparison

- **A subagent failed:** `ls {REVIEW_DIR}/` shows what completed; re-run only the missing
  reviewer(s). Pass 1 files present → resume from Pass 2. Checkpoints mean completed work is
  never lost.
- **Compare reviews:** each review has its own folder —
  `diff ~/.claude/reviews/{project}/{a}/ ~/.claude/reviews/{project}/{b}/`;
  clean up old reviews with `rm -rf` when desired.

## Example Usage

```bash
/expert-review                      # all reviewers, delta from main
/expert-review contracts,concurrency
/expert-review --full               # entire codebase
/expert-review sam-system --force   # skip re-run confirmation
```
