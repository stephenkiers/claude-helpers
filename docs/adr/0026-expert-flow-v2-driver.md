# ADR-0026: /expert-flow v2 — a deterministic Python driver with a disk-only step boundary

**Status:** Proposed (2026-11-10). Lands with the docs-and-measurement Layer 0 under #477 (parent #456).
It is finalized to Accepted at Layer 5, after the D6 exit bar is met (see Decision 11).

## Context

`/expert-flow` (ADR-0023, v1: `commands/expert-flow.md`) runs the lifecycle as a model-driven
orchestrator. The orchestrator owns the step loop, `state.json`, receipt interpretation, resume rules,
retry and take-over decisions, and gates. Each of those is deterministic work done in prose. Two
consequences follow, and both show up in the dogfood record:

1. **Orchestrator-owned stops.** A flow stops, or a human has to take over, because the orchestrator
   misread state, skipped a rule, or resumed from the wrong place. These are not failures of the step
   work. They are failures of the coordinator.
2. **Token cost on every turn.** Interpreting the orchestrator's prose rules costs model tokens on each
   invocation, even for steps that are pure bookkeeping.

ADR-0013 already moved the deterministic parts of `/track-and-start`, `/shipit` and `/cleanup` into a
stdlib, fail-closed Python CLI in `scripts/workflow/`. The same pattern fits the flow loop. The
question this ADR settles is how the model and the deterministic loop should meet.

Three facts constrain the design, all recorded in ADR-0023 or measured since:

- **Subagents cannot ask.** Questions have to be relayed through files and a turn boundary (ADR-0023 §2).
- **A background child's completion notification re-invokes its parent.** A driver that polls a step
  races the step's own completion, and a step that is still running looks dead.
- **A model can drop or repeat a directive.** Whatever sits between the driver and the model must
  tolerate a missed or doubled call, and must not depend on the model to echo correctness tokens back.

The v1 behaviour has to be preserved, not re-invented. The inventory (`docs/v2-inventory/`, #477 Layer 0)
records every v1 rule the driver must reproduce, drop or change, keyed to ADR-0023 amendment dates. The
drift test (Layer 0) fails when a new ADR-0023 amendment is not reflected in that inventory. Under the
v1 freeze (Decision 8), that is how drift is caught during the build. The corrected baseline stop-population
excludes healthy `awaiting` flows; orchestrator-classified stops are 8 of 44 stops (18%), which is 8 of 212 flows in the window (4% of flows); the earlier figure was 8 of 66 (12%) over a population that wrongly included healthy awaiting flows (see `docs/metrics/expert-flow-v1-baseline.md`).

## Decision

### 1. Extends ADR-0013; supersedes ADR-0023 for the loop, the state write protocol and the resume cascade

v2 follows ADR-0013's pattern: stdlib only (plus the already-approved PyYAML exception of ADR-0013
Amendment 7 where a `project.yaml` read needs it), fail-closed results, and schema-validated
deterministic Python. It is a new consumer of `scripts/workflow/`, not a fork of it.

It **supersedes ADR-0023 for three things only**: the step loop, the state write protocol, and the
resume cascade. Those become driver code, and the prose rules in `commands/expert-flow.md` that describe
them are no longer the source of truth for a v2 flow.

It **keeps ADR-0023 byte-compatible** for everything else:

- the relay files (`questions/<STEP>-NN.json`, `answers/<STEP>-NN.json`) and `flow-render-questions.py`, reused with additive parameterization. Scripts `scripts/flow_log.py` (flow-log reads) and `scripts/flow-render-questions.py` (context rendering) are eligible for additive parameterization (a planned exception, per Decision 7 human ruling); the v1 scripts stay pinned to `~/.claude/flows/` and to the v1 relay format;
- the receipt trailer (`Decision: OK | FAILED`, `ATTEMPT: <k>`, and the `KEY: value` lines), verified
  against on-disk artifacts by `flow-verify-receipt.py`;
- the marker lifecycle (`<worktree>/.claude/flow-run`);
- the verifier trust boundary, including `flow-verify-receipt.py` and the fidelity audit;
- the binding human ledger (`human.jsonl`, written only by `scripts/flow-ledger.py`);
- the five bounded exceptions, with **no widening and no sixth**: (1) the automatic base-stale Rebase,
  (2) the manual "Retry with sonnet" for single-unit implement/fix shortfalls (automatic escalation is dormant and dropped), (3) the bounded self-heal of check failures and
  Fix now, (4) the fleet heal, and (5) the one-shot Unknown-skill auto-retry. The bounded environment
  chore (the dependency precondition, ADR-0023 2026-11-01 amendment) is also kept as it stands.

**Human ruling (seq 1):** additive parameterization of `flow_log.py` and `flow-render-questions.py` pinned to `~/.claude/flows/` is not work against v1 and does not break the ledger seq 1 freeze.

Where ADR-0023 and this ADR disagree about the loop, the state write protocol or the resume cascade, this
ADR wins for v2 flows. v1 is not changed by it (Decision 8).

### 2. Package location and reuse

The driver lives in `scripts/flow_driver/`. It **imports** from `scripts/workflow/` and never copies it
(the D5 condition). Shared helpers are added to the existing `scripts/workflow/fsutil.py` as additive
changes: an `fsync` option on `atomic_write_text` (default unchanged for existing callers), and a
non-blocking `flock` context manager. No new parallel atomic-write or lock module is created. A third
copy of the atomic-write pattern is the failure mode this decision prevents.

`scripts/fleet.py` is edited only in Layer 4, and only additively, behind `--driver v2` (Decision 7).

### 3. The disk-only boundary and the closed verb set

The driver is a pure transition function: `(disk state, "something happened") -> (new state, one
directive)`. The stub (`commands/expert-flow-v2.md`, a model) is a dumb executor. It calls
`driver next <flow>` after any event, reads the one-line `NEXT: <verb> <file>` the driver prints, and does
what it names.

The verb set is closed: **SPAWN**, **RESUME**, **WAIT**, **ASK**, **PARK**, **STOP**, **DONE**.

- `WAIT` is an interactive end-turn, or a fleet foreground wait chosen from `flags.fleet`. It is never
  chosen by the stub.
- An unknown verb is a **STOP**. The stub never guesses.

The driver writes each directive to a file before it prints the `NEXT:` line. That file is an audit log
only. The stub reads it and never echoes anything back. Directive files are keyed by `(step, attempt)` and
overwritten on each invocation; a log cap on retained directive history or replay limits is an open Layer 5 question.

**No model-copied tokens.** There is no ack, no generation counter, no stub-carried nonce, no directive
sequence the stub returns, and no correlation token in answer files that the stub supplies (Q4 A). The
boundary is disk-only, so a repeated `driver next` call is harmless.

### 4. Driver-side PR and merge-state checks (Q4, human direction seq 1)

Before the driver emits a ship or merge directive it checks PR and merge state on disk and through `gh`.

- **Ship.** Ship is skipped **only** when an existing PR's `headRefOid` equals the expected branch HEAD
  (the local branch HEAD, which must also equal the HEAD recorded by the latest implement, fix or verify
  receipt). With no PR, the driver emits SPAWN ship. When the PR head SHA differs from the expected HEAD,
  the driver emits STOP with both SHAs in the reason. It never skips in that case, and it never re-pushes
  automatically. A `gh` error is a STOP (fail closed), never a spawn.
- **Merge.** A merge directive is emitted only from the human merge gate (Layer 3, `gates.py`), and only
  behind the `--auto-merge` refusal and the healed-run forced ask. After the merge step's receipt lands,
  the driver re-reads state with `gh pr view --json state,mergedAt`, and only a MERGED PR advances the flow
  to done (this is the #476 mitigation). A receipt that reports success on an unmerged PR is a STOP naming
  the mismatch. A `gh` error is a STOP.
- Layer 2 never emits a merge directive. A PR that is already MERGED advances the flow to done. Every other
  case at the merge step is a STOP with the reason "merge gate not built (Layer 3)".

These checks run only in mutating commands (Decision 6).

### 5. Single writer: `driver_format: 2`

v2 state is written by one writer, the driver, under these rules:

- `driver_format: 2` is stamped at creation. Writes are refused on any state that lacks it, including v1
  state and skeleton state.
- A non-blocking `LOCK_NB` `flock` is taken on `FLOW_DIR/.driver.lock` for the whole mutating invocation.
  A second mutating invocation fails fast with a one-line reason. It never waits.
- Writes go to a temp file in the same directory, are fsynced, and are then moved into place with
  `os.replace`. A concurrent reader never sees a torn file.
- State is re-read after the lock is taken, before any decision is made on it.
- Schema is validated on every read that feeds a decision.

Attempt counts are kept two ways: a counter in state, and a count derived from archived receipts. A
reconcile runs under the lock and logs any mismatch (W3). Every transition is keyed by
`(run_id, step, attempt)` and is idempotent under that key.

> **2026-11-16 note (#485, review rulings).** Reconcile repairs a counter that is behind the archived receipts
> (logged as `attempt-counter-repaired`), and it defers the repair for a step whose spawn is still pending. A
> malformed receipt is re-run automatically by `resume`: the receipt is archived, the counter is repaired, and the
> command exits 0 with outcome `rerun`. This is a self-heal inside the existing archive-and-count rule, not a new
> remediation. Layer 2 (#486) owns two obligations: a cap on automatic re-runs (a step that keeps writing
> malformed receipts must stop, not loop), and consuming `rerun` through `record_spawn`, which is what opens the
> next attempt.

### 6. Read-only and mutating commands are split

Commands are either read-only or mutating, and the split is structural, not a convention:

- **Read-only** (`status --json`, `list`): take no lock, write nothing, spawn no writing subprocess, and make
  no network call. They never call `flow-verify-receipt.py`, which writes artifacts, and never run base-stale
  detection, which fetches.
- **Mutating** (`next`, `answer`, `clear-live`, `--abandon`, `--retro` and the rest): take the flow lock as
  in Decision 5. Only these may read receipts through the verifier, run the ship and merge checks of
  Decision 4, and run the North Star preflight.

A byte-identical test covers a read-only command: the flow dir and `.git/refs` are unchanged after it runs.

`fleet.py` reads v2 `state.json` directly (W2). It does not shell out to `driver status --json`. A
field-contract test pins the fields it reads, as catalogued in `docs/v2-inventory/rules.md` section 2 (the fleet state contract), so the meaning of each is preserved on v2 flows. The complete list includes `issue`, `updated`, `step`, `status`, `live`, `attempts.plan`, `stop_reason[step]`, `stop_source`, `failures`, `pr`, `worktree`, and `healed`.

### 7. Flows root; v1 is never adopted (Q3)

v2 flows live under `~/.claude/flows-v2/<repo>/`. v1 flows live under `~/.claude/flows/`, and v2 never
continues a v1-started flow. v2 may read v1 flows read-only (`--list`, `status`), labelled "v1, finish with
/expert-flow". v1 finishes its own flows, and v1 is removed once none remain in flight.

Separating the roots keeps v1's newest-`updated` scan and `fleet.py`'s glob blind to v2 directories. This
holds from the walking skeleton onward, so a skeleton flow can never be mistaken for a v1 flow.

Scripts pinned to `~/.claude/flows/` in Decision 1 (`scripts/flow_log.py`, `scripts/flow-render-questions.py`) are extended additively to accept a flows-root parameter or `--driver v2` mode, so they work unchanged for v1 flows and as specified for v2 flows (ADR-0018 write-prefix amendment for v2). This additive parameterization is not work against v1 and does not break the ledger seq 1 freeze (human ruling, seq 1).

Fleet mode (Layer 4) adds one additive switch to `scripts/fleet.py`, under `--driver v2` only. It changes the
prompt string (`/expert-flow-v2`), the flows root (`flows-v2`), and the fleet run identity passed to the
session. The v1 command line and environment are pinned by a test as unchanged.

### 8. Liveness: `pending_spawn`, the driver nonce, and clear-live (Q5, human direction seq 1)

Every SPAWN or RESUME is preceded by a `pending_spawn` record: step, attempt, a driver-written nonce, a state,
and in fleet mode the recording fleet run identity (`FLEET_RUN_ID`). Its states are:

- `spawned`: SPAWN emitted, no receipt and no question yet. Treated as **live**.
- `parked-awaiting-answer`: the step agent wrote a question file and the driver emitted ASK or PARK. The agent
  is waiting, not running. A new answer moves it on; nothing else does.
- `resumed`: the answer is on disk and the driver emitted RESUME (SendMessage). Treated as **live**.
- `receipt-landed`: a receipt whose `ATTEMPT` value matches has landed. `pending_spawn` is kept with state
  `receipt-landed` (not live), so the landing stays visible until the next `record_spawn` replaces it.

> **2026-11-16 note (#485, review rulings).** Text above originally said `receipt-landed` clears `pending_spawn`.
> Layer 1 keeps the record in state `receipt-landed`; it never counts as live. Layer 2 clears it by recording the
> next spawn. A stale receipt (its `ATTEMPT` does not match the pending attempt) is archived by `resume` and the
> command exits 0; it is not a refusal.
>
> The `resume` cadence assumption: receipt classification confirms a landed or malformed receipt only after two
> reads with an identical fingerprint. That stability is only meaningful when resumes are separated by real time or
> proven liveness. Layer 2 must add a quiet interval or a `subagent_gone` gate before it acts on `rerun`.

An outstanding `spawned` or `resumed` record with no receipt counts as live. Recovery then takes one of two
forms:

- **Interactive.** The human runs a logged `driver clear-live`, which records which nonce it cleared.
- **Fleet mode.** When a later invocation carries a `FLEET_RUN_ID` different from the recorded one, while the
  current run holds `locks/<issue>.lock` (so the recording run has released it), a `spawned` or `resumed`
  record gets a **logged automatic clear-live** with both run identities. This is what stops a stub crash
  from parking a fleet ticket. A `parked-awaiting-answer` record is never auto-cleared, because a normal unpark
  starts a new `fleet.py run` by design. The driver then emits RESUME to the waiting agent and moves the record
  to `resumed` under the new run identity. Layer 4 must show that a released lock proves the step agent is
  gone, including grandchild processes (`fleet.py` notes that a grandchild can outlive its parent). If it
  cannot, the automatic clear-live falls back to a park.

**Nonce.** The nonce never crosses the model boundary as a value to return. It names the driver-written prompt
file (`prompts/<step>-a<attempt>-<nonce>.md`), names the directive audit file, and appears in log entries and
in `clear-live` records. Receipt correlation uses the existing `ATTEMPT` trailer value matched against
`pending_spawn.attempt`. **The trailer is unchanged.** A prompt file whose nonce is not the current nonce is
stale, and any event attributed to it is logged and ignored.

**Re-spawn (W1).** The driver tries RESUME (SendMessage) first. A re-spawn with answers on disk is allowed only
for a step that a test has shown honours pre-written answers, and only once `pending_spawn` has been cleared.
A failed RESUME for a step not on that allowlist is a STOP. A re-spawn gets a new nonce.

### 9. Recovery, ledger, North Star, and the self-heal rule

**Recovery table.** `recovery.py` holds the recovery decisions as data, with counters in state that survive a
crash: Retry (max 2), take-over, keep-previous, redo (max 2), the one-shot Unknown-skill retry, the base-stale
auto-rebase, and the flow-healer heal (which records `healed: true` in state, for the merge gate). No new
remediation is added.

> **2026-11-16 note (#485, review rulings).** Two Layer 1 behaviours are bounded self-heals inside this table,
> not new remediations: the automatic re-run of a malformed receipt with the attempt-counter repair
> (Decision 5 note), and archiving a stale receipt with exit 0 (Decision 8 note). Layer 2 (#486) adds the re-run
> cap and the `record_spawn` consumption of `rerun`. `resume.py` implements the resume cascade as an ordered, named rule table. The rule that
fired is logged. Rule 1b's ordering stays before rules 4 and 5, as in v1. `pending_spawn` with no receipt is
recoverable, not a fresh start.

**Ledger.** The human ledger stays byte-compatible and is written only through the existing
`scripts/flow-ledger.py`, which the driver calls and does not copy. The driver re-renders the per-step ledger
snapshot from the run key when it is missing, instead of stopping with "ledger unavailable" (the
orchestrator-owned stop the evidence names). Fenced records are passed only to the workers the Ledger block
allows, never to blind Pass 1. On each receipt, the `Addressed:` carry-forward check runs: a direction the step
could act on and did not carry is a STOP. Ship runs an idempotent close-out of every still-pending direction.

**North Star degrade (Layer 2).** Before a step that seats North Star Nick is spawned, the driver runs the
existing preflight (`scripts/north-star-preflight.py`) in Python. Missing inputs become a visible warning in
the step directive and the receipt (the existing `NORTH STAR BLIND:` line). They do not become a take-over stop.
The warning is never hidden.

**Self-heal rule.** A receipt rebuilt by the driver gets a distinct name and a `rebuilt` mark. A rebuild never
turns a FAILED receipt into OK. Rebuilt receipts are counted separately in telemetry, so they cannot inflate the
v2 metric.

**Stub.** The stub grows to the single-ticket path up to and including ship. It stays under 150 lines. It never
writes `state.json`, `answers/` or `fleet-ask.json`. Only the driver writes those.

### 10. Lock order

Lock order is **fleet ticket lock, then flow lock**. `main.lock` is **never** taken while the flow lock is held. A test pins the order. Layer 4 adds a fleet-versus-human-resume test, since
that is where an inversion would appear.

### 11. The D6 exit criterion and the seq 1 v1 freeze

**Exit criterion (D6).** v2 completes N consecutive real tickets with **zero orchestrator-owned take-overs**. The
set must include at least one epic and one `--gates` run. N is set by the human after the Layer 0 baseline. A
test fails when ADR-0023 gains an amendment that the inventory does not cover, so the bar stays current.
Layer 0 attributes every recorded stop to an orchestrator, step agent or environment owner (D2), and v2 is judged
on orchestrator-owned stops plus first-turn token cost. Rebuilt receipts are excluded from the count.

**Freeze (seq 1).** During the v2 build, no work is done against v1. That includes bug-fix PRs. #476
is a v1 defect and stays open against v1 for the whole build. The human can override this. v2 avoids
#476 only from Layer 3 on, through post-merge verification. #446's hook half (the `flow-assessor` missing from `KNOWN_FLOW_AGENTS` defect) has been fixed independently. v2 avoids #446 by design, running Bash-needing assess checks in driver Python. The hook files, the agent definitions and the v1 orchestrator are not edited during the build.

### 12. Layer sequence

The build is layered, and each layer is its own sub-issue and PR under #456. The walking skeleton (Decision
13) runs before any schema exists. Layer 3 must pin the `--auto-merge` refusal and the healed-run forced ask
before any merge path exists, and Layer 2 ends in STOP before merge. The layer list and acceptance criteria are
in `docs/plans/expert-flow-v2-driver.md`.

**Driver scope for test-path stops (ADR-0019 2026-11-12 amendment, #468).** The Layer 3 driver must carry
both the plan-time check and the pre-Round-1 units check, and their named stop reasons: `plan-test-paths`,
`test-path-in-units`, `test-only-plan`, `existing-test-edit` and `planned-change-breaks-test`. Each is a
named stop. None is a retry, so the driver adds no recovery counter for them. Since 2026-11-17 (#497, ADR-0019 amendment), `existing-test-edit` in the `fix` step is a per-item human approve/decline fork rather than a stop; Layer 3 must carry it, together with its caveats (ADR-0023 2026-11-17 amendment): the approval is a per-item human answer pinned by a ledger record, the approval-commit scan is a step-level control the receipt verifier does not re-run, and patches persist under `FLOW_DIR/test-edits/` for Retry and resume.

### 13. The walking skeleton comes before the state schema (D4)

Before Layer 1's state schema is written, a minimal driver and stub are built and run. The skeleton uses a
temporary `pending_spawn` record and the closed verb set, runs one trivial step on an existing agent type, and
performs one park/answer cycle. It has no ship or merge path. It runs once interactively and once headless, and
it records: whether each directive runs exactly once, what a stub crash or compaction between `NEXT:` and the
receipt does, how often a double spawn actually occurs, and whether a headless session kills the background
agent unless WAIT is in fleet foreground mode. Its findings are cited by Layer 1's schema. Its state format is
throwaway, and it is stamped `driver_format: "skeleton"`, so Layer 1 treats it as foreign (read-only, never
written).

## Rejected alternatives

- **In-place extraction of the v1 orchestrator (Q1 B).** Changing `commands/expert-flow.md` in place would
  break the freeze and remove v1 as a working fallback until v2 is measured. Rejected: v2 is built beside v1 (Q1 A).
- **Hybrid (Q1 C).** Some loop logic in the driver and some in prose. Rejected: it keeps both sources of truth
  and the orchestrator-owned stop class intact.
- **Token handshake (Q4 B/C).** The stub returns correctness tokens to the driver: ack tokens, generation
  counters, echoed directive sequence numbers, stub-carried nonces, or correlation tokens in answer files. Rejected:
  the model is unreliable as a source of correctness tokens, and a repeat call would need its own defence. The
  disk-only boundary (Decision 3) replaces it.
- **Heartbeat liveness (Q5 A).** Each step writes a heartbeat the driver polls. Rejected: the stub is unreliable as
  a heartbeat source, and polling races a background child's completion notification (ADR-0023 §context).
  `pending_spawn` with logged clear-live instead (Decision 8).
- **Resident driver spawning steps with `claude -p` (Q5 C).** A long-running driver launches each step with
  `claude -p` and holds the per-flow lock for the whole step. Liveness becomes a real mutex. Rejected: it turns the
  driver from a short command the stub calls into a resident process, it changes the layered design, and steps
  launched that way bypass the agent hook allowlists that restrict what each step agent may do. Revisit only if
  Layer 4 shows that fleet lock release cannot prove liveness.

## Consequences

- **Good:** the loop, the state write protocol and the resume cascade become deterministic, tested Python. The
  orchestrator-owned stop class is what the D6 bar measures, and the driver removes the prose it lived in.
- **Good:** one writer, a non-blocking lock, atomic replace and a fail-fast refusal make concurrent invocations
  safe without waiting. A stale receipt cannot satisfy a newer attempt.
- **Good:** v2 never adopts v1 flows, and its root keeps v1 scans and `fleet.py` blind to skeleton and early
  layer directories.
- **Cost:** two orchestrators exist during the build. v1 keeps its known defects for that whole period (the
  accepted cost of the seq 1 freeze). The inventory and drift test keep v1 rules from drifting unrecorded.
- **Cost:** the driver checks PR and merge state with `gh`, which is a network call on mutating commands. A
  `gh` error stops the flow rather than spawning.
- **Residual:** a double spawn of a non-ship step before any receipt exists is detected only after the fact, by
  the `ATTEMPT` trailer (Q4 A, accepted). `pending_spawn` treats it as live and clear-live handles recovery. The
  residual cost is wasted work, not a duplicate push or merge.
- **Residual:** the fleet automatic clear-live depends on a released lock proving the step agent is gone. Layer 4
  verifies this. If it does not hold, the fallback is a park.
- **Residual:** the shared-guard defects (#406, #430) stay open and unaddressed by v2. v2 avoids them by design, not by fixing them. The `flow-assessor` hook defect (#446, hook half) has been fixed independently.
- **Residual:** the N in the D6 bar is a human decision, made after the Layer 0 baseline.

## Amendments

### 2026-11-17 — Layer 2 (#486): step loop, recovery, ledger, stub, list/abandon/retro

This amendment records what Layer 2 settles, and where the code does less than the plan said. The plan is
`docs/plans/expert-flow-v2-driver.md` (Sub-issue 2), and its Layer 2 decisions are the session rulings D1 to D8, W1,
and Q1 to Q5 in the Layer 2 plan. Those are recorded here by content, not by label. Anything marked **not built** is
still open and is not a Layer 2 deliverable.

**Counters and state (Q3 B).** Retry, redo and Unknown-skill counts derive from `attempt_log` outcomes (`retried`,
`redone`, `unknown-skill-retried`, `receipt-malformed`). Flow-level one-shots live in a `recovery` block
(`rebases`, `heals`, `retros`), written in the same atomic replace as the transition they gate. `healed` stays a
top-level key. A `heads` block holds the ship guard's expected HEADs. Both blocks load with zeroed defaults from
Layer 1 files. There is no `driver_format` bump. Older writers drop both blocks; Layer 1 has no real users, so this
is accepted.

**Re-spawn allowlist (D3).** `W1_ALLOWLIST` is an empty frozenset. Every failed RESUME is STOP `resume-failed`. A step
joins the allowlist only with a test that shows it honours pre-written answers, recorded here in a new amendment.

**Rerun gate and malformed cap (D4, D5).** A malformed receipt is re-run only after `subagent_gone` (a `notification`
with no landed receipt) or after a quiet interval of 60 seconds (`RERUN_QUIET_SECONDS`), measured across invocations
from the first-seen timestamp stored in state. The driver never sleeps. A malformed receipt is re-run once per attempt.
A second malformed receipt in a row is STOP `malformed-cap`. Layer 2 consumes `rerun` through `record_spawn`, so the
next attempt opens in the same replace.

**North Star degrade (D1).** Before every `plan` and `review` spawn, the driver runs only the `files` phase of
`scripts/north-star-preflight.py`, without `--allow-degraded`. The `files` phase applies to review in every case, since
review picks its effort inside the step. On BLIND, the directive carries a `NORTH STAR BLIND:` line, records
`allow_degraded: true`, and the step command gets `--allow-degraded`. A residual `north-star-preflight:` failure is
STOP, never Retry.

**Closed PR (D2).** A CLOSED unmerged PR is STOP `ship-pr-closed: PR #<N> closed unmerged`, whatever its head. Ship is
never skipped and never re-run automatically.

**Ship guard expected HEAD.** The driver records `heads.verify` from `git rev-parse HEAD` in the worktree when it lands
an OK verify receipt. Before ship, a local HEAD that differs is STOP `expected-head-mismatch`, naming both SHAs. When
the ship receipt lands, the driver records `heads.ship`. Every later guard check compares against `heads.ship`. A PR
head that differs from the expected head is STOP `ship-head-differs`, with both full SHAs. `gh` errors are STOP
`gh-error`. The check and the resulting state commit happen in one lock hold.

**Hook preflight and #407 (D6).** One function derives the restricted flow agents from the installed agent definition
files and checks the guard hooks v1's preflight checks. A miss is STOP `hook-preflight`, naming the hook. It runs at
`start` and before any restricted spawn. #407 is "v2 half done, v1 still open". Nothing in v1 changed.

**Fleet identity (D7).** `pending_spawn.fleet` exists in the schema and loads from state. No Layer 2 code path sets it,
and nothing reads `FLEET_RUN_ID`. The automatic fleet clear-live is Layer 4 (#488).

**Stop reasons (W1 session).** `StopCode` is a closed Literal. `stop_reason(code, detail)` builds the `"<code>: <detail>"`
string, and `parse_stop_reason` parses it. The `state.json` field stays `str`. There is no schema change.

**Sequential `next` (Q1 A).** The stub runs one `driver next` at a time, and the stub rule says so. A `locked` refusal
exits 1 with a logged line, and there is no backoff. The stub does not retry a locked refusal.

**Wait and stale events (Q4 B).** A `wait-timeout` with a live record and no landed receipt is STOP `wait-timeout`,
naming `clear-live`. It is never another WAIT. An event attributed to a stale nonce is archived and logged, never
landed. The directive is WAIT while the record is live, and otherwise the current directive again.

**Retention (Q5 A, with the Carl change). Selective prune, not whole-directory removal.** The age is
`RETENTION_DAYS = 30`. The sweep runs only from `start`, only over the started flow's repo under `flows-v2`, and only
for flows with status `done` or `abandoned` and an `updated` stamp older than 30 days. The flow `start` was asked to
open is never swept, and live flows (running, failed, awaiting, initializing) are never swept. Each candidate is pruned
under its own lock, with its status and age re-checked inside the lock. Pruning never removes the flow directory itself.

What the sweep removes (`scripts/flow_driver/retention.py`, `_prune`):

- Always: the `prompts/` and `directives/` directories, and the stale-receipt archives (`archive/*/a*.stale.*.md`).
- Only once `retro.md` exists: the `questions/` and `answers/` directories and the driver log. Layer 2 has no retro
  (`retro --flow` exits 3), so in practice `retro.md` does not exist yet and these survive.

What survives every sweep: `state.json`, `retro.md`, `human.jsonl`, `steps/` and the step receipts. The plan's Step 9
intent to keep the driver log and `questions/` holds only while `retro.md` is absent; after a retro they are removed
by design. Live flows and v1 flows (under `~/.claude/flows/`, which the sweep never reads) are spared. A survival
test is planned as a separate round, not part of this amendment. #357 stays open.

**Recovery commands.** `recover --choice retry` (after a FAILED receipt, at most two per step, counted from `retried`)
and `recover --choice take-over` are built. `recover --choice keep-previous` is refused, because it needs a redo
lineage, which Layer 2 does not build. **Not built:** keep-previous as a driver-rebuilt receipt, the redo path, and the
`resume --redo` cap. `resume --redo` exits 3 (`rule-0-redo`, Layer 3).

**Abandon.** A `spawned` or `resumed` record is refused and names `clear-live`. A `parked-awaiting-answer` record is not
live under Decision 8, so it is not refused. Abandon sets `status: abandoned`, records `abandoned` for the open attempt,
and deletes nothing.

**Retro.** **Not built for v2.** `retro --flow` on a v2 flow exits 3 (`not-yet-built`), and on a v1 flow it is refused
with the v1 label. The `retro` SPAWN and the `retro.md` mark do not exist yet.

**Loop wiring. Not built.** The recovery table in `recovery.py` holds the Heal, AutoRebase, Redo, Unknown-skill retry and
keep-previous decisions. The transition in `steps.py` does not call them. A FAILED landing is STOP `step-failed`.
So a base-stale verify failure is not rebased automatically, a lint or test failure is not healed automatically, and
Unknown-skill failures are not retried automatically. The directive file does not yet list the recovery choices with
their exact commands, which the plan's Step 8 called for. These gaps are recorded here, and the stub documents them.

**Assess.** Skipped until Layer 3. Layer 2 goes from verify to the ship guard, with no assess stage.

**#439 and #327 (v1 Retry and take-over defects).** Layer 2 ports the v1 Retry and take-over semantics as they are.
Neither defect is fixed here, and no new remediation is added. Divergence status: ported, not fixed.

**Step 6 (flows-root compatibility).** The human ruled yes on the `prompts/flow-reference.md` question. That ruling is
the reason for the ADR-0018 reciprocal amendment. This amendment does not confirm that real step commands write receipts
into a v2 flow. Until a test shows that, the stub treats real-step completion as unproven, and its Ask-block note stays
in place. The in-flow `FLOWS_ROOT` edits in `commands/expert-plan.md` and `commands/expert-review.md` are kept. This is the
human ruling B on the 2026-11-17 expert-review of this PR (issue #486), which records the decision to keep those edits
and not pin them to the v1 root. The edits are part of this PR and are not a separate change.

**Layer 2 scope.** The stub runs worktree through ship and then stops at the merge-not-built STOP. It refuses
`--auto-merge`. No merge directive exists in Layer 2.

Layer 3 amends ADR-0025 so that its auto-merge rejection covers `commands/expert-flow-v2.md`. The pointer to this ADR's
Layer 5 supersession step will land at Layer 5 in ADR-0023. Layer 5 finalizes this ADR (Decision 11, D3), amends ADR-0008
and ADR-0025, and updates `CLAUDE.md` when the D6 bar is met.

### 2026-11-18 — Layer 3 (#487): relay, step gates, merge gate

- **Decision 3 amended: eight verbs.** `MERGE` joins SPAWN, RESUME, WAIT, ASK, PARK, STOP and DONE. Only `gates.py` constructs
  a `MergeDirective`; an AST test pins that. An unknown verb is still STOP, so a Layer 4 fleet reader that has not learned
  MERGE fails closed. Layer 4 must learn it.
- **Decision 4 superseded where it said Layer 2 never emits a merge directive.** Layer 3 adds the merge gate: a head-SHA
  bound ask, a by-number post-merge check that the PR is MERGED, PR-state-first re-entry after a crash, and a re-verify path
  when the head moved.
- **`--auto-merge` stays refused (Q3 = A).** Every merge asks, healed or not. `driver start --auto-merge` prints
  `STOP auto-merge-refused`. Fleet auto-merge is not adopted; it needs its own ADR-0025 section 3 amendment.
- **The human merge gate is prompt-enforced (Q4 = A), like v1's AskUserQuestion.** The orchestrator model runs
  `driver answer` and can answer its own gate. That residual is not eliminated. Head-SHA binding limits a forged answer to
  the head shown.
- **Additive state fields, no `driver_format` bump (Q2 = A).** Recovery wiring is partial (Q5 = A): Retry, Redo, Accept and
  re-verify are the wired entries; the rest (auto-rebase, check heal, Unknown-skill retry, Retry with sonnet) are a follow-up.
- **Test-path stops.** The Decision 12 test-path codes (`plan-test-paths`, `test-path-in-units`, `test-only-plan`,
  `existing-test-edit`, `planned-change-breaks-test`) are carried as named stops from FAILED receipts and get no Retry.
- **Audit and assess are opt-in** on `driver start` for now, so the approved Layer 2 loop tests are unchanged by default.
  Audit rejections key on step and attempt; Accept is eligible only for allowlisted classes imported from `flow-accept.py`.
- Folds #304, #408 and #476 (Session D3).
