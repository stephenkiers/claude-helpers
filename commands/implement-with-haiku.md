---
description: "Parallel round-1 Haiku implementers, orchestrator-owned integration gate with anti-cheat scanning, bounded convergence loop, machine-checked spec-blind, adversary review."
allowed-tools: Read, Bash(gh issue view:*), Bash(git log:*), Bash(git branch:*), Bash(git rev-parse:*), Bash(git diff:*), Bash(git worktree:*), Bash(git merge:*), Bash(git checkout:*), Bash(git branch -D:*), Bash(git branch -d:*), Bash(pwd:*), Bash(find:*), Bash(date:*), Bash(echo:*), Bash(cat:*), Bash(wc:*), Bash(grep:*), Bash(cargo:*), Bash(npm:*), Bash(npx:*), Bash(pnpm:*), Bash(yarn:*), Bash(swift:*), Bash(xcodebuild:*), Agent
---

# Implement with Haiku

Splits the plan into independent work units, runs them as parallel background `plan-implementer`
agents in isolated worktrees, then applies an **orchestrator-owned integration gate** before handing
off to spec-blind test writing and adversary review.

The flow:

1. **Round 1 — Implementer(s)** — one Haiku per work unit, in parallel worktrees
2. **Integration gate** — orchestrator runs build/type-check + anti-cheat scan + bounded fix loop
3. **Round 2 — Spec-blind test author** — writes tests from the plan without reading the implementation
4. **Round 3 — Adversary** — assumes something is wrong, finds divergence and failure modes

## Step 1: Find the plan

In priority order:

1. **Args contain an issue number or URL** → fetch with `gh issue view <number>`
2. **`.claude/github-cache.json` exists** → read it, use `issue.body` as the plan
3. **A plan is visible in the current conversation** → use it directly

If none yield a plan, tell the user and stop.

## Step 2: Get context

```bash
pwd
git branch --show-current
git log --oneline -5
git rev-parse HEAD
find . -maxdepth 2 -name "tsconfig*.json" -o -name ".eslintrc*" -o -name "eslint.config.*" -o -name "prettier.config.*" -o -name ".prettierrc*" -o -name "pyproject.toml" -o -name ".editorconfig" -o -name "biome.json" 2>/dev/null | head -20
```

Remember the SHA from `git rev-parse HEAD` — call it `START_SHA`. You will use it throughout.

Also read these files using the `Read` tool if they exist (skip silently if not):
- `CLAUDE.md` — project conventions and coding rules
- `.claude/project.yaml` — project-specific context
- `.claude/implement-with-haiku.md` — project-specific gates and overrides for this
  command; where it conflicts with this command's defaults, the project file wins

**Stage timing.** Each agent self-measures and returns an `ELAPSED_SECONDS:` line. Report per-round
agent compute, not end-to-end wall clock (which includes idle between turns).

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
- Always emit **≥ 1** unit. If the plan is too coupled to split safely, emit 1 unit.

**Single-unit fallback:** If you emit exactly 1 unit, skip Steps 4a–4d entirely. Run `plan-implementer`
directly in the main working directory (background, `run_in_background: true`) with the same self-contained
prompt described in Step 4b. Proceed to the Integration Gate when it completes.

## Step 4a: Create one worktree per unit (multi-unit only)

For each unit, create a branch off `START_SHA` and a worktree:

```bash
BRANCH=$(git branch --show-current)
MAIN_WT=$(git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2)
WT_PARENT="${MAIN_WT}/.claude/worktrees"
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
- Absolute path of `WT_PATH` as the working directory
- The branch name
- The 5 most recent commit messages (for commit style)
- Contents of `CLAUDE.md` if found (under a "Project conventions" heading)
- Contents of `.claude/project.yaml` if found (under a "Project context" heading)
- Names of any style/lint/format config files found in Step 2
- The shared contracts/interfaces extracted in Step 3
- `OWNED FILES (only touch these):` — the unit's owned-files list
- `FORBIDDEN FILES (do not read or modify):` — all other units' owned files
- **"Do not write tests in this pass. A separate pass will write tests from the plan."**
- The verification commands from the plan (or `n/a` if none for this unit)
- The complete report trailer instruction (copy from `plan-implementer.md`'s trailer section)

Confirm to the user: "Launched N round-1 unit(s) in parallel. Waiting for completions."

## Step 4c: Process unit completions (serialized merge)

When each unit's `plan-implementer` agent returns, **immediately** process it before the next
one arrives. The orchestrator serializes all merges — never concurrently.

**First: validate the report.** All four trailer lines must be present:
- `ELAPSED_SECONDS: <n | unknown>`
- `VERIFIED: pass | fail | n/a`
- `FILES_TOUCHED:` (with paths on subsequent lines)
- `COMMITTED: yes | no`

If any trailer line is missing → **interrupted handoff**: surface the unit's report and offer:
- Re-run this unit (re-launch with the same prompt in its existing worktree)
- Inspect its worktree diff manually
- Mark failed and continue with remaining units

**If `COMMITTED: no`:** Mark unit `failed`. Leave its worktree in place for inspection. Surface
the report and reason.

**If `COMMITTED: yes`:** Merge from the main worktree (never cd into the unit's worktree):
```bash
# From main worktree, merge the unit's branch
git merge --no-ff "$WT_BRANCH" -m "merge: round-1 unit ${UNIT_ID}"
```

- **Clean merge** → tear down the worktree:
  ```bash
  git worktree remove "$WT_PATH"
  git worktree prune
  git branch -D "$WT_BRANCH"
  ```
  Mark unit `merged`.

- **Conflict on an owned file** → resolve it yourself using full plan knowledge (you know both
  units' intent). Commit the resolution. Tear down the worktree. Mark unit `merged`.

- **Conflict on an unassigned/shared file** → **stop and ask the human** before resolving.
  Leave the worktree in place until resolved.

## Step 4d: Join barrier

**Do not advance to the Integration Gate until every unit is in a terminal state** (`merged`,
`conflict-resolved`, or `failed`). Track state per `id` — never count notifications (they interleave).

If any units are `failed` (committed: no), surface a summary and ask the human whether to:
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

**Any flag → gate failure.**

### Gate step 3: Determine outcome

- **No build errors AND no tamper flags** → Gate **passes**. Proceed to Round 2.
  Emit: `GATE: pass — build clean, no tamper flags`

- **Any failure** → Gate **fails**. Emit a summary of failures. Dispatch a fix-Haiku (next section).

### Gate step 4: Fix-Haiku convergence loop (on failure)

Max **K = 3** iterations. On each iteration:

1. Launch a fix `plan-implementer` agent (background) in an isolated worktree branched from current
   HEAD (not START_SHA). Prompt it with:
   - The specific failures from the gate (compile errors, stub locations, tamper flags)
   - "Fix only these specific failures. Do not touch test files. Do not modify build config scripts."
   - The owned files that need fixing
   - Full project context
2. When it returns, validate its trailer (same incomplete-report check as Step 4c).
3. Merge its branch back (same serialized merge pattern — no-ff, tear down worktree after).
4. Re-run Gate steps 1–3 on the updated tree.
5. If gate passes → exit loop. If still failing and iterations < K → repeat.
6. If gate still fails after K iterations → **stop and surface to the human** with all outstanding
   failures. Do not proceed to Round 2. Let the human decide.

Emit a line each time a fix-Haiku is dispatched: `GATE attempt <i>/<K>: dispatching fix-Haiku`

---

## Round 2: Spec-blind test author

Record the current HEAD before launching round 2:
```bash
git rev-parse HEAD  # store as ROUND2_START_SHA
```

Get the list of files round 1 changed:
```bash
git diff --name-only "$START_SHA"..HEAD
```

Launch a background `plan-implementer` with a **spec-blind test author** prompt:

> Your job is to write tests for the plan below. The plan has already been implemented by a prior
> pass — but you must **not** look at how it was implemented. Tests written from the implementation
> just encode the implementer's assumptions; tests written from the plan alone are an independent
> reading of the spec.
>
> **Plan:**
> [verbatim plan]
>
> **DO NOT read these files** (they are round 1's implementation):
> [file list from git diff --name-only START_SHA..HEAD]
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
> 2. Identify what behavior the plan implies should be testable.
> 3. Write tests covering that behavior — happy path plus at least one edge case per testable unit.
> 4. Run the tests. **Do not fix the implementation if tests fail** — failing tests are signal.
> 5. Commit the tests with a clear message.
> 6. Report: framework used, test paths added, pass/fail summary, failure messages verbatim if any.
> 7. **Self-check (required):** end your report with a line `SPEC_BLIND: yes` if you did not read
>    any forbidden file or run `git diff`/`git status`, or `SPEC_BLIND: no` followed by what you
>    read and why. Be honest — this is for evaluating whether the spec-blind constraint holds.
>
> [Full report trailer per plan-implementer instructions]
>
> [Project conventions, project context]

Tell the user: "Gate passed. Launching round 2 (spec-blind test author)."

### After round 2: machine-check spec-blind (Part C)

Validate the report trailer (same incomplete-report check). Then machine-check spec-blindness:

```bash
# Files round 2 touched
git diff --name-only "$ROUND2_START_SHA"..HEAD
```

Cross-reference against the round-1 implementation files (`git diff --name-only START_SHA..ROUND2_START_SHA`).
If any round-2 file is also a round-1 implementation file → **SPEC_BLIND: VIOLATED (touched impl files)**.

This is independent of the agent's self-reported `SPEC_BLIND:` line. Both are recorded in the summary.

On violation → **flag round 2's signal as compromised** and **proceed to round 3** (do not block —
this is an unattended background run; the adversary is the backstop).

---

## Round 3: Adversary

Collect the full file split:
```bash
IMPL_FILES=$(git diff --name-only "$START_SHA".."$ROUND2_START_SHA")
TEST_FILES=$(git diff --name-only "$ROUND2_START_SHA"..HEAD)
```

Launch a background `plan-implementer` with an **adversary** prompt:

> You are an adversarial reviewer. A prior pass implemented this plan, and a separate pass wrote
> spec-blind tests. Your job is to find divergence between the implementation and the plan —
> assume something is wrong somewhere. Don't confirm correctness; argue against it.
>
> **Plan:**
> [verbatim plan]
>
> **Round 1 report (implementer):**
> [report]
>
> **Round 2 report (test author):**
> [report]
> **SPEC_BLIND machine-check result:** [verified by diff | VIOLATED — touched impl files]
>
> **Implementation files (round 1):** [IMPL_FILES]
> **Test files (round 2):** [TEST_FILES]
>
> Treat this as your plan:
> 1. Read the implementation files.
> 2. Read the test files. Note which fail and why.
> 3. Investigate divergence: where does the implementation drift from the plan? What did the
>    implementer rationalize past? Where would this break in production? Consider edge cases,
>    error paths, concurrent access, malformed input, resource leaks, missing validation.
> 4. For each finding, decide:
>    - **Fix it** — only if it affects correctness AND the fix is unambiguous.
>    - **Flag it** — if ambiguous, or if it concerns plan quality rather than implementation.
> 5. **Do not touch style, naming, formatting, or comments** unless they directly impact behavior.
> 6. If you fixed anything, re-run the tests and report results.
> 7. Commit fixes (if any) with a clear message.
> 8. Report: issues found (numbered). For **each** issue, label its source as one of:
>    - `[FROM_TEST]` — surfaced by a failing round 2 test
>    - `[INDEPENDENT]` — found by reading the code, not caught by any test
>    - `[PLAN_GAP]` — the plan itself was ambiguous or missing a constraint
>
>    Then state whether you fixed or flagged it (with reasoning), and report final test status.
>
> [Full report trailer per plan-implementer instructions]
>
> [Project conventions, project context]

Tell the user: "Round 2 complete — [N tests added, M failing | no tests committed]. Launching round 3 (adversary review)."

### After round 3: validate and surface

Validate the round-3 trailer. Then surface the final summary.

---

## Incomplete report handling (applies to every round and every unit)

A report missing any of the four required trailer lines (`ELAPSED_SECONDS`, `VERIFIED`,
`FILES_TOUCHED`, `COMMITTED`) is an **interrupted handoff** — do not silently proceed.

Surface the truncated report and offer a menu:
- **Re-run** — re-launch with the same prompt (unit retains its worktree / state)
- **Inspect** — let the human review the worktree diff and decide next steps
- **Skip** — mark this unit failed, continue with the rest (only for round-1 units)
- **Abort** — stop the flow entirely

---

## Orchestrator verification lessons (applies to every round)

Hard-won failure modes from real runs. The multi-agent gate is only as good as the
orchestrator's own verification — never trust round self-reports.

- **Self-reports overstate success.** Agents have reported "all tests pass" over real
  failures, "clippy clean" from a run without `-D warnings` or with `--lib` only, and
  "cargo test passed" when only `cargo check` ran (the test binary didn't compile).
  Re-run the exact gate commands yourself after every round.
- **`COMMITTED: yes` can be false; `COMMITTED: no` can hide finished work.** Verify with
  `git --no-pager log --format='%h %s' <START_SHA>..HEAD` and
  `git --no-pager diff --name-only <START_SHA> HEAD`. If round 1 left complete work
  uncommitted, validate, fix, and commit it yourself — rounds 2/3 need a committed base.
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
- **Agents die mid-format and drift cwd.** Expect to finish formatting/commits yourself;
  when a merge conflicts unexpectedly on an agent's files, check whether it edited the
  MAIN worktree instead of its own (`git -C <wt> status` both places) — the work is
  usually salvageable in place.
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

Collect each round's `ELAPSED_SECONDS`. Format all as `mm:ss`. Sum = total agent compute.

```
ROUND 1 — Implementer (parallel)
  Units: <N>   Merged clean: <c>   Conflicts resolved: <c>   Failed: <c>
INTEGRATION GATE (trusted)
  Build/type-check: <pass | fail>   Convergence iterations: <i>/<K>
  Tamper flags: tests-modified <c>  verify-neutered <c>  stubs <c>
ROUND 2 — Spec-blind test author
  SPEC_BLIND: <verified by diff | VIOLATED — touched impl files> (self-report: yes | no)
  Tests added: <count, paths>   Pass/fail: <P passed, F failed>
ROUND 3 — Adversary
  Issues found: <count>
    [FROM_TEST]:    <count>  ← signal that round 2 caught real divergence
    [INDEPENDENT]:  <count>  ← signal that adversary stance earned its keep
    [PLAN_GAP]:     <count>  ← signal that the plan needs sharpening
  Fixed: <count>   Flagged: <count>
  Final test status: <P passed, F failed>
TIMING (agent compute per round — self-measured; excludes idle between turns)
  Round 1 (implement):  <mm:ss>  [<N> units in parallel]
  Gate convergence:     <i> iteration(s)
  Round 2 (tests):      <mm:ss>
  Round 3 (adversary):  <mm:ss>
  Total agent compute:  <mm:ss>
```

Then a one-line **timing read** naming the long pole (e.g. "Round 1 dominated at 6:12 of 9:40 total
across 2 parallel units; wall-clock was approximately half that").

Then a one-line **experiment read**: which rounds produced signal, which didn't.

Suggested next steps:
- Review the diff and run `/expert-review`
- Ship with `/shipit`
- If round 3 flagged `[PLAN_GAP]` issues, revise the plan before re-running
- If round 2's spec-blind was VIOLATED, treat round-3 findings with lower confidence (adversary
  may have had prior exposure to the implementation)
