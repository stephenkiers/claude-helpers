# Flow v3: build progress

If this session restarts: read this file first, then continue from the first unit not marked accepted.

## Units

| # | Unit | State | Commit |
|---|---|---|---|
| 0 | Probes (`docs/v3/probes.md`) | accepted | (with interfaces commit) |
| - | Shared interfaces: `types.py`, `config.py`, `schemas.py`, `docs/v3/INTERFACES.md` | accepted | (with interfaces commit) |
| 1 | `runner.py` + fake `claude` binary | accepted | 60a79ce4 |
| 2 | `state.py`, `driver.py`, `engine.py`, `record.py`, `cli.py` skeleton | accepted | e4a78535 |
| 3 | `repoconfig.py`, `checks.py`, `gitops.py`, `provider.py` (local), doctor (init CLI lands with unit 10) | accepted | ed1c552c |
| 4 | `heal.py` | accepted | 892ae847 |
| 5 | `questions.py`, `voice.py`, `principles.py` | accepted | db264af0 |
| 6 | `steps/worktree.py`, `steps/plan.py` + prompts | in progress | |
| 7 | `steps/spec_tests.py`, `steps/implement.py`, `steps/work.py` + prompts | in progress | |
| 8 | `steps/review.py` + prompts | in progress | |
| 9 | `steps/fix.py`, `steps/ship.py`, `mergegate.py`, `provider.py` (github) | in progress (ship, mergegate, github first; fix after 7 and 8) | |
| 10 | `fleet.py`, inbox, `commands/flow3.md`, `why`, `principles init`, docs | not started | |
| 11 | End-to-end sandbox runs | not started | |

## Section 12 coverage (each row must have a test)

| Row | Covered by |
|---|---|
| Machinery (bad structured output) | `tests/test_flow3_heal.py`: schema and transient tests |
| Environment (base behind, check timeout, cleanup after merge) | `tests/test_flow3_heal.py`: base-stale, environment (timeout, missing dependency), cleanup tests |
| Work failed (fix loop, partial unit) | `tests/test_flow3_heal.py`: check-work and unit-shortfall tests |
| Test-edit approval | pending: test-edit rule test (implement) |
| No reason recorded | `tests/test_flow3_heal.py` final tests; `tests/test_flow3_driver.py` stop construction |
| Genuine (prerequisite not merged) | pending: worktree stage dependency test |
| Questions: principle / fact / detail / human | `tests/test_flow3_questions.py`: resolver order tests |

## Decisions the plan did not settle

1. **Model overrides live in `.claude/flow.yaml` `models:` only.** The plan names both that key and a
   separate `.claude/flow3.json`; one file is simpler. The key `"*"` overrides every role (used to
   keep sandbox runs on `haiku`).
2. **Healing lives in `heal.py`, not `runner.py`.** `runner.run` is one launch; `heal.call` wraps it
   with the section 5 budgets. The plan lists "retry" under the runner; this keeps every bounded
   response in one module with one test file.
3. **An `engine.py` module** holds the impure loop around the pure `driver.step`, so the driver stays
   a pure function as the plan requires.
4. **A `context.py` module** carries the per-flow context (`Ctx`) handed to stages.
5. **Stages:** `worktree, plan, spec_tests, implement, review, fix, ship, merge_gate, merge, done`.
   Plan verification is inside the plan stage; re-review is the review stage run again.
6. **Probe findings that changed the design** (see `probes.md`): a call can report `subtype: success`
   with `is_error: true`, so classification reads `is_error` and `terminal_reason` first; a permission
   denial does not fail a call, so `permission_denials` is checked on every result; a real
   usage-limit hit could not be reproduced, so `limit` is classified from three independent signals.
7. **The `local` provider stores issues and PRs inside the bare remote** (`<origin>/flow3/`).
8. **"What the experts think" on a card** may hold a single view from the step that asked, when the
   question did not come from a panel (an implementer's question has no panel behind it).

9. **A crashed flow can be resumed.** `resume` on a flow whose status is still `running` re-runs the
   stage (stages are re-enterable); `engine.run_flow` holds a per-flow lock so a live run is never doubled.
10. **Merge hook keys are `merge.pre_merge` / `merge.post_merge`** as PLAN section 9 writes them.
11. **Doctor's merge dry run** is limited to what is safe: for `method: command` it checks the
    command's program exists; it never runs the merge command.
12. **`heal.write_call`** wraps a writing call with the path rule, partial-work keeping, one escalation
    and the commit, so implement and fix share one code path. Stages never call `runner.run`.
13. **Risk and purpose questions never go to the principle resolver** (no model call at all): they
    always reach the human.
14. **Stage data shapes and the shared `steps/work.py` helper** are fixed in `INTERFACES.md`
    ("Stage contracts") so units 6 to 9 could be built in parallel.
15. **Send back from the merge gate** is a saved direction plus `data.sendback`; fix turns it into one
    fix unit, then review runs on the fix diff and the flow ships again.

## Spend (notional USD, real model calls)

| What | Cost |
|---|---|
| Unit 0 probes (14 haiku calls) | 0.01 |
