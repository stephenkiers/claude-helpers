# Flow v3: build report (2026-11-25)

## Summary

Flow v3 is built on `feature/v3-flow` (about 30 local commits, nothing pushed) and `just check` is green.
It ran real tickets end to end in sandbox repositories with the `local` provider: one single ticket,
a fleet of three merged one at a time, and a repo that merges through its own command. It has never
run against GitHub or a real repository, and every sandbox run used `haiku`.

- **Works in the sandbox:** issue text to merged for five tickets; parking on questions and
  answering in free text; one reply covering several questions and directions; a principle answering
  a question; an injected check failure healed; a stale base healed; send back from the merge gate;
  `doctor` refusing a red check; `why` for each decision source.
- **The two questions this report asked are answered** (next section); the first changed the code
  after the sandbox runs.
- **The weak spot is review and fix quality on `haiku`:** findings are not de-duplicated, and fix and
  review can undo each other. Expect long "open findings" lists at the merge gate until that is tuned.
- **Eight driver defects were found by the real runs and fixed** (PROGRESS decisions 21 to 30). Expect
  the first real ticket to find more: the fake binary did not catch any of these.
- **Not verified:** the `github` provider live, a usage-limit hit, three tickets truly running at
  once, and a `fact` question arising on its own.
- **v1 and v2 are untouched** and no test was deleted. v3 makes about 45,000 lines of them redundant
  (table below); removing them is one decision after a real ticket merges through v3.
- The branch is 4 commits behind `main`. I did not rebase it.

Start with `docs/v3/USING.md`, then the "First things to try" list below.

## Questions, answered

Both were answered on 2026-11-25, after the build.

1. **A `fix` answer given in the last allowed review round now runs a fix.** One more fix run
   handles only the findings you answered `fix`, then the flow ships with no further review
   (PROGRESS decision 31). Other findings from that round still show as open at the gate. This is
   covered by tests with the fake binary, not by a real run.
2. **A finding in a protected test file stays open at the gate** (decision 24). No change. Revisit
   when a real ticket shows how often it comes up.

## What works

Verified by real model calls in sandbox repositories (records and commands in `docs/v3/e2e/`,
summary in `docs/v3/e2e/TRANSCRIPT.md`):

| Plan section 13 item | Result |
|---|---|
| Single ticket, issue text to merged | Done: `solo`, $0.28, three free-text answers, one send-back, then merged |
| A question answered by a principle | Done: fleet ticket #3, plan question `Q1`, by `P-errors` |
| A question answered as `assumed` | Partly: the one in a merged flow was a defect (below); a clean one exists only in an abandoned run |
| Injected check failure healed | Done: fleet tickets #1, #2 and #3, green after fixer round 1. Not in the single-ticket run |
| Fleet of three, one parked while others proceed | Done: #2 parked at plan while #1 reached its gate |
| Free-text reply covering two questions and one direction | Done: one reply answered three questions and set two directions |
| All three merged one at a time | Done |
| `merge.method: command` merges correctly | Done, after two fixes (decisions 27, 28) |
| `doctor` rejects a red full check | Done: `e2e/doctor-red.txt` |
| `why` for human, principle, assumed, fact | Done, with `fact` and `assumed` from outside the merged flows (below) |

Verified by tests with the fake binary (27 test files, about 7,300 lines, all in `just check`):
every driver transition; every row of the section 5 heal table; the resolver order; no stop without
class and reason; protected-path enforcement; the merge lock and pinned merge on the `local`
provider; the review sweep over every `review`-context reviewer in the index; a whole-flow
integration test through the real CLI.

Size: `scripts/flow3/` 8,709 lines of Python, `prompts/flow3/` 805, `commands/flow3.md` 78,
`docs/v3/USING.md` 91.

## What was not verified

- **The `github` provider has never made a live call.** It is covered only by tests that fake `gh`.
- **No real repository.** The sandboxes are a three-function Python library.
- **Only `haiku`** (plus the driver's own single escalation to `sonnet`). Cost and quality on the
  real role models are unknown. The per-call costs the binary reported are notional: the five
  finished flows total about $1.00.
- **Three tickets running in the same instant.** The fleet was `1,2>3` with `--max 2`, so two ran at
  once and the third waited on its dependency.
- **A `fact` question arising in a flow.** No step asked one. The `why` output for `fact` comes from
  a question I fed by hand through the same resolver, with a real scout call.
- **A clean `assumed` answer in a merged flow.** See the defect below.
- **A usage-limit hit.** It could not be reproduced in the probes; the classification rests on three
  signals read from documentation and other error shapes.
- **Merge hooks** (`pre_merge`, `post_merge`) and **the install step** with a real package manager.
- **`/flow3` as a slash command.** The command doc was written and linted, not invoked: that needs
  `/setup-local`, which I was told not to run.
- **Hooks and guards.** v3 calls run headless with a tool list per role; no v1 guard hook applies to
  them, and I did not test what a model can do outside that list.

## Defects found by the real runs

Fixed, each with a test (PROGRESS decisions 21 to 30):

1. The prompt was read as a value of `--tools`; it is now passed after `--`.
2. A red spec test was classified as an environment failure.
3. The plan put new tests in an existing test file, which the implementer was told both to edit and
   never to edit. Spec tests now go in new files only.
4. A spec test imported a library the sandbox did not have; two fixer rounds were spent on a file
   nobody could edit. It is now a missing-dependency stop.
5. The merge gate showed "Open: none" while findings from the last review round were unfixed.
6. A command merge succeeded and the flow then stopped, because the local provider did not see it;
   the resume then tried to merge again.
7. A fixer that could not finish stopped the whole flow, twice, when the rest of its work was in
   protected tests. It now leaves those findings open for the gate.
8. A fixer asked whether to lift protection on a test file, labelled the question a detail, and the
   driver took the fixer's own recommendation ("lift it") as `assumed`. The path rule still reverted
   the edit, so no protected file changed, but the question should have reached you. Any question
   that names a protected path is now a risk question. Fixed after the fleet run finished, so it is
   covered by a test and not by a real run.

Seen and not fixed:

- **Review findings are not de-duplicated.** Ticket #3 reached its gate with 23 open findings, many
  of them the same point from different reviewers; ticket #2 had eight near-identical ones.
- **Fix and review churn on `haiku`.** A fix removed a check and the next review asked for it back.
- **A re-review asked again about a decision the human had already made**, and the plan auditor
  re-raised an `assumed` answer.
- `flow3 gate` prints a screen for a flow that is not at the gate.
- `tests/test_spec_blind_341_followups.py` (a v1 suite this branch does not touch) failed once in one
  full `just check` run, passed three times alone, and passed in the full run before and after.
- `flow3 why` always shows "Downstream: (none recorded)".
- A card said "What we decided before: Nothing relevant" when the same flow's plan had a related
  decision.

## What was cut

- **Rebase onto `main`**: the branch is 4 commits behind. Not done, to leave the history as built.
- **Doctor's merge dry run** for `method: command` only checks that the program exists; it never
  runs the command.
- **De-duplication of review findings** beyond what the review prompts ask for.
- No ADR was written for v3. `docs/v3/PLAN.md` and the decisions in `docs/v3/PROGRESS.md` are the record.

## First things to try by hand

1. `just check`.
2. Read `docs/v3/USING.md`, then `docs/v3/e2e/TRANSCRIPT.md` and one gate screen
   (`docs/v3/e2e/trio-3-gate.txt`) to see what a merge gate looks like with weak reviewers.
3. In one real repo: `python3 -m scripts.flow3 init`, `doctor`, `principles init`, then one small
   ticket with the `github` provider. This is the first live use of that provider. Watch where it
   stops and whether each card can be answered cold.
4. On that ticket, check the cost shown by `status` against your own usage figures.
5. A fleet of three independent tickets with `--max 3`.
6. Compare each stop with the table in plan section 12.

## What v3 makes redundant

Nothing below was deleted or changed in behaviour. Line counts are from this branch.

| v1 / v2 | Lines |
|---|---|
| `commands/expert-flow.md` | 2,495 |
| `commands/expert-flow-v2.md` | 118 |
| `commands/expert-fleet.md` | 323 |
| `commands/heal-flow.md` | 53 |
| `commands/implement-with-haiku.md` (as a flow step; still useful alone) | 3,376 |
| `prompts/flow-reference.md` | 687 |
| `scripts/flow_driver/` (v2) | 8,442 |
| `scripts/workflow/` (v3 imports `fsutil` from it; the rest serves v1) | 16,942 |
| `scripts/fleet.py` | 956 |
| `scripts/flow-verify-receipt.py` | 6,994 |
| `scripts/flow-bash-guard.py`, `flow-agent-guard.py`, `flow-skill-guard.py` | 1,826 |
| `scripts/flow-ledger.py`, `flow-redo.py`, `flow-accept.py`, `flow-render-questions.py`, `flow-stop-baseline.py` | 2,562 |
| `agents/flow-*.md` | about 493 |
| Total | about 45,000, plus their tests |

`scripts/workflow/` also serves `/shipit`, `/cleanup` and the merge queue, so only part of it can go.
The guards also protect non-flow uses; check before removing.

## Tests deleted or changed

None deleted. `tests/test_workflow_mypy_support.py` had one expected string updated for the new
`mypy.ini` file list; `tests/run_all.py` and `mypy.ini` each gained the flow3 entries.

## Limits kept

Nothing was pushed, no PR was opened, no remote was touched, nothing was merged outside sandbox
repositories. Files changed only in this worktree and in a temporary directory; `~/.claude/flow3/`
was not used (the sandbox set `FLOW3_HOME`). `~/.claude/settings.json`, `install.sh` and
`/setup-local` were not touched. Tests make no real model calls.
