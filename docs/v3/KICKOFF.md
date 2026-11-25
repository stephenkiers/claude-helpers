# Flow v3: overnight build kickoff

You are the orchestrator for building Flow v3 in this worktree, unattended, overnight. Nobody will
answer questions until morning. Read `docs/v3/PLAN.md` in full before doing anything; it is the
specification. Read the repo's `CLAUDE.md` for its conventions.

## Your role

You orchestrate, review, and verify. Cheaper models write the code.

- Spawn a subagent for each build unit with an explicit model: `sonnet` for the units in the plan's
  build order, `haiku` for mechanical work (boilerplate tests, doc tables, renames). Do not write
  module code yourself except for small corrections (under about 30 lines) where briefing another
  agent would cost more than the fix.
- Brief each agent completely: the unit's section of the plan, the exact interfaces it must match
  (paste the signatures), the files it may create or touch, the tests it must write, and the command
  that must pass. It knows nothing you do not tell it.
- When an agent reports back, do not trust the report. Read the diff, run the unit's tests yourself,
  run `ruff check scripts/flow3 tests/test_flow3_*.py` and `mypy scripts/flow3`. Send it back with
  specifics, or fix small things yourself. Two failed rounds on a unit: re-brief a fresh agent with
  what went wrong, on the same model first, then one tier up.
- Run independent units in parallel where their files do not overlap (after the spine, units 5 to 9
  mostly qualify). Define the shared interfaces yourself first (`schemas.py`, the driver's state and
  action types, the runner's call signature) so parallel units agree.

## Order of work

1. Create `docs/v3/PROGRESS.md` and keep it current: one line per unit (not started / in progress /
   accepted, with the commit), plus a short list of decisions you made that the plan did not settle.
   If this session is restarted, read it first and continue from there.
2. Read section 12 of the plan (last month's real hard stops). Every row there must be covered by
   a test of `heal.py`, the resolver, or the test-edit rule.
3. Unit 0 (probes) yourself, with real `claude -p` calls on `haiku`. Keep them tiny. Write what you
   observe to `docs/v3/probes.md`. If parallel headless calls or limits behave differently from what
   the plan assumes, adjust `runner.py`'s design and note it in PROGRESS.
4. Units 1 to 10 per the plan's build order.
5. Unit 11: the real end-to-end runs (single ticket, then the three-ticket fleet, then the
   `merge.method: command` repo) on a sandbox repository under a temporary directory with the
   `local` provider. Use cheap models for the sandbox run where the config allows; it is proving the
   plumbing, not the judgment. Fix what it exposes and run it again until it completes.
6. `just check` (it takes several minutes; run it in the background with a 900 second timeout, and
   on failure grep for the failing lines rather than reading the whole log). Fix until green.
7. Write `docs/v3/REPORT.md` as the plan's section 13 describes.

Commit after each accepted unit, locally, on `feature/v3-flow`, with a plain message naming the unit.

## Deciding without a human

- Where the plan is silent on a detail, choose the simplest thing that serves the plan's three rules,
  record it in PROGRESS under decisions, and continue.
- Where the plan is wrong (something it assumes turns out false), prefer the smallest change that
  keeps its intent, record what you found and what you changed, and continue.
- If something would change the purpose or the architecture (a different execution model, dropping a
  whole section), do not do it. Build everything that does not depend on it, and put the question at
  the top of REPORT.
- Watch spend: sandbox runs use the cheapest models the config allows, build agents are `sonnet` or
  `haiku`, and PROGRESS keeps a running note of what the real-model probes and runs cost.
- Cut scope before cutting verification. An honest REPORT with units 1 to 8 solid beats eleven units
  that have not been run. The order of the build table is the priority order.

## Hard limits

- Do not push, open a PR, or touch any remote. Do not merge anything.
- Change files only inside this worktree, under `~/.claude/flow3/`, and in temporary directories you
  create. Do not edit `~/.claude/settings.json`, do not run `install.sh` or `/setup-local`.
- v1 and v2 need not keep working on this branch: reuse, move, or change their code freely
  (`scripts/workflow/`, `scripts/flow_driver/`, prompts, reviewer personas) and delete tests that only
  pin behaviour you changed, saying so in REPORT. Do not delete the v1 or v2 commands themselves
  tonight; list what v3 makes redundant instead. Prefer reuse over rewriting when the existing code
  is sound and does not drag v1 concepts in with it.
- The sandbox end-to-end run uses only the `local` provider. Never point a test at a real GitHub repo.
- This is a public repo: no private project names, private issue numbers, or personal filesystem
  paths in any committed file. `just lint` runs a check for this.
- Follow `CLAUDE.md` shell conventions in `commands/flow3.md` (no bare positional tokens; `printf '%s'`
  into `jq`, never `echo`; `jq --arg`, never string-built JSON).
- Real model calls in tests: none. Unit tests use the fake binary. Only unit 0 and unit 11 spend on
  real calls, and both should stay small.

## Finish

Do not stop until `just check` is green and REPORT is written, or until you have genuinely run out of
things you can do without a human. If you hit a usage limit, wait and continue. The last thing you do
is print the REPORT's summary section.
