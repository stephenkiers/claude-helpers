# ADR-0023: /expert-flow — Lifecycle Orchestrator with a Question Relay

**Status:** Accepted (2026-10-09). Numbered 0023 because 0022 was taken by `/expert-spike` while
the flow proposal (which referred to itself as ADR-0022) was being spiked.

## Context

The lifecycle commands in this repo — `/expert-plan`, `/track-and-start`, `/implement-with-haiku`,
`/expert-review`, `/shipit`, `/merge-and-cleanup` — are each good enough that a ticket's golden
path is mostly typing the next command and answering the questions it asks. The expensive part is
no longer the work; it is the human sitting between the commands as a relay. Epic #245 asked for
one command that runs the chain end to end and only surfaces the questions.

Two constraints shaped the design, both verified by the Phase 0 spike (#246, findings in #260):

- **Subagents cannot ask.** The harness strips `AskUserQuestion` from subagents. Any command that
  runs as a child of an orchestrator loses its ability to ask the human — and every command in the
  chain asks something (planning checkpoints, review rulings, the merge-queue setup, force-push
  confirmations).
- **The orchestrator must survive `/compact`.** A run spans hours. Anything the orchestrator needs
  to continue must be on disk, not in the conversation.

The spike also settled the engine: **subagent-per-step** (harness 2.1.296), not an in-process
`Skill` chain or an external process. Its constraints are recorded in the proposal
(`docs/plans/expert-flow-proposal.md` §2): the concurrency cap of 20 includes the step agent, so a
step's own panel may spawn at most 19; a background child's completion notification re-invokes the
step, so steps must not poll; a newly created command is `Skill`-invocable only after a turn
boundary; and a resume must verify the answers file exists on disk before `SendMessage`.

## Decision

### 1. One orchestrator, eight steps, the existing commands unchanged in meaning

`commands/expert-flow.md` runs `plan → track → implement → review → fix → verify → ship → merge` by
spawning one `general-purpose` subagent per step with a one-line prompt: run this command in this
directory, you are inside a flow, follow `prompts/flow-reference.md`. The orchestrator is pinned to
`model: sonnet` (ADR-0004: it is coordination, not judgment); each step's command keeps its own
frontmatter model.

The orchestrator does no engineering work. Its `allowed-tools` grant read-only `git`/`gh`, `Write`
(for files under the flow directory and one marker file), `Task`, `SendMessage`, and
`AskUserQuestion`. No `Edit`, no write-capable git (one human-gated exception, the Rebase option in
the gated Rebase amendment below), no `gh` mutation. Its `Write` grant is not
path-scoped; the intended scope is files under the flow directory and the one marker file (a
prompt-level rule, see Consequences). Step agents are `general-purpose` Task subagents and run with
full capability: they are not panel agents and carry none of the `tools:` restriction that
`CLAUDE.md` describes for them. ADR-0008 (machine-enforced guardrails) was considered for
expert-flow and deferred. A step that fails produces a
hard stop with three options — Retry (capped at two attempts per step), Take over, Abort — and
nothing else, except that base-stale verify failures also offer a fourth, human-gated option, Rebase
(see the gated Rebase amendment below). `/expert-rebase` is suggested in the stop message when a step's receipt suggests it,
never invoked. This is the same stance ADR-0021 took for the merge queue: no auto-remediation,
because remediation is where an orchestrator starts making engineering decisions nobody reviewed.

### 2. The question relay: a file, a turn boundary, and a resume

Every converted command applies three blocks defined once in `prompts/flow-reference.md`:

- **Resolve FLOW_DIR** — `--flow <dir>` flag (for `/expert-plan` and `/stack-sync`, which can run
  where no ticket worktree exists), else the one-line marker `<worktree>/.claude/flow-run` the
  orchestrator writes after the track step, else not in a flow. The marker is what lets nested
  `Skill` invocations (`/merge-and-cleanup` → `/cleanup`, `/shipit` → `/stack-sync`) inherit the
  flow without the caller forwarding anything.
- **Ask** — at every `AskUserQuestion` site: when `FLOW_DIR` is empty, call the tool as written.
  When set, write `questions/<step>-<SEQ>.json` (the tool's input, verbatim, plus `step`,
  `command`, `context_path`), append an `<!-- awaiting-answers -->` marker to the receipt, close
  telemetry as `interrupted`, print `AWAITING ANSWERS`, and **end the turn**. The orchestrator asks
  the human in its own conversation, writes `answers/<step>-<SEQ>.json`, verifies it exists, and
  resumes the step with `SendMessage`. The step continues exactly as if the tool had returned.
- **Receipt** — `steps/<NN>-<step>.md`: the command's existing final summary, unchanged, plus a
  `Decision: OK | FAILED` and `ATTEMPT: <k>` trailer and the one or two `KEY: value` lines the orchestrator
  parses (`FINAL_PLAN_PATH`, `REVIEW_DIR` + `CONFIRMED` counts, `PR_URL`, `MERGED`); verified against on-disk artifacts — see §2.2.

**Behaviour-neutral outside a flow** is the invariant: every block starts by checking `FLOW_DIR`,
and with it empty the commands are byte-for-byte the same behaviour they had before. The relay
never adds a question and never picks a default. **For the plan checkpoint in a flow** (amendment
2026-10-15), silence-acceptance of a shown D or W item is the human's acceptance, not a default the
relay picks; `accepted-on-continue` is the human's explicit word, and the relay still never picks a
default for a Q on its own. It relays every question a command would ask
outside a flow, except the deliberately not relayed set below. `tests/test_flow_ask_sites.py`
enforces the shape: every line in a converted command that mentions `AskUserQuestion` must
reference the Ask block (or be an explicit negation such as "never relayed"), and every converted
command must reference `flow-reference.md`. The roster is the set of commands the flow can run.

Four kinds of decision point are deliberately **never relayed**: `/expert-review`'s needs-measurement
items (there is nothing to choose until a command has been run; they wait for `/verify-queue`),
`/cleanup`'s verify-queue `done|defer|ignore` batch (defaults to `defer`), `/implement-with-haiku`'s
`--pause` checkpoint (the flow never passes `--pause`), and decision points that became FAILED hard
stops in a flow. When a step fails, the flow does not turn the failed decision into a relayed
question; it stops under §1 with Retry / Take over / Abort (plus Rebase on base-stale verify failures, see the
gated Rebase amendment).

### 2.1 Receipt and marker lifecycle

- Receipts are **attempt-scoped**: each carries `ATTEMPT: <k>`, and the orchestrator reads the receipt
  for the attempt it dispatched. A retry writes a new attempt's receipt; earlier receipts stay on disk.
- An `<!-- awaiting-answers -->` marker is valid only while `state.json` status is `running` or
  `awaiting`. In any other status the marker is stale and is ignored. This stale rule governs the
  orchestrator's pending check only; it does not apply to verifier counting on a `step-end` receipt, which counts
  the markers that receipt carries (see the 2026-10-14 receipt-markers amendment).
- If several awaiting markers are present, the last one whose answers file is absent is the open
  question. Earlier markers are answered or stale.
- `AWAITING` is not a receipt `Decision` value. A waiting step is identified by its marker, not its receipt.
- The merge gate is the orchestrator's own question and is modeled in state: `gate_passed` records the
  answer and `step: merge-gate` marks where the flow sits. **`--resume` always re-asks the merge gate:**
  `gate_passed` is reset to `false` on every resume, so a human reviewing a resumed flow can re-confirm
  the merge decision. `auto_merge` is never restored from state; `--auto-merge` must be passed on each run.

### 2.2 Receipts are verified against artifacts, not trusted (amendment 2026-10-14, #276)

A receipt is a claim; disk and git are evidence. On `Decision: OK`, the orchestrator verifies the
step's on-disk artifacts with `scripts/flow-verify-receipt.py` before the step advances. The verifier
runs after validation rules 1–7 and before `live` is cleared or `PLAN_PATH` / `REVIEW_DIR` recorded,
so a rejected step leaves no poisoned state for `--resume`.

**Decision — A. Keep strict, record it:** The verifier rejects a plan step when any roster row (except `auditor` and `synthesis`) has a stand-in contribution (`## Decision` followed by `FAILED`), even though `/expert-plan` degrades gracefully on any single expert's absence. The rationale: #276 is about not trusting unverified work, so this stands closed on such evidence. Recovery cost: Retry (full plan re-run), Take over, or Abort. A future ruling (see #252 dogfooding) may relax this to a coverage-gap note if the cost proves real in practice.

**Decision — A. Document all three in the ADR:** Three verifier details are now recorded here:

1. **Mtime freshness:** For plan and review steps, the verifier checks that `context.md` (plan) and `claude-action-plan.md` (review) mtime are >= `--since` minus 5 seconds, to verify artifacts were written during this flow step's window. Timing is necessary because the session dir or review dir could theoretically be reused from an older run.

2. **Roster exemptions:** The plan verifier skips `auditor` and `synthesis` roster rows when checking for contributions; only seated experts' contributions are verified.

3. **Contribution filename fallback:** The verifier first looks for a contribution file named `<file_stem>-contribution.md` (where `file_stem` comes from the reviewer index), then falls back to `<kebab-cased expert name>-contribution.md` as a second candidate.

The checks per step:
- **plan**: session dir named by `FINAL_PLAN_PATH`'s invocation id; every `selected-experts.md` roster row (except `auditor` and `synthesis`) must have a contribution file with both properties: its last non-empty line is `<!-- contribution-end -->`, and it is not a stand-in (§2 Receipt defines stand-in as `## Decision` followed by `FAILED`). Context mtime must be >= `--since` - 5 seconds. `FINAL_PLAN_PATH` must exist, and the session directory must contain `plan.md`. A marker-named question with no answers file, or an unreadable one, fails closed: in the report-checkpoint branch with `plan checkpoint incomplete (marker files without a readable answered report: <basenames>)`, and on the legacy path with the `unanswered:` / `unreadable:` suffixes. A checkpoint ledger carrying D/W/Q handles without a validated report fails closed with the `ledger has D/W/Q handles` reason. (Verifier widening: 2026-10-19 amendment.)
- **review**: `REVIEW_DIR` contains `claude-action-plan.md`, `final-report.md`, `findings.json`, and ≥2 reviewer checkpoints (`*-pass1.md` or `*-pod.md`). Action plan mtime must be >= `--since` - 5 seconds.
- **implement**: ≥1 new commit on the branch since the step began (base SHA recorded at spawn).
- **fix**: ≥1 new commit, with a carve-out: zero commits are accepted when `claude-action-plan.md` has no *Doing it* items. When a fix has nothing actionable, zero commits is correct.

A deviation also fails an OK receipt: `Deviation:` at line start, or `NOT dispatched` outside fenced code blocks, blockquotes and backtick spans. The reserved line `Deviation:` is never written by a step; a command that cannot run as documented writes `Decision: FAILED` and never substitutes its own procedure.

A missing artifact or deviation becomes a plain `FAILED` hard stop: `<step> artifacts missing (<what>)` or `receipt admits deviation`. The verifier lives in `scripts/flow-verify-receipt.py` (read-only, stdlib only, exit 0/1/2), and the orchestrator grants one `allowed-tools` entry to run it. No `PANEL:` line or `PLAN_SESSION_DIR:` key was added to the receipt grammar (Q3 in #276 proposal): the invocation id is derived from `FINAL_PLAN_PATH`'s basename, and the session dir is found by glob. No new required receipt keys.

`--review-effort 1` is rejected by `/expert-flow`: swarm effort writes no `findings.json`, which the review check (and `/expert-review`'s own receipt) requires.

**The strength stated plainly:** this catches honest drift and raises the cost of skipping. It does not prove a panel ran. Hand-written roster and contribution files with the sentinel would pass. It is not ADR-0008-style enforcement; a determined forger can work around it (the only defence for that is deployment controls outside this ADR). Stronger evidence (persona headers, a minimum size, `context.md` naming the issue URL) is deferred to future work.

Nested `Skill` and `Agent` spawning from step agents is available (spike #246 Q1) and the step prompt template now names the `Skill` tool, forbidding substitution of hand-written procedures.

### 3. State on disk, resumable, never auto-deleted

`~/.claude/flows/<repo-key>/<issue>-<slug>-<flow-id>/` holds `state.json` (issue, worktree, branch,
current step, status, attempts, plan path, review dir, PR, flags), `steps/`, `questions/`,
`answers/`, and a fixed-format `log.md`. `state.json` is the source of truth; the conversation is
a cache of it. `--resume` re-verifies from disk (worktree exists, last receipt) and **re-runs the
current step from its beginning** — a step subagent from an earlier session is gone, and an open
`awaiting-answers` marker is stale. Flow directories are never deleted by the command; `--list`
shows them.

### 4. The four epic-level decisions

Recorded here so the dogfooding phase (#252) argues with a decision, not a blank:

1. **Open-ended planning questions become free-text relays**, not a sidecar markdown exchange: one
   option labelled `Answer in your own words`, with the human's "Other" text passed through.
2. **The merge gate is human by default.** `--auto-merge` skips it per run and is never a stored
   default. The gate is the orchestrator's one own question; Hold leaves the flow paused at `merge`.
   `--auto-merge` trusts the plan and PR author; this ADR does not require enforcement of that trust.
3. **Epic v1 is sequential.** Ticket N+1 branches from main after N merges, because the flow already
   ends each ticket in `/merge-and-cleanup`; a stack buys nothing unless Hold is chosen. `--stack`
   is deferred until "Hold" is observed in practice (proposal §9 lists what it needs).
4. **Verify is the project's `commands.check` only.** Coverage, performance, and E2E remain
   `/expert-is-it-done`'s job after merge, where there is a merged diff to judge.

### 5. Epic mode piggybacks on `/expert-plan`

`--epic` runs the plan step with `--epic`, which makes `/expert-plan` mark `context.md` so
contributors (`prompts/plan-contribution-contract.md`) and the synthesizer
(`prompts/plan-synthesize-and-check.md`) emit a `## Sub-tickets` section — ordered, each with
title, 2–5 line scope, and `depends-on`. The human approves or edits it in one relayed question
(two edit rounds at most), sub-issues are created through `/track` with `Part of #<epic>` in the
body, and the single-ticket flow runs per sub-issue in a child flow directory. No new planning
machinery. The planning ADRs (0018, 0020) point here for `--flow` and `--epic` handling; the
flow-side behaviour is recorded in §2 and §5.

## Consequences

- **Epic mode is experimental** (shipped ahead of the #252 dogfooding gate). The proposal notes
  (#245 §9) that a sequential epic needs one complete relay cycle; if a ticket encounters >1 Take over,
  epic mode should defer until the relay's completeness is verified under #252 dogfooding.
- **Deterministic failures are still offered Retry.** The stop menu (§1) does not distinguish
  retryable from non-retryable failures, and it counts both toward the two-attempt cap. A
  step whose `Reason:` says it is deterministic and not retryable (the North Star preflight, #273:
  missing install or `gh` auth, empty snapshot) will fail identically on Retry; the reason text
  steers the human to Take over (fix the cause, then `--resume`) or `--allow-degraded`, which
  `/expert-flow` never forwards. Classifying failures as non-retryable in the menu itself is left
  open.
- One command, one conversation, and every human decision still made by the human. The cost of
  that is a strict discipline in the converted commands (end the turn, never guess), which the
  lint enforces structurally and the dogfooding (#252) must verify behaviourally.
- `tests/test_flow_ask_sites.py` makes adding a new `AskUserQuestion` to any rostered command a
  deliberate act: the line has to say how it relays. That is the point.
- `/track-and-start`'s existing-issue pivot lives in the command doc, not the `track plan` CLI
  (#262). The track step uses the doc path (`--issue N --plan-file P`); closing #262 would let the
  orchestrator call the CLI directly, and nothing in this ADR depends on which it is.
- The orchestrator's `Write` is not path-scoped (the same residual noted for panel agents in
  `CLAUDE.md`); "writes only under `FLOW_DIR` and the marker" is a prompt-level rule. The real
  control is the absence of `Edit` and write-capable git, bar the one human-gated Rebase in the gated
  Rebase amendment.
- Step agents run with full capability (§1). No machine-enforced guardrail applies to them; ADR-0008
  was considered and deferred for expert-flow.
- **Telemetry correlation (expected, to be confirmed in #252 dogfooding):** nested step-command
  telemetry lands under the long-lived `expert-flow` orchestrator record. ADR-0016 correlates
  telemetry only for sequential invocations, so this is a change to be checked, not assumed safe.
- **Open work touching the same orchestrator surfaces:** #179 (receipt-path safety) and #217 (checkpoint
  hygiene). The decision-relay completion check (decision points are asked and verified in
  `flow-verify-receipt.py --flow-dir`) shipped with the 2026-10-14 amendment.
- `/expert-implement-with-haiku-and-ship` is superseded; its CLAUDE.md entry is removed.

## Amendment: worktree-first invocation

The first dogfood runs kept the orchestrator in the main worktree for the whole flow, so the user's
session, its cwd, PR detection and metadata lived in `main` while the work happened in the ticket
worktree. `/expert-flow` is now worktree-first:

- A new first step `worktree` (receipt `steps/00-worktree.md`) runs
  `/track-and-start --issue <N> --worktree-only` in the main worktree. The receipt is
  orchestrator-written and verified against `git worktree list --porcelain` **before** anything is
  created at the reported path; only then are the flow marker and local-exclude line written (they
  moved here from the track step, which now only re-asserts the marker).
- A fresh single-ticket run started from main hands off after that step: state is
  `step: plan, status: paused`, event `handoff`, telemetry closed `interrupted`, and the orchestrator
  prints `cd <WORKTREE> && claude` then `/expert-flow <N>`. Run inside the worktree, a plain
  `/expert-flow <N>` with an existing flow behaves as `--resume <N>`, so no duplicate flow directory
  is created. (This behaviour is superseded in part by the 2026-10-15 amendment: a plain `/expert-flow <N>` asks the Existing-flow question; only the bare form in the hand-off state resumes silently.)
- Any invocation from main for a flow whose worktree exists prints the same hand-off and stops
  (`guard_block`); running from some other worktree is an error. Epic mode keeps running child flows
  from main with no hand-off.
- `plan` now runs in the ticket worktree; `track` (plan attach, reusing the worktree) and `merge`
  still run in the main worktree. `/cleanup` removes the worktree the orchestrator sits in, so after
  `merge` it runs no cwd-relative command and the done line adds `next: cd <MAIN_WORKTREE>`.

## Amendment (2026-10-13): gated Rebase on base-stale failures

The original §1 hard stop offered Retry / Take over / Abort and nothing else, and the orchestrator held
no write-capable git. In dogfooding, a `verify` failure caused only by a stale base (the receipt carries
`CHECK_CLASS: base-stale`) had no recovery path short of a hand-run `/expert-rebase`. This amendment
sanctions one narrow, human-gated git operation to close that gap.

- **Fourth option.** The hard stop menu gains **Rebase**, offered only when the failed step is `verify`
  and its receipt carries `CHECK_CLASS: base-stale`. It is not offered on stacked branches (the
  "Is-stacked" check in `prompts/worktree-reference.md`); there the stop prints the `/expert-rebase`
  suggestion instead.
- **Runs only after explicit human confirmation.** On confirmation, in the ticket worktree, the
  orchestrator runs `git fetch origin` and then `git rebase origin/<default branch>`. It does not push;
  the ship step pushes. A clean rebase is logged as `rebased` and verify re-runs through the normal loop.
  On conflicts the orchestrator runs `git rebase --abort`, prints the `/expert-rebase` hand-off line,
  and takes the Take over outcome. It never resolves conflicts.
- **Narrowed grants.** The git grants for this purpose are the exact forms `Bash(git fetch origin)`,
  `Bash(git rebase origin/main)`, `Bash(git rebase origin/master)`, and `Bash(git rebase --abort)` (the
  last used only to back out a conflicted rebase). The command's `allowed-tools` carries only these
  forms in place of the broader `Bash(git fetch:*)` and `Bash(git rebase:*)`. A repo whose default branch
  is neither `main` nor `master` gets a permission prompt for the rebase. Any remaining glob grant (such
  as `Bash(grep:*)` or `Bash(jq:*)`) is prompt-scoped beyond its literal prefix: the grant does not
  restrict the arguments a command is given.
- **Rationale.** The orchestrator already holds unscoped `Write` and `Task`, so the gap was never a
  capability the flow lacked; it was the missing record of what the orchestrator is allowed to do with
  git. The "orchestrator does not mutate git" stance in §1 and the Consequences now has one explicit
  exception: this human-gated, mechanical rebase. Conflict resolution, automatic rebase retries, and any
  push remain out of scope and need a human decision.

## Amendment (2026-10-14): base-stale Rebase is automatic, with an `/expert-rebase` worker fallback

The gated Rebase amendment above made the human click Rebase at every base-stale `verify` stop. In
dogfooding (#1777), main moved again before the human could act and verify stopped a second time; the
human was being used as a retry button for a mechanical operation. This amendment supersedes the
"explicit human confirmation" and "conflicts are never resolved" parts of that amendment.

- **Automatic, not offered.** On a `verify` failure with `CHECK_CLASS: base-stale` (not stacked,
  `failures[verify] < 2`), the orchestrator runs the same `git fetch origin` + `git rebase
  origin/<default branch>` without asking. Clean: log `rebased` and re-run verify.
- **Worker on conflict.** The orchestrator aborts the rebase and spawns one `general-purpose` Task
  subagent that follows `/expert-rebase` with overrides: no push, no questions, apply panel consensus,
  and abort with `NEEDS_HUMAN` on disagreement or intent-dependent conflicts. The orchestrator itself
  still never resolves a conflict and holds no new git grants; the worker is a Task subagent like
  every other step agent. `NEEDS_HUMAN`, a failed post-check, or an unreachable worker falls through to
  the ordinary Retry / Take over / Abort menu.
- **Still out of scope.** Pushing (ship pushes; `/shipit` owns the confirmed force-push after a
  stale-base rebase), stacked branches (stop prints the `/expert-rebase` suggestion), and any
  remediation of a non-base-stale failure.
- **Bounded.** The two-attempt cap per step is unchanged, so a base that keeps moving ends at a human
  stop rather than a loop.

## Amendment (2026-10-13): Start fresh deletes the old flow's worktree

Before this amendment, a plain `/expert-flow <N>` that found an existing flow offered Resume, Start fresh,
and Abort, and "Start fresh" only created a new flow directory and marked the old one abandoned. Its
worktree was left in place, so the new run's `worktree` step found two worktrees matching `<N>-` and could
not proceed. That was a dogfooding defect, not a design choice.

- **Start fresh deletes the old worktree.** The Start fresh option now names the old worktree, and it is
  the orchestrator's one sanctioned destructive action. It runs only after the human chose Start fresh,
  in this order: check the old worktree for uncommitted changes or unpushed commits (any found are listed,
  and a single `Delete worktree` question must be answered `Delete anyway`); delete it through the
  `/cleanup` helper's `cleanup plan` and `cleanup apply` (not a raw `git worktree remove`); create the
  new flow; then mark the old flow `abandoned`. A failed deletion is a hard stop with the existing flow
  untouched.
- **The abandon write does not bump `updated`.** The old flow's `state.json` is marked `abandoned` after
  the fresh run's is written, and leaving `updated` alone keeps the fresh run the newest flow, so the
  shared newest-flow lookup and a bare `--resume <N>` pick it.
- **Newest-flow lookup.** Every newest-flow selection (`--list`, `--resume <N>`, `--abandon <N>`,
  `--retro <N>`, and the Existing-flow check) uses one shared lookup: a `state.json` whose `updated` is
  not ISO-8601 is skipped with a stderr note, and the selection is `sort_by([.updated, .dir]) | last`, so
  ties break by directory name.
- **Rationale.** The orchestrator's rule that it never mutates git or the filesystem was always stated as
  having exactly one exception, the human-gated rebase. Deleting a worktree the human has asked to replace
  is a second, equally human-gated exception, and it goes through the existing `/cleanup` helper so
  the deletion logic is not duplicated.

## Amendment (2026-10-14): more human in the loop — context-carrying relay, full checkpoint relay, step gates

**Problem.** In practice the flow asked far fewer questions than the same commands run by hand, and the
ones it did ask arrived as a bare option list with a `skim:` pointer. Three causes, all observed in
`~/.claude/flows/`: plan steps dropped checkpoint batches after the first or adopted the recommended
defaults ("Q5 to Q7 were not asked"); open-ended planning themes never became free-text asks; and the
orchestrator relayed the stripped `questions[]` payload only, so the human decided without the decision
brief, the round-up, or the action plan they would have read in chat. Nothing enforced any of it: the
verifier and the fidelity audit checked that artifacts existed, not that the human had been consulted.

**Decision.**

- **Question files carry their context.** The Ask block adds a top-level `summary` (markdown the
  human reads before the questions) and a per-question `brief`. The orchestrator prints `summary`
  verbatim, then each `brief` under its question id, *before* the mandatory `AskUserQuestion` call;
  a file with neither is printed as its whole `context_path` file. Both fields are required for
  `/expert-plan`'s checkpoint (ticket refresher, North Star line, expert round-up; each `#### Q[n]`
  brief verbatim) and `/expert-review`'s rulings (triage digest; each *Needs you* block verbatim).
  The pointer-not-content rule for `context_path` still holds; the content now has its own fields.
- **Every checkpoint question is asked.** In a flow, `/expert-plan` relays every `#### Q[n]` brief across
  successive question files, each open-ended brief as its own free-text Ask; `/expert-review`
  relays every *Needs you* item. Adopting a default, dropping a later batch, or skipping the checkpoint
  for a "reduced" run is `Decision: FAILED` (`reduced run: checkpoint questions dropped` /
  `rulings dropped`). `flow-verify-receipt.py` enforces it from disk (`--flow-dir`): the briefs in
  `decisions.md` against the questions in `questions/plan-*.json` (`plan checkpoint incomplete`), the
  *Needs you* items against `questions/review-*.json` (`review rulings incomplete`); `decisions.md` is
  now a required plan artifact. The verifier is the sole owner of these counts. The fidelity auditor judges evidence quality and receipt wording for reduced runs. **Amendment 2026-10-15:** for the plan step
  with a report-kind checkpoint, the completeness rule is refined — see the amendment below. This rule
  applies to `/expert-review` Needs-you relay without change.
  Standalone `/expert-plan` outside a flow still applies the rule above.
- **Step gates.** After `plan`, `implement`, `review`, `fix`, and `verify` close `OK`, the orchestrator
  prints what the step produced; when the relay already printed the step's `summary`, the gate prints
  only the delta (for `plan`, the checkpoint results and decision index plus the head of the plan; for
  `review`, the action-plan header, *Doing it*, and *Deferred*), and whatever it prints is also written
  to `${FLOW_DIR}/gates/<NN>-<STEP>.md`; for other steps, it prints the receipt and `git log` of new
  commits, or the check result. Then it asks **Continue / Pause / Abort** (header `Step gate`). Continue
  is recorded in `state.json` `gates[<step>]` and is never re-asked; Pause leaves the flow `paused` at
  the completed step and `--resume` re-asks the gate (resume rule 2); free text is a Pause whose note is
  stored as `gate_note` and printed back on resume. #277's prose-ingestion work will extend the same
  anchor rule to the gate: a reply that plainly names Continue or Abort maps to that option, anything
  else stays a Pause with a note. `--no-gates` skips them for one run and, like `--auto-merge`, is never
  restored on resume. The gates are the orchestrator's own decision points, like the merge gate, so §2's
  rule stands: the *relay* never adds a question; the orchestrator does, at fixed points, on its own
  authority. This amendment supersedes "the orchestrator's one own question" in §2.1 and §4 (the
  orchestrator now has six fixed decision points: five step gates + the merge gate) and the §3 rule that
  `--resume` always re-runs the current step (a flow paused at a gate re-asks the gate; resume rule 2).

**Consequences.** A flow now stops after every substantive step and at every decision the commands
raise, with the material to decide on in the conversation; a human who changes nothing still reads
everything. Runs take more wall-clock turns; `--no-gates` is the per-run opt-out, and relayed
questions can never be opted out of. A plan or review that reaches the human with fewer questions
than it raised is a failed step, not a quiet default.

## Amendment (2026-10-14): receipt markers survive finalization

**Problem.** The 2026-10-14 checkpoint-relay amendment's verifier counts marker-named question files to enforce that every checkpoint question was relayed (§2's "every checkpoint question is asked" — **note:** this rule is superseded for report-kind plan checkpoints by amendment 2026-10-15, which uses the new completeness rule). But seven prose sites in five command docs and flow-reference told step agents to drop the markers on finalization, and flow-reference listed "`Decision:` together with an awaiting marker" as a forbidden trailer shape. The result was a plan step failing after all questions were answered, with the verifier reporting `plan checkpoint incomplete (briefs: 10, asked: 0)`.

**Decision.**

1. `<!-- awaiting-answers: … -->` and `<!-- reused-answers: … -->` markers stay on `OK` and `FAILED` receipts. They are this attempt's relay record. The receipt is stubbed per attempt (Attempt scope in Lifecycle), so markers are attempt-scoped.
2. The "`Decision:` together with an awaiting marker" forbidden shape is deleted.
3. The verifier counts a marker only when `answers/<STEP>-<SEQ>.json` exists. On the legacy path, a marker without its answers file is a failed step with `unanswered: <basenames>`; in the report-checkpoint branch the same case fails with the marker-files reason (see the 2026-10-19 amendment).
4. Disk globbing is not a counting source. A receipt with zero markers fails closed when briefs or needs-you items exist, even when answered question files exist on disk. A zero-brief, zero-item checkpoint with zero markers passes.
5. Resume rule 5 re-asks only from receipts without `<!-- step-end -->`.

**Clarification.** This amendment clarifies §2.1 ("Earlier markers are answered or stale") and makes the 2026-10-14 amendment's "against the questions in `questions/plan-*.json`" precise: it means the questions in the files named by answered markers.

**Trust assumption.** The verifier trusts agent-authored receipt markers: it counts what the step agent wrote, and it cannot independently confirm that a marker was written. A dropped marker is therefore a known failure mode. If every question was answered but the agent drops the markers on finalization, the step hard-fails with `, receipt carries no plan-* markers` (or `, receipt carries no review-* markers` for a review step), which reads as a relay problem when the relay actually succeeded. An `answers/<STEP>-<SEQ>.json` fallback (orchestrator-owned evidence that a question was asked and answered, independent of the receipt) is the follow-up if recurrence is seen. That follow-up is tracked in #308.

**Substitution probe (2026-10-15).** A throwaway command doc `zz-probe-303.md` containing `$0`, `$1`, `$2`, `${0}`, `${1}`, `${2}`, `$(1)`, `$ARGUMENTS[0]`, `$ARGUMENTS[1]` and `$ARGUMENTS`, each wrapped as `name=[token]`, was invoked via the Skill tool with `args: "alpha beta"`. The expanded body was:

```
bare0=[alpha]  bare1=[beta]  bare2=[$2]
braced0=[${0}]  braced1=[${1}]  braced2=[${2}]
cmdsub1=[$(1)]
argsidx0=[alpha]  argsidx1=[beta]  args=[alpha beta]
```

So: numbering is 0-based; a bare token with no matching argument stays literal; the braced `${N}` and `$(1)` forms survive unsubstituted; `$ARGUMENTS`, `$ARGUMENTS[N]` and bare `$N` are substituted. The probe file was deleted afterward. Note that `$ARGUMENTS[N]` is also substituted, so it is as unsafe as a bare token in places where the literal text is wanted. The probe ran through the Skill tool, not a user-typed slash command; the harness uses the same expansion for both, but that was not separately probed.

**Substitution re-probe (2026-10-17).** A follow-up probe via the Skill tool re-tested the awk field form with the command `commands/handoff-resume.md` (which contains `awk '{print $(2)}'`), invoked with args `"alpha beta gamma delta"` (4 arguments). The expanded awk field reference `$(2)` was substituted to `gamma` (the third argument, 0-based indexing), resulting in `{print gamma}`. With the single argument `"alpha"`, the out-of-range reference `$(2)` stayed literal. The form `$(3)` was not probed. This contradicts the 2026-10-15 claim that "`$(N)` follows the same rule" as the unsubstituted `$(1)`: the 2026-10-15 result (`$(1)` unsubstituted with two arguments) does not generalize. With four arguments, `$(2)` **is substituted**, so `$(N)` for N >= 2 is not a safe awk form and should be avoided. The earlier `$(1)` result remains unexplained and unreproduced with four arguments, suggesting the probe was incomplete. The five live awk sites that used `$(1)`/`$(2)` (in `commands/handoff-resume.md`, `commands/expert-rebase.md` and `commands/stack-sync.md`) were exposed to substitution when invoked with 3+ arguments; all were replaced in the same PR with `cut`, `grep`, `git diff` and `python3` forms, and `tests/test_command_doc_shell_conventions.py` now fails on any `$(N)` token in `commands/*.md`.

**Consequences.** An agent that drops markers still hard-fails, now with an error that says why (`, receipt carries no plan-* markers` or `, receipt carries no review-* markers`). If that keeps happening, a robustness upgrade (an `attempt` field plus disk-only counting scoped to the attempt) becomes the fix.

## Amendment (2026-10-19): fidelity audit owns judgment only, verifier owns counts (#318)

**Problem.** The fidelity auditor re-resolved receipt marker paths against the plan-session directory instead of `FLOW_DIR`, contradicting the verifier's count on #293 even though every question and answers file existed in the right place.

**Decision.**

- **Ownership.** Rule 8's verifier (`flow-verify-receipt.py --flow-dir`) is the sole owner of every mechanical checkpoint fact: question counts, answers-file existence, dispositions, excerpts, ledger and reaction completeness, contributions, and artifact existence. The auditor's `plan:` and `review:` checklists are judgment-and-wording only: does the evidence show no protest, are excerpts substantive, did anyone push back twice, and does wording admit a reduced run. Receipt marker paths are relative to `FLOW_DIR`; the auditor is handed absolute `<FLOW_DIR>/…` evidence paths.
- **Verifier widening.** The verifier now also checks artifact existence: `FINAL_PLAN_PATH` must exist, the plan session directory must contain `plan.md`, a marker-named question with no answers file or an unreadable one fails closed (`plan checkpoint incomplete (marker files without a readable answered report: …)`), a checkpoint ledger carrying D/W/Q handles without a validated report fails closed, and an unreadable marker fails closed with the marker-files reason in the report branch and with the legacy `unreadable: <basenames>` suffix otherwise (an unreadable answers file for a report question also fails closed with the marker-files reason). This widening is what makes the verifier the sole owner of artifact existence.
- **Rejected.** Ticket proposal 3 (contradiction → `unavailable` reason code) — moot under this decision; a `RELAY:` stdout line — avoids a new contract that #310 and #308 will rework (they will rework the marker logic); audit re-run budget — moot, because the re-run existed to let the auditor recover from a mechanical miscount, and counts are now the verifier's alone, so there is nothing for the auditor to re-run. Each stands.

**Consequences.** The auditor can never diagnose a mechanical shortfall — that job moved wholly to the verifier. The auditor can only read the checkpoint's evidence files from their absolute FLOW_DIR paths, judge the quality of the excerpts it finds, and flag suspect wording. Coverage is verifier-resolution tests (split-location fixtures: answers in FLOW_DIR, decoys in the session directory) plus prompt pins that hold the auditor's `plan:` and `review:` checklists to judgment and wording.

**Related.** #310 and #308 will rework the marker logic this amendment depends on; the marker-resolution and fail-closed rules above should be re-checked when those land.

## Amendment (2026-10-15): bare /expert-flow in the ticket worktree (#302)

**Problem.** A ticket worktree created by `/track-and-start --worktree-only` or a completed hand-off requires the user to re-type `/expert-flow <N>` or copy `--resume <flow-dir>`. A bare `cd <worktree> && claude "/expert-flow"` should "just work".

**Decision.**

- **The bare form** takes no issue number and no other flags (`--resume`, `--abandon`, `--plan-effort`, etc.) — it is only meaningful inside a ticket worktree. An invocation with flags but no issue rejects with `(a bare /expert-flow takes no flags)`.
- **Detection order** (inside the ticket worktree):
  1. If the worktree holds a valid marker (`<HERE>/.claude/flow-run`) pointing to a flow under `FLOWS_ROOT` with matching `state.issue == worktree basename's leading digits`, continue with that flow.
  2. Otherwise, if there is no marker but `<HERE>/.claude/github-cache.json` exists, require its `.branch` to be non-empty and equal to the current branch (a parallel restatement of the `trusted_github_cache` rule in the command doc, not a shared implementation), `.issue.number` all digits, `.issue.number == basename leading digits`, and `.issue.url == "https://github.com/<REPO>/issues/<N>"`. On all four matches, continue as plain `/expert-flow <N>` with a fresh run if no flow exists. The URL check prevents a local-plan ID from binding to a same-numbered GitHub issue.
  3. Otherwise, ask the free-text "Which issue" question (Q7 below). Invalid input or no answer → `guard_block`, no flow started.
- **Silent resume predicate** (Q1): A bare run skips the "Resume?" question only when `state.json` has `step == "plan"`, `status == "paused"`, `live == null`, and `(.attempts.plan // 0) == 0` — the exact state the Hand-off writes. Any other `paused` or `awaiting` flow gets the "Existing-flow" question (Q3), which keeps Abort available in the worktree; Start fresh must be run from the main worktree, because its guard refuses to delete the worktree the session is in. Every other state keeps the existing Existing-flow question. This silent resume is a deliberate, narrowly scoped exception to Epic #290 principle 2 ("never adopted silently"): only the exact hand-off state, reached by a bare run in its own ticket worktree, resumes without a question.
- **Hand-off template**: Both "go into the worktree" sites (Main guard_block at step 0, and the post-track Hand-off) print the same one-line, shell-quoted command:
  ```
  ▶️ expert-flow #<ISSUE> — worktree ready: <WORKTREE>
     next:  cd <QUOTED_WORKTREE> && claude "/expert-flow"
  ```
  where `QUOTED_WORKTREE=$(printf '%q' "$WORKTREE")`. A `--from` run from Main instead prints the explicit form with the start point and plan-file preserved.
- **Trail line on silent resume of the hand-off flow** (Q3): The trail line prints on silent resume of the hand-off flow, and only then; the Existing-flow route prints nothing. It reads:
  ```
  ▶️ expert-flow #<N> — resuming flow <basename of NOARG_FLOW> at plan (hand-off).  Not this one? /expert-flow --abandon <N>, or /expert-flow <N> from the main worktree to choose Start fresh
  ```
  The "from the main worktree" phrase is necessary (audit Finding 1) because Start fresh's guard refuses to delete the worktree the session is in.
- **`/track-and-start` alternative**: All four handoff blocks (Local Plan, Tracker Ticket, GitHub Pivot, GitHub Fresh) add a labelled alternative: `Or run the whole lifecycle with /expert-flow` (GitHub blocks specify "starts again from planning — it re-runs /expert-plan and track for this issue"). Local Plan and Tracker blocks add an honest label: "(/expert-flow runs only for a GitHub issue — in this worktree it will stop and point you to the main worktree; it also needs a GitHub remote)". The existing `/implement-with-haiku` line stays.
- **No-issue question** (Q7, new ask site): When detection finds no issue, `AskUserQuestion` header `Which issue`, free-text input, parsed exactly like `ISSUE_ARG` (the `case "$ISSUE_ARG"` block in "Fresh run: resolve the issue", then `gh issue view`). Context carries the detection failure reason (e.g. "no `.claude/github-cache.json` here", "cache branch ≠ current branch"). Invalid input, `Abort`, or no answer → `flow not started — run /expert-flow <issue>` (guard_block, no telemetry). A non-GitHub worktree that has a non-GitHub cache blocks any answer with `this worktree is not for a GitHub issue — run /expert-flow <issue> from the main worktree`.
- **Per-run flags** (`--no-gates`, `--auto-merge`) remain not carried across the hand-off. This is pre-existing (resume rule 9), not a regression. *(`--no-gates` is gone since the 2026-10-16 third amendment; its replacement `--gates` is stored and does carry.)*

**Consequences.** `/track-and-start --worktree-only` and a completed hand-off both lead to a copyable bare `claude "/expert-flow"` that resumes at `plan` without prompting. A fresh `/track-and-start` GitHub-issue worktree with no existing flow runs `/expert-plan` and `track` again (accepted, Q6). Local Plan and Tracker worktrees always end at a pointer to the main worktree. A no-remote project stops at Project detection, not the new question. Concurrent bare orchestrators on the same hand-off flow both see `live == null` before either spawns; the second to start loses (same as today's `--resume` rule 1, no new concurrency hazard). The bare form is keyed on the argument count being zero, not on the parsed issue: any argument at all, including an empty string or one that parses to nothing, `guard_block`s, so an argument lost to a parse failure (the #307 class) fails loudly instead of silently routing to the no-argument run.

## Amendment (2026-10-15): The plan checkpoint is a report and a conversation

**Problem.** The 2026-10-14 checkpoint-relay amendment turned the `/expert-plan` step into a series of AskUserQuestion forms, one per brief. But reading a plan brief in the conversation is a cost that grows with the question count, and the form-escape-hatch design assumes the human can skim the forms before picking answers. A report that presents the briefs once, in a stable order, and takes one free-text reply is cheaper and more adaptable to follow-up.

**Decision.**

The plan step checkpoint in a flow becomes one readable markdown report instead of per-brief forms. The report shows: what the ticket does (ticket refresher), what was decided (D items), what is worth a look (W items), and what questions need answering (Q items, each with recommendation, experts, why, and business case). Every Q, every item an expert protested, and every item that changed since the human last saw it is a must-answer item. The human replies in one free text, and a shared prose interpreter maps the reply to per-item dispositions. Unresolved must-answer items are re-asked once in prose, then through a form escape hatch. The plan step runs a reaction pass after each human reply, and any first-time expert pushback opens a conversation round. The loop ends when every Q is answered or the human says "continue". Silence accepts D and W items only.

This amendment supersedes the 2026-10-14 rule "Every checkpoint question is asked" for the plan step in a flow only. Standalone `/expert-plan` outside a flow keeps today's per-question format, and `/expert-review`'s Needs-you relay keeps today's rule.

The new invariant for the plan checkpoint in a flow: **every brief is shown with its rationale; every Q, protested item, and changed item is answered in the human's own words, or explicitly accepted by the human's "continue" (recorded as `accepted-on-continue` with the quoted "continue"); silence accepts only D and W items the human was shown; nothing is ever guessed.**

**Implementation.** Tiers and conversation loop are spelled out in Step 5 of `commands/expert-plan.md` (flow branch). Briefly:

1. **Report file per round:** `kind: report` question file (not a per-brief Ask) carries the ticket refresher, D/W/Q handles, each item's recommendation, experts, why, business case, and must-answer reason. Tier comes from recorded reaction evidence: a `disagreement` brief is always a Q, any protest always makes an item a Q, and everything else is D or W.

2. **Prose reply:** the human replies to the report in one free-text message. A shared prose interpreter maps that reply to per-item dispositions with quoted excerpts. The raw reply is saved verbatim.

3. **Re-ask and form:** unresolved must-answer items are re-asked once in prose, then through a form escape hatch (D5). A form answer carries `source: "form"` and the selected label or Other text as the excerpt.

4. **Read-only echo:** if any reading is ambiguous (no excerpt names the item or option, or the excerpt maps to more than one; content paraphrase and positional references count as naming), print "Here's how I read it" and wait. Otherwise proceed with no extra stop.

5. **Reaction passes:** one pre-show pass per seated expert (contributors plus Contrarian Carl) before the report is shown; one reply-mode pass per round on any ruling. Each expert either resolves, agrees, flags as could-go-other-way, or protests (outcome: Q, D, or W with evidence). Only first-time pushbacks open a round; one pushback per expert per ruling.

6. **"Continue" end condition:** "continue" (or equivalent instruction to move on) is an end condition, not an ambiguity. Record any explicit answers in the same reply. Record each must-answer item still open as `accepted-on-continue`, with the quoted "continue" and value = the human's latest ruling on that handle in this attempt if one exists, otherwise the panel recommendation. No confirm stop.

7. **Verifier rule:** a `kind: report` checkpoint fails closed on: missing or unsentinelled reaction files; unresolved must-answer items; items tiered D or W with recorded protests or disagreement; missed must-answer quotes; `accepted-on-continue` without `loop_end: "continue"`; repeated pushbacks on the same ruling by the same expert.

The reason strings in the verifier are, for example, `plan checkpoint incomplete (must-answer without quote: Q2)`, `plan checkpoint tier mismatch (W3 protested by tara-typesafe)`, `plan checkpoint tier mismatch (W1 is a disagreement)`, `plan checkpoint repeated pushback (contrarian-carl on Q2)`, `receipt carries no plan-* markers`.

**Scope:** flow plan step only (Steps 1–6 of the plan, plus the Terms and Approach sections). The ADR amendment, the report variant in `prompts/flow-reference.md`, the reaction contract in `prompts/plan-checkpoint-reaction.md`, and the verifier and audit updates apply to the flow plan step. Standalone `/expert-plan` and `/expert-review` rulings stay as they are. A backport is a separate follow-up issue opened only if this succeeds.

**Consequences.** The checkpoint becomes a conversation: the panel proposes, the human answers, and experts may push back on rulings once. The human's answer is the final word. No question is dropped, no default is adopted, and nothing is guessed. A retried attempt re-asks everything. The cost is two or more extra paid reaction dispatches per expert per checkpoint (the human accepted this explicitly). The report replaces the per-brief forms, so the number of turns taken per checkpoint is fewer than with the 2026-10-14 rule.

## Amendment (2026-10-16): a resumed `OK` the loop never validated is validated before its gate

On `--resume`, a step whose receipt reads `OK` normally goes straight to its step gate. But Take over lets a human finish a `failed` step by hand, and the receipt then reads `OK` even though the loop never checked it. In #311's flow, the review step went failed → Take over → resume → gate, and the Fidelity audit never ran. Now resume rule 2 first checks how the receipt's `OK` was reached. The detection rule below (the newest `<STEP> (ok|failed)` line) is superseded by the second amendment; the `ok` branch and the rule that a resumed receipt is validated before its gate still hold.

~~If it is `failed`, the flow runs step 5's receipt validation and, for `plan`/`implement`/`review`/`fix`, the Fidelity audit before it shows the gate. If both pass, it logs `ok` and shows the gate. If either fails, it records a failure and Hard stops.~~ *Superseded by the second amendment: a newest `failed` line no longer marks a receipt hand-finished, because every spawned attempt's failure writes one. Hand-finished is now keyed to the `taken-over` line that Take over writes.*

## Amendment (2026-10-16, second): a hand-finished `OK` is human-attested, not audited

This replaces the `failed`-branch rule of the previous 2026-10-16 amendment (see the strikethrough there) and its
detection rule. The `ok` branch and the no-outcome-line branch of that amendment carry over, with the wording below.
Hand-finished is now keyed to a `taken-over` event, not to a `failed` line. A `failed` line is written by every
spawned attempt's failure (receipt validation, reduced-run rejection), so it cannot tell a human finish from an
agent shortcut. Take over is the only action that can record a human's choice, so it writes the event.

On `--resume`, the newest of the step's lifecycle lines (`begin`, `retry`, `ok`, `failed`, `taken-over`) in `log.md`
decides how a receipt reading `OK` reaches its step gate:

- **`taken-over`** → **hand-finished**. A human chose Take over and finished the step by hand. Receipt validation
  (rules 1–8, including the `ATTEMPT:` check) and the Fidelity audit are skipped. The orchestrator logs a
  `hand-finished` line, and the gate digest is prefixed `hand-finished, not audited`. The audit exists to catch
  agent shortcuts (rounds not run, panels never dispatched, checkpoint defaults adopted), and a human finish has
  none of those to catch, so the Take over choice is the attestation. The `taken-over` line stays the newest
  lifecycle line until a later `begin`, `retry`, `ok`, or `failed` supersedes it, so the classification holds on
  every later resume without a new attempt.
- **`ok`** → the loop validated the receipt; the gate follows as before.
- **`failed`, `begin`, or `retry`, or no line** → not hand-finished. The receipt is spawned-agent output and is
  validated before its gate, the same as the loop would have done. An empty result is never passed through
  unchecked. `--since` is the newest `begin` or `retry` line's timestamp; if neither exists the validation fails.

Take over writes `taken-over` at the Hard stop (for the failed step) and at the merge gate (for `merge`). The merge
gate's Take over is audit only: its step has no step gate, so resume rule 2 never reads it.

A resume-time validation failure logs `failed` and goes to Hard stop **without incrementing
`failures[STEP]`**, because no attempt ran on resume. The Retry budget counts only spawned attempts.

Spawned-agent receipts are validated as before. Only the hand-finished case is exempt.

## Amendment (2026-10-16, third): step gates are opt-in

**Problem.** The 2026-10-14 "more human in the loop" amendment made the flow stop after `plan`, `implement`,
`review`, `fix`, and `verify` to ask Continue / Pause / Abort, with `--no-gates` as a per-run opt-out that
`--resume` never restored and epic child flows ignored. In practice that is a stop with nothing to decide:
the human's part of the flow is answering what a step asks, not confirming that it finished. The gates were
added alongside the relay-fidelity fixes, but what actually catches a step that skipped its questions is the
receipt verifier and the Fidelity audit, not a human clicking Continue. A flow that stops to be told to go on
is being babysat, and `--no-gates` could not be made to stick.

**Decision.** `--no-gates` is replaced by `--gates`, and the default flips:

- Step gates are **off by default**. Without `--gates` the flow stops only when a step relays a question,
  at the merge gate, and on a failure (Hard stop). The Step gate section, its digest files
  (`gates/<NN>-<STEP>.md`), Pause / Abort / free-text handling, and resume rule 2 are unchanged and run
  only when gates are on.
- `--gates` is persisted as `flags.step_gates` and **restored on `--resume`**. The "never a stored default"
  rule was written for flags that remove a safety stop (`--auto-merge`, which keeps that rule); a flag that
  adds stops is safe to remember. Epic child flows inherit the parent's `flags.step_gates`.
- The 2026-10-14 amendment's "a flow now stops after every substantive step" consequence no longer holds
  by default; "a plan or review that reaches the human with fewer questions than it raised is a failed
  step" still does, enforced by the verifier and the audit, not by the gate.

**Consequences.** A default run goes from issue to the merge gate with no stop that is not an answer or a
failure. Reviewing what a step produced before the next builds on it is a per-flow choice (`--gates`), and a
flow started with it keeps it across resumes and hand-offs. Existing `state.json` files carry `flags.no_gates`;
a resume reads a missing `flags.step_gates` as `false`.
