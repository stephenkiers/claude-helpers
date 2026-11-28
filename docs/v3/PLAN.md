# Flow v3: plan

Status: plan, not yet built. Branch `feature/v3-flow`. v1 (`/expert-flow`) and v2 (`scripts/flow_driver/`)
stay untouched and keep working while v3 is built and tried.

## 1. What this is for

One ticket goes from issue to merged PR with the human deciding only what a human should decide:
purpose, architecture, behaviour, and the merge. Everything else either heals, is answered by a
recorded principle, or is decided by the step and reported.

The spirit, in the owner's words: experts plan it, cheap background agents implement it, experts
review it, cheap agents fix it, then it ships and merges through a gate that is smart, healthy, and
merges one at a time.

**No feature parity and no backwards compatibility with v1 or v2.** Anything not listed in section 3
is out. v3 reuses whatever v1 and v2 code is worth reusing, and may change or break that code to do so.
This repo has one user; target repos are migrated to v3's config (section 9) rather than v3 bending
to old layouts.

## 2. The three rules that make it simple

1. **Python runs the flow. Models only do work.** A Python driver owns the state machine and launches
   every step itself as a bounded headless call (`claude -p`). No model reads prose to decide what
   happens next.
2. **Steps return data, and the driver keeps all of it.** Every call uses `--json-schema`. The driver
   reads `structured_output` and writes the record itself, so no model hand-writes a receipt that then
   needs verifying. Nothing is thrown away: section 8 lists what is kept so any decision can be
   audited later.
3. **Facts come from the driver, not from a model's say-so.** The driver runs the check command and
   reads the exit code. The driver reads `git` for what changed. A step saying "tests pass" counts
   for nothing.

Verified on this machine (Claude Code 2.1.332, subscription login, no API key):

- `claude -p --output-format json --json-schema <schema>` returns a JSON **array of events**. The
  last element with `type == "result"` carries `structured_output`, `session_id`, `is_error`,
  `subtype`, `num_turns`, `total_cost_usd`, `duration_ms`, `permission_denials`.
- A step can return `status: needs_answers` with questions, and a later
  `claude -p --resume <session_id>` with the answers continues with full context.
- `--permission-mode dontAsk` with `--allowedTools` lets a step edit files and run allowlisted
  commands unattended. A command outside the allowlist is denied and reported in
  `permission_denials`; it does not hang.
- Always redirect stdin (`< /dev/null`), or the CLI waits 3 seconds for input.

Not yet verified (the build must probe these early, see section 11): behaviour when several headless
calls run in parallel against subscription limits, and the exact error shape of a usage-limit hit.

## 3. Scope

In:

- One ticket, end to end, in its own worktree: plan, plan verification, implement, check, review,
  fix, re-review, ship, merge.
- A fleet: five or six tickets running at once, with one inbox for everything that needs the human
  (section 10).
- Per-repo process: each repo declares its own install, checks, and merge procedure (section 9).
- Answers in the human's own words: a typed or dictated reply is interpreted, saved verbatim, and
  recorded as decisions with reasons (section 6).
- Expert planning (several isolated experts, then synthesis).
- Implementation and fixes by cheap models, with automatic escalation to a stronger model.
- Spec-blind tests, written before the code exists.
- Blind-first review across every reviewer lens, then verification, then triage.
- Batched question relay with context, through a resolver that asks the human only what needs a human.
- Principles (recorded, human-approved) and a decision log that proposes new principles.
- Bounded self-heal for environment and work failures.
- Hard stops that always record a reason and the command to resume.
- A human merge gate, then a serialized merge that tests the exact tree that lands.
- Cost and time per step, read from each call's own result.

Out (not built; may come later):

- Stacked branches, the big-stones pre-pass, automatic epic decomposition.
- Coworker PR review mode, redo gates, step gates, the assess stage, v1 state compatibility.
- The old token-yield tracking (`reviewer-yield.py`) and Precedent Pat's prediction logging. Their
  purpose is covered by sections 6 and 8.
- Any force-push. v3 never force-pushes (see section 7.8).

## 4. Layout

```
scripts/flow3/
  __main__.py      entry: python3 -m scripts.flow3 <subcommand>
  cli.py           init | doctor | run | fleet | inbox | answer | say | resume | status | merge | principles
  repoconfig.py    read and validate the repo's flow.yaml (section 9)
  fleet.py         launch and supervise several flows; the inbox (section 10)
  record.py        the audit record: step records, journal, decision records (section 8)
  voice.py         interpret a free-text or dictated reply into answers and directions (section 6)
  driver.py        the state machine: one pure function (state, event) -> (state, action)
  state.py         flow directory, atomic state.json writes under a lock, events.jsonl
  runner.py        the only place that launches `claude`; parse, retry, budget, limits
  schemas.py       JSON schemas for every step's output
  prompts.py       loads prompt templates from prompts/flow3/ and fills them
  questions.py     the resolver (section 6)
  principles.py    read principles files, append decisions, propose principles
  heal.py          failure classification and bounded recovery (section 5)
  checks.py        run the project's check command, capture and trim output
  gitops.py        worktree, commit, diff, push, protected-path checks
  provider.py      `github` (gh) and `local` (bare remote, for tests) issue/PR/merge operations
  mergegate.py     section 7.8
  steps/           plan.py implement.py review.py fix.py ship.py
prompts/flow3/     one markdown template per call type (not commands; never argument-substituted)
commands/flow3.md  thin slash command: start the driver, show questions, pass answers back
tests/test_flow3_*.py
docs/v3/           this plan, the kickoff, progress, and the morning report
```

Python 3.8+, standard library plus PyYAML (already a dev dependency, used only to read
`reviewers/index.yaml`). Fully annotated; `scripts/flow3` is added to `mypy.ini`.

Reuse by import where it fits and is small: `scripts/workflow/checks.py` (check command
resolution), `deps.py` (frozen install), `worktrees.py` (layout), `fsutil.py` (atomic writes). If
reusing one drags in v1 concepts (receipts, flow ledger, merge queue), write the 50 lines fresh
instead.

State lives in `~/.claude/flow3/<repo>/<issue>-<slug>/`:

- `state.json`: current step, attempt counts, session ids, branch, PR, flags.
- `events.jsonl`: append-only; one line per call (model, cost, duration, turns, outcome), per check
  run, per heal, per question and answer, per stop.
- `questions.json` and `questions.md`: the open batch, when there is one (machine and readable forms).
- `stop.json`: when stopped. Class, reason, evidence path, resume command.
- Everything in section 8: calls, step records, the journal, human input, decisions.

## 5. Stops and healing

A flow stops for exactly three things: a question that needs the human, the merge gate, or a failure
that survived its heal budget. Finishing a step is never a reason to stop.

Every failure is classified by `heal.py` before anything else happens:

| Class | Examples | Automatic response | Then |
|---|---|---|---|
| `limit` | usage limit, rate limit, overloaded | wait with backoff (up to a configurable ceiling, default 6h) and retry the same call | stop `limit` |
| `transient` | call timed out, CLI crashed, empty or unparseable result | retry once in a fresh session | stop `runner` |
| `schema` | output did not match the schema | resume the same session once with the validation error | stop `runner` |
| `environment` | missing dependency, stale install, missing tool | frozen install from the lockfile once, re-run | stop `environment` |
| `base-stale` | branch behind base, conflicts | merge base into branch; on conflict one resolver call, then check | stop `conflict` |
| `check-flaky` | a test fails then passes on immediate re-run | re-run once (skipped when `check.confirm_red: false`); record the flaky test name | continue |
| `check-known` | the tree already has a result for this command, green or red | reuse it and journal the tree; no run | as the recorded result |
| `check-work` | lint, type, or test failure caused by the change | fixer call with the trimmed failure (cheap model), re-check; second round on the stronger model | stop `check` |
| `unit-shortfall` | an implementer did not finish its unit | retry once on the stronger model with the partial work kept | stop `implement` |
| `blocked` | a worker says it cannot do this from where it is, and names what it needs | never a stronger-model retry. A file outside the unit: hand on to the unit that owns it. An environment change: the install once. A shell command or a human decision: no heal | stop `environment`, `implement` or `human`, naming the need and quoting the worker |
| `denied` | a step needed a tool outside its allowlist | no retry | stop `permission`, naming the command |
| `cleanup` | worktree removal or local update fails after the provider confirms the merge | retry once | finish the flow as merged, with a warning |
| `protected` | a step changed spec-test files or files outside its unit | revert those paths, retry once with the rule restated | stop `protected` |

Each automatic response is bounded as written and recorded in `events.jsonl` with what it did. A stop
writes `stop.json` and prints one screen: what failed, the evidence file, what was already tried,
and the exact resume command. `flow3 status` shows the same. A stop with no recorded reason is a bug
and a test asserts it cannot happen (every `Stop` action requires a non-empty class and reason).

Section 12 maps last month's real hard stops onto this table.

## 6. Questions, principles, and learning

### The resolver

Steps do not ask the human. They return `questions[]` in their output, each with: `id`, `question`,
`context` (what the step was doing and why it matters), `options` (each with consequences),
`recommended`, `kind`, `reversible`.

`kind` is one of:

- `purpose`: what the thing is for, who it serves, what "done" means.
- `architecture`: boundaries, data shape, ownership, a new dependency, anything other code will lean on.
- `behaviour`: a user-visible choice the ticket does not settle.
- `risk`: weakening or deleting a test, a destructive or irreversible operation, security posture.
- `detail`: naming, placement, export style, error wording, internal structure.
- `fact`: something that can be checked by reading code, running a command, or reading docs.

The resolver handles each question in this order and records every outcome:

1. **Fact:** a scout call checks it and answers. A fact is never asked.
2. **Driver:** some questions are the driver's own bookkeeping (which unit owns a file, what runs first,
   a tool a worker lacks). The driver settles these by its rules and never shows them.
3. **Principle:** one call reads the principles files and the question. If a principle clearly
   decides it, the answer is that principle's, cited by id. The call must also be able to say
   "no principle decides this" and "a principle applies but this case looks like its `revisitIf`".
4. **Detail with no principle:** the step's recommended option is taken and recorded as `assumed`.
5. **Everything else (`purpose`, `architecture`, `behaviour`, `risk`, or a `revisitIf` hit):** goes
   to the human. `risk` questions lead with the risk.

A principle may answer an `architecture` or `behaviour` question only when it decides it directly.
A principle never answers a `risk` or `purpose` question. Nothing in the resolver hides a review
finding: principles shape answers to questions, not which findings are reported.

The step is resumed in its own session with all answers, however they were reached.

### What a question looks like

The human may come back to a question hours later with no memory of the ticket. Every question is a
card that stands on its own, and the driver refuses to show one with a missing part (the step is
resumed once to supply it):

- **Ticket and stage:** which ticket, one line on what it is for, and where the flow is.
- **The decision:** one sentence, in plain words.
- **Why it has come up:** what the step was doing when it hit this, with the file or behaviour concerned.
- **Impact:** what changes in the app or codebase under each option, what it costs, and whether it
  can be undone later.
- **What the experts think:** which expert favours which option and why, in a line each, with dissent
  kept visible rather than averaged away.
- **What we decided before:** principles that bear on it and earlier human answers on the same theme,
  each with its date, ticket, and the reason given then. Shown as context; never pre-selected.
- **Recommendation**, and what happens to the rest of the flow while this waits.

Questions are batched per stage: one batch after planning, one after review, and more only if fixing
raises something new.

### Answering in your own words

An answer is free text. The human can pick options, or talk: paste a dictated transcript that covers
several questions, argues with the framing, and adds instructions nobody asked about.

1. The reply is saved verbatim first (`human/<timestamp>.md`), before anything interprets it.
2. One call (`voice.py`) reads the reply against the open cards and returns, per question: the answer,
   the reason in the human's own words (quoted), and a confidence. It also returns **directions**:
   anything said that is not an answer to a card ("and stop adding wrappers around everything").
3. The interpretation is shown back in a few lines. Confident items proceed immediately. Only
   an item the call could not place, or a contradiction, is asked again.
4. Each answer becomes a decision record (section 8). Each direction is binding on every later step
   of that flow, is shown at the merge gate with how it was honoured, and is a candidate principle.

`flow3 say <flow> "<text>"` gives a direction at any time, not only when asked. It reaches the next
step that starts; a running step is not interrupted.

### Principles

Two files, both plain markdown, read whole by the models that need them:

- Project: `docs/principles.md` in the target repo (committed; path configurable), or, when the repo has
  none, `~/.claude/claude-helpers/<owner>.<name>.principles.md`.
- Personal: `~/.claude/principles.md`.

Project wins on conflict. Format:

```markdown
# Goal
One paragraph: what this project is working towards. Every plan is checked against it.

## P-strong-types: Prefer strong types
Statement: Make illegal states unrepresentable; no stringly-typed values across module boundaries.
Spirit: Catch mistakes at the boundary so later code can trust what it receives.
Revisit if: The type would cost more ceremony than the mistake it prevents.
```

Principles are a lens the work is done through, not law: every use is cited, listed at the merge
gate, and can be overruled there. `flow3 principles init` writes a starter file from a short
interview (goal first), seeded with the themes in section 12.

### Learning

Every resolved question is appended to `~/.claude/flow3/<repo>/decisions.jsonl`: the question, kind,
theme tags, options, the answer, who answered (`human`, `principle:<id>`, `assumed`, `fact`, `driver`), the
reason, and a pointer to the verbatim source. This log is what "what we decided before" on a question
card is drawn from, by theme match, and it is the answer to "what did I decide, and why".

`flow3 principles suggest` reads that log and proposes principles where the human answered the same
theme the same way at least three times, or overruled an `assumed` or principle answer at the merge
gate. Each proposal shows the answers it came from. The human accepts, edits, or drops each one.
Nothing becomes a principle without that. The driver mentions pending suggestions in the merge gate
summary (one line); it never interrupts a flow to propose one.

## 7. The steps

Model defaults are in one table in `scripts/flow3/config.py`, overridable per repo in
`.claude/flow3.json` and per run by flags.

| Role | Default model |
|---|---|
| plan experts, plan synthesis | sonnet (synthesis: opus) |
| spec-blind test author | sonnet |
| implementer, fixer | haiku, escalating to sonnet |
| review scouts | haiku |
| full reviewers, verifier | sonnet |
| triage | opus |
| resolver, fact scout | sonnet / haiku |
| PR text | haiku |

### 7.1 Worktree

The driver creates the ticket worktree and branch from the base, runs the frozen dependency install,
and runs the check once on the untouched base. A red base is a stop (`base-red`) before any model
is spent: the flow cannot tell its own failures from inherited ones otherwise.

### 7.2 Plan

1. The driver reads the issue, the principles, and a file map.
2. Three to five experts are chosen from reviewers whose `contexts.plan` is set in
   `reviewers/index.yaml`: North Star Nick always, the rest by one routing call. Each runs isolated,
   in parallel, read-only, and returns structured advice, risks, and questions.
3. Synthesis returns the plan: goal, non-goals, **units** (each with files it may touch, steps,
   acceptance criteria), the **test spec** (behaviour to prove, written against public interfaces),
   the public interface signatures, and merged questions.
4. Questions go through the resolver. Synthesis is resumed with the answers and returns the final plan.
5. **Verification.** One independent auditor call, which did not take part in planning, checks the
   final plan against the ticket, the answers, the directions, and the principles: does it do what
   was asked, does it honour every answer, is anything in it a decision nobody made. Gaps go back to
   synthesis once; a gap that is really an unasked question goes through the resolver.
6. The driver validates the plan mechanically: every unit names files, units' files do not overlap
   unless ordered, every acceptance criterion maps to a unit, the test spec is not empty unless the
   plan declares the change untestable and says why.

### 7.3 Spec-blind tests

Before any implementation exists, a test author gets the test spec and interface signatures only and
writes the tests. Blindness is structural: the code is not there to read. The driver commits the
tests and records their paths as protected.

### 7.4 Implement

Units run in order (file-disjoint units may run in parallel in a later version; not now). Each unit
is one implementer call on the cheap model with its unit, the interface, and the protected paths.
After each unit the driver inspects `git`: files outside the unit or inside protected paths are a
`protected` failure; otherwise the driver commits. Then the driver runs the check (section 5 handles
failure).

A spec test still red after the heal budget, or after a check fixer says it cannot fix the red from where it is (it may not edit a spec test), goes to a judge call: *code wrong*, *test wrong*, or
*spec wrong*. Code wrong: one more fixer round on the stronger model. Test wrong, and the edit
tightens or corrects without weakening: the judge's edit is applied and recorded. Test wrong and the
edit weakens or removes an assertion, or spec wrong: a `risk` question to the human, leading with
the risk.

### 7.5 Review

1. **Sweep.** Every reviewer with a `review` context (all of them, including the ones v1 only ran
   when named) is covered by about six scout calls, each holding four or five lenses and the same
   diff. A scout returns, per lens, candidate findings with file and line, or "nothing".
2. **Promote.** Lenses with candidates get a full blind reviewer call with the persona file and the
   diff. Security Sage and North Star Nick always get one. Reviewers do not see each other's output
   or the scouts' candidates from other lenses.
3. **Verify.** One or two skeptic calls check every finding against the code: confirmed, refuted, or
   needs measurement.
4. **Triage.** One call sorts confirmed findings into *doing it*, *needs you*, *needs measurement*,
   *deferred* (with a reason), plus the gut check across findings. *Needs you* items become
   questions and go through the resolver. *Needs measurement* items whose command the driver can run
   safely (read-only, inside the worktree) are run and resolved; the rest are listed at the merge gate.

### 7.6 Fix

*Doing it* findings are grouped into fix units and implemented like 7.4 (cheap model, protected
paths, driver-owned check). Then one verify call confirms each finding is addressed in the new diff.
Unaddressed findings get one more round, then are listed at the merge gate as open.

Then the review runs again on the fix diff only (sweep, promote, verify, triage), because fixes
introduce their own problems. New *doing it* findings get one more fix round. The loop is capped at
two review rounds after the first; anything still open is listed at the merge gate.

### 7.7 Ship

The driver commits, pushes, and opens the PR through the provider, following the repo's config. One cheap call writes the title
and body from the plan and the diff. If the branch is behind base, base is merged in and the check
re-run first.

### 7.8 Merge

The merge gate is one screen for the human: what changed, check result, findings fixed and open,
what was deferred and why, every decision that was `assumed` or answered by a principle (overrule
any of them here), cost and time. Options: merge, send back with notes (returns to fix), stop.

After approval, `mergegate.py`:

1. Takes a per-repo lock (`flock`). Merges happen one at a time. A second flow waits its turn.
2. Fetches. If base moved, merges base into the branch (conflicts: section 5, `base-stale`).
3. Runs the repo's **full** check on that exact tree, plus any `pre_merge` commands the repo declares.
   A tree that already passed the full check is not checked twice (keyed by the git tree id).
4. Pushes (a normal push; the branch only moved forward) and merges the way the repo declares: by
   default a squash-merge with the head commit pinned, so what merges is what was tested; or the
   repo's own merge command (section 9), run under the same lock. If base moved during the check, repeat from 2, at most
   three times, then stop `merge-contended`.
5. Confirms the provider reports the PR merged, removes the worktree, releases the lock.

Because base is merged in rather than rebased, v3 never rewrites pushed history and needs no
force-push. A stale lock (holder process dead) is detected by pid and taken over.

## 8. The record, cost, and time

Nothing a flow produces is deleted. The flow directory is the audit trail:

- `calls/<n>-<role>/`: for every model call, the exact prompt sent, the raw result, the structured
  output, and the session id (the full transcript stays recoverable through it).
- `steps/<nn>-<name>.md`: a readable record of each step, **rendered by the driver** from the
  structured outputs: what the step was given, what it produced, what it decided and why, what was
  healed. Because a model does not write it, there is nothing to verify it against.
- `journal.md`: the flow as a story, appended as it happens. One short entry per event that matters:
  questions and how each was resolved, heals, escalations, stops, the merge.
- `human/`: every reply and direction, verbatim, with its interpretation beside it.
- `decisions/`: one record per decision: the card as shown, the answer, who decided (the human, a
  principle by id, an assumption, a checked fact), the reason, and what it changed downstream.
- `checks/`: full output of every check run, with the tree id it ran against.

`flow3 why <flow> <decision or file path>` answers "why was this done" from that record: the decision,
its card, the verbatim reply, and the step that acted on it.

**Spend.** Every call reports `total_cost_usd`, `duration_ms`, and `num_turns`; the runner logs them
with step, role, and model. `flow3 status` and the inbox show cost and elapsed time per ticket, per
step, and a running total for the day. A repo sets a soft budget per ticket (default 15 USD): crossing
it adds a line to the inbox and does not stop the flow. A hard ceiling (default 40 USD) is a stop
(`budget`). On a subscription these are notional figures; they are still the honest measure of which
steps are expensive. Per-call limits (turns, wall clock) are in the config table.

**Speed.** Experts run in parallel, reviewers run in parallel, tickets run in parallel. Mid-flow
checks use the repo's fast check; the full check runs once, at merge. A tree that already passed is
never re-checked. No step waits on a human unless it is one of the three stop reasons.

## 9. Each repo declares its own process

Repos differ: how dependencies install, what the fast and full checks are, how a merge is done and
what must run before and after it. v3 reads one file per repo. It looks outside the repo first, at
`~/.claude/claude-helpers/<owner>.<name>.flow.yaml`, then at a committed `.claude/flow.yaml`; the outside
file wins whole, so a repo you do not own needs nothing checked in or ignored. `flow3 init` writes the
outside file unless `--in-repo` is passed:

```yaml
base: main
provider: github            # or: local
install: pnpm install --frozen-lockfile
check:
  fast: just check-minimum   # mid-flow
  full: just check           # at merge; the authority
  timeout_secs: 900
merge:
  method: squash             # squash | command
  command: just merge {pr}   # used when method is command; must leave the PR merged
  pre_merge: []              # extra commands on the tested tree, e.g. a schema preflight
  post_merge: []             # e.g. a deploy trigger; failure is a warning on a merged flow
protected_paths: []          # never edited without a risk question (migrations, lockfiles, ...)
principles: docs/principles.md
budget: {soft_usd: 15, hard_usd: 40}
models: {}                   # overrides for the table in section 7
```

- `flow3 init` writes this file for a repo by detecting what it can (justfile targets, package
  scripts, lockfile type, an existing `.claude/project.yaml` or `repo-cache.json`, an existing
  merge script) and asking about the rest once.
- `flow3 doctor` proves the file works before any ticket runs: installs, runs both checks on the
  untouched base, and dry-runs the merge procedure as far as it safely can. A repo that fails doctor
  cannot start a flow.
- When `merge.method` is `command`, the repo's own procedure runs inside v3's lock, after v3's
  full check on the exact tree. v3 then confirms with the provider that the PR is merged rather
  than trusting the command's exit code.
- Where a repo's old process cannot be expressed this way, change the repo, not v3.

## 10. Fleet and inbox

A flow is one process with its state on disk, so a fleet is thin:

- `flow3 fleet start <issue> <issue> ... [--max 4]` launches one driver process per ticket, each in
  its own worktree. `A>B` on the command line means B starts only after A has merged.
- All calls from all flows share one cap on concurrent model calls (file-based slots, default 6), so
  six tickets cannot stampede the usage limit. A `limit` failure in one flow makes every flow back off.
- A ticket that needs the human parks: it writes its batch and exits. The others keep going.
- `flow3 inbox` is the one place the human looks: every ticket's state, cost, and elapsed time, and
  everything waiting, grouped as questions, merge gates, and stops, oldest first. Answering resumes
  that ticket in the background.
- The slash command `/flow3` is the comfortable front end to the inbox: it shows the cards, takes a
  typed or pasted spoken reply for one ticket or several at once, runs the interpretation, and hands
  the answers to the driver. It contains no flow logic.
- Merges still happen one at a time under the per-repo lock, each tested on the tree that lands.

## 11. Build order

Each unit is written by a cheaper model, with tests, and accepted only after the orchestrator reads
the diff and the tests pass. Units 1 to 4 are the spine; the rest hang off it.

| # | Unit | Proves |
|---|---|---|
| 0 | Probes: parallel headless calls (4 at once), a forced `--max-turns` exhaustion, a schema mismatch, a call killed mid-run. Record the real result shapes in `docs/v3/probes.md`. | `heal.py` classifies from real shapes, not guesses |
| 1 | `runner.py` + fake `claude` binary for tests (`FLOW3_CLAUDE_BIN`) | launch, parse, timeout, retry, limits, resume |
| 2 | `state.py`, `driver.py` (pure transitions), `record.py`, `cli.py` skeleton | state machine tested without any model; the record is written from the first call |
| 3 | `repoconfig.py`, `checks.py`, `gitops.py`, `provider.py` (`local` first), `init`, `doctor` | driver-owned facts; per-repo process |
| 4 | `heal.py` | every row of the section 5 table, by test |
| 5 | `questions.py`, `voice.py`, `principles.py` | resolver order, complete cards, spoken replies, decision log, suggest |
| 6 | `steps/plan.py` + prompts | plan validation |
| 7 | spec tests + `steps/implement.py` + prompts | protected paths, escalation, judge |
| 8 | `steps/review.py` + prompts | sweep covers every review lens, promote, verify, triage |
| 9 | `steps/fix.py`, `steps/ship.py`, `mergegate.py`, `provider.py` (`github`) | lock, pinned merge, retry on moved base |
| 10 | `fleet.py`, inbox, `commands/flow3.md`, `flow3 why`, `flow3 principles init`, docs | fleet, the human surface |
| 11 | End-to-end on a sandbox repo with real models (`local` provider) | section 13 |

## 12. Last month's hard stops

Measured over one month of v1 flow records across all repos (2026-10-25 to 2026-11-25). Failure rows
are one distinct reason per flow and step, classified by rule; question splits are extrapolated from
92 hand-labelled items. Treat the numbers as plus or minus ten points.

### Failure stops (715 rows)

| Cause | Share | What it was | In v3 |
|---|---|---|---|
| Machinery | 28% (200) | Malformed receipt lines, fidelity audit and ledger errors, checkpoint handles, unit-record anchors | **Gone by construction.** There are no receipts, audits, or unit records to get wrong. A bad structured output is the `schema` row: one resume with the error. |
| Environment | 25% (178) | Branch behind base blocking the check, check timeout, skill or agent not registered, primary-checkout drift, cleanup failing after a merge | **Healed.** `base-stale` merges base in and re-checks. No skills or agent registry are involved in launching a step. Cleanup after a confirmed merge is retried and, if it still fails, reported as a warning on a finished flow, never as a failed one. |
| Work failed | 17% (120) | Fix loop exhausted, implementer delivered part of a unit, final check not run | **Mostly healed.** The human took over two thirds of the time and usually just finished the code or rebased and retried; retry worked about 70% of the time when chosen. v3 does both itself: keep partial work, escalate the model, two fixer rounds. What survives is a real `check` or `implement` stop. |
| Test-edit approval | 16% (113) | The plan required an existing test to change and the gate stopped to ask, or the anti-cheat gate refused the plan's own edit | **Mostly gone.** See the rule below. |
| No reason recorded | 12% (86) | A failure counter with nothing written down | **Impossible.** A stop without class and reason cannot be constructed. |
| Genuine | 3% (18) | Prerequisite ticket not merged, upstream release missing | Still a stop, with the reason. Dependencies named in the issue are checked in 7.1 before any model runs. |

**Existing-test edits.** The plan lists the existing tests it expects to change and why. An edit that
keeps or tightens what a test asserts (new cases, stricter expectations, renames, moved fixtures) is
applied without asking and listed at the merge gate. An edit that removes a test or an assertion, or
loosens an expectation, is a `risk` question that leads with what would no longer be proven. One
judge call classifies each edit from the diff; when unsure it must say "weakens".

### Questions (about 1,490 items in 766 batches; 69% came from planning)

| Kind | Share | In v3 |
|---|---|---|
| Needed the human (product behaviour, privacy and retention, security posture, scope reach, data architecture) | 36% | Asked, batched, with context. |
| A principle would have answered it | 53% | Resolver step 2, or step 3 when no principle exists yet. |
| A checkable fact or a rubber-stamp confirmation | 7% | Resolver step 1. Never asked. |
| Test-edit approvals and other | 4% | The rule above. |

The human picked the recommended or first-listed option 75 to 85% of the time and explicitly
overruled 8.5%. So taking the recommendation and reporting it, with overrule at the merge gate, would
have matched roughly four answers in five.

The principle themes that kept recurring, which seed `flow3 principles init` (the human edits or
drops each one; none is assumed):

1. **Docs follow code.** When code and an ADR or doc disagree, fix the doc in the same change unless
   that changes direction.
2. **Smallest change that meets the ticket.** Everything else becomes a filed follow-up, never
   silently included.
3. **Fail closed.** Model absent as absent, not as empty. Validate strictly at boundaries.
4. **Extend what exists.** Reuse the shared helper or module; enforce invariants at the lowest layer
   (the type or the datastore); one source for a constant.
5. **Tests prove behaviour.** Strengthened, never weakened; real evidence over throwaway proofs.
6. **Bounded budgets.** Explicit timeouts and limits; say what is not safe to cancel.

### Expected effect

Machinery, no-reason, most environment, and most test-edit stops go away: roughly 70% of failure
stops. Because a batch still stops if any one item needs the human, question batches fall by an
estimated 35 to 50%, not by the 60% of items that are answerable. Overall, stops should fall by
about half to two thirds. This is an estimate to test against real tickets, not a result.

## 13. Done means

- `just check` is green on the branch (lint, typecheck including `scripts/flow3`, full test suite).
- Tests cover: every transition in `driver.py`; every row of the section 5 table; the resolver order;
  no `Stop` without class and reason; protected-path enforcement; the merge lock and pinned merge
  against the `local` provider; the review sweep including every `review`-context reviewer in the index.
- One real end-to-end run on a sandbox repository (created under a temporary directory, `local`
  provider, real models): a small ticket goes from issue text to merged on the sandbox's base, with
  at least one question answered by a principle, one `assumed`, and one injected check failure healed.
  The run's `events.jsonl` and a short transcript are saved under `docs/v3/e2e/`.
- A fleet run on the sandbox: three tickets at once, one of them parked on a question while the others
  proceed, answered with a free-text reply covering two questions and one direction, all three merged
  one at a time.
- A sandbox repo using `merge.method: command` merges correctly, and `flow3 doctor` rejects a repo
  whose full check is red.
- For the end-to-end run, `flow3 why` explains one decision from each source (human, principle,
  assumed, fact).
- `docs/v3/REPORT.md` says plainly what works, what was not verified, what was cut, and the first
  things to try by hand.
- Nothing pushed, no PR opened, nothing outside the worktree changed except flow state under
  `~/.claude/flow3/` and temporary directories.
- v1 and v2 are **not deleted** overnight, though their code may be reused, moved, or changed. REPORT
  lists what v3 makes redundant, with line counts, so removal is one decision after the first real
  ticket merges through v3.

## 14. What the human should check in the morning

1. Read `docs/v3/REPORT.md`.
2. `just check`.
3. In one real repo: `flow3 init`, `flow3 doctor`, `flow3 principles init`, then one small real ticket
   with the `github` provider. Watch where it stops and whether the question cards are enough to
   answer cold.
4. Then a fleet of three small tickets.
5. Compare the stops against section 12.
