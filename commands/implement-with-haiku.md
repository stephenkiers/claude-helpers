---
description: "Parallel round-1 Haiku implementers, orchestrator-owned integration gate with anti-cheat scanning, bounded convergence loop, machine-checked spec-blind, adversary review."
allowed-tools: Read, Bash(gh issue view:*), Bash(git log:*), Bash(git branch:*), Bash(git rev-parse:*), Bash(git ls-tree:*), Bash(git diff:*), Bash(git worktree:*), Bash(git apply:*), Bash(git add:*), Bash(git status:*), Bash(git commit:*), Bash(git checkout HEAD -- *), Bash(git checkout * -- *), Bash(git mv:*), Bash(git rm:*), Bash(git branch -D:*), Bash(git branch -d:*), Bash(pwd:*), Bash(find:*), Bash(date:*), Bash(echo:*), Bash(cat:*), Bash(wc:*), Bash(grep:*), Bash(rg:*), Bash(mktemp:*), Bash(cargo:*), Bash(npm:*), Bash(npx:*), Bash(pnpm:*), Bash(yarn:*), Bash(swift:*), Bash(xcodebuild:*), Agent
---

# Implement with Haiku

Splits the plan into independent work units, runs them as parallel background `plan-implementer`
agents in isolated worktrees, then applies an **orchestrator-owned integration gate** before handing
off to spec-blind test writing and adversary review.

The flow:

1. **Round 1 — Implementer(s)** — one Haiku per work unit, in parallel worktrees, staging diffs
   for the orchestrator to apply and commit
2. **Integration gate** — orchestrator runs build/type-check + anti-cheat scan + bounded fix loop
3. **Round sizing** — classify the run (mechanical / test-only / full) to skip rounds that don't apply
4. **Round 2 — Spec-blind test author** (own worktree) and **Round 3 pass 1 — Adversary** (read-only)
   run **concurrently**, alongside two read-only sweeps (duplication, doc-drift)
5. **Round 3 follow-up** — short pass applying earlier findings and reviewing round 2's tests
6. **Round 4 — Test cleanup** (orchestrator-run) — delete clearly-junk tests, relocate + rename the
   survivors to the repo's own test layout/naming convention

## Step 1: Find the plan

In priority order:

0. **Args contain a path to an existing `claude-action-plan.md` file** → read it directly, set
   `PLAN_SOURCE=claude-action-plan`. If the path doesn't exist, fall through to priority 1–3 below
   with a one-line warning.
1. **Args contain an issue number or URL** → fetch with `gh issue view <number>`
2. **`.claude/github-cache.json` exists** → read it, use `issue.body` as the plan
3. **A plan is visible in the current conversation** → use it directly

If none yield a plan, tell the user and stop.

### Parse claude-action-plan.md into directives

**Note:** This subsection's assumptions about `claude-action-plan.md`'s shape (section headings,
table columns, `STATUS`/`DECISION` fields) are defined authoritatively in `prompts/triage.md`'s
`## Output` section. If triage.md's template changes, re-check this subsection's rules against it.

When `PLAN_SOURCE=claude-action-plan`, first run a structural pre-check for the pre-migration
`action-plan.md` format (old `- **Ruling**: _(pending...)_` placeholder style, no `STATUS`/`DECISION`
fields). **Do not use "zero `- **STATUS**:` matches" alone as the signal** — a fully modern,
all-"Doing it" plan with no escalations legitimately has zero `- **STATUS**:` occurrences (that field
only appears on *Needs you*/*Needs measurement* items), and would be wrongly rejected. Instead, check
for either of two positive signals that the file predates the rename: (a) it contains the literal old
placeholder `- **Ruling**:`, or (b) it lacks a `## Doing it` section heading entirely — a structural
anchor present in every `claude-action-plan.md` produced by the current template regardless of how
many items it holds (see `prompts/triage.md`'s `## Output` section). If either signal fires, stop
immediately with a loud error: "`<path>` does not look like a current-format `claude-action-plan.md`
— it looks like a pre-migration `action-plan.md` file (old format). This command only reads the
current `claude-action-plan.md` format; there is no automatic migration. See
docs/adr/0007-triage-and-decision-memory.md's Amendment section for migration policy." This is a hard
stop for this plan source, not a non-blocking warning.

If the check passes, extract directives from the file's sections by matching literal field values
— no prose interpretation:

- **Doing it** table rows → one directive each: `[accepted: doing-it]` + Finding cell (verbatim) + Fix
  cell (verbatim or compressed to a clause). Both Finding and Fix must be preserved; do not drop the
  Finding.
- Items with `- **STATUS**: decided` or `- **STATUS**: measured` → one directive each:
  `[decided: ruling recorded]` + the finding paragraph + the `- **DECISION**:` field verbatim.
- Items with `- **STATUS**: no-op` → **excluded from directives entirely**. A no-op ruling ("Leave
  as-is" was chosen) is decided; there is nothing to implement.
- Items with `- **STATUS**: pending-decision` or `- **STATUS**: pending-measurement` → **excluded
  from work units**. Collect them into a one-time warning printed at Step 1 completion: `N item(s) in
  this action plan still lack a recorded ruling and will be skipped: [titles]. Resolve them via
  /expert-review's ruling flow before re-running if you want them included.` Non-blocking — matches
  this command's existing "surface, don't block" pattern.

**Severity extraction (new):** Alongside the above, extract the `Severity` field from each item for a
count-by-severity tally:
- **Doing it** table rows → read the `Severity` column directly (e.g., `HIGH`, `Medium`, `LOW`).
- **STATUS: decided** or **STATUS: measured** items → read the inline `**Severity**: <value>` from the
  item's `**Where**: path:line · **Severity**: HIGH · **Raised by**: Reviewer` line.
- **STATUS: pending-decision** or **STATUS: pending-measurement** items → read the same inline
  `**Severity**:` field, and collect both the finding title and severity for the exclusion warning.
- **Normalization:** convert to uppercase (`Medium` → `MEDIUM`), strip trailing parentheticals (e.g.,
  `HIGH (disputed; ...)` → `HIGH`). Any value outside `CRITICAL`, `HIGH`, `MEDIUM`, `LOW` is treated
  as unclassified (e.g., a row with `Severity: cluster` or `Severity: unassigned`); include it in the
  item's processing but drop it from the per-severity tally to avoid breaking the count.

**Severity counters (new):** Maintain two per-severity counters while building directives:
- **Completed severities**: tally the `Severity` value for every `[accepted: doing-it]` or
  `[decided: ruling recorded]` directive created (i.e., all items that actually became directives
  per the rules above).
- **Skipped-pending severities**: tally the `Severity` value for every `STATUS: pending-decision` or
  `STATUS: pending-measurement` item excluded from directives (unrelated to the pending-exclusion
  warning — this is a side count tracking the severity of work not yet approved).
- Note: items with `STATUS: no-op` are excluded from both counters, consistent with them already
  being excluded from directives entirely.

- **Deferred** and the **gut check** section are never turned into directives.

This parsed, tagged directive list becomes "the plan" fed into Step 3 ("Split the plan into work
units").

## Step 2: Get context

```bash
pwd
git branch --show-current
git log --oneline -5
git rev-parse HEAD
find . -maxdepth 2 -name "tsconfig*.json" -o -name ".eslintrc*" -o -name "eslint.config.*" -o -name "prettier.config.*" -o -name ".prettierrc*" -o -name "pyproject.toml" -o -name ".editorconfig" -o -name "biome.json" 2>/dev/null | head -20
```

Remember the SHA from `git rev-parse HEAD` — call it `START_SHA`. You will use it throughout.

Create one scratch directory for patch files, used by every apply-diff step below regardless of
whether the run ends up single- or multi-unit:
```bash
SCRATCH=$(mktemp -d)
```

Record the absolute paths of your current directory and `$SCRATCH` as literal strings — every prompt
you compose below must contain the resolved absolute path, never the shell variable name, because
shell variables do not persist between separate Bash tool calls and a prompt containing the literal
text `$SCRATCH/...` hands the agent a path that does not exist:
```bash
pwd            # record the printed value as MAIN_WT_PATH (a literal string you will paste below)
echo "$SCRATCH"   # record the printed value as SCRATCH_PATH (a literal string you will paste below)
```

Also read `.claude/implement-with-haiku.md` using the `Read` tool if it exists (skip silently if
not) — project-specific gates and overrides for this command; where it conflicts with this
command's defaults, the project file wins.

Do not read `CLAUDE.md` or `.claude/project.yaml` into this context. Every agent launched below
reads them itself from its own working directory — see the "Project conventions" block used in
every launch prompt. Following ADR-0001's progressive-disclosure principle, each subagent loads
its own context on demand rather than receiving it pre-loaded from the orchestrator. (For comparison:
`expert-review.md` uses a different, complementary pattern: read `.claude/project.yaml` once
centrally and distribute the value into every reviewer prompt, achieving the same goal of keeping
the orchestrator's context lean.)

**Stage timing.** Each agent self-measures and returns an `ELAPSED_SECONDS:` line. Report per-round
agent compute, not end-to-end wall clock (which includes idle between turns).

### Canonical launch prompt blocks (used in all agent prompts below)

These three blocks are defined once here for reference. **Use identical wording each time they
appear** in every agent launch prompt — they are the single source of truth. Maintaining consistent
wording across all launch sites prevents drift.

**Block A — Project conventions:**
> **Project conventions:** before you start, read `<MAIN_WT_PATH>/CLAUDE.md` and
> `<MAIN_WT_PATH>/.claude/project.yaml` (skip either silently if it does not exist), where
> `<MAIN_WT_PATH>` is the literal absolute path recorded in Step 2. Read them from that path
> regardless of which worktree you are otherwise working in — a fresh worktree only contains
> tracked files, so a relative `./CLAUDE.md` read from inside it can silently miss an untracked or
> uncommitted-but-modified conventions file that the main worktree actually has.

**Block B — Working directory line:**
> `Working directory: <path>` (this exact prefix — `plan-implementer`'s first step greps for
> it verbatim to `cd` there before doing anything else)

Where `<path>` is replaced with the absolute path for the specific launch (e.g., `WT_PATH`,
`R2_WT_PATH`, `MAIN_WT_PATH`).

**Block C — Full report trailer reference (choose based on launch type):**

For writable passes:
> [Full report trailer per plan-implementer instructions, ending in `REPORT_FILE: <path> | none`]

For read-only passes:
> [Full report trailer per plan-implementer instructions — `STAGED: no (read-only pass)`, ending in `REPORT_FILE: <path> | none`]

## Step 2.5: Pre-flight orphan sweep

Before creating anything, clean up worktrees from any prior aborted run. Run from the main worktree
using `git -C` — never `cd` into a worktree you intend to delete.

```bash
BRANCH=$(git branch --show-current)
MAIN_WT=$(git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2)

# Prune stale entries first
git worktree prune

# Find leftover haiku worktrees from a prior run
git worktree list --porcelain | awk '/^worktree / {print $2}' | while read -r wt; do
  bn=$(git -C "$wt" branch --show-current 2>/dev/null || true)
  case "$bn" in
    "${BRANCH}-haiku-"*)
      echo "Removing orphan worktree: $wt (branch: $bn)"
      git worktree remove --force "$wt" 2>/dev/null || true
      git branch -D "$bn" 2>/dev/null || true
      ;;
  esac
done
git worktree prune
```

## Step 3: Split the plan into work units

Analyze the plan and emit **1..N work units**. Each unit must have:
- `id` — a short slug, e.g. `auth`, `api`, `ui` (used in branch names)
- `sub-task` — a self-contained description of only this unit's work
- `owned-files` — explicit list of every file this unit will create or modify
- (derived) `forbidden-files` — all other units' owned files, which this unit must not touch

**Rules:**
- Split only along genuinely independent seams (no shared mutable state).
- Every **shared file** (barrel/index exports, `package.json`, route registries, migration registries)
  must be assigned to **exactly one** unit — never left unassigned.
- **Extract shared contracts/interfaces** (types, enums, constants) into the sub-task text given to
  *all* units — cheap drift reduction without inter-agent communication.
- When a work unit's sub-task text includes a `[decided: ruling recorded]` or `[accepted: doing-it]`
  directive, the split must carry that tag through verbatim into the sub-task text — never strip it.
- Always emit **≥ 1** unit. If the plan is too coupled to split safely, emit 1 unit.

**Single-unit fallback:** If you emit exactly 1 unit, skip Steps 4a–4d entirely. Run `plan-implementer`
directly in the main working directory (background, `run_in_background: true`) with the same self-contained
prompt described in Step 4b. The agent stages its changes (`git add -A`, no commit); you commit them
yourself in the main worktree once `STAGED: yes` is confirmed via `git status --porcelain`. Proceed to
the Integration Gate when it completes.

## Step 4a: Create one worktree per unit (multi-unit only)

For each unit, create a branch off `START_SHA` and a worktree:

**Placement must be flat, never nested under `main/`.** A worktree nested inside the main
worktree's own directory tree (e.g. `${MAIN_WT}/.claude/worktrees/...`) sits inside whatever
build-tool root lives at `main/` — for a Cargo workspace, that's `main/Cargo.toml`. A build tool
run inside that nested path can walk up, find the parent's manifest, and silently resolve against
the wrong workspace instead of erroring — this cost a prior run a lost commit and a commit to the
wrong branch (both self-reported as success). Flat siblings avoid the whole class of problem, for
any build tool, in any project:

```bash
BRANCH=$(git branch --show-current)
MAIN_WT=$(git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2)
WT_PARENT="$(dirname "$MAIN_WT")"
mkdir -p "$WT_PARENT"

# For each unit (replace UNIT_ID with the actual id):
UNIT_ID="<id>"
WT_BRANCH="${BRANCH}-haiku-${UNIT_ID}"
WT_PATH="${WT_PARENT}/${WT_BRANCH}"
git worktree add "$WT_PATH" -b "$WT_BRANCH" "$START_SHA"
```

Track the created worktrees: `[{id, branch: WT_BRANCH, path: WT_PATH, status: "running"}]`

## Step 4b: Launch round-1 implementers (background, parallel)

Launch one background `plan-implementer` agent per unit simultaneously (`run_in_background: true`).

Each prompt must be **fully self-contained** (the agent has no other context). Include:
- The unit's sub-task text (verbatim, from Step 3)
- A literal line **`Working directory: <WT_PATH>`** (this exact prefix — `plan-implementer`'s
  first step greps for it verbatim to `cd` there before doing anything else)
- The branch name
- The 5 most recent commit messages (for commit style)
- The Project conventions block (see Step 2)
- Names of any style/lint/format config files found in Step 2
- The shared contracts/interfaces extracted in Step 3
- `OWNED FILES (only touch these):` — the unit's owned-files list
- `FORBIDDEN FILES (do not read or modify):` — all other units' owned files
- **"Do not write tests in this pass. A separate pass will write tests from the plan."**
- **"Stage your changes (`git add -A`) and do not commit. The orchestrator applies your diff and
  commits it."**
- For each directive tagged `[decided: ruling recorded]` or `[accepted: doing-it]` in this unit's
  sub-task, include this blockquote before the finding + fix verbatim:
  > **This was already decided by a human reviewer — implement exactly what is described below. Do not
  > treat it as open, do not propose an alternative, do not flag it back as ambiguous.**
- The verification commands from the plan (or `n/a` if none for this unit)
- The complete report trailer instruction (copy from `plan-implementer.md`'s trailer section, which
  ends in `REPORT_FILE: <path> | none`, not a commit)
- A literal line `Report file: <SCRATCH_PATH>/round1-<unit-id>-report.md` (absolute path, with
  `<unit-id>` substituted for this unit and `<SCRATCH_PATH>` the literal path recorded in Step 2).
  Instruct the agent: "When you finish, use the `Write` tool to write your report — steps
  completed, deviations, verification result — to that exact path, and set `REPORT_FILE:` in your
  trailer to it. Keep what you return in your reply to the trailer plus at most two sentences."

Confirm to the user: "Launched N round-1 unit(s) in parallel. Waiting for completions."

**Note on usage-gate seams:** No usage-gate check is inserted inside this round-1 fan-out. Both
round-1's parallel per-unit launches (here) and the post-gate four-way concurrent launch are atomic,
single messages launching multiple background agents at once — there is no "next not-yet-launched unit"
to gate mid-flight. The usage-gate depends on `agent_transcript_path`, which only exists once
`SubagentStop` fires for a given agent, so mid-fan-out polling is structurally impossible. Usage
gates run only after all concurrent agents in a fan-out have completed.

## Step 4c: Process unit completions (serialized apply-and-commit)

When each unit's `plan-implementer` agent returns, **immediately** process it before the next
one arrives. The orchestrator serializes all applies/commits — never concurrently.

**First: validate the report.** All four handoff-critical trailer lines must be present:
- `ELAPSED_SECONDS: <n | unknown>`
- `VERIFIED: pass | fail | n/a`
- `FILES_TOUCHED:` (with paths on subsequent lines)
- `STAGED: yes | no`

If any of these four lines is missing → **interrupted handoff**: surface the unit's report and offer:
- Re-run this unit (re-launch with the same prompt in its existing worktree)
- Inspect its worktree diff manually
- Mark failed and continue with remaining units

`REPORT_FILE:` is handled separately — see the Report-file check below.

**Never trust the trailer — verify via git.** Regardless of what `STAGED:` says, check the
worktree yourself first:
```bash
git -C "$WT_PATH" status --porcelain
```
If there is unstaged or untracked work sitting in the tree (the codified salvage step — an
agent that reported `STAGED: no` may still have left good, unstaged work behind), stage it
before deciding:
```bash
git -C "$WT_PATH" add -A
```

**Drift check — before deciding the worktree is empty, verify the main worktree is clean on this
unit's owned files.** A drifted agent's edits may have landed in the main worktree instead. Check:
```bash
git status --porcelain -- <owned-files>
git rev-parse HEAD  # vs the expected HEAD from after the last orchestrator commit
```

If the main worktree has uncommitted changes **on this unit's owned files** and the unit
worktree is empty, this is **cwd drift** — proceed to the salvage procedure below instead of
marking the unit failed. If the main worktree's HEAD has changed unexpectedly, surface that
anomaly to the human before proceeding.

If the main worktree is dirty on files owned by a **still-running** unit, do not apply/commit
anything yet that would sweep those files in; note it and re-check at that unit's completion.

**If the worktree is genuinely empty** (no staged changes after the above, and main worktree
clean on this unit's files): Mark unit `failed`. Record `<unit-id>: no report (unit failed)` for
use in Round 3 pass 1's input list below (same pattern as the Step 4c report-file check uses for
missing unit reports). Leave its worktree in place for inspection. Surface the report and reason.

**The inverse is a lost-work anomaly, not a success:** if the worktree is genuinely empty (both
`git -C "$WT_PATH" status --porcelain` and `git -C "$WT_PATH" diff HEAD` are empty — worktree-wide check, not file-scoped) **but** the
report claims `STAGED: yes` or otherwise claims work was done, do not shrug this off as "no
changes needed" — **first check the main worktree for the unit's work** (`git status --porcelain
-- <owned-files>` in main). If the work is sitting there, this is cwd drift, not lost work —
proceed to the salvage procedure below. If main is clean on the unit's files, treat it as an
interrupted handoff and use the Incomplete-report menu (Re-run / Inspect / Skip / Abort) below.

**Report-file check (per unit):** verify `<SCRATCH_PATH>/round1-<unit-id>-report.md` exists and is
non-empty yourself (`wc -c < <path>` — zero or missing is a failure) before trusting the agent's
`REPORT_FILE:` line. Unlike the Round 3 pass-1 handoff below, a missing or empty round-1 unit report
does **not** trigger a retry of the unit (the unit's implementation work already succeeded and
re-running the full prompt risks a duplicate/conflicting diff over already-staged work, just to
regenerate a prose file). Instead, record `<unit-id>: no report (unit succeeded; report file
missing or empty)` for use in Round 3 pass 1's input list below, and continue.

### Salvage procedure for cwd drift

When a drifted agent's work is in the main worktree (not the unit's own worktree):

1. **Confirm no stray commits:** `git log --oneline <expected-HEAD>..HEAD` in main must be empty
   (cwd drift creates uncommitted changes only, never new commits).
2. **Scope the dirt:** `git status --porcelain` in main must touch **only this unit's owned files**.
   Any overlap with other units' owned files → **stop and surface to the human** (concurrent
   corruption risk).
3. **Verify the drifted work is actually the unit's deliverable.** Read the diff against the
   sub-task: does this match what the unit was supposed to do? Never assume drifted work is
   complete.
4. **Harvest to the scratch dir first, before any cleanup** (snapshot survives any later mistake):
   ```bash
   git add -A -- <owned-files>
   git diff --staged --binary -- <owned-files> > "$SCRATCH/unit-${UNIT_ID}-drifted.patch"
   ```
5. **Commit in main as the unit's round-1 commit** (same message convention as the normal apply
   path):
   ```bash
   git commit -m "<summary>"
   ```
6. **Tear down the unit's empty worktree** (the work was already in main):
   ```bash
   git worktree remove "$WT_PATH"
   git worktree prune
   git branch -D "$WT_BRANCH"
   ```
   Mark unit `merged (salvaged-from-main)` and surface that label in the final summary's
   round-1 line.

**Otherwise, apply the unit's staged diff from the main worktree** (never cd into the unit's
worktree; never merge or commit inside it):

Write `<summary>` as a single imperative sentence describing what this unit implements, not the orchestration step it belongs to. It must stand alone in `git log` — no round numbers, no unit slugs. Lead with a strong verb: `add`, `implement`, `extract`, `refactor`. Example: `implement JWT authentication middleware` or `add user profile API endpoints`.

```bash
git -C "$WT_PATH" diff --staged --binary > "$SCRATCH/unit-${UNIT_ID}.patch"
git apply --index "$SCRATCH/unit-${UNIT_ID}.patch"
git commit -m "<summary>"
```
Committing here — in the main worktree — is what lets pre-commit hooks run correctly; this is
the fix for the nested-worktree hook-resolution failures seen historically.

- **Clean apply** → tear down the worktree:
  ```bash
  git worktree remove "$WT_PATH"
  git worktree prune
  git branch -D "$WT_BRANCH"
  ```
  Mark unit `merged`.

- **`git apply` fails (conflict)** — same rules as a merge conflict, since that's what this is:
  - **Conflict on an owned file** → resolve it yourself using full plan knowledge (you know both
    units' intent). Apply the resolved changes and commit. Tear down the worktree. Mark unit `merged`.
  - **Conflict on an unassigned/shared file** → **stop and ask the human** before resolving.
    Leave the worktree in place until resolved.

## Step 4d: Join barrier

**Do not advance to the Integration Gate until every unit is in a terminal state** (`merged`,
`conflict-resolved`, or `failed`). Track state per `id` — never count notifications (they interleave).

**Checkpoint 1/5 — Round 1 join.** Run:
```bash
python3 "$HOME/.claude/scripts/run-metrics.py" usage-check --seam round1-join
```
Print the `USAGE-GATE:` line verbatim, then say: "Step 1/5 done: round 1 join. All merged units are
already committed; nothing is pending if you stop here. If that token count looks large, run
`/compact` first. Reply 'continue' when ready for the integration gate." Then stop and wait for the
user's next message — do not call `AskUserQuestion`, do not compute a proceed/stop decision yourself,
and ignore the printed line's `DECISION:` field (it's informational only).

If any units are `failed` (worktree genuinely empty), surface a summary and ask the human whether to:
- Abort the run
- Proceed to the gate with the successfully-merged units only

---

## Integration Gate (Part B — runs after all units merge, before round 2)

The orchestrator (you, Sonnet) now runs the checks — not a Haiku. The implementer's self-reported
`VERIFIED:` is noted but not trusted; the gate is authoritative.

### Gate step 1: Build / type-check

Run the project's build and type-check commands. Use the config files found in Step 2 to determine
which tool applies (`tsc`, `cargo check`, `swiftbuild`, `npm run build`, `pnpm typecheck`, etc.).

```bash
# Examples — run whichever applies:
npx tsc --noEmit
cargo check
pnpm run typecheck
```

Record: **build pass | fail** and any error output.

### Gate step 2: Anti-tamper scan

The implementer is forbidden from touching test files or neutering the verify pipeline. Scan now.

```bash
# Files changed in round 1
git diff --name-only "$START_SHA"..HEAD
```

**Test-file tampering:** Flag any changed file matching test-path globs:
- `*.test.*`, `*.spec.*`, `*_test.*`, `test_*.{ts,js,py,rs,swift}`, `tests/**`, `__tests__/**`, `spec/**`
- Removed test files are always a gate failure.

**Neutered verification:** Flag any changed build/config file:
- `package.json`, `Makefile`, `Cargo.toml`, `pyproject.toml`, `build.gradle`, `.github/workflows/**`
- Then check the diff of those files for: `|| true`, `--no-verify`, `it.skip`, `xit(`, `xfail`,
  `pytest.mark.skip`, `#[ignore]`, CI steps commented out, test commands replaced with `echo` or `:`

**Stub/placeholder bodies:** In each changed implementation file, grep for:
```bash
grep -n "TODO\|FIXME\|unimplemented!()\|NotImplementedError\|raise NotImplemented\b\|throw new NotImplemented" <file>
```
Also flag functions/methods whose entire body is `pass`, `return`, or an empty block `{}` with no
other statements (use judgment — a stub is different from an intentionally minimal implementation).

**Wired but never called:** For each newly exported/public symbol the plan's deliverables call for
(new functions, components, hooks, endpoints meant to be integrated — not internal helpers), `rg`
the repo for at least one non-test call site:
```bash
rg -n "\b<symbol_name>\b" --glob '!*test*' --glob '!*spec*'
```
Zero references for a symbol the plan says to wire up is a real defect — build and gate can both
pass while the feature is silently unreachable (seen in the corpus: a run shipped helper functions
that were built but never connected to anything).

**Any flag → gate failure.**

### Gate step 3: Determine outcome

- **No build errors AND no tamper flags** → Gate **passes**. Proceed to Round 2.
  Emit: `GATE: pass — build clean, no tamper flags`

- **Any failure** → Gate **fails**. Emit a summary of failures. Dispatch a fix-Haiku (next section).

### Gate step 4: Fix-Haiku convergence loop (on failure)

Max **K = 3** iterations. On each iteration:

1. Launch a fix `plan-implementer` agent (background) in an isolated worktree branched from current
   HEAD (not START_SHA). Prompt it with:
   - A literal line **`Working directory: <FIX_WT_PATH>`** (this exact prefix —
     `plan-implementer`'s first step greps for it verbatim to `cd` there before doing anything else),
     where `<FIX_WT_PATH>` is the absolute path of this iteration's fix worktree
   - The specific failures from the gate (compile errors, stub locations, tamper flags)
   - "Fix only these specific failures. Do not touch test files. Do not modify build config scripts."
   - "Stage your changes (`git add -A`) and do not commit. The orchestrator applies your diff and
     commits it."
   - The owned files that need fixing
   - The Project conventions block (see Step 2)
2. When it returns, validate its trailer (same incomplete-report check as Step 4c, `STAGED:` not
   `COMMITTED:`).
3. Apply its diff back (same apply-and-commit pattern as Step 4c — `git diff --staged` from the
   fix worktree, `git apply --index` + commit in the main worktree, tear down worktree after).
4. Re-run Gate steps 1–3 on the updated tree.
5. **Seam log only — no stop here.** Run:
   ```bash
   python3 "$HOME/.claude/scripts/run-metrics.py" usage-check --seam gate-fix-loop
   ```
   Print the `USAGE-GATE:` line verbatim (for the seam log in the final summary), then continue
   straight to the next gate iteration — do not stop and wait, do not call `AskUserQuestion`, and
   ignore the printed line's `DECISION:` field (it's unreliable — see below). This seam was a
   mandatory stop-and-wait checkpoint (2/5) until measured data across 8 real runs showed it never
   once needed a `/compact` break — round1-join (checkpoint 1/5) had already absorbed round 1's
   context cost, and a single fix-Haiku's contribution here is small. See
   [issue #174](https://github.com/stephenkiers/claude-helpers/issues/174). (The `DECISION:` field
   itself is not a usable per-run signal regardless — most subagent transcripts in this pipeline
   don't get counted at all, so it defaults to a degraded floor estimate, not an actual token count.)
6. If gate passes → exit loop. If still failing and iterations < K → repeat.
6. If gate still fails after K iterations → **stop and surface to the human** with all outstanding
   failures. Do not proceed to Round 2. Let the human decide.

Emit a line each time a fix-Haiku is dispatched: `GATE attempt <i>/<K>: dispatching fix-Haiku`

---

## Round sizing

Before fanning out, classify the run from the round-1 diff (`git diff --name-only "$START_SHA"..HEAD`)
and the plan. Record the classification for the final summary.

- **Mechanical** — formatting/lint/config-only diff, no logic change (e.g. clippy/prettier churn,
  dependency bumps). **Stop here.** No Round 2, no Round 3, no Round 4, no sweeps. Surface the final
  summary now.
- **Test-only deliverable** — the plan's own output *is* tests (not application code). Skip Round 2
  (there's nothing spec-blind to add); keep Round 3 to review the tests themselves; **Round 4 applies**
  (relocating/cleaning the delivered tests to repo convention is the whole point).
- **Everything else** — full pipeline below, **including Round 4**.

The gate and anti-tamper scan are never skippable — they already ran.

---

## Post-gate fan-out: Round 2, Round 3 (pass 1), and sweeps — concurrent

Launch all of the following **in a single message** (background, parallel) once round sizing says
to proceed. Round 2 gets its own worktree with the diff-handoff protocol, which is what makes running
it alongside Round 3 pass 1 safe — Round 3 pass 1 only reads the main worktree's round-1 result and
never touches Round 2's tests.

**Note on usage-gate seams:** No usage-gate check is inserted inside this post-gate fan-out either.
Like the round-1 fan-out above, the concurrent launch of round 2, round 3 pass 1, and the two sweeps
is atomic — there is no seam where some agents have completed and others remain pending. Usage gates
run after this entire fan-out has joined and all its agents have provided transcript data via `SubagentStop` events.

Record the current HEAD before launching:
```bash
git rev-parse HEAD  # store as ROUND2_START_SHA
IMPL_FILES=$(git diff --name-only "$START_SHA"..HEAD)  # round-1 implementation files
```

**Checkpoint 3/5 — Pre-fanout.** Run:
```bash
python3 "$HOME/.claude/scripts/run-metrics.py" usage-check --seam pre-fanout
```
Print the `USAGE-GATE:` line verbatim, then say: "Step 3/5 done: gate passed (round sizing:
<classification>). Round 1 is committed and gate-clean; no tests or reviews exist yet. If that token
count looks large, run `/compact` first. Reply 'continue' to launch round 2, round 3 pass 1, and the
sweeps." Then stop and wait for the user's next message — do not call `AskUserQuestion`, do not
compute a proceed/stop decision yourself, and ignore the printed line's `DECISION:` field.

### Round 2: Spec-blind test author (own worktree)

```bash
R2_BRANCH="${BRANCH}-haiku-round2"
R2_WT_PATH="${WT_PARENT}/${R2_BRANCH}"
git worktree add "$R2_WT_PATH" -b "$R2_BRANCH" "$ROUND2_START_SHA"
```

`R2_WT_PATH` is fully determined by strings you already hold as literals (`WT_PARENT` from Step 2.5,
`BRANCH` from Step 2.5) — compute its literal absolute value yourself and use that literal string
below, never the shell variable name (same rule as decision 14: a prompt containing the literal text
`$R2_WT_PATH` hands the agent a path that does not exist, since shell variables don't persist across
separate Bash calls).

Launch a background `plan-implementer` in `$R2_WT_PATH` with a **spec-blind test author** prompt:

> `Working directory: <R2_WT_PATH>` (this exact prefix — `plan-implementer`'s first step greps for
> it verbatim to `cd` there before doing anything else)
>
> Your job is to write tests for the plan below. The plan has already been implemented by a prior
> pass — but you must **not** look at how it was implemented. Tests written from the implementation
> just encode the implementer's assumptions; tests written from the plan alone are an independent
> reading of the spec.
>
> **Plan:**
> [verbatim plan]
>
> **DO NOT read these files** (they are round 1's implementation): [IMPL_FILES]
>
> **DO NOT run `git diff` or `git status`** — they would expose the implementation.
>
> **DO read:**
> - Existing test files (to learn project conventions, fixtures, helpers)
> - Test config (`pytest.ini`, `vitest.config.*`, `jest.config.*`, `Cargo.toml` test sections, etc.)
> - The plan above
>
> Treat this as your plan:
> 1. Identify the project's test framework and conventions.
> 2. **Before writing anything, grep existing test files for `describe`/`test`/`it`/`#[test]` blocks
>    that already cover the plan's symbols.** List what you find. Do not add near-duplicate coverage
>    for behavior something already tests — this is the single most common failure of this pass.
> 3. Identify what behavior the plan implies should be testable that isn't already covered.
> 4. Write tests covering that behavior — happy path plus at least one edge case per testable unit.
>    **Every new test must import and call the real production symbol it claims to cover.** A test
>    that asserts against a hand-built local copy of the logic, or that would still pass if the
>    implementation were deleted, is not coverage — it's a defect.
> 5. Run the tests. **Do not fix the implementation if tests fail** — failing tests are signal.
> 6. **Stage your changes (`git add -A`) and do not commit.** The orchestrator applies your diff
>    and commits it.
> 7. Report: framework used, existing coverage found (step 2), test paths added, pass/fail summary,
>    failure messages verbatim if any.
> 8. **Self-check (required):** end your report with a line `SPEC_BLIND: yes` if you did not read
>    any forbidden file or run `git diff`/`git status`, or `SPEC_BLIND: no` followed by what you
>    read and why. Be honest — this is for evaluating whether the spec-blind constraint holds.
>
> [Full report trailer per plan-implementer instructions, ending in `REPORT_FILE: <path> | none`]
>
> **Project conventions:** before you start, read `<MAIN_WT_PATH>/CLAUDE.md` and
> `<MAIN_WT_PATH>/.claude/project.yaml` (skip either silently if it does not exist), where
> `<MAIN_WT_PATH>` is the literal absolute path recorded in Step 2. Read them from that path
> regardless of which worktree you are otherwise working in — a fresh worktree only contains
> tracked files, so a relative `./CLAUDE.md` read from inside it can silently miss an untracked or
> uncommitted-but-modified conventions file that the main worktree actually has.

### Round 3 pass 1: Adversary, read-only (main worktree)

Runs against the main worktree's round-1 result. May **propose** fixes in its report; must not
apply them yet — Round 2's tests don't exist yet, and it must not touch the concurrently-running
Round 2 worktree.

Launch a background `plan-implementer` (main worktree, read-only in practice — no edits) with:

> `Working directory: <MAIN_WT_PATH>` (this exact prefix — `plan-implementer`'s first step greps for
> it verbatim to `cd` there before doing anything else)
>
> You are an adversarial reviewer. A prior pass implemented this plan. Your job is to find
> divergence between the implementation and the plan — assume something is wrong somewhere. Don't
> confirm correctness; argue against it.
>
> **Plan:** [verbatim plan]
> **Round 1 unit reports — read each of these files:** [one absolute path per successful unit,
> i.e. `<SCRATCH_PATH>/round1-<unit-id>-report.md` for each]. Units with no report (see Step 4c)
> are listed here as `<unit-id>: no report (unit failed)` or `<unit-id>: no report (unit
> succeeded; report file missing or empty)` as applicable — do not fabricate a report for them.
> **Implementation files:** [IMPL_FILES]
>
> Treat this as your plan:
> 1. Read the implementation files.
> 2. Investigate divergence: where does the implementation drift from the plan? What did the
>    implementer rationalize past? Where would this break in production? Consider edge cases,
>    error paths, concurrent access, malformed input, resource leaks, missing validation.
> 3. **Do not edit any files in this pass, except the report file named below, which you must `Write`** — a second, shorter pass will apply fixes after tests
>    exist. For each finding, write out the proposed fix (as a description, not a diff) and note
>    whether it looks unambiguous or needs human judgment.
> 4. Write your numbered findings to `<SCRATCH_PATH>/round3-pass1-report.md` using the `Write`
>    tool: a numbered list, each item with *finding*, *proposed fix (description, not a diff)*,
>    and *unambiguous | needs-human-judgment*. Set `REPORT_FILE:` in your trailer to that path.
>    In your reply, return only a one-or-two-line summary plus the trailer — do not repeat the
>    findings inline.
>
> [Full report trailer per plan-implementer instructions — `STAGED: no (read-only pass)`, ending in `REPORT_FILE: <path> | none`]
>
> **Project conventions:** before you start, read `<MAIN_WT_PATH>/CLAUDE.md` and
> `<MAIN_WT_PATH>/.claude/project.yaml` (skip either silently if it does not exist), where
> `<MAIN_WT_PATH>` is the literal absolute path recorded in Step 2. Read them from that path
> regardless of which worktree you are otherwise working in — a fresh worktree only contains
> tracked files, so a relative `./CLAUDE.md` read from inside it can silently miss an untracked or
> uncommitted-but-modified conventions file that the main worktree actually has.

### Sweeps (read-only, Haiku, findings only — never auto-fixed)

Two more background `plan-implementer` agents, both read-only against the main worktree, both
reporting findings into the final summary only:

**Duplication sweep:**
> `Working directory: <MAIN_WT_PATH>` (this exact prefix — `plan-implementer`'s first step greps for
> it verbatim to `cd` there before doing anything else)
>
> From this diff's new/changed symbols [IMPL_FILES], `rg` the repo for the same logic shape
> repeated at 3 or more call sites. Report each as: symbol/pattern, call sites (file:line), and a
> one-line suggested extraction. Do not edit anything.
>
> [Full report trailer per plan-implementer instructions — `STAGED: no (read-only pass)`, ending in `REPORT_FILE: <path> | none`]
>
> **Project conventions:** before you start, read `<MAIN_WT_PATH>/CLAUDE.md` and
> `<MAIN_WT_PATH>/.claude/project.yaml` (skip either silently if it does not exist), where
> `<MAIN_WT_PATH>` is the literal absolute path recorded in Step 2. Read them from that path
> regardless of which worktree you are otherwise working in — a fresh worktree only contains
> tracked files, so a relative `./CLAUDE.md` read from inside it can silently miss an untracked or
> uncommitted-but-modified conventions file that the main worktree actually has.

**Doc-drift check:**
> `Working directory: <MAIN_WT_PATH>` (this exact prefix — `plan-implementer`'s first step greps for
> it verbatim to `cd` there before doing anything else)
>
> This diff is: [IMPL_FILES]. If it touches, or the plan below names, any ADR/CHANGELOG/README
> file, verify each doc claim against the actual code — flag anything the doc asserts that the
> diff contradicts or doesn't support. Also flag code changes that contradict an ADR named in the
> plan even if that ADR wasn't touched. Report findings only; do not edit anything.
>
> **Plan:** [verbatim plan]
>
> [Full report trailer per plan-implementer instructions — `STAGED: no (read-only pass)`, ending in `REPORT_FILE: <path> | none`]
>
> **Project conventions:** before you start, read `<MAIN_WT_PATH>/CLAUDE.md` and
> `<MAIN_WT_PATH>/.claude/project.yaml` (skip either silently if it does not exist), where
> `<MAIN_WT_PATH>` is the literal absolute path recorded in Step 2. Read them from that path
> regardless of which worktree you are otherwise working in — a fresh worktree only contains
> tracked files, so a relative `./CLAUDE.md` read from inside it can silently miss an untracked or
> uncommitted-but-modified conventions file that the main worktree actually has.

---

## After the fan-out: apply Round 2, machine-check, Round 3 follow-up

### Apply Round 2's diff

Same protocol as Step 4c — never trust the trailer:
```bash
git -C "$R2_WT_PATH" status --porcelain   # check for unstaged leftovers
git -C "$R2_WT_PATH" add -A               # if any
git -C "$R2_WT_PATH" diff --staged --binary > "$SCRATCH/round2.patch"
git apply --index "$SCRATCH/round2.patch"
git commit -m "test: add spec-blind tests for <comma-separated feature areas>"
git worktree remove "$R2_WT_PATH" && git worktree prune && git branch -D "$R2_BRANCH"
```

Replace `<feature areas>` with the actual modules or features covered, e.g. `test: add spec-blind tests for auth, user profile, and session management`.

**Drift check — before deciding the worktree is empty, verify the main worktree is clean.**
A drifted Round 2 agent's test files may have landed in the main worktree instead. If the
R2 worktree is empty but `git status --porcelain` in main shows test files, this is cwd drift.
Harvest the diff first via the salvage procedure (Step 4c's salvage steps 1–4, adapted for
test files), then proceed to teardown and apply.

If the diff is empty and `STAGED: no` — inspect the worktree before declaring no tests were
written; genuinely finished-but-unstaged work should still be applied.

The inverse is a lost-work anomaly, not a success: if the diff is empty **and** `STAGED: yes` (or
the report otherwise claims work was done), **first check the main worktree for test files** before
offering the menu. If the work is in main, this is cwd drift — salvage in place. If main is clean
on test files, treat it as an interrupted handoff and use the Incomplete-report menu (Re-run /
Inspect / Skip / Abort) below.

Then re-run the new tests yourself and record actual pass/fail counts — **never echo the agent's
claimed counts into the summary uncorrected.**

### Machine-check spec-blindness (Part C)

```bash
TEST_FILES=$(git diff --name-only "$ROUND2_START_SHA"..HEAD)
```
Cross-reference `TEST_FILES` against `IMPL_FILES`. Any overlap → **SPEC_BLIND: VIOLATED (touched
impl files)**. This is independent of the agent's self-reported `SPEC_BLIND:` line — record both.
On violation, don't block; flag round 2's signal as compromised and continue (the adversary is the
backstop for an unattended run).

**Additional Part C machine checks, orchestrator-run (not trusted from any report):**

1. **Dedup check** — compare the set of test names in `TEST_FILES` against the set that existed at
   `ROUND2_START_SHA` (`comm` on sorted unique test-fn-name lists, not a count — a duplicated block
   plus a dropped test cancel out in a count). Near-duplicates → flag.
2. **Reference check** — each file in `TEST_FILES` must reference at least one symbol from
   `IMPL_FILES`. Zero references → flag as `vacuous`.
3. **Bounded mutation smoke (two mutations)** — pick the file in `IMPL_FILES` referenced by the most
   new tests. Run two mutations:
   
   **Precondition:** Before each mutation, run `git status --porcelain -- <file>`. If the file is
   dirty, stop Part C's mutation checks entirely for this run, surface the dirty state in the summary,
   and skip both mutations (do not attempt to stash or force-clean).
   
   **Mutation (a) — full-file revert:** Revert the file to its pre-round-1 state and re-run `TEST_FILES`:
   ```bash
   git checkout "$START_SHA" -- <that file>
   # re-run only TEST_FILES
   git checkout HEAD -- <that file>   # restore, always, even on failure
   git status --porcelain -- <file>   # postcondition: must be empty
   ```
   Expect at least one new-test failure. Zero failures → flag as `vacuous`.
   
   **Note on "zero failures" ambiguity:** A `vacuous` or `weak-assertion` flag can indicate either
   (a) the assertion form itself is too loose, or (b) the test's chosen input simply doesn't exercise
   the actual boundary value (a coverage gap, not an assertion-quality defect). The automated Part C
   flags catch the mechanism; step 3 of the Round 3 follow-up prompt (the "Actively hunt for weak
   assertions" step) should be read as covering the assertion-quality half. A `weak-assertion` flag
   with no matching `[WEAK_ASSERTION]` finding from step 3 likely indicates the boundary-coverage-gap
   case rather than a genuinely weak assertion.
   
   **Mutation (b) — boundary operator flip:** In the same file, deterministically select the function
   referenced by the most new tests, then find the first comparison/boundary operator (`==`/`!=`,
   `<`/`<=`, `>`/`>=`) in source order within that function. Flip it (e.g. `<` → `<=`),
   re-run `TEST_FILES`, then restore:
   ```bash
   # edit the file to flip the operator
   # re-run only TEST_FILES
   git checkout HEAD -- <that file>   # restore, always, even on failure
   git status --porcelain -- <file>   # postcondition: must be empty
   ```
   Expect at least one new-test failure. Zero failures → flag as `weak-assertion`.
   
   **Outcome if no comparison/boundary operator found:** If the selected function contains no
   comparison/boundary operator at all, mutation (b) is not applicable for this run; skip it and
   do not flag `weak-assertion`.

These flags don't block. They're handed to the Round 3 follow-up pass below and surfaced in the
final summary.

**Checkpoint 4/5 — Post-fanout.** Run:
```bash
python3 "$HOME/.claude/scripts/run-metrics.py" usage-check --seam post-fanout
```
Print the `USAGE-GATE:` line verbatim, then say: "Step 4/5 done: post-fanout. Round 2's tests are
committed; Round 3 pass 1's findings and the sweep findings are UNAPPLIED and will be lost if the
session closes here. If that token count looks large, run `/compact` first. Reply 'continue' to apply
round 3's fixes." Then stop and wait for the user's next message — do not call `AskUserQuestion`, do
not compute a proceed/stop decision yourself, and ignore the printed line's `DECISION:` field.

### Round 3 follow-up (short)

Collect: `TEST_FILES` (above), the Part C flags (`dedup` / `vacuous` / `weak-assertion`), and Round 3 pass 1's proposed fixes.

Launch a background `plan-implementer` (main worktree) with:

> `Working directory: <MAIN_WT_PATH>` (this exact prefix — `plan-implementer`'s first step greps for
> it verbatim to `cd` there before doing anything else)
>
> You did an adversarial read-only pass earlier on this implementation and proposed fixes (below).
> Since then, spec-blind tests were written. Your job now:
>
> **Your earlier findings and proposed fixes — read this file:**
> `<SCRATCH_PATH>/round3-pass1-report.md`
> **New test files:** [TEST_FILES]
> **Automated flags on the new tests:** [Part C flags, if any — `dedup` / `vacuous` / `weak-assertion`]
>
> 1. Read the new test files. Note which fail against the current implementation and why.
> 2. Apply your earlier proposed fixes now, if you still believe them correct given the tests.
> 3. **Actively hunt for weak assertions** — independent of whatever Part C flagged. For each new
>    test, ask: "what plausible broken implementation would this assertion still let through?" Flag
>    tests that only check type, truthiness, non-null, or "no exception raised" instead of the
>    actual expected value, and tests on boundary/comparison logic whose assertion doesn't pin the
>    exact expected outcome (this is not an exhaustive list — use judgment for other weak-assertion
>    shapes). Label each `[WEAK_ASSERTION]`.
> 4. Review the automated flags on the new tests (`dedup`, `vacuous`, `weak-assertion`) — are they real
>    problems? If a flagged test is genuinely `vacuous`, duplicate, or `weak-assertion` as you judged
>    above, note it (do not delete another pass's tests unless clearly wrong).
> 5. **Do not touch style, naming, formatting, or comments** unless they directly impact behavior.
> 6. Stage any fixes (`git add -A`, do not commit) and report.
> 7. Report findings in two sections:
>
>    **Implementation issues** (numbered): For **each**, label its source:
>    - `[FROM_TEST]` — surfaced by a failing round 2 test
>    - `[INDEPENDENT]` — found by reading the code in pass 1, not caught by any test
>    - `[PLAN_GAP]` — the plan itself was ambiguous or missing a constraint
>
>    State whether you fixed or flagged each.
>
>    **Weak assertion findings** (numbered separately): Tests flagged as `[WEAK_ASSERTION]` (from step 3,
>    or confirmed via Part C's weak-assertion flag). These are test-quality judgments, not implementation
>    bugs — list them as their own section with brief description of the assertion gap. If a test is
>    flagged by both Part C's mechanical check and your own step-3 hunt, list it once in this section,
>    noting that both sources agree (do not double-count it).
>    
>    Emit this section's heading even when there are no findings, with body text "None found."
>
>    Report final test status.
>
> [Full report trailer per plan-implementer instructions, ending in `REPORT_FILE: <path> | none`]
>
> **Project conventions:** before you start, read `<MAIN_WT_PATH>/CLAUDE.md` and
> `<MAIN_WT_PATH>/.claude/project.yaml` (skip either silently if it does not exist), where
> `<MAIN_WT_PATH>` is the literal absolute path recorded in Step 2. Read them from that path
> regardless of which worktree you are otherwise working in — a fresh worktree only contains
> tracked files, so a relative `./CLAUDE.md` read from inside it can silently miss an untracked or
> uncommitted-but-modified conventions file that the main worktree actually has.

**Applying and verifying Round 3's fixes:** same diff-apply pattern as Round 2 above. Before
committing, validate scope — `git -C <its worktree or the diff> diff --staged --name-only` should
touch only files the findings name. Then re-run gate step 1 (build/type-check) and the affected
tests yourself before committing; a fix that breaks the build is not a fix.

```bash
git commit -m "test: fix weak assertions and coverage gaps from adversary review"
```

If the fixes are narrow, name the specific area, e.g. `test: tighten boundary assertions in auth tests`.

Tell the user: "Round 2 complete — [N tests added, M failing]. Round 3 complete — [K issues,
J fixed, W weak assertions flagged]. Sweep findings: [duplication: N, doc-drift: N]."

---

## Round 4 — Test cleanup (orchestrator-run)

Round 2's spec-blind author invents its own test filenames — the command gives it **no** naming
scheme, so when a plan is organized into "Wave A / Wave B / …" units the author mirrors those labels
into filenames like `tests/test_wave_a_spec.py`. That's an artifact of *this* orchestration process,
not the repo's real convention, and nothing downstream has relocated or deleted them: Part C and
Round 3 only **flag** `vacuous` / `weak-assertion` / `dedup` and are told never to delete another
pass's tests unless clearly wrong. Round 4 closes that gap: the orchestrator (you, Sonnet) deletes
clearly-junk tests and relocates + renames the survivors to the repo's own layout, so the committed
suite looks like it was written by someone who knows the repo.

You run this **directly in the main worktree** — no sub-agent, no worktree/diff handoff. Path/import
rewrites are judgment-heavy and this matches how the Integration Gate and Part C already run. The
same discipline still applies: **never trust a self-report; verify every claim via git and by
re-running the tests yourself.**

### 4.0 Applicability

**Checkpoint 5/5 — Pre-round 4.** Run:
```bash
python3 "$HOME/.claude/scripts/run-metrics.py" usage-check --seam pre-round4
```
Print the `USAGE-GATE:` line verbatim, then say: "Step 5/5 done: pre-round 4. Round 4 itself has no
subagent cost; this is the last checkpoint for the run. If that token count looks large, run
`/compact` first. Reply 'continue' to run test cleanup." Then stop and wait for the user's next
message — do not call `AskUserQuestion`, do not compute a proceed/stop decision yourself, and ignore
the printed line's `DECISION:` field.

- **Mechanical** run → skip Round 4 (the run already stopped after round sizing).
- **No test files added/changed across Rounds 2–3** → skip Round 4 (nothing to clean).
- **Test-only** run → Round 4 **applies** (relocating/cleaning the delivered tests is the point).
- **Full** run → Round 4 **applies**.

### 4.1 Enumerate the new tests (the working set)

**Recompute** the working set at Round-4 time rather than reusing the `TEST_FILES` value from Part C:
```bash
TEST_FILES=$(git diff --name-only "$ROUND2_START_SHA"..HEAD)
```
HEAD has advanced since Part C computed `TEST_FILES` (right after Round 2's apply), so recomputing now
naturally folds in **both** Round 2's tests **and** any test files Round 3's follow-up added — the
stale cached value would miss the latter. This is the **working set** — Round 4 only ever touches
files in it, never pre-existing tests.

### 4.2 Learn the repo's convention (evidence, not assumption)

Determine the target layout/naming by inspecting *sibling* tests that already existed at `START_SHA`.
Step 2 already located the general project config (`pyproject.toml`, lint/format configs); read the
framework's **test** config directly if not already in hand (`pytest.ini`/`pyproject.toml`
`[tool.pytest]`, `vitest.config.*`, `jest.config.*`, `Cargo.toml` test sections, etc.) for its
`testpaths`/`roots`/`include` globs. Establish, per language:

- **Directory convention** — co-located (`foo.test.ts` next to `foo.ts`), mirrored tree (`tests/`
  mirroring `src/`), or per-package `tests/` dir.
- **Filename convention** — the dominant pattern among existing siblings (`test_<module>.py`,
  `<module>_test.py`, `<Name>.spec.ts`, `#[cfg(test)]` inline, …). The correct name is derived from
  the **module/symbol under test**, never from the plan's wave/unit slug.

```bash
# example discovery — adapt per detected framework
git ls-tree -r --name-only "$START_SHA" | rg '(test|spec)' | rg '\.(py|ts|tsx|js|rs|swift)$'
```

If there are **zero** pre-existing tests to learn from, do not guess a bespoke layout — fall back to
the framework's documented default and note in the final summary that no in-repo precedent existed.

### 4.3 Classify each file in the working set

- **Junk (delete) — only these three, each attributable to a specific file:**
  - files carrying Part C's **reference-check** `vacuous` flag — the *per-file* check (each file in
    `TEST_FILES` must reference ≥1 symbol from `IMPL_FILES`; zero references → `vacuous`). A test that
    never touches production code tests nothing; delete it.
  - tests that assert against a hand-built copy of the logic (the "would still pass if the
    implementation were deleted" defect the Round 2 prompt already forbids),
  - exact/near duplicates of coverage that existed at `START_SHA` (from the Part C dedup `comm` result).
- **Not auto-deleted — surface, don't delete (fixable signal, not junk):**
  - Part C's **mutation-smoke** `vacuous` flag is **suite-level, not per-file**: it reverts one impl
    file and re-runs the *whole* `TEST_FILES` set, so a zero-failure result only proves the tests
    covering *that one reverted file* were too weak to notice it vanish — it never names which file is
    junk and says nothing about tests targeting other files. Treat it exactly like `weak-assertion`:
    surface it (Round 3 already reports it), and at most hand the tests that *reference the reverted
    file* to Round 3 to strengthen. **Never** expand a suite-level mutation-smoke flag into a `git rm`
    — doing so deletes good tests for other modules on the strength of one weak test elsewhere.
  - a `weak-assertion` flag — a weak assertion is fixable, not worthless.
- **Useful (relocate/rename):** everything else.

### 4.4 Delete the junk

`git rm` each junk file (or delete the specific test fn if only part of a file is junk), one at a
time, recording path + reason for the final summary.

**Record what evidence was available.** Part C's mutation smoke can be skipped whole-cloth (dirty-file
precondition, or no comparison operator found — see Part C). When it was skipped, the reference-check
is Round 4's *only* machine-checked junk evidence, so a `Deleted (junk): 0` result means "not vetted
by the smoke," not "vetted and clean." Note in the summary whether the mutation smoke ran or was
skipped (with reason) so a human can tell those two cases apart.

### 4.5 Relocate + rename the survivors

Move each survivor to the 4.2 convention:

- Move with `git mv` so history is preserved.
- Rewrite the moved file's own imports for its new location, and update any references to it elsewhere
  (test config include globs, `conftest.py`, barrel files) — grep for the old path/name first.
- **Merge rather than clobber:** if a survivor's target path collides with an existing test file, fold
  the new tests into the existing file under the repo's conventions instead of overwriting.

### 4.6 Verify green, then commit

Re-run the tests **yourself** — a **fresh full collection under the new layout**, not a path-scoped
re-run of the pre-move set (the old paths no longer exist, and a stale include target would silently
run the wrong set while still reporting a plausible count). Confirm the total matches pre-cleanup
minus the deletions. **For any file merged in 4.5,** additionally confirm the target file's collected
test count rose by exactly the folded-in count — a same-name collision (e.g. two `test_boundary()` in
one module) silently shadows one test with no error, and only a per-file count check catches it.
Re-run gate step 1 (build/type-check), since moves can break import paths. Only then:

```bash
git commit -m "test: relocate spec-blind tests to repo convention and drop vacuous tests"
```

If tests go red after a move, fix the import/path (that's the expected failure mode) — a relocation
that changes pass/fail counts beyond the intended deletions is a mistake, not cleanup.

---

## Report-file handoff check (applies wherever a prompt names a `Report file:`)

Before forwarding a report path into any later prompt, verify the file yourself — never trust the
agent's `REPORT_FILE:` line alone (same discipline as `STAGED:` / `VERIFIED:`). Check existence
*and* non-emptiness in one command, on the path **you** named, not the one the agent reported:
```bash
wc -c < "<absolute report path>"
```
Missing file → command errors. Empty file → `0`. Either is a failure.

**Round 1 unit reports:** see Step 4c's report-file check above — no retry, record as missing, continue.

**Round 3 pass-1 report (the one handoff that actually gates a downstream decision):** on failure,
re-launch that agent once with the identical prompt. If the file is still missing or empty after
the retry, hard-stop that stage — do not forward an empty or absent report, and never treat it as
"no findings found." Surface it via the Incomplete-report menu (Re-run / Inspect / Skip / Abort)
and record it in the final summary.

A `REPORT_FILE:` line that disagrees with the path you named is a report-file failure too — treat
it exactly the same way.

---

## Incomplete report handling (applies to every round and every unit)

A report missing any of the four required trailer lines (`ELAPSED_SECONDS`, `VERIFIED`,
`FILES_TOUCHED`, `STAGED`) is an **interrupted handoff** — do not silently proceed.

A missing `REPORT_FILE:` line is not itself an interrupted handoff — note it and continue. What
matters is the file on disk at the path you named; see the Report-file handoff check above.

Surface the truncated report and offer a menu:
- **Re-run** — re-launch with the same prompt (unit retains its worktree / state)
- **Inspect** — let the human review the worktree diff and decide next steps
- **Skip** — mark this unit failed, continue with the rest (only for round-1 units)
- **Abort** — stop the flow entirely

---

## Orchestrator verification lessons (applies to every round)

Hard-won failure modes from real runs. The multi-agent gate is only as good as the
orchestrator's own verification — never trust round self-reports.

- **Destructive git mid-run has wiped staged work while the run reported success.** Destructive
  git subcommands (`reset`, `checkout` off-branch, `stash`, `clean`, etc.) are now hook-blocked
  for plan-implementer ([ADR-0008](../docs/adr/0008-machine-enforced-agent-guardrails.md)), and
  the mutation smoke above requires the target file to be clean before it runs. But those are
  guardrails, not proof of absence — never treat an empty harvest as "no changes needed" when the
  report's trailer claims otherwise; treat it as an anomaly per the rules above.
- **Self-reports overstate success.** Agents have reported "all tests pass" over real
  failures, "clippy clean" from a run without `-D warnings` or with `--lib` only, and
  "cargo test passed" when only `cargo check` ran (the test binary didn't compile).
  Re-run the exact gate commands yourself after every round.
- **`STAGED: yes` can be false; `STAGED: no` can hide finished work.** Verify with
  `git -C <worktree> status --porcelain` before trusting either. If an agent left complete work
  unstaged, stage it, apply it, and commit it yourself — rounds 2/3 need a committed base.
  Run git state checks ONE command at a time; large parallel batches contaminate each
  other's output and can fake a disaster that didn't happen.
- **Read new test files, don't count them.** A test that never imports the unit under
  test (asserting hand-built literals against themselves) passes even if the
  implementation is deleted. Confirm each file imports and exercises the real unit
  (renderHook for hooks, real deserialize for parsers). Reject tests that re-implement
  a copy of the logic and assert on the copy.
- **"Pure move" refactors duplicate and drop.** Compare unique test fn-name SETS before
  vs after (`comm`), not attribute counts — a duplicated block plus two dropped tests
  cancel out in the count.
- **Agents die mid-format and drift cwd.** A drifted agent's edits can land in the main
  worktree instead of its own, presenting as an empty unit worktree but `STAGED: yes` (or
  `STAGED: no` with unstaged work). Before offering Re-run, check the main worktree for the
  unit's owned files (`git status --porcelain -- <owned-files>`). If the work is there, salvage
  in place via the procedure in Step 4c's "Salvage procedure for cwd drift" section — never
  Re-run without first checking main, as real incidents wasted duplicate runs that way.
- **The adversary punts.** Round 3 may "document the limitation" instead of writing the
  hard test, report `VERIFIED: n/a` without running anything, or mislabel an explicit
  plan requirement as `[PLAN_GAP]`. Re-triage its findings; write the real test
  yourself if needed.
- **Treat dead-code/unused warnings as "scaffolded but not wired"** — they repeatedly
  exposed features the self-report claimed were integrated.
- **Fresh worktrees need deps installed** before test results mean anything (a missing
  dev dep can error out the whole suite and produce misleading counts).

---

## Final summary

Collect each round's `ELAPSED_SECONDS` (self-measured) plus your own orchestrator-measured
wall-clock per phase. Format all as `mm:ss`. Sum of `ELAPSED_SECONDS` = total agent compute.

**Usage gate log.** Accumulate each seam's `USAGE-GATE:` output line as the run progresses (the
orchestrator displays these to the user at each seam). In the final summary, render each seam's line
verbatim — a skipped seam must show as a visibly MISSING row in this enumerated list (a gap, not
merely an absence from the transcript). At the end, run `usage-check --seam final` one more time and
include both its `USAGE-GATE:` line and its `USAGE-GATE-SEAMS:` line verbatim. The `USAGE-GATE-SEAMS:` line
is the authoritative record of which seams were actually reached during this run (generated from on-disk state, not narrated by the orchestrator). Extract the per-agent accounted/unaccounted tally
(the `agents=N/M` field) from the final seam output and include it here; note once that upstream
`token_confidence` is always hardcoded as `"low"` (a blanket disclaimer on the mechanism, not a
signal about any specific transcript's trustworthiness).

```
ROUND SIZING: <mechanical | test-only | full>
ACTION PLAN SOURCE: <path>  [only when PLAN_SOURCE=claude-action-plan]
  Doing it applied: <n>   Rulings applied: <n>   Skipped (still pending): <n>
ROUND 1 — Implementer (parallel)
  Units: <N>   Applied clean: <c>   Conflicts resolved: <c>   Failed: <c>
INTEGRATION GATE (trusted)
  Build/type-check: <pass | fail>   Convergence iterations: <i>/<K>
  Tamper flags: tests-modified <c>  verify-neutered <c>  stubs <c>  wired-but-uncalled <c>
POST-GATE FAN-OUT (concurrent — round 2, round 3 pass 1, sweeps)
ROUND 2 — Spec-blind test author
  SPEC_BLIND: <verified by diff | VIOLATED — touched impl files> (self-report: yes | no)
  Tests added: <count, paths>   Pass/fail (orchestrator re-run): <P passed, F failed>
  Part C flags: dedup <c>  vacuous <c>  weak-assertion <c>
ROUND 3 — Adversary (pass 1 + follow-up)
  Issues found: <count>
    [FROM_TEST]:    <count>  ← signal that round 2 caught real divergence
    [INDEPENDENT]:  <count>  ← signal that adversary stance earned its keep
    [PLAN_GAP]:     <count>  ← signal that the plan needs sharpening
  Fixed: <count>   Flagged: <count>
  Weak assertion findings: <count>  ← test-quality signal
  Round-3 test status: <P passed, F failed>
  Report-file handoffs: <ok | retried: n | FAILED: stage>
ROUND 4 — Test cleanup (orchestrator-run)
  Convention detected: <co-located | mirrored-tree | tests-dir> / <naming pattern>
  Junk-detection evidence: mutation-smoke <ran | skipped: reason>, reference-check ran
  Deleted (junk): <count, paths + reason>
  Relocated/renamed: <count>  (e.g. tests/test_wave_a_spec.py → <repo-convention path>)
  Post-cleanup test status (orchestrator re-run — AUTHORITATIVE final status): <P passed, F failed>
SWEEPS
  Duplication findings: <count>
  Doc-drift findings: <count>
USAGE GATE LOG (subagent token accounting — accumulate seam lines as printed, then render final summary)
  Round 1 join:       <USAGE-GATE: line verbatim from seam>
  Gate fix-loop:      <USAGE-GATE: line verbatim from seam or SKIPPED>
  Post-gate fanout:   <USAGE-GATE: line verbatim from seam>
  After fanout:       <USAGE-GATE: line verbatim from seam>
  Pre-round 4:        <USAGE-GATE: line verbatim from seam>
  Final:              <USAGE-GATE: line verbatim from seam final, then USAGE-GATE-SEAMS: line from seam final>
  Agents accounted:   <N>/<M>  (from agents=N/M field in final seam output)
  Token confidence:   low (upstream `token_confidence` is always reported as "low" per ADR-0016)
TIMING (agent compute per round — self-measured; excludes idle between turns)
  Round 1 (implement):        <mm:ss>  [<N> units in parallel]
  Gate convergence:           <i> iteration(s)
  Fan-out wall-clock:         <mm:ss>  [round 2 ∥ round 3 pass 1 ∥ 2 sweeps]
  Round 2 (tests):            <mm:ss>
  Round 3 (pass 1 + follow-up): <mm:ss>
  Round 4 (cleanup — orchestrator): <mm:ss>
  Total agent compute:        <mm:ss>
```

Then a one-line **timing read** naming the long pole (e.g. "Round 1 dominated at 6:12 of 9:40 total
across 2 parallel units; the round-2/round-3 fan-out ran concurrently and added only 1:50 to
wall-clock despite 4:30 of combined agent compute").

Then a one-line **experiment read**: which rounds and sweeps produced signal, which didn't.

**Only when `PLAN_SOURCE=claude-action-plan`:** emit a one-line **severity recap**, tallying the
completed and skipped-pending severities from the Step 1 count (omit zero-count severity values to
keep it scannable). Examples:
- All completed (no pending skips): `Action plan: (0 Critical, 9 High, 3 Medium, 8 Low) — all
  completed.`
- Partial with pending: `Action plan: (6 High, 3 Medium, 5 Low) completed, (3 Low) skipped — still
  pending a ruling.`
- If the Round 1 line shows `Failed: <c>` > 0, append to this sentence: ` Note: <c> round-1
  unit(s) failed — some items above may not actually be applied; see ROUND 1 status.`

For GitHub-issue or conversation-plan sources (where severity data is unavailable), **omit this
sentence entirely** — no recap.

Suggested next steps:
- Review the diff and run `/expert-review`
- Ship with `/shipit`
- If round 3 flagged `[PLAN_GAP]` issues, revise the plan before re-running
- If round 2's spec-blind was VIOLATED, treat round-3 findings with lower confidence (adversary
  may have had prior exposure to the implementation)
- If Part C flagged tests as `vacuous`, `weak-assertion`, or `dedup` and Round 3's follow-up didn't
  resolve them, review those test files by hand before trusting their coverage
- If Round 4 found **no in-repo test precedent**, the chosen layout/naming was a framework default,
  not a learned convention — worth a human glance to confirm it matches your intent
- If Round 4's `Junk-detection evidence` shows the **mutation smoke was skipped**, its junk detection
  ran on the reference-check alone — eyeball the delivered tests' coverage by hand before trusting it
- Review sweep findings (duplication, doc-drift) — they're informational, not auto-fixed
