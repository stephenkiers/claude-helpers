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
`AskUserQuestion`. No `Edit`, no write-capable git, no `gh` mutation. Its `Write` grant is not
path-scoped; the intended scope is files under the flow directory and the one marker file (a
prompt-level rule, see Consequences). Step agents are `general-purpose` Task subagents and run with
full capability: they are not panel agents and carry none of the `tools:` restriction that
`CLAUDE.md` describes for them. ADR-0008 (machine-enforced guardrails) was considered for
expert-flow and deferred. A step that fails produces a
hard stop with three options — Retry (capped at two attempts per step), Take over, Abort — and
nothing else. `/expert-rebase` is suggested in the stop message when a step's receipt suggests it,
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
  parses (`FINAL_PLAN_PATH`, `REVIEW_DIR` + `CONFIRMED` counts, `PR_URL`, `MERGED`).

**Behaviour-neutral outside a flow** is the invariant: every block starts by checking `FLOW_DIR`,
and with it empty the commands are byte-for-byte the same behaviour they had before. The relay
never adds a question and never picks a default. It relays every question a command would ask
outside a flow, except the deliberately not relayed set below. `tests/test_flow_ask_sites.py`
enforces the shape: every line in a converted command that mentions `AskUserQuestion` must
reference the Ask block (or be an explicit negation such as "never relayed"), and every converted
command must reference `flow-reference.md`. The roster is the set of commands the flow can run.

Four kinds of decision point are deliberately **never relayed**: `/expert-review`'s needs-measurement
items (there is nothing to choose until a command has been run; they wait for `/verify-queue`),
`/cleanup`'s verify-queue `done|defer|ignore` batch (defaults to `defer`), `/implement-with-haiku`'s
`--pause` checkpoint (the flow never passes `--pause`), and decision points that became FAILED hard
stops in a flow. When a step fails, the flow does not turn the failed decision into a relayed
question; it stops under §1 with Retry / Take over / Abort.

### 2.1 Receipt and marker lifecycle

- Receipts are **attempt-scoped**: each carries `ATTEMPT: <k>`, and the orchestrator reads the receipt
  for the attempt it dispatched. A retry writes a new attempt's receipt; earlier receipts stay on disk.
- An `<!-- awaiting-answers -->` marker is valid only while `state.json` status is `running` or
  `awaiting`. In any other status the marker is stale and is ignored.
- If several awaiting markers are present, the last one whose answers file is absent is the open
  question. Earlier markers are answered or stale.
- `AWAITING` is not a receipt `Decision` value. A waiting step is identified by its marker, not its receipt.
- The merge gate is the orchestrator's own question and is modeled in state: `gate_passed` records the
  answer and `step: merge-gate` marks where the flow sits. **`--resume` always re-asks the merge gate:**
  `gate_passed` is reset to `false` on every resume, so a human reviewing a resumed flow can re-confirm
  the merge decision. `auto_merge` is never restored from state; `--auto-merge` must be passed on each run.

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
  control is the absence of `Edit` and write-capable git.
- Step agents run with full capability (§1). No machine-enforced guardrail applies to them; ADR-0008
  was considered and deferred for expert-flow.
- **Telemetry correlation (expected, to be confirmed in #252 dogfooding):** nested step-command
  telemetry lands under the long-lived `expert-flow` orchestrator record. ADR-0016 correlates
  telemetry only for sequential invocations, so this is a change to be checked, not assumed safe.
- **Open work touching the same orchestrator surfaces:** #230 (decision-relay completion check),
  #179 (receipt-path safety), and #217 (checkpoint hygiene). Sequencing and priority TBD.
- `/expert-implement-with-haiku-and-ship` is superseded; its CLAUDE.md entry is removed.
