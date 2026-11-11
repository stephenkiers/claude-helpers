# /expert-flow v2: deterministic Python driver, built beside a frozen v1

**Ticket:** #477 (this PR, closes #477). **Parent:** #456 (stays open). **Decision record:** ADR-0026 (Proposed).

## Goal

Build a parallel `/expert-flow-v2` in which a Python driver owns the step loop, `state.json`, receipt handling,
resume rules and gates. The model does only step work and relays questions. v1 (`commands/expert-flow.md`) stays
frozen and working until v2 meets a measured exit bar.

This PR delivers the approved plan and a docs-and-measurement Layer 0. Layers 1 to 5, plus a walking skeleton, each
become their own sub-issue, flow and PR under #456. The bodies for those sub-issues are in the layer list below.

## Selected Scope

**In this PR (#477):**

- This plan, committed under `docs/plans/`, with the layer breakdown and each layer's acceptance criteria.
- Layer 0: the v1 behaviour inventory (`docs/v2-inventory/`), the stop-attribution baseline
  (`scripts/flow-stop-baseline.py`, `docs/metrics/expert-flow-v1-baseline.md`), and the inventory-versus-ADR-0023
  drift test.
- ADR-0026 in **Proposed** status, and its index row.
- No driver code, no stub command, no edits to v1 files.

**In later sub-issues,** each its own flow and PR, in this order: walking skeleton, Layer 1 (state, receipts,
resume), Layer 2 (step loop, recovery, ledger, stub, `--list/--abandon/--retro`; ends in STOP before merge), Layer 3
(relay, assess, gates, merge gate, merge directive), Layer 4 (epic and fleet), Layer 5 (dogfood, ADR-0026 accepted,
v1 deprecation).

**Systems involved:** `commands/expert-flow-v2.md` (new, from the skeleton on), `scripts/flow_driver/` (new),
`scripts/workflow/` (imported, not copied; `fsutil.py` extended additively in Layer 1), `scripts/fleet.py` (one
additive `--driver v2` switch, Layer 4 only), `docs/adr/`, `docs/v2-inventory/`, `docs/metrics/`, `tests/`.

**Not touched during the v2 build:** `commands/expert-flow.md` and every other v1 orchestrator file, the flow guard
hooks and agent definitions, and v1 bug fixes (see seq 1). The only edits to existing test files are additive roster
entries.

**Bookkeeping:** the #456 issue text names a worktree and branch that do not match the working branch for this ticket.
The working branch (`fix/477-...`) is authoritative. The PR closes #477 only and references #456, never `Closes #456`.

## Decisions

All rulings are the human's, made at the plan checkpoint (rounds 1 and 2). Each ruling is in brief here, with the full
reasoning in ADR-0026.

- **D1 (deliverable): B.** Plan plus docs-only Layer 0 (inventory, baseline, ADR-0026 proposal). Layers become
  sub-issues of #456.
- **D2 (success metric): A.** Layer 0 attributes every recorded stop to an orchestrator, step agent or environment.
  v2 is judged on orchestrator-owned stops plus first-turn token cost.
- **D3 (ADR timing): A.** Proposed ADR-0026 lands in this PR and is finalized at Layer 5.
- **D4 (walking skeleton): B.** A driver/stub skeleton is built and run before Layer 1's state schema.
- **D5 (package location): A.** `scripts/flow_driver/`, importing from `scripts/workflow/` and never copying it.
- **D6 (v1 exit criterion): A.** v2 completes N consecutive real tickets (at least one epic and one `--gates` run)
  with zero orchestrator-owned take-overs. A test fails when ADR-0023 gains an amendment the inventory does not cover.
  The human sets N after the Layer 0 baseline.
- **W1 (after park or compaction): A.** RESUME first, then re-spawn with answers on disk. Re-spawn is allowed per step
  only where a test shows that step honours pre-written answers, and only once `pending_spawn` is cleared.
- **W2 (fleet reads state): A.** `fleet.py` reads v2 `state.json` directly. A field-contract test pins the fields it
  reads. A second test asserts that read-only driver commands leave the flow dir and `.git/refs` byte-identical.
- **W3 (attempt counts): C.** A counter in state plus a count derived from archived receipts. A reconcile runs under
  the per-flow lock and logs any mismatch.
- **Q1 (shape): A.** Parallel v2 that replaces v1 after dogfood. In-place extraction and the hybrid were rejected.
- **Q2 (overlapping issues): B, plus direction seq 1.** Every overlapping issue folds into the matching v2 layer.
  v1 is left alone. No work against v1 happens during the v2 build, including bug-fix PRs for #446 and #476. The human
  can override later. The human declined to tag #446 and #476 stops out of the baseline.
- **Q3 (adoption and root): A.** v2 never continues a v1-started flow. v2 flows live under `~/.claude/flows-v2/<repo>/`.
  v2 reads v1 flows read-only. v1 finishes its existing flows and is removed once none remain.
- **Q4 (boundary): A, plus the driver-side check (seq 1).** The boundary is disk-only, with no model-copied tokens.
  Before emitting a ship or merge directive, the driver checks PR and merge state on disk and through `gh`.
- **Q5 (liveness): B, plus the fleet-mode check (seq 1).** `pending_spawn` plus a driver-written nonce. A spawn is live
  until a logged `driver clear-live`. In fleet mode, release of `fleet.py`'s per-ticket lock counts as a logged automatic
  clear-live for spawned or resumed records, so a stub crash does not park a ticket. A parked spawn is never auto-cleared.
- **Q5 option C (not chosen, recorded as rejected):** a resident driver that launches each step with `claude -p` and
  holds the lock for the whole step. See ADR-0026.

**Human ledger direction, seq 1 (binding):** no work against v1 during the v2 build, including bug-fix PRs for #446
and #476 (Q2); the driver-side PR/merge-state check before ship and merge (Q4); and the fleet-mode automatic clear-live
on per-ticket lock release for spawned or resumed records (Q5). Each applies to the layers below as written.

## Layer list

Steps 1 to 6 are this PR (#477). Layers 0 to 5 below are sub-issues of #456, each depending on the one before it. The
walking skeleton sits between Layer 0 and Layer 1.

| # | Sub-issue | Depends on | Lands |
|---|---|---|---|
| 0 | Docs and measurement (this PR, #477) | none | this PR |
| S | Walking skeleton of the driver/stub boundary | #477 merged, and the Layer 0 findings in this plan | its own PR |
| 1 | Layer 1: state, receipts, resume | S | its own PR |
| 2 | Layer 2: step loop, recovery, ledger, stub, `--list/--abandon/--retro` | 1 | its own PR |
| 3 | Layer 3: relay, assess, gates, merge gate | 2 | its own PR |
| 4 | Layer 4: epic and fleet | 3 | its own PR |
| 5 | Layer 5: dogfood, ADR-0026 accepted, v1 deprecation | 4, and the D6 bar | its own PR |

---

### Sub-issue S: walking skeleton of the driver/stub boundary (before Layer 1)

**Depends on:** #477 merged.

**Scope:**

- A minimal `scripts/flow_driver/` with a `next` command, a temporary `pending_spawn` record and the closed verb set
  (SPAWN, RESUME, WAIT, ASK, PARK, STOP, DONE; unknown verb is STOP).
- A minimal `commands/expert-flow-v2.md`, labelled experimental in its frontmatter `description` and in its first line
  ("experimental, not for real tickets"), with additive roster entries in `test_flow_command_contract.py`,
  `test_flow_ask_sites.py` and `test_flow_receipt_marker_contract.py`.
- It runs one trivial step on an existing agent type (no new agents, no hook changes) and one park/answer cycle. It has
  no ship or merge path.
- Skeleton flow dirs live under `~/.claude/flows-v2/<repo>/`, never under `~/.claude/flows/`. Their state is stamped
  `driver_format: "skeleton"`, so Layer 1 treats them as foreign (read-only, never written).
- Run once interactively and once headless (`claude -p`). Record the findings in `docs/v2-inventory/skeleton-findings.md`.
  Layer 1's schema cites that document. The skeleton's state format is throwaway.

**Acceptance criteria:**

- The stub executes each directive exactly once in the interactive run, or the number of repeats is recorded.
- A stub kill injected after `NEXT:` is run, and whether a re-run double-spawns is recorded.
- The double-spawn rate is recorded, not assumed to be zero.
- Whether a headless session kills the background agent unless WAIT is in fleet foreground mode is recorded, with agent
  survival time under headless idle with and without the foreground WAIT.
- A unit test asserts that skeleton flow dirs are created only under the `flows-v2` root, and that the stub frontmatter
  carries the experimental label.
- `just check` passes. The three roster tests change only by additive entries.

---

### Sub-issue 1: Layer 1, state, receipts, resume

**Depends on:** S (the skeleton findings must be cited).

**Scope:**

- `state.py`: `driver_format: 2` at creation; a non-blocking `LOCK_NB` `flock` on `FLOW_DIR/.driver.lock` for the whole
  mutating invocation, failing fast with a clear message; tmp in the same directory, fsync, then `os.replace`; a re-read
  after the lock is taken; schema validation. The root is `~/.claude/flows-v2/<repo>/`. Writes are refused on any state
  without `driver_format: 2`. v1 state is read-only, for `list` and `status`. The fleet field contract keeps its meaning.
- The lock and atomic-write helpers extend `scripts/workflow/fsutil.py`: an fsync option on `atomic_write_text` (default
  unchanged for existing callers) and a non-blocking flock context manager. No new helper module. `fleet.py` is not
  edited in this layer.
- Attempt accounting: the counter in state and the count derived from archived receipts; reconcile under the lock that
  logs mismatches; every transition keyed by `(run_id, step, attempt)` and idempotent under it.
- `receipts.py`: reads receipts and calls `flow-verify-receipt.py` only from mutating commands. A stale-attempt receipt
  never satisfies the current attempt: its `ATTEMPT` trailer value must equal `pending_spawn.attempt`. The trailer format
  is unchanged. Rebuilt receipts follow the ADR-0026 self-heal rule.
- `resume.py`: the cascade is an ordered, named rule table, and the rule that fired is logged. `pending_spawn` with no
  receipt is recoverable, not a fresh start.
- Command split: `status --json` and `list` are structurally read-only. Worktree paths resolve through the primary
  resolver.
- Every refusal (locked, v1-owned, skeleton-owned, schema-invalid, stale receipt) is a non-zero exit with a one-line
  reason, which the stub surfaces as STOP.

**Acceptance criteria:**

- Two concurrent mutating invocations: the second fails fast. A concurrent reader during a write never sees a torn file.
- Writes against v1 state or skeleton state are refused. v1 flows show in `list`, read-only.
- Fleet field contract: every field `fleet.py` and `heal.py` read keeps its meaning on a v2 flow (`issue`, `updated`,
  `step`, `status`, `live`, `attempts.plan`, `stop_reason[step]`, `pr`, `worktree`).
- Deleting an archive makes the attempt reconcile log a mismatch.
- A stale-ATTEMPT receipt is rejected.
- One test per named resume rule, plus the 1b-before-4/5 ordering.
- Read-only commands leave the flow dir and `.git/refs` byte-identical.
- `fsutil.py`: the new fsync option and flock context manager are tested, and existing `atomic_write_text` callers see
  unchanged behaviour.
- Replay fixtures carry human-assigned expected outcomes, and are synthetic or scrubbed, never raw.
- `just check` passes, and `mypy.ini` is extended additively.

---

### Sub-issue 2: Layer 2, step loop, recovery, ledger, stub, `--list/--abandon/--retro`

**Depends on:** 1.

**Scope:**

- **Transition function.** `steps.py` turns disk state plus an event into the next directive. The driver writes each
  directive to a file before printing `NEXT: <verb> <file>`. That file is an audit log only. The stub calls
  `driver next <flow>` after any event and never echoes anything back.
- **Spawn and liveness.** `pending_spawn` (step, attempt, nonce, state, and in fleet mode the fleet run identity) is
  written before every SPAWN or RESUME. States: `spawned` (live), `parked-awaiting-answer` (waiting, not running; a new
  answer moves it on), `resumed` (live), `receipt-landed` (cleared). An outstanding `spawned` or `resumed` record with no
  receipt is live. Interactive recovery is a logged `driver clear-live` by the human. A re-spawn requires a cleared
  `pending_spawn` and gets a new nonce. The per-step re-spawn allowlist (W1) is settled by tests here; until a step is
  proven, a failed RESUME for it is a STOP.
- **Nonce.** It is never given to the stub as a value to copy back. It names the prompt file (`prompts/<step>-a<attempt>-<nonce>.md`),
  the directive audit file and the log entries, and `clear-live` records which nonce it cleared. Receipt correlation uses
  the `ATTEMPT` trailer, which is unchanged. A prompt file whose nonce is not current is stale, and any event attributed to
  it is logged and ignored.
- **Recovery table.** `recovery.py` holds the decisions as data, with counters in state that survive a crash: Retry (max
  2), take-over, keep-previous, redo (max 2), the one-shot Unknown-skill retry, the base-stale auto-rebase, and the
  flow-healer heal (which records `healed: true` for Layer 3's merge gate). No new remediation.
- **Ledger (`ledger.py`).** Written only through the existing `scripts/flow-ledger.py`, which the driver calls and does not
  copy. Per-step snapshot at spawn, re-rendered from the run key when missing, instead of a "ledger unavailable" stop.
  Fenced records only to the workers the Ledger block allows (never blind Pass 1). On each receipt, the `Addressed:`
  carry-forward check: a direction the step could act on and did not carry is a STOP. The idempotent ship-time close-out.
- **North Star degrade.** Before spawning a step that seats North Star Nick, the driver runs `scripts/north-star-preflight.py`
  in Python. Missing inputs become a visible warning carried into the directive and the receipt (`NORTH STAR BLIND:`), not a
  take-over stop.
- **Ship guard (Q4, seq 1).** Before SPAWN for ship, the driver checks the receipts on disk and `gh pr view` for the
  branch. No PR: SPAWN ship. A PR whose `headRefOid` equals the expected branch HEAD (which must equal the HEAD recorded by
  the latest implement, fix or verify receipt): ship is skipped. A PR whose head differs: STOP with both SHAs in the reason,
  never a skip and never an automatic re-push. A `gh` error is a STOP. This runs only in mutating commands.
- **No merge in Layer 2.** A PR already MERGED advances the flow to done. Every other arrival at the merge step is a STOP
  with the reason "merge gate not built (Layer 3)". `--auto-merge` is refused at flow start.
- **Driver-side preflight (#407).**
- **Commands.** `--list` covers both roots, labels v1 flows "v1, finish with /expert-flow", and stays cheap over hundreds of
  directories. A retention rule caps archives and logs. `--abandon` and `--retro` are in this layer.
- **Stub.** Grows to the single-ticket path up to and including ship. Under 150 lines. Never writes `state.json`,
  `answers/` or `fleet-ask.json`.
- **Test harness.** A recorded-transcript harness drives a fake stub through the event sequences, with no model.

**Acceptance criteria:**

- The recorded-transcript harness covers: the interactive happy path ending in the pre-merge STOP; a double completion
  notification; a crash after `NEXT:`; a dead agent; a failed RESUME (re-spawn only after clear-live and only for
  allowlisted steps); a park/answer/resume cycle (`spawned` → `parked-awaiting-answer` → `resumed` → `receipt-landed`); an
  unknown verb (STOP); and a ship replay.
- No-merge pin: from every state at or after ship, no Layer 2 transition emits a merge directive. An already-MERGED PR
  advances to done, anything else is STOP. `--auto-merge` is refused.
- Nonce: the prompt file name carries the current nonce. A re-spawn after clear-live gets a new nonce. An event attributed to
  a stale nonce is ignored and logged. The receipt `ATTEMPT` trailer format is unchanged.
- One case per recovery entry: Retry and its cap, take-over, keep-previous, redo and its cap, unknown-skill one-shot (fires
  once, then STOP), base-stale auto-rebase (naive rebase, then the `/expert-rebase` worker on conflict, never a push), and the
  flow-healer heal (once per flow, records `healed`).
- The Retry and redo caps survive a crash.
- Ship guard, with a fake `gh` on PATH: no PR spawns ship; a PR whose head equals the expected HEAD skips ship; a PR whose
  head differs is a STOP naming both SHAs; a `gh` error is a STOP.
- Ledger: the snapshot is written at spawn and re-rendered from the run key when missing; fenced records reach only allowed
  workers; an uncarried direction in `Addressed:` is a STOP; the ship-time close-out is idempotent across two runs.
- North Star: missing preflight inputs produce a visible warning in the directive and the receipt, not a stop.
- `--list` stays fast over hundreds of synthetic directories.
- Stub under 150 lines. `just check` passes.

---

### Sub-issue 3: Layer 3, relay, assess, gates, merge gate

**Depends on:** 2.

**Scope:**

- **Pinned first.** Tests for the `--auto-merge` refusal and for the healed-run forced ask come before any other work in this
  layer, and before the Layer 2 pre-merge STOP is replaced by a merge path.
- **`relay.py`.** Reuses `questions/<STEP>-NN.json` and `answers/<STEP>-NN.json` and `flow-render-questions.py` unchanged. The
  stub passes the human's answer to `driver answer`, and the driver writes the answer file. The driver correlates each answer
  to the currently parked question from disk, with no stub-carried token. A flow killed mid-ASK re-presents the same question
  number.
- **Folded issues.** #304: the relay form carries its context. #408: v2's review-step handling uses the scripted ruling writer
  `scripts/review-ruling.py`. `/expert-review` itself is not edited.
- **`assess.py`.** Every assess check that needs Bash (range and anchor verification, git reads, the already-done check) runs in
  driver Python. `flow-assessor` is spawned only for read-and-judge work on the driver's precomputed results. The script gate
  blocks ship until every outstanding item has a disposition. If the assessor still needs Bash, #446 is recorded as unaddressed
  alongside #406 and #430.
- **`gates.py`.** `--gates` step gates (Continue, Redo with notes, Pause, Abort), redo capped at two, and the human merge gate.
  A merge directive is emitted only from this module, after the gate. Never under `--auto-merge` on a healed run.
- **Post-merge verification (#476).** After the merge step's receipt lands, re-read PR state with
  `gh pr view --json state,mergedAt` (and the queue result when the merge went through the merge queue). Only a MERGED PR
  advances to done. A receipt reporting success on an unmerged PR is a STOP naming the mismatch. A `gh` error is a STOP.
- **Fidelity audit orchestration.** The driver runs the existing audit on the receipt, reads the ledger snapshot Layer 2
  re-renders, and turns a rejection into the existing Accept-and-continue and Retry options.
- **ADR-0025.** Amended so the auto-merge rejection covers `commands/expert-flow-v2.md`.

**Acceptance criteria:**

- The `--auto-merge` refusal and the healed-run forced-ask tests exist and pass before any merge path is added.
- A merge directive is emitted only from `gates.py`, after the gate.
- Post-merge verification with a fake `gh`: a merge receipt reporting success on an OPEN PR is a STOP; a MERGED PR advances to
  done; a `gh` error is a STOP.
- A flow killed mid-ASK re-presents the same question. An answer for a non-parked question is refused.
- The renderer output is byte-identical to v1 for the same question file.
- Assess: a fake assessor with Bash denied still completes the stage. The script gate blocks ship until every item has a
  disposition. An already-done item is reported.
- Gates: a `--gates` flow stops at each gated step with Continue, Redo, Pause or Abort. Redo archives the previous attempt and
  is capped at two.
- Fidelity audit: a rejected receipt offers Accept-and-continue (allowlisted rejections only) and Retry. An accepted one advances.
- `just check` passes.

---

### Sub-issue 4: Layer 4, epic and fleet

**Depends on:** 3.

**Scope:**

- **Epic.** `epic.py` runs sub-issues sequentially.
- **`fleet.py --driver v2`.** An additive switch, active only under `--driver v2`, that changes three things: the prompt string
  (`/expert-flow-v2`), the flows root (`flows-v2`), and the fleet run identity passed to the session. Each `fleet.py run` generates
  a run id (pid plus start time, or a uuid) after taking `locks/<issue>.lock`. It records the id in `fleet.json` beside the
  existing `pid`, and exports it with the fleet dir to the session as `FLEET_RUN_ID` and `FLEET_DIR`.
- **`fleet_hooks.py`.** The driver writes `fleet-ask.json` in the exact shape `fleet.py` reads, so `current_stop_key` hashing is
  deterministic. WAIT mode comes from `flags.fleet`, never from the stub.
- **Fleet-mode clear-live (Q5, seq 1).** The driver records `FLEET_RUN_ID` in `pending_spawn` on SPAWN or RESUME. On a later
  invocation whose `FLEET_RUN_ID` differs, while the current run holds `locks/<issue>.lock`:
  - a `spawned` or `resumed` record gets a logged automatic clear-live with both run identities, so a stub crash does not park
    the ticket;
  - a `parked-awaiting-answer` record is never cleared. A normal unpark starts a new `fleet.py run --message-file`, and the
    driver emits RESUME, moving the record to `resumed` under the new run id.

  This layer verifies that a released lock proves the step agent is gone, including grandchild processes (`fleet.py` notes a
  grandchild can outlive its parent). If it does not, the automatic clear-live falls back to a park.
- **Lock order.** A test pins ticket lock, then flow lock, with no `main.lock` while the flow lock is held.
- **Folded issue.** #424 (stop, fix and resume within the fleet).

**Acceptance criteria:**

- Epic: sub-issues run sequentially; a stopped child halts the epic; children inherit `--gates`.
- A fleet-mode stub crash (record `spawned` or `resumed`) followed by a new run produces a logged automatic clear-live with both
  run identities, and no park.
- A normal park and unpark: a `parked-awaiting-answer` record under a new `fleet.py run --message-file` gets no clear-live. The
  driver emits RESUME, and the record becomes `resumed` under the new run id.
- While the recording run still holds the lock, no clear-live happens.
- The lock-order test passes.
- `fleet.py` without `--driver v2` builds an unchanged command line and environment (pinned by test). With `--driver v2`, the run id
  is recorded in `fleet.json` and exported to the session.
- The `fleet-ask.json` shape matches what `fleet.py` reads.
- The released-lock-proves-liveness check is recorded, with the fallback to a park if it fails.

---

### Sub-issue 5: Layer 5, dogfood, ADR-0026 accepted, v1 deprecation

**Depends on:** 4, and the D6 bar.

**Scope:**

- **Dogfood.** Run real tickets through v2 and compare against the Layer 0 baseline: orchestrator-owned stops, first-turn tokens
  and step times. Rebuilt receipts are counted separately.
- **When the D6 bar is met:** flip ADR-0026 to Accepted; amend ADR-0008 and ADR-0025; update `CLAUDE.md` and the docs; deprecate
  v1, which then only finishes its remaining flows; remove v1 (`commands/expert-flow.md` removal only) once `--list` shows no v1
  flow in flight.
- **Lift the seq 1 freeze** at this point, unless the human lifted it earlier.

**Acceptance criteria:**

- Dogfood results against the baseline are recorded for at least N consecutive real tickets, including one epic and one `--gates` run.
- Zero orchestrator-owned take-overs across that set, with rebuilt receipts excluded from the count.
- The inventory drift test is green, meaning no ADR-0023 amendment lacks an inventory entry.
- ADR-0026 is Accepted; ADR-0008 and ADR-0025 carry their amendments.
- v1 removal happens only after `--list` shows no v1 flow in flight.

---

## Layer 0 (this PR): acceptance criteria

These are the criteria the #477 PR is gated on. Steps 1 to 5 of the plan, and this document, are covered here.

- `just check` passes.
- `tests/test_v2_inventory_coverage.py` fails when a synthetic ADR-0023 amendment heading is added without an inventory reference,
  and passes on the real files.
- `tests/test_flow_stop_baseline.py` runs on synthetic flow dirs covering merge and done, plan/awaiting, and step-agent, environment
  and orchestrator stop reasons. It asserts the classification counts, lists the ambiguous rows, and asserts that the script writes
  nothing under the fixture root (byte-identical hash before and after).
- The pseudonym check passes, and the baseline document carries aggregate counts only, with no repository names or paths.
- `git diff --stat main` shows no v1 file and no existing test changed beyond additive entries.

## Risks

- **v1 defects stay live for the whole build (#446, #476, and the rest).** The human chose a full freeze (seq 1) with the trade in
  front of them. Mitigation: the human can override per issue. v2 avoids #476 from Layer 3 on, through post-merge verification, and
  avoids #446 by running its Bash-needing checks in Python. v1 keeps both defects for the whole build.
- **The baseline includes stops caused by #446 and #476, which makes the v2 bar easier to beat.** The human declined to tag them.
  Mitigation: the per-class report from Step 3 is published as is, and Layer 5 reads the orchestrator class, where those two issues
  mostly do not land. This is an accepted residual.
- **The rewrite's premise may not hold.** If orchestrator-owned stops are a small share, a five-layer rewrite may be out of
  proportion. Mitigation: the baseline runs before any code, and the human re-confirms scope before the skeleton starts.
- **An intermediate layer merges a PR without the human gate.** Mitigation: Layer 2 ends in STOP before merge (pinned by test) and
  refuses `--auto-merge`. The merge directive exists only in Layer 3's `gates.py`, after the refusal and healed-run tests are pinned.
  The skeleton has no ship or merge path.
- **The stub skips or repeats a directive.** Mitigation: the disk-only boundary makes repeat calls to `driver next` harmless.
  `pending_spawn` closes the double spawn before a receipt lands. The skeleton measures the actual rate. Ship is guarded by the PR
  head-SHA check, and merge by the human gate plus post-merge verification.
- **A double spawn of a non-ship step before any receipt exists is detected only after the fact**, by the `ATTEMPT` trailer (Q4 A,
  accepted). Mitigation: `pending_spawn` is treated as live, plus clear-live. The residual cost is wasted work, never a duplicate
  push or merge.
- **Skipping ship on a stale PR.** A PR can exist while local HEAD is ahead of it. Mitigation: ship is skipped only when the PR head
  SHA equals the expected branch HEAD. A mismatch is a STOP with both SHAs, and this is tested.
- **A normal fleet unpark is mistaken for a crash.** Mitigation: parked spawns are excluded from the automatic clear-live, the driver
  learns its fleet run identity from `fleet.py` (env var), and Layer 4 tests a normal park and unpark.
- **The nonce becomes a dead field or leaks into the receipt trailer.** Mitigation: its consumers are named (prompt-file name,
  directive file, log, clear-live record). Receipt correlation stays on `ATTEMPT`, and a test pins the trailer as unchanged.
- **Read-only commands mutate state, or polls collide with the lock.** Mitigation: the command split is structural (ADR-0026), and
  the byte-identical test covers the flow dir and `.git/refs`.
- **Self-heal hides step-agent receipt defects.** Mitigation: a rebuilt receipt gets a distinct name and a mark, never turns FAILED
  into OK, and is counted separately.
- **The ledger stop survives into v2.** Mitigation: Layer 2 owns the ledger path, re-renders the snapshot from the run key, and tests
  the carry-forward, the unaddressed-direction stop and the close-out.
- **Fleet lock release may not prove the step agent is dead.** A grandchild process can outlive its parent. Mitigation: Layer 4 verifies
  this, with a fallback to a park.
- **Schema drift breaks `fleet.py`'s derived ticket states.** Mitigation: the field-contract test, plus a test that the `--driver v2`
  default path is unchanged.
- **A lock-order inversion between fleet and a human resume.** Mitigation: a defined order, tested in Layer 4.
- **Duplicate atomic-write helpers drift.** Mitigation: Layer 1 extends `scripts/workflow/fsutil.py` instead of adding a module, and
  existing callers keep their default behaviour, tested.
- **Skeleton or early-layer flow dirs are picked up by v1 or `fleet.py`.** Mitigation: every v2 build, skeleton included, writes only
  under `~/.claude/flows-v2/<repo>/`, and the stub is labelled experimental.
- **The half-finished rewrite and two orchestrators.** Mitigation: each layer is its own sub-issue with acceptance criteria. The D6 bar
  is measurable, and the drift test catches v1 rules added mid-build.
- **Real flow-dir fixtures leak private names.** Mitigation: prefer synthetic fixtures. Any scrubbed real fixture gets a human-assigned
  expected outcome and a pseudonym check before commit.
- **The `fleet.py` edit touches shared fleet code.** Mitigation: the edit is additive only (prompt string, flows root, run identity export,
  all under `--driver v2`), it is scheduled in Layer 4, and a test pins the v1 command line and environment as unchanged.

## Open Items

- **The dissent on Q2.** A reviewer pushed back in round 2. A blanket freeze on all v1 work, including the #446 and #476 bug fixes, is a
  policy the ticket did not ask for, and it leaves known defects live for the whole build. The smaller alternative was to freeze new v1
  features only. The human chose the full freeze (seq 1) with that trade in front of them. The dissent is carried here and as a risk. The
  human can override at any time.
- **The Q5 option C question.** A resident driver that launches each step with `claude -p` would make liveness a real mutex with no
  human clear-live. It turns the driver into a resident process and bypasses the agent hook allowlists, so it was not chosen. ADR-0026
  records it as a rejected alternative. Revisit only if Layer 4 shows that fleet lock release cannot prove liveness.
- **N for the D6 exit bar.** The human sets N after reading the Layer 0 baseline.
- **The scope check after the baseline.** If orchestrator-owned stops are a small share of all stops, the human re-confirms the layered
  scope before the skeleton sub-issue starts.
- **The "0 of 47" figure.** The baseline reconciles the figure from the ticket with a local count of 14 of 84 reaching merge or done. If the
  ticket figure came from a different telemetry definition, the baseline adopts that definition and says so.
- **Stale branch and issue text.** A stale branch named for #456 and the #456 issue text, which names the wrong worktree and branch, need a
  human decision: delete or reuse the branch, and correct the body.
- **Filing the sub-issues** (Step 6) is a human or `/track` action after this PR merges. The bodies are the layer sections above.
- **W1's per-step allowlist** (which steps honour pre-written answers) is settled by Layer 2 tests. Until a step is proven, a failed RESUME
  for that step is a STOP.
- **#406 and #430 (shared guards)** remain open and are not addressed by v2. Fixing them is v1 or shared work under the seq 1 freeze. The same
  holds for the hook half of #446.
