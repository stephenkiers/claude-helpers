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

**Done (#570).** The check runs once per stage, not once per unit. See PROGRESS decision 38.

## 10. A card that was not a human's question (2026-11-25, from the human)

The card in entry 8 reached the human, who called it weird and asked whether it was built for humans.
It was not. Nothing about the product or the design differed between its options; the only question
was which worker would type the same two lines. It was phrased in the driver's own terms ("unit 1",
"widen this unit"), terms the human never chose and has no reason to know, and it was labelled an
architecture decision.

The resolver asks whether a principle, a fact or a past decision answers a question. It never asks
whether the question is one only a human can answer.

**Options to weigh.**

- Settle "a file I need belongs to another unit" in the driver: leave the edit to the owning unit,
  note it in the journal, no card.
- Add a resolver outcome for questions about the flow's own bookkeeping (units, file lists, ordering):
  the driver takes the option that keeps the plan as written.
- Cards never use the words "unit" or a unit id; they describe the work in the ticket's terms.

**Done (#571).** The resolver settles driver bookkeeping questions without showing them. See PROGRESS decision 36 (the driver settles ownership questions) and PROGRESS section 6 (the driver is a resolver step).

## 11. Answering the card in entry 8 still stopped the flow (2026-11-25)

The answer was "the unit that owns the file makes the change". The worker obeyed, reported the unit
`partial` because that change was still outstanding, was retried on a stronger model, said the same
thing, and the flow stopped as an unfinished unit. Two paid calls to learn nothing. The worker had no
way to say "my part is done; this other part belongs to someone else".

The same worker also listed, as unfinished, a plan step that told it to run `git` commands. Workers
have no shell. The plan asked a unit for something no unit can do.

**Done (PROGRESS decision 36).** Workers hand such changes on through `elsewhere`. Still open: the
planner writing steps that need a shell, and steps that name files outside the unit.

## 12. The base moved during the run and the check refused the branch (2026-11-25)

The repo's check starts by refusing a branch that is behind the base. The base gained two commits
during the trial. The driver read the refusal as a failure in the unit's work: it ran the same check
four times on the same tree, then paid for two fixer calls. Both fixers said, correctly, that the fix
was a rebase and that they had no shell. The second "I cannot do this" stopped the flow as an
unfinished unit.

**Done (PROGRESS decision 37).** A red check on a branch behind the base merges the base first.

Still open:

- The same failing check ran four times on one tree before anything changed. Entry 9 undercounted.
- A fixer that says "this is not fixable from here" is treated as a unit that ran out of room, and is
  retried on a stronger model. It needs its own outcome.

**Done (PROGRESS decisions 38 to 40).** A tree is measured once per command, the check runs once per
stage, and "cannot fix from here" is its own outcome that is never retried on a stronger model.

## 13. The ticket was merged by other work while the flow was running (2026-11-25)

The trial re-implemented a ticket whose earlier implementation was still open as a pull request. That
pull request merged into the base during the run. The driver brought the base in (decision 37), hit
conflicts in most of the files it had written, made one resolver call, saw the check fail, undid the
merge and stopped as `conflict`. That is the designed behaviour and it left the branch clean.

What it did not do: notice that the base now contains the ticket's own change. A stop that said "the
base already has a commit for this issue" would have saved the resolver call and told the human what
actually happened. The driver could look for the issue number in the commits it is about to merge.

Trial totals at this stop: 1h21m, 29 model calls, $8.69, one of nine units finished. Most of the
elapsed time and seven of the stops were driver defects (entries 5 to 12), not the ticket.

## 14. Second trial: a small ticket, stopped at ship on a red the driver could not explain (2026-11-27)

A one-unit front-end ticket in a private project, run after decisions 38 to 41. The flow took 7
minutes and $2.52 over 30 model calls, asked no questions, and stopped at ship as `check` with the
fast check red. It reached review, fix and ship for the first time on a real repo. Nothing was
pushed.

What worked as designed: the red tree was measured once and reused three times; the fixer said it
was blocked and was not retried on a stronger model; no bookkeeping question reached the human.

The red was the spec tests, not the implementation. The test author sent a status payload with a
session id of `"A"`; the repo's schema requires a UUID there, so the hook dropped every status event
and four of six tests failed. The author had flagged the payload shape as an assumption.

Still open:

- **The trimmed check output hid the failures.** The log was 24,149 lines of ANSI-coloured test
  output. `checks.trim` kept the last 67 lines and 14 "interesting" lines, and all 14 were console
  noise from passing tests that contain the word `Error`. The four failing test names and their
  Expected/Received lines were cut. The fixer correctly said it could not see what failed. Trim
  should strip ANSI codes and prefer a test runner's failure blocks (`(fail)`, `Expected:`,
  `Received:`, `error:` at line start) over lines that merely mention an error. **Done (PROGRESS decision 42).**
- **A spec test that fails on arrival has no owner.** Spec tests are protected, so the implementer,
  the check fixer and the review fixer may not edit them. When the only failing tests are the ones
  the flow just wrote, the driver should either send them back to the test author with the failure
  or stop with a card that says so. Today it stops as a bare `check`. **Done (PROGRESS decision 43):** the judge reads them, and a
  wrong test is corrected or carded.
- **Review and fix ran on a tree known to be red.** After the blocked fixer, implement finished as
  done and the red was left "for the merge gate" (decision 40). Twenty review calls then ran on work
  whose tests did not pass. A blocked check fixer at the end of implement should stop there. **Done (PROGRESS
  decision 43).**
- **A test author's stated assumption was not checked.** "I assumed the payload has this field" is a
  checkable fact; a scout reading the schema would have caught the UUID rule before any test was
  written. **Done as prompt text only (PROGRESS decision 44).**
- The stop reason says only "the fast check still fails". It should name the failing tests. **Done (PROGRESS
  decision 42).**
- **The plan settled a judgment question without showing it (from the human).** Plan experts raised
  three questions. Two were facts and the plan checked them in the code, which is right. The third
  was a scope choice: the ticket's first acceptance test is unreachable as written, so either test
  the closest reachable case or add the missing behaviour. The plan took the recommended option,
  listed the other as a non-goal, and no card or decision record was written. The human's rule:
  until the driver has earned trust, every question that is not straightforward reaches the human as
  a card with the question, the options, the recommendation and the impact of each, to be
  rubber-stamped. "Reversible, with a recommendation" is not grounds to settle it silently. Fewer
  questions is the goal later, and it should be reached by an explicit opt-in, not by default.
