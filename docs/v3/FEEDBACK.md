# Flow v3: feedback from real use

Findings from running v3 on real repositories, recorded as they come up. Nothing here is decided or
built yet; decisions go in `PROGRESS.md`.

## 1. Pay for the full check once, right before merge (2026-11-25, from the human)

**Feedback.** Do not prove the base green with a full check. The base is `main`; assume it is green.
The branch is rebased before merge anyway, so a full check earlier proves little. On a repo with an
expensive check, the full check should run about once per ticket, right before merge.

**What v3 does today.** Seen on the first real repo, a private project whose full check builds and
tests a Rust workspace and a desktop app:

| When | Check | How often |
|---|---|---|
| `doctor` | full, on a throwaway checkout of the base | once per `flow.yaml` content, not once per run |
| Worktree step | fast, on the untouched base | every ticket |
| After implement | fast | every ticket |
| After each fix round | fast | up to three times per ticket |
| Merge gate | full | every ticket |

`init` sets `check.fast` to the same command as `check.full` when it cannot detect a cheaper one,
so in that repo every row is a full check.

**What the base check buys.** Attribution: if the base is red, every later failure looks like the
ticket's fault and fixers chase problems they cannot fix. It also caught a real setup gap here: a
fresh worktree fails the check until a per-worktree setup script has run, and `doctor` found that
before any model call was spent.

**Options to weigh.**

- Drop the base check from the worktree step; trust the base. On a red check mid-flow, check the
  base then, and only then, to decide whose failure it is.
- Make `doctor` prove only that install and setup work in a fresh worktree, not that the full check
  passes.
- Require a real `check.fast` (or ask for one at `init`) instead of silently reusing the full
  command; warn when both are the same and the full check is slow.
- Run mid-flow checks scoped to what the ticket changed.
- Keep one full check at the merge gate, on the tree that will merge, as the only authority.

## 2. A fresh worktree may need more than the package install (2026-11-25)

`init` detected `bun install` as the install step. The repo's check also needs a build artefact that
a per-worktree setup script creates. `doctor` failed until that script was added to `install`.
`init` cannot know this; the `doctor` failure message printed the repo's own hint, which was enough.
Cost: one wasted `doctor` run.

## 3. `doctor` gives no progress (2026-11-25)

While `doctor` runs a long check it prints nothing, and its output shows only the last error line on
failure. A human watching cannot tell a slow check from a hung one.

## 4. Is `doctor` required at all? (2026-11-25, from the human)

**Feedback.** The human is not sure `doctor` should be a requirement before `run`.

**What it does.** Five steps: tools present, config parses, base fetches, install works in a
throwaway checkout, full check passes there. `run` and `fleet start` refuse until all five passed
for the current `flow.yaml`.

**Assessment.** The first four cost seconds and each prevents a stop a few minutes into a paid run.
The fifth is the expensive one and is the same base-green proof as entry 1. On the first real repo
it did catch a real setup gap, but a ticket's first check would have caught the same gap, at the
cost of one stopped flow.

**Option.** Keep `doctor` as a cheap required preflight (the first four steps) and make the full
check on the base opt-in (`doctor --full`), never a condition of `run`.

**Done (PROGRESS decision 32).** `doctor` no longer runs the checks unless asked (`--checks`), and the
per-ticket base check is opt-in (`check.base: true`). Entries 1 and 4 are otherwise still open: the
mid-flow checks run as before.

## 5. A commit hook rejected the driver's commit (2026-11-25)

The first real ticket stopped at `spec_tests` after 18 minutes: the repo's pre-commit hook checks
formatting, the spec-test author wrote one unformatted import line, and `git commit` failed. The stop
was classed `runner` with the hook output as the reason, which was readable but not healable.

**Done (PROGRESS decision 33).** `flow.yaml` takes a `format` command that the driver runs before each
commit. Still open: a commit that a hook rejects for another reason is a `runner` stop with no heal,
and `init` does not detect a formatter or a hook.

Up to the stop the run had: answered one architecture question from a principle, written a nine-unit
plan with five advisers, and healed one unit shortfall by retrying on a stronger model.

## 6. Red-first spec tests cannot pass a validating commit hook (2026-11-25)

After the format fix the same commit failed again: the hook also typechecks, and spec tests written
before the implementation reference fields and exports that do not exist yet. This is structural for
any repo whose commit hook validates code, not a one-off.

The human's answer: the hook is not needed for flow commits, since the flow handles that.

**Done (PROGRESS decision 34).** `commit_hooks: false` in `flow.yaml`. Not built: holding the spec
tests outside the worktree until implement finishes, which would keep hooks on at the cost of one
hook run per driver commit. Also seen: a resume after a failed commit pays for a fresh test-author
call instead of committing the work already staged.

## 7. A question the driver could not show stopped the flow (2026-11-25)

At implement, 47 minutes in, an implementer needed a one-line change in a file outside its unit (a
caller that had to pass a new argument, or the crate would not compile). It asked, correctly, and
marked the question a risk, but gave no risk text. The driver refused the card and stopped as
`runner`. The human had a question to answer and no way to answer it.

**Done (PROGRESS decision 35).** The card is shown with a fallback risk line. Still open: the plan
gave the unit a signature change without the caller, so the question was avoidable; the planner
should put a changed function's callers in the same unit.

## 8. A card offered an option the driver cannot carry out (2026-11-25)

After the fix in entry 7 the same worker asked again, this time as an architecture card with three
options. One was "widen this unit to include the caller's file". The driver has no way to do that:
a unit's file list is fixed by the plan, so the edit would be reverted again whatever the human
answered. Workers write their own options and nothing checks them against what the driver can do.

The root cause is in the plan: a step of the unit said, in words, to edit the caller's file, and the
unit's file list did not include it. Another unit owns that file. Plan validation checks that units
do not share files; it does not check that a unit's steps stay inside its own files.

**Options to weigh.**

- Let an answer widen a unit's files for that one call (not protected paths), and tell workers that
  this is the one "outside my files" option they may offer.
- Have the plan auditor check each step's named files against the unit's file list.

## 9. The fast check runs after every unit (2026-11-25)

Read from the code while handling entry 8: implement runs `check.fast` after each unit, with up to
two fixer rounds, not once after the stage. With nine units and no cheap `check.fast`, that is nine
or more runs of the full command. In this trial they are cheap by accident: the full command stops
at its first failing part, and the red-first spec tests fail an early part until the last units
land. A repo whose tests fail late would pay for each run in full. This is the largest multiplier in
entry 1's table and was missing from it.
