# ADR-0026: /expert-flow v2 — a deterministic Python driver with a disk-only step boundary

**Status:** Proposed (2026-11-10). Lands with the docs-and-measurement Layer 0 under #477 (parent #456).
It is finalized to Accepted at Layer 5, after the D6 exit bar is met (see Decision 9).

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
v1 freeze (Decision 8), that is how drift is caught during the build.

## Decision

### 1. Extends ADR-0013; supersedes ADR-0023 for the loop, the state write protocol and the resume cascade

v2 follows ADR-0013's pattern: stdlib only (plus the already-approved PyYAML exception of ADR-0013
Amendment 7 where a `project.yaml` read needs it), fail-closed results, and schema-validated
deterministic Python. It is a new consumer of `scripts/workflow/`, not a fork of it.

It **supersedes ADR-0023 for three things only**: the step loop, the state write protocol, and the
resume cascade. Those become driver code, and the prose rules in `commands/expert-flow.md` that describe
them are no longer the source of truth for a v2 flow.

It **keeps ADR-0023 byte-compatible** for everything else:

- the relay files (`questions/<STEP>-NN.json`, `answers/<STEP>-NN.json`) and `flow-render-questions.py`;
- the receipt trailer (`Decision: OK | FAILED`, `ATTEMPT: <k>`, and the `KEY: value` lines), verified
  against on-disk artifacts by `flow-verify-receipt.py`;
- the marker lifecycle (`<worktree>/.claude/flow-run`);
- the verifier trust boundary, including `flow-verify-receipt.py` and the fidelity audit;
- the binding human ledger (`human.jsonl`, written only by `scripts/flow-ledger.py`);
- the five bounded exceptions, with **no widening and no sixth**: (1) the automatic base-stale Rebase,
  (2) the single-unit implement/fix Sonnet escalation, (3) the bounded self-heal of check failures and
  Fix now, (4) the fleet heal, and (5) the one-shot Unknown-skill auto-retry. The bounded environment
  chore (the dependency precondition, ADR-0023 2026-11-01 amendment) is also kept as it stands.

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
only. The stub reads it and never echoes anything back.

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
field-contract test pins the fields it reads (`issue`, `updated`, `step`, `status`, `live`, `attempts.plan`,
`stop_reason[step]`, `pr`, `worktree`), so the meaning of each is preserved on v2 flows.

### 7. Flows root; v1 is never adopted (Q3)

v2 flows live under `~/.claude/flows-v2/<repo>/`. v1 flows live under `~/.claude/flows/`, and v2 never
continues a v1-started flow. v2 may read v1 flows read-only (`--list`, `status`), labelled "v1, finish with
/expert-flow". v1 finishes its own flows, and v1 is removed once none remain in flight.

Separating the roots keeps v1's newest-`updated` scan and `fleet.py`'s glob blind to v2 directories. This
holds from the walking skeleton onward, so a skeleton flow can never be mistaken for a v1 flow.

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
- `receipt-landed`: a receipt whose `ATTEMPT` value matches has landed. `pending_spawn` is cleared.

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
remediation is added. `resume.py` implements the resume cascade as an ordered, named rule table. The rule that
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

**Freeze (seq 1).** During the v2 build, no work is done against v1. That includes bug-fix PRs. #446 and #476
are v1 defects, and they stay open against v1 for the whole build. The human can override this. v2 avoids
#476 only from Layer 3 on, through post-merge verification, and avoids #446 by design (Bash-needing assess checks
run in driver Python). The hook defect itself, which is `flow-assessor` missing from `KNOWN_FLOW_AGENTS`, stays
unfixed under the freeze. The hook files, the agent definitions and the v1 orchestrator are not edited during
the build.

### 12. Layer sequence

The build is layered, and each layer is its own sub-issue and PR under #456. The walking skeleton (Decision
13) runs before any schema exists. Layer 3 must pin the `--auto-merge` refusal and the healed-run forced ask
before any merge path exists, and Layer 2 ends in STOP before merge. The layer list and acceptance criteria are
in `docs/plans/expert-flow-v2-driver.md`.

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
- **Residual:** the `flow-assessor` hook defect (#446, hook half) and the shared-guard defects (#406, #430) stay
  open and unaddressed by v2. v2 avoids them by design, not by fixing them.
- **Residual:** the N in the D6 bar is a human decision, made after the Layer 0 baseline.

## Amendments

None yet. Layer 3 amends ADR-0025 so that its auto-merge rejection covers `commands/expert-flow-v2.md`. Layer 5
finalizes this ADR (Decision 11, D3), amends ADR-0008 and ADR-0025, and updates `CLAUDE.md` when the D6 bar is met.
