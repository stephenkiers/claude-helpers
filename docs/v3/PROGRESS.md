# Flow v3: build progress

If this session restarts: read this file first, then continue from the first unit not marked accepted.

## Units

| # | Unit | State | Commit |
|---|---|---|---|
| 0 | Probes (`docs/v3/probes.md`) | accepted | (with interfaces commit) |
| - | Shared interfaces: `types.py`, `config.py`, `schemas.py`, `docs/v3/INTERFACES.md` | accepted | (with interfaces commit) |
| 1 | `runner.py` + fake `claude` binary | in progress | |
| 2 | `state.py`, `driver.py`, `engine.py`, `record.py`, `cli.py` skeleton | in progress | |
| 3 | `repoconfig.py`, `checks.py`, `gitops.py`, `provider.py` (local), init, doctor | in progress | |
| 4 | `heal.py` | not started | |
| 5 | `questions.py`, `voice.py`, `principles.py` | not started | |
| 6 | `steps/worktree.py`, `steps/plan.py` + prompts | not started | |
| 7 | `steps/spec_tests.py`, `steps/implement.py` + prompts | not started | |
| 8 | `steps/review.py` + prompts | not started | |
| 9 | `steps/fix.py`, `steps/ship.py`, `mergegate.py`, `provider.py` (github) | not started | |
| 10 | `fleet.py`, inbox, `commands/flow3.md`, `why`, `principles init`, docs | not started | |
| 11 | End-to-end sandbox runs | not started | |

## Section 12 coverage (each row must have a test)

| Row | Covered by |
|---|---|
| Machinery (bad structured output) | pending: heal `schema` test |
| Environment (base behind, check timeout, cleanup after merge) | pending: heal `base-stale`, `environment`, `cleanup` tests |
| Work failed (fix loop, partial unit) | pending: heal `check-work`, `unit-shortfall` tests |
| Test-edit approval | pending: test-edit rule test (implement) |
| No reason recorded | pending: `FlowStop` / `make_stop` construction tests |
| Genuine (prerequisite not merged) | pending: worktree stage dependency test |
| Questions: principle / fact / detail / human | pending: resolver order tests |

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

## Spend (notional USD, real model calls)

| What | Cost |
|---|---|
| Unit 0 probes (14 haiku calls) | 0.01 |
