---
description: Launch a background Haiku agent to implement the current issue or a passed plan. Re-run to iterate and improve.
allowed-tools: Read, Bash(gh issue view:*), Bash(git log:*), Bash(git branch:*), Bash(git rev-parse:*), Bash(git diff:*), Bash(pwd:*), Bash(find:*), Agent
---

# Implement with Haiku

Launch a background `plan-implementer` Haiku agent and return immediately. You'll be notified when it's done.

The flow runs **three rounds with distinct cognitive roles** — same model, divergent prompts:

1. **Implementer** — builds to plan
2. **Spec-blind test author** — writes tests from the plan without reading the implementation
3. **Adversary** — assumes the implementation is wrong somewhere, finds divergence and production failure modes

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

Remember the SHA from `git rev-parse HEAD` — call it `START_SHA`. You'll use it between rounds to identify which files each round changed.

**Stage timing (telemetry).** This flow captures the wall-clock of each round so you can see which round is the long pole (it's the input that decides whether parallelizing a round is worth it). Use a tiny on-disk log so the numbers survive context summarization between notifications:

```bash
TIMING_LOG="/tmp/iwh-timing-${START_SHA}.txt"
echo "T_START=$(date +%s)" > "$TIMING_LOG"   # overall start; truncates any stale log for this SHA
```

At each round boundary you'll append a `KEY=$(date +%s)` line (instructions below). Each round's duration is `end - start`; the total is `last - T_START`. Read the log back with `cat "$TIMING_LOG"` when you build the final summary.

Also read these files using the `Read` tool if they exist (skip silently if not):
- `CLAUDE.md` — project conventions and coding rules
- `.claude/project.yaml` — project-specific context

## Step 3: Launch round 1 — Implementer

Record the round-1 start immediately before spawning:

```bash
echo "R1_START=$(date +%s)" >> "$TIMING_LOG"
```

Spawn a `plan-implementer` sub-agent with `run_in_background: true`.

The prompt must be fully self-contained — include:
- The full plan text (verbatim)
- Absolute path of the working directory
- Current branch name
- The 5 most recent commit messages (for commit style)
- Contents of `CLAUDE.md` if found (under a "Project conventions" heading)
- Contents of `.claude/project.yaml` if found (under a "Project context" heading)
- Names of any style/lint/format config files found by the `find` above (so the agent knows to read them before editing)
- **An explicit instruction: "Do not write tests in this pass. A separate pass will write tests from the plan."**

## Step 4: Confirm

One sentence confirming round 1 is launched and that two further rounds (tests, adversary review) will follow automatically.

---

## Iteration loop (3 rounds, distinct roles)

After each task notification, count prior `plan-implementer` notifications in this conversation to determine which round just finished.

### After round 1 (implementer)

Record the round-1 end first: `echo "R1_END=$(date +%s)" >> "$TIMING_LOG"`.

**If `COMMITTED: no`:** stop. Surface the report (include round-1 duration `R1_END - R1_START`). Suggest `/expert-review`, `/shipit`, or re-run.

**If `COMMITTED: yes`:**
- Get the list of files round 1 changed:
  ```bash
  git diff --name-only START_SHA..HEAD
  ```
- Launch round 2 (background `plan-implementer`) with a **spec-blind test author** prompt:

  > Your job is to write tests for the plan below. The plan has already been implemented by a prior pass — but you must **not** look at how it was implemented. Tests written from the implementation just encode the implementer's assumptions; tests written from the plan alone are an independent reading of the spec.
  >
  > **Plan:**
  > [verbatim plan]
  >
  > **DO NOT read these files** (they are round 1's implementation):
  > [file list]
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
  > 4. Run the tests. **Do not fix the implementation if tests fail** — failing tests are signal for the next pass.
  > 5. Commit the tests with a clear message.
  > 6. Report: framework used, test paths added, pass/fail summary, failure messages verbatim if any.
  > 7. **Self-check (required):** end your report with a line `SPEC_BLIND: yes` if you did not read any forbidden file or run `git diff`/`git status`, or `SPEC_BLIND: no` followed by what you read and why. Be honest — this is for evaluating whether the spec-blind constraint actually holds.
  >
  > End with `COMMITTED: yes` or `COMMITTED: no`.
  >
  > [Project conventions, project context as in round 1]

- Record the round-2 start before spawning: `echo "R2_START=$(date +%s)" >> "$TIMING_LOG"`.
- Tell the user: "Round 1 complete — implementation committed (took `R1_END - R1_START`s). Launching round 2 (spec-blind test author)."

### After round 2 (test author)

Record the round-2 end first: `echo "R2_END=$(date +%s)" >> "$TIMING_LOG"`.

- Get the full file list since `START_SHA`:
  ```bash
  git diff --name-only START_SHA..HEAD
  ```
- Separate into round 1's files (implementation) and round 2's new files (tests).
- Launch round 3 (background `plan-implementer`) with an **adversary** prompt:

  > You are an adversarial reviewer. A prior pass implemented this plan, and a separate pass wrote spec-blind tests. Your job is to find divergence between the implementation and the plan — assume something is wrong somewhere. Don't confirm correctness; argue against it.
  >
  > **Plan:**
  > [verbatim plan]
  >
  > **Round 1 report (implementer):**
  > [report]
  >
  > **Round 2 report (test author):**
  > [report]
  >
  > **Implementation files (round 1):** [list]
  > **Test files (round 2):** [list]
  >
  > Treat this as your plan:
  > 1. Read the implementation files.
  > 2. Read the test files. Note which fail and why.
  > 3. Investigate divergence: where does the implementation drift from the plan? What did the implementer rationalize past? Where would this break in production? Consider edge cases, error paths, concurrent access, malformed input, resource leaks, missing validation.
  > 4. For each finding, decide:
  >    - **Fix it** — only if it affects correctness AND the fix is unambiguous.
  >    - **Flag it** — if ambiguous, or if it concerns plan quality rather than implementation.
  > 5. **Do not touch style, naming, formatting, or comments** unless they directly impact behavior.
  > 6. If you fixed anything, re-run the tests and report results.
  > 7. Commit fixes (if any) with a clear message.
  > 8. Report: issues found (numbered). For **each** issue, label its source as one of:
  >    - `[FROM_TEST]` — surfaced by a failing round 2 test
  >    - `[INDEPENDENT]` — found by reading the code, not caught by any test
  >    - `[PLAN_GAP]` — the plan itself was ambiguous or missing constraint
  >
  >    Then state whether you fixed or flagged it (with reasoning), and report final test status.
  >
  > End with `COMMITTED: yes` or `COMMITTED: no`.
  >
  > [Project conventions, project context as in round 1]

- Record the round-3 start before spawning: `echo "R3_START=$(date +%s)" >> "$TIMING_LOG"`.
- Tell the user: "Round 2 complete — [N tests added, M failing | no tests committed]. Launching round 3 (adversary review)."

### After round 3 (adversary)

Record the round-3 end first, then read back the full log:

```bash
echo "R3_END=$(date +%s)" >> "$TIMING_LOG"
cat "$TIMING_LOG"
```

Compute each duration in seconds (`*_END - *_START`) and the total (`R3_END - T_START`); format as `mm:ss`.

Surface a final summary in this exact structure so the experiment is legible at a glance:

```
ROUND 1 — Implementer
  Files changed: <count>
  Commit: <sha or "none">

ROUND 2 — Spec-blind test author
  SPEC_BLIND: <yes | no — and what was read if no>
  Tests added: <count, paths>
  Pass/fail: <P passed, F failed>

ROUND 3 — Adversary
  Issues found: <count>
    [FROM_TEST]:    <count>  ← signal that round 2 caught real divergence
    [INDEPENDENT]:  <count>  ← signal that adversary stance earned its keep
    [PLAN_GAP]:     <count>  ← signal that the plan needs sharpening
  Fixed: <count>   Flagged: <count>
  Final test status: <P passed, F failed>

TIMING (wall-clock per stage)
  Round 1 (implement):  <mm:ss>
  Round 2 (tests):      <mm:ss>
  Round 3 (adversary):  <mm:ss>
  Total:                <mm:ss>
```

Then a one-line **timing read** naming the long pole — e.g. "Round 1 dominated at 6:12 of 9:40 total; parallelizing it is where the wall-clock win is" or "All three rounds were comparable (~3 min each); no single long pole."

Then a one-line **experiment read**: which rounds produced signal, which didn't. Examples:
- "Round 2 stayed spec-blind and caught 2 divergences; round 3 found 1 more independently. All three roles earned their keep."
- "Round 2 self-reported reading the implementation; spec-blind constraint did not hold this run."
- "Round 2 tests all passed; round 3 still found 3 independent issues — tests may be too weak for this plan."
- "Round 3 flagged 4 PLAN_GAP issues — sharpen the plan before re-running."

Suggested next steps:
- Review the diff and run `/expert-review`
- Ship with `/shipit`
- If round 3 flagged `[PLAN_GAP]` issues, revise the plan before re-running
