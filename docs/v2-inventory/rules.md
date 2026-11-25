# v1 rules inventory

Line ranges refer to `commands/expert-flow.md` as it stood when this inventory was written. "ADR" means
`docs/adr/0023-expert-flow.md`. Where a rule was added by an amendment, the amendment is named by its
heading in the index at the end. Riskiest areas come first.

Disposition values: **port, pinned by test** (layer named) or **dropped, reason**.

## 1. Rule table

### 1.1 Step loop and state

| v1 lines | Area | ADR section / amendment | Target layer | Disposition |
|---|---|---|---|---|
| 995-1187 | The step loop: spawn each step, run the verifier, fidelity audit and gate in order, advance `STEP` only after `WRITE-STATE` | ADR base text; 2026-10-13 gated Rebase, 2026-10-14 more human in the loop | Layer 2 (Step 9) | port, pinned by test |
| 853-956 | Flow state: `state.json` keys (`step`, `status`, `live`, `attempts`, `failures`, `stop_reason`, `stop_source`, `flags`, `pr`, `worktree`, `healed`, `redo_archive`, `skill_retries`) | ADR base text; 2026-10-27 assess keys; 2026-10-29 #380 `healed` | Layer 1 (Step 8) | port, pinned by test (state.py: v2 state schema; v1 field contract retained) |
| 957-994 | Step list and order (`worktree` through `merge`) | ADR base text | Layer 2 (Step 9) | port, pinned by test |
| 1188-1199 | Orchestrator-written receipts (`steps/<NN>-<step>.md`) | ADR section 2.2 (2026-10-14, #276) | Layer 1 (Step 8) | port, pinned by test (receipts.py: receipt classification and archive) |
| 1308-1327 | Re-entry pre-checks | ADR base text | Layer 2 (Step 9) | port, pinned by test (spawn gating; ship case Layer 3) |
| 1188-1199 (receipt contract, used by 995-1187) | `Decision: OK` / `FAILED`, `ATTEMPT`, `CHECK`/`CHECK_EXIT`/`CHECK_SHA`, `RUN_KEY`/`RECEIPT_CONTRACT` keys | 2026-10-26 #338; 2026-10-29 #346 | Layer 1 (Step 8) | port, pinned by test (receipts.py: well-formedness and verifier parity) |
| 1897-1928 | Attempt logging and `attempts[STEP]` counting; first-attempt identity floor for plan and review freshness | 2026-10-21 #310 (counted from disk by attempt) | Layer 1 (Step 8) | port, pinned by test (state.py: reconcile and counter repair) |
| 2015-2034 (deps) | Deps precondition: `deps ensure` before worktree, implement, fix and verify; a manifest change stops for approval | 2026-11-01 #417 | Layer 2 (Step 9) | port, pinned by test |
| 1953-1993 | Deps precondition block (`deps ensure`, `commands.install`) | 2026-11-01 #417 | Layer 2 (Step 9) | port, pinned by test |
| 1994-2328 | Per-step details (worktree, track, plan, implement, review, fix, verify, ship, merge) | ADR base text; many amendments (see index) | Layers 2 and 3 | port, pinned by test |
| 2030-2034 | Check detection (`checks ensure-check`), marker and receipt written only after it | ADR base text; 2026-10-26 #338 | Layer 2 (Step 9) | port, pinned by test |
| 2074-2100 | Plan-step question file, relay, persisted answer, ledger record, refusal line | 2026-10-15 plan checkpoint; 2026-10-30 #288 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_relay.py (test_park_allocates_and_reenters, test_archive_consumed, test_exact_option_match); the plan-step ledger record and refusal line have no test yet |

### 1.2 `--resume` cascade (lines 506-664)

Order matters. Rule 1b (heal in progress) runs before rules 4 and 5, so a heal is never re-spawned or lost.

| v1 lines | Rule | ADR section / amendment | Target layer | Disposition |
|---|---|---|---|---|
| 546 | Rule 0: redo entry (`--redo <STEP>` only), runs before rule 1 | 2026-10-20 #286 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_recovery.py (test_redo_bound_before_limit, test_redo_bound_at_limit) and tests/test_flow_driver_gates.py (test_redo_cap); the entry ordering before rule 1 has no test yet |
| 547-555 | Rule 1: live step, a previous session's step agent still running | ADR base text; 2026-10-14 more human in the loop | Layer 1 (Step 8) | port, pinned by test (resume.py: rule 1 decision and receipts.classify outcomes) |
| 556-602 | Rule 2: `status: paused` at a step gate (`plan`, `implement`, `review`, `fix`, `verify`) | 2026-10-16 third (step gates opt-in); 2026-10-16 second (`taken-over`) | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_gates.py (test_step_gate_parks_with_options, test_step_gate_answers) and tests/test_flow_driver_resume.py (test_resume_reasks_step_gate) |
| 603-604 | Rule 2a: assess stage (`STEP` is `assess`, or `ship`/`merge` with no passing outstanding check) | 2026-10-27 #383 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_assess_loop.py (test_nothing_outstanding_skips, test_spawn_wait_ask_answer_advance) and tests/test_flow_driver_assess_next.py (test_nothing_outstanding_advances) |
| 605 | Rule 3: `status: done` prints `flow already done`; no telemetry | ADR base text | Layer 2 (Step 9) | port, pinned by test |
| 606-608 | Rule 3a: `status: abandoned` asks once (fleet parks it as a `gate` ask) | ADR base text | Layer 3 (Step 10) | re-pointed to the Layer 3 recovery follow-up (not wired in #487) |
| 609 | Rule 1a: pending human input (legacy file and human ledger) | 2026-10-14 #287 ledger; 2026-10-30 #288 binding | Layer 2 (Step 9) | port, pinned by test |
| 610-616 | Rule 1b: heal in progress (`HEAL:` records); runs before rules 4 and 5 | 2026-10-29 #380; 2026-11-02 #425 | Layer 3 (Step 10) | re-pointed to the Layer 3 recovery follow-up (not wired in #487) |
| 617-618 | Rule 4: `status: failed` (aborted) re-runs the recorded step while `failures[STEP] < 2`, else Hard stop | ADR base text; 2026-10-20 #286 | Layer 3 (Step 10) | re-pointed to the Layer 3 recovery follow-up (not wired in #487) |
| 619-635 | Rule 5: `awaiting`, `paused` or `running` with the step subagent gone | ADR base text; 2026-10-14 more human in the loop | Layer 2 (Step 9) | port, pinned by test |
| 636-641 | Rule 3b: flow resume after a failed redo (before rule 4; `redo_archive`) | 2026-10-20 #286 | Layer 3 (Step 10) | re-pointed to the Layer 3 recovery follow-up (not wired in #487) |
| 642-646 | Rule 6: step `merge` checks the PR state first (`gh pr view --json state`) | 2026-11-05 #462 (merge gate authoritative); #476 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_gates.py (test_merge_gate_already_merged_has_no_question, test_merge_gate_stops); the loop-level PR-state read has no test yet |
| 647-650 | Rule 7: worktree needed by `implement`, `review`, `fix`, `verify`, `ship`, `merge` | ADR base text; 2026-10-14 worktree-first | Layer 2 (Step 9) | port, pinned by test |
| 651 | Rule 8: epic flows resume the current sub-ticket's child flow | ADR base text (epic mode) | Layer 4 (Step 11) | port, pinned by test |
| 652-658 | Rule 9: `flags.auto_merge` forced to `false` on resume; `flags.step_gates` restored; `healed` never reset | 2026-10-16 third; 2026-10-29 #380 | Layer 3 (Step 10) | refused by design (Q3 = A): v2 `start --auto-merge` stops `auto-merge-refused`; pinned by `tests/test_flow_driver_loop.py` (test_10_auto_merge_is_refused_before_any_state); the v2 case in `tests/test_fleet_invariants.py` is added alongside |
| 659-664 | Rule 10: telemetry closes an open stage | ADR base text | Layer 2 (Step 9) | port, pinned by test |

### 1.3 Retry, take-over and hard-stop tree (lines 1625-1952)

| v1 lines | Rule | ADR section / amendment | Target layer | Disposition |
|---|---|---|---|---|
| 1625-1648 | Redo note: raw note to a file, sanitised, echoed, recorded in the ledger, archived | 2026-10-20 #286 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); no test yet for the note sanitising, echo and archive in the driver (tests/test_flow_redo.py covers the separate scripts/flow-redo.py) |
| 1649-1666 | Redo procedure (shared by the step-gate entry and `--redo`); cap of two redos, counted separately from the two failures | 2026-10-20 #286 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_gates.py (test_redo_cap) and tests/test_flow_driver_recovery.py (test_redo_bound_before_limit, test_redo_bound_at_limit) |
| 1667-1678 | Hard stop: Retry (cap 2), Take over, Keep previous attempt, Abort; nothing else except the bounded exceptions | ADR base text; 2026-10-16 fourth | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_recovery.py (test_retry_bound_before_limit, test_retry_bound_at_limit) and tests/test_flow_driver_audit_loop.py (test_reject_stops_and_retry_recovers); Keep previous and Abort have no test yet |
| 1679-1684 | Rule 2a of the hard stop: auto-retry once for an Unknown skill, `skill_retries[STEP] = 1` | 2026-11-04 fifth bounded exception | Layer 3 (Step 10) | re-pointed to the Layer 3 recovery follow-up (not wired in #487) |
| 1685-1713 | Auto-rebase on verify base-stale failures: naive rebase, then `/expert-rebase` worker on conflicts; never a push | 2026-10-13 gated Rebase; 2026-10-14 automatic Rebase | Layer 3 (Step 10) | re-pointed to the Layer 3 recovery follow-up (not wired in #487) |
| 1714-1771 | Check heal (verify): one `flow-healer` heal per flow on `lint` or deterministic `test` only; healed run forces the merge gate | 2026-10-29 #380 | Layer 3 (Step 10) | re-pointed to the Layer 3 recovery follow-up (not wired in #487) |
| 1772-1783 | Auto-escalate on implementer shortfall (implement and fix) | 2026-10-16 fourth; 2026-10-25 #341 | Layer 3 (Step 10) | dropped: dormant (automatic escalation on implementer shortfall is not ported; see row 20; manual retry is kept) |
| 1784-1788 | Accept eligibility (plan, implement, review, fix, verify), human-only, only via `scripts/flow-accept.py` | 2026-10-29 #365 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_audit_assess.py (test_accept_eligibility_fails_closed) and tests/test_flow_driver_audit_loop.py (test_accept_allowlisted_rejection, test_accept_refused_off_allowlist) |
| 1789-1872 | Keep previous attempt (Recommended, offered first), Take over, Abort, Retry with sonnet, and `Retry, split finer` (only before escalation) | 2026-10-16 fourth; 2026-11-02 #419 | Layer 3 (Step 10) | re-pointed to the Layer 3 recovery follow-up (not wired in #487) |
| 1873-1896 | Escalation telemetry | 2026-10-25 #341 | Layer 2 (Step 9) | port, pinned by test (log events only) |
| 1929-1952 | Stop lines printed at a stop (`flow` log events, fleet `(fleet: parked)` suffix) | 2026-11-02 #421 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_loop.py (test_40_stop_reason_round_trip_for_every_code); the fleet `(fleet: parked)` suffix has no test yet |
| 1667-1872 (shared) | Takeover at the Hard stop and at the merge gate writes `taken-over`; the only record that a human chose it | 2026-10-16 second | Layer 1 (Step 8) | port, pinned by test |

### 1.4 Assess, verify, fidelity and relay

| v1 lines | Rule | ADR section / amendment | Target layer | Disposition |
|---|---|---|---|---|
| 2180-2263 | Assess stage before ship: verify each outstanding unit; already-done reported; unfinished relayed with the assessor's options | 2026-10-27 #383 slice 1; 2026-11-02 #435 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_assess_loop.py (test_spawn_wait_ask_answer_advance, test_bad_records_halt) and tests/test_flow_driver_assess_next.py (test_nothing_outstanding_advances, test_spawn_then_ask_then_gate) |
| 2188-2191 | Fix-now carry rule (persisted in the answers file) | 2026-10-29 #380 | Layer 3 (Step 10) | re-pointed to the Layer 3 recovery follow-up (not wired in #487) |
| 2192-2216 | Range overlap check for `fix:unknown` items; script-anchored already-done | 2026-11-02 #435 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); no test yet for the range overlap check or the script-anchored already-done check |
| 2217-2218 | Already-done notice, always shown | 2026-10-27 #383 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_assess_next.py (test_done_items_ask_nothing); the always-shown notice itself has no test yet |
| 2219-2255 | Ask split into forms, per-form persistence; every item must have a disposition before ship and merge | 2026-10-27 #383 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_assess_next.py (test_spawn_then_ask_then_gate, test_drop_needs_a_note_and_gate_catches_gaps) and tests/test_flow_driver_assess_loop.py (test_item_answer_without_batch_refused) |
| 2256-2262 | Fixed is derived by re-running `--outstanding-set`, never self-reported | 2026-10-27 #383 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_assess_next.py (test_set_reads_ok_receipts, test_set_empty_and_malformed) |
| 1200-1307 | Fidelity audit orchestration: the verifier owns counts, the audit owns judgment only | 2026-10-19 #318 | Layer 2 (Step 9) | port, pinned by test |
| 1219-1221 | `fidelity audit failed: ledger unavailable` stop (unrecognised run-key source, or a failed locate) | 2026-10-19 #318 | Layer 2 (Step 9) | dropped: v2 re-renders the ledger snapshot from the run key instead of stopping (plan Layer 2 ledger bullet); a Layer 2 test pins the re-render |
| 1328-1355 | Human ledger: per-step snapshot at spawn, fenced records only to allowed workers, `Addressed:` carry-forward, unaddressed-direction stop, ship-time close-out only | 2026-10-14 #287; 2026-10-30 #288 | Layer 2 (Step 9) | port, pinned by test |
| 1356-1372 | Relaying a question (common rules) | 2026-10-14 more human in the loop | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_relay.py (test_park_allocates_and_reenters, test_park_non_step_needs_nn) |
| 1372-1447 | Report-kind questions (plan checkpoint, review checkpoint) | 2026-10-15 plan checkpoint; 2026-11-02 #421 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_relay.py (test_paths_per_kind, test_render_question); the report-kind checkpoint content has no test yet |
| 1448-1516 | Form-kind questions (legacy path) | ADR base text | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_relay.py (test_exact_option_match, test_free_text_accepts_anything, test_answer_is_exclusive) |
| 1517-1558 | Prose interpreter: model-only, echoes the interpretation, verifies the answers file before resume | 2026-10-14 more human in the loop | Layer 3 (Step 10) | re-pointed to the Layer 3 recovery follow-up (not wired in #487) |
| 1559-1565 | Relay closing rules | 2026-10-14 more human in the loop | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_relay.py (test_archive_consumed, test_nothing_parked_refused) |
| 1566-1624 | Step gate: opt-in (`flags.step_gates`); Continue, Redo with notes (plan, review), Pause, Abort | 2026-10-16 third; 2026-10-20 #286 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_gates.py (test_gates_off_advances, test_step_gate_parks_with_options, test_step_gate_answers, test_redo_cap) |
| 2295-2298 | Verify budgets (`failures[verify]` bounds the check heal) and pre-merge comment check (`--trusted-only`) | 2026-10-29 #380; 2026-10-30 Ask-based relay | Layer 3 (Step 10) | re-pointed to the Layer 3 recovery follow-up (not wired in #487) |

### 1.5 Merge gate and auto-merge

| v1 lines | Rule | ADR section / amendment | Target layer | Disposition |
|---|---|---|---|---|
| 2299-2301 | Merge gate: check PR state first; if already `MERGED`, skip the gate | #476 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_gates.py (test_merge_gate_already_merged_has_no_question) |
| 2302-2307 | Healed run: the gate is always asked, even under `--auto-merge` (any `HEAL:` record, `healed: true`, or a `Flow-Heal:` commit) | 2026-10-29 #380 | Layer 3 (Step 10) | landed as: every merge asks, healed or not (Q3 = A) |
| 2308-2316 | Merge gate question: Merge, Hold, Take over; fleet parks it as a `gate` ask | 2026-10-29 #380; 2026-11-02 #421 | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_gates.py (test_merge_gate_asks_even_when_healed, test_merge_answer_rereads_by_number, test_merge_gate_stops) and tests/test_flow_driver_resume.py (test_resume_reasks_merge_hold) |
| 91-96 | `--auto-merge` per-run only; a healed run suspends it | 2026-10-29 #380 | Layer 3 (Step 10) | refused by design (Q3 = A): v2 `start --auto-merge` stops `auto-merge-refused`; pinned by `tests/test_flow_driver_loop.py` (test_10_auto_merge_is_refused_before_any_state); the v2 case in `tests/test_fleet_invariants.py` is added alongside |
| 2467 | No stored default for `--auto-merge`; resume never restores it | ADR base text | Layer 3 (Step 10) | refused by design (Q3 = A): v2 `start --auto-merge` stops `auto-merge-refused`; pinned by `tests/test_flow_driver_loop.py` (test_10_auto_merge_is_refused_before_any_state); the v2 case in `tests/test_fleet_invariants.py` is added alongside |
| 97-103 | `--gates` stored per flow; inherited by epic children | 2026-10-16 third | Layer 3 (Step 10) | landed in #487 (code in scripts/flow_driver); pinned by tests/test_flow_driver_gates.py (test_gates_off_advances); per-flow storage has no test yet; epic inheritance is Layer 4 |
| 2440-2451 | Exit paths (Hold at merge gate, Pause at step gate, Take over, Abort, Done) | ADR base text | Layer 2 (Step 9) | port, pinned by test |

### 1.6 Setup, fresh run, subcommands, epic and fleet

| v1 lines | Rule | ADR section / amendment | Target layer | Disposition |
|---|---|---|---|---|
| 136-210 | Fleet mode section, including the foreground-`wait` rule at 200-207 (`fleet.py wait`, `--timeout 540`, Bash timeout 600000, no end-of-turn idle) | ADR base text (fleet mode) | Layer 4 (Step 11) | port, pinned by test (WAIT mode from `flags.fleet`) |
| 211-293 | Setup and argument parsing | ADR base text | Layer 2 (Step 9) | port, pinned by test |
| 282-293 | Project detection and repo key (primary resolver, never first-entry main worktree) | #472-#475 | Layer 2 (Step 9) | port, pinned by test (primary resolver only) |
| 294-410 | No-argument run | 2026-10-15 bare `/expert-flow` in the ticket worktree (#302) | Layer 2 (Step 9) | port, pinned by test |
| 411-449 | Where the orchestrator runs | 2026-10-15 #302; worktree-first | Layer 2 (Step 9) | port, pinned by test |
| 450-470 | Newest-flow lookup (shared) | ADR base text | Layer 2 (Step 9) | port, pinned by test (v1 scans stay blind to v2 dirs, Q3) |
| 471-505 | `--list` | ADR base text | Layer 2 (Step 9) | port, pinned by test |
| 665-681 | `--abandon` | ADR base text | Layer 2 (Step 9) | port, pinned by test |
| 682-703 | `--retro`, including closing retro gaps | 2026-10-21 #320 | Layer 2 (Step 9) | port, pinned by test |
| 728-739 | Fresh run, rule 0: fleet run refuses an existing flow with a guard block | ADR base text (fleet mode) | Layer 4 (Step 11) | port, pinned by test |
| 740-801 | Fresh run: Start fresh deletes the old flow's worktree (sanctioned destructive action) | 2026-10-13 Start fresh | Layer 2 (Step 9) | port, pinned by test |
| 802-843 | Fresh run: resume path from Main (hand-off stop) | 2026-10-15 #302 | Layer 2 (Step 9) | port, pinned by test |
| 844-852 | Telemetry begin | ADR base text | Layer 2 (Step 9) | port, pinned by test |
| 2329-2388 | Epic mode (experimental): sub-issues sequentially; child flows inherit `--gates` | ADR base text | Layer 4 (Step 11) | port, pinned by test |
| 2389-2437 | Every exit path (commands per exit) | ADR base text | Layer 2 (Step 9) | port, pinned by test |
| 2452-2476 | What this command deliberately does not do (no `--stack`, no stored `--auto-merge`, no auto-remediation beyond the bounded exceptions) | ADR base text | Layer 2 (Step 9) | port, pinned by test (negative constraints) |
| 47 | Bounded remediation exceptions: Rebase (base-stale), Unknown-skill retry, Retry with sonnet (manual), check heal and Fix now, fleet heal | 2026-10-29 #380; 2026-11-02 #425; 2026-11-04 | Layers 3 and 4 | port, pinned by test (five exceptions, none wider) |

### 1.7 Model-only primitives

The six primitives are model-only behaviour that a deterministic driver must replace with a closed verb set.

| v1 lines | Primitive | Target layer | Disposition |
|---|---|---|---|
| 39, 58 | `Task`: spawn a step agent | Step 7 (verb set: SPAWN) | port, pinned by test |
| 39 | `SendMessage`: resume a step agent | Step 7 (verb set: RESUME) | port, pinned by test |
| 58, 202-207 | End-turn wait: an end-of-turn idle is not a wait (headless sessions abort background agents about ten minutes after an idle turn) | Step 7 (verb set: WAIT) | port, pinned by test |
| 58 | `ScheduleWakeup` (1800 seconds per step, fallback heartbeat) | Step 7 (verb set: WAIT) | dropped: the end-turn wait row above replaces it in v2 (no heartbeat, ADR-0026 Decision 3); v1 line 58 keeps it only as a fallback |
| 200-207 | Fleet `wait` (foreground `fleet.py wait --timeout 540`) | Layer 4 (Step 11) | port, pinned by test |
| 1356-1565 | `AskUserQuestion` (relay, step gate, merge gate, hard stop) | Layers 2 and 3 | port, pinned by test (fleet mode parks instead of asking) |

## 2. Fleet state contract

Every `state.json` field that `scripts/fleet.py` or `scripts/workflow/heal.py` reads. Each must keep its
meaning on a v2 flow (plan Step 8 field-contract test, W2 ruled A: `fleet.py` reads v2 `state.json`
directly).

| Field | Read by | Lines | Meaning that must hold on a v2 flow |
|---|---|---|---|
| `issue` | `fleet.py` (state lookup per ticket); `heal.py` (`str(state.get("issue")) != str(issue)` guard) | `fleet.py:263-265`; `heal.py:502` | Matches the ticket number; a mismatch means the flow is not this ticket's |
| `updated` | `fleet.py` (newest-state selection) | `fleet.py:265` | Set on every write; the newest `updated` wins. v1's newest-scan and `fleet.py:261`'s glob cannot see v2 dirs (Q3) |
| `step` | `fleet.py` (`plan` paused check, stop key, derived state); `heal.py` (step, stop key) | `fleet.py:147`, `:283`, `:464`, `:584`; `heal.py:505`, `:566`, `:570` | Name of the current step; drives the stop key and the heal step |
| `status` | `fleet.py` (`done`, `failed`, `paused`, `awaiting`, `running`); `heal.py` (`status` in stop key) | `fleet.py:459`, `:577-591`; `heal.py:506`; `stop_key_for` | Same vocabulary as v1: `running`, `awaiting`, `paused`, `failed`, `done`, `abandoned` |
| `live` | `fleet.py` (plan-stall check: `live is None`) | `fleet.py:283` | `null` when no step agent is live; a non-null value means a session is live. The driver clears it through a logged `driver clear-live` (Q5) |
| `attempts.plan` | `fleet.py` (plan-stall check: `attempts.plan == 0`) | `fleet.py:284` | Counts plan attempts from disk |
| `stop_reason[step]` | `fleet.py` (stop key text); `heal.py` (`reasons`, `stop_key_for`) | `fleet.py:149-151`; `heal.py:524`, `:146` | Reason string for the current step's stop. Empty when absent; the stop key hashes it |
| `stop_source` | `heal.py` (`_str_map(state.get("stop_source"), _STOP_SOURCES)`) | `heal.py:523` | Source of the stop; restricted to the known source vocabulary in `heal.py` |
| `failures` | `heal.py` (`failures` map, bounds deps and heal attempts) | `heal.py:512` | Per-step failure count; the heal refuses past its bound |
| `pr` | `fleet.py` (`held` state when merge is paused with a PR); fleet row output | `fleet.py:584`, `:653-654` | PR URL once ship has run; `held` requires `status: paused` at `merge` |
| `worktree` | `fleet.py` (worktree for heal and row output); `heal.py` (worktree for the heal) | `fleet.py:474`, `:653-654`; `heal.py:546` | Absolute worktree path of the ticket |
| `healed` | `fleet.py` (healed-run forced ask check); `heal.py` (indirectly through merging decisions) | `fleet.py:651-654` (row output, healed-run context) | Boolean or present when the flow has had a successful check heal; forces the merge gate to ask |

Also read by `heal.py` through the flow dir and not through `state.json`: `steps/<NN>-verify.heal.md`
`HEAL:` records and `fleet-ask.json` (`status: open`, `step`, `header`). `fleet-ask.json` bytes are
hashed by `stop_key_for`, so its shape must match what `fleet.py` reads (Step 11).

## 2.1. v2 state schema (Layer 1, scripts/flow_driver/state.py)

The v2 `state.json` schema (driver_format: 2) is defined by the frozen dataclass in `scripts/flow_driver/state.py`. Every key listed below is strict; unknown top-level keys are preserved verbatim in `extra` (Q5 = A). The schema answers skeleton findings: AC2-K1-kill, AC2-K2-kill (live projection), PHANTOM-LIVE, ORPHAN-OVERLAP (flock fd non-inheritable), LATE-RECEIPT, RESUME-b, PERMISSION-VOID (attempt_log), and S-D1 (coverage disclaimer); see `docs/v2-inventory/skeleton-findings.md`.

### v2 state.json keys and types

| Key | Type | Meaning |
|---|---|---|
| `driver_format` | `int` | Always 2 |
| `run_id` | `str` | 16 hex characters, minted by `init` |
| `issue` | `int` | Ticket number |
| `repo` | `str` | Repo key `OWNER/NAME` |
| `created` | `str` | ISO format `YYYY-MM-DDTHH:MM:SSZ`, set at init, never changed |
| `updated` | `str` | ISO format `YYYY-MM-DDTHH:MM:SSZ`, updated on every write |
| `step` | `str` | Current step: `worktree`, `plan`, `track`, `implement`, `review`, `fix`, `verify`, `assess`, `ship`, `merge` |
| `status` | `str` | Status: `running`, `awaiting`, `paused`, `failed`, `done`, `abandoned` |
| `live` | `object` or `null` | Null, or `{step, attempt, nonce}` exactly matching the projection of `pending_spawn` (D1). Clears together with `pending_spawn` |
| `pending_spawn` | `object` or `null` | Null, or `{step, attempt, nonce, state, receipt_seen}`; `state` is `spawned`, `parked-awaiting-answer`, `resumed`, or `receipt-landed`; `receipt_seen` is null or `{sha256: str, size: int}` |
| `attempts` | `object` | Per-step attempt counter: `{step: int >= 0}`. Advanced only by `record_spawn` and reconcile repair. The fleet reads `attempts.plan` as the plan counter |
| `failures` | `object` | Per-step failure count: `{step: int >= 0}` |
| `stop_reason` | `object` | Per-step stop reason: `{step: str}` |
| `stop_source` | `object` | Per-step stop source: `{step: "receipt" \| "validation" \| "audit" \| "tree"}` |
| `pr` | `str` or `null` | PR URL once ship has run |
| `worktree` | `str` or `null` | Absolute worktree path, set at init and stored verbatim (never resolved; Layer 1 never calls the primary resolver; see #472-#475) |
| `healed` | `bool` | True when a check heal has run (forces merge gate to ask). Never reset on resume |
| `attempt_log` | `object` | Per-step per-attempt outcome history: `{step: {"<attempt>": "receipt-landed" \| "cleared" \| "receipt-malformed" \| "abandoned"}}`. Kept separate from `attempts` so the fleet's `attempts.plan` keeps its meaning (PERMISSION-VOID) |
| `applied` | `array[str]` | Canonical transition keys already applied; every key is unique; see apply_transition in scripts/flow_driver/state.py (Step 3) |
| `extra` | `object` | Unknown top-level keys preserved verbatim; never interpreted by the driver |

### Fleet field contract

The fields in the row above labeled as read by fleet or heal (§2: `issue`, `updated`, `step`, `status`, `live`, `attempts.plan`, `stop_reason`, `stop_source`, `failures`, `pr`, `worktree`, `healed`) keep their documented meanings and types on v2 flows. §2 is authoritative for their semantics.

### Driver exit-code contract (Layer 1)

| Exit code | Outcome class | Meaning |
|---|---|---|
| 0 | Acted-on | `init` succeeded; `resume` reached an acted-on outcome (`wait`, `receipt-landed`, `rerun`), or is reading-only (`status --json`); `list` exit 0 always. `rerun` exits 0 because the driver healed the malformed or stale attempt itself |
| 1 | Refusal | A command precondition failed and the driver does not heal it itself (a `locked` refusal is transient; the caller retries later): locked, v1-owned, skeleton-owned, schema-invalid, no-state, exists, verifier-timeout (after one automatic retry). One line to stderr in form `refused: <class>: <detail>`. Nothing on stdout |
| 2 | Usage error | Bad flags, mismatched `--flow` arguments, path inside neither `~/.claude/flows/` nor `~/.claude/flows-v2/`. The skeleton's existing code, including `FlowPathError` |
| 3 | Not-yet-built | Fail-closed, distinct from refusal. A resume rule that is not yet built in Layer 1 fires, returning `outcome=not-yet-built` and the rule's layer number. Stdout carries `RESUME rule=<name> outcome=not-yet-built`; stderr is exactly `not-yet-built: <rule name> is Layer <N>`. Exit 3 is a STOP-with-reason only for rules not yet built; it disappears rule by rule as later layers land. No live v2 flow runs in production until Layer 2, so the residual (Step 7: a self-healed flow stops at exit 3 on the next `resume` without spawning) is not observed |

### Lock order (ADR-0026 §10)

1. Fleet per-ticket lock (`fleet.py`'s per-flow locking, if any)
2. Flow lock (`.driver.lock` via `scripts/workflow/fsutil.py:nonblocking_flock` (taken by `state.py:mutating`))
3. `main.lock` is never taken while the flow lock is held

### Resume cascade notes

- **First match wins.** `resume.RULES` is evaluated in order and the first rule whose predicate holds decides. Rule 1
  (live step) fires only while `status` is `running` or `awaiting`.
- **Cadence assumption.** `receipts.classify` confirms landed and malformed receipts by two reads with an identical
  fingerprint. That is only meaningful when resumes are separated by real time or liveness. Layer 2 must add a quiet
  interval or a `subagent_gone` gate before acting on `rerun`.
- **Timeout wording.** A verifier timeout is retried once automatically (a `verifier-timeout-retry` log event is
  written); a second timeout is the refusal `verifier-timeout`.
- **Re-targeted rows.** Rows 10, 11 and 21 of the inventory are re-targeted to Layer 2 (#486) and Layer 3 (#487), and
  the `auto-merge` default (`flags.auto_merge` false unless passed per run) carries to #486 and #487 unchanged.

### Rule table ordering note

`rules.md` §1.2 lists rules in a specific order: Rule 1b (heal in progress) runs before rules 4 and 5. The table order places Rule 3b after Rule 5, but Rule 3b's text in §1.2 says "before rule 4". Layer 1 asserts only 1b < 4 and 1b < 5 (see `scripts/flow_driver/resume.py` test 39). Rule 3b ordering is settled by Layer 3 (#487), which gives it real behavior.

## 3. Overlapping-issue dispositions

From the plan's "Overlapping-issue dispositions" (Q2 and seq 1: fold the overlapping issues into layers).

| Issue | Disposition |
|---|---|
| #424 | Layer 4: stop, fix and resume within the fleet |
| #408 | Layer 3 |
| #407 | Layer 2 (driver-side preflight) |
| #406 and #430 | Not ported. v2 uses the existing hooks unchanged, so they stay open against the shared guards under the seq 1 hook freeze |
| #446 | Split. The hook defect (#446, hook half — `flow-assessor` missing from `KNOWN_FLOW_AGENTS`) has been fixed independently (commit f33a8bff, 2026-11-05). v2's Layer 3 assess stage runs every check that needs Bash in driver Python, and `flow-assessor` is spawned only for read-and-judge work |
| #304 | Layer 3 |
| #472-#475 | Layers 1 and 2 use the primary resolver and never the first-entry main-worktree rule |
| #476 | Layer 3 merge path: the driver verifies PR state after the merge step returns (Q4) |

## 4. ADR-0023 amendment index

Every ADR-0023 amendment heading, quoted as written in `docs/adr/0023-expert-flow.md`, with the rules it
adds and its disposition. The drift test matches these headings. Headings are listed in file order.
Headings marked `###` are amendment-tagged subsection headings. The subsection `### Amending §1` (under the
2026-10-16 fourth amendment) is covered by that amendment's row.

| # | Heading (as written) | Rules | Target layer | Disposition |
|---|---|---|---|---|
| 1 | Amendment: worktree-first invocation | Worktree-first: the ticket worktree is created first, then the flow runs from inside it | Layer 2 | port, pinned by test |
| 2 | Amendment (2026-10-13): gated Rebase on base-stale failures | Rebase was gated behind a human answer on base-stale failures | Layer 3 | dropped: superseded by #3 (automatic Rebase) |
| 3 | Amendment (2026-10-14): base-stale Rebase is automatic, with an `/expert-rebase` worker fallback | Automatic naive rebase, then `/expert-rebase` worker on conflicts; never a push | Layer 3 | port, pinned by test |
| 4 | Amendment (2026-10-13): Start fresh deletes the old flow's worktree | Start fresh removes the old flow's worktree (sanctioned destructive action) | Layer 2 | port, pinned by test |
| 5 | Amendment (2026-10-14): more human in the loop — context-carrying relay, full checkpoint relay, step gates | Relay carries context; full checkpoint relay; step gates | Layer 3 | port, pinned by test |
| 6 | Amendment (2026-10-14): receipt markers survive finalization | Receipt markers are kept through finalization | Layer 2 (Step 9) | port, pinned by test (marker preservation in the step loop) |
| 7 | Amendment (2026-10-19): fidelity audit owns judgment only, verifier owns counts (#318) | Verifier owns counts; fidelity audit (expert-scout, Haiku) owns judgment only | Layer 2 | port, pinned by test |
| 8 | Amendment (2026-10-15): bare /expert-flow in the ticket worktree (#302) | Bare `/expert-flow` in the ticket worktree auto-detects the issue; resumes silently only in the exact hand-off state | Layer 2 | port, pinned by test |
| 9 | Amendment (2026-10-15): The plan checkpoint is a report and a conversation | Plan checkpoint is a report plus conversation; decision briefs shown through a report | Layer 3 | port, pinned by test |
| 10 | Amendment (2026-10-16): a resumed `OK` the loop never validated is validated before its gate | Resumed `OK` is validated before its gate | Layer 1 | port, pinned by test |
| 11 | Amendment (2026-10-16, second): a hand-finished `OK` is human-attested, not audited | Hand-finished `OK` is `taken-over`; human-attested, no audit | Layer 1 | port, pinned by test |
| 12 | Amendment (2026-10-16, third): step gates are opt-in | Step gates opt-in; `flags.step_gates` restored on resume | Layer 3 | port, pinned by test |
| 13 | Amendment (2026-10-14): per-flow human-input ledger (#287) | Per-flow `human.jsonl` written only by `scripts/flow-ledger.py`; snapshot at spawn; fenced records to allowed workers | Layer 2 | port, pinned by test |
| 14 | Amendment (2026-10-16, fourth): one implementer escalation path (#315) | One escalation path: Retry, Keep previous, Take over, Abort, manual Retry with sonnet; `Retry, split finer` only before escalation; `### Amending §1` sub-rules | Layer 3 | port, pinned by test (manual path). Automatic escalation dropped: dormant (see row 20 and rule 1772-1783) |
| 15 | Amendment (2026-10-20): Redo with notes at the plan and review gates (#286) | Redo with notes at plan and review; cap of two redos and two failures, counted separately; keep-previous | Layer 3 | port, pinned by test |
| 16 | Amendment (2026-10-20): a bounded `implement-ledger.py locate` fails closed on unreadable records | `locate` is bounded and fails closed | Layer 2 | port, pinned by test |
| 17 | Amendment (2026-10-21): relayed questions are counted from disk by attempt; plan/review freshness is a first-attempt identity floor (#310) | Counts from disk by attempt; first-attempt identity floor | Layer 1 | port, pinned by test |
| 18 | Amendment (2026-10-21): multi-round report checkpoints verify across rounds (#335, #340) | Multi-round report checkpoints verify across rounds | Layer 2 | port, pinned by test |
| 19 | Amendment (2026-10-21): Closing retro gaps from a dogfood run (#320) | `--retro` gaps closed | Layer 2 | port, pinned by test |
| 20 | Amendment (2026-10-25): maxTurns look-back (#341) and the turns-exhaustion probe status | Turns look-back; the turns-exhaustion probe was not run; automatic escalation dormant | Layer 3 | dropped: no turns source exists and the probe was never run; automatic escalation stays dormant. Manual retry is kept |
| 21 | Amendment (2026-10-25): Unambiguous post-tier checkpoint ledger (#344) | Post-tier checkpoint ledger is unambiguous | Layer 1 | port, pinned by test |
| 22 | Amendment (2026-10-26): the implement/fix check gate is verifiable and fails closed (#338) | `CHECK`, `CHECK_EXIT`, `CHECK_SHA` keys; `check-not-run` and `check-not-configured`; K=3 fix loop; `checkOptOut` | Layer 3 | port, pinned by test |
| 23 | Amendment (2026-10-26): cwd-drift as a hard stop (#336, #338) | cwd drift is a hard stop | Layer 2 | port, pinned by test |
| 24 | Amendment (2026-11-05): verify, implement and fix check gates run the minimum check (#387) | Verify, implement and fix gates run `checks validate --mode minimum` | Layer 3 | port, pinned by test |
| 25 | Amendment (2026-10-26): Restricted step agents for implement, fix, and review (#358) | `flow-implement` and `flow-review` restricted; prompt-scoped `Write` only for receipts and markers | Layer 3 | port, pinned by test (agent definitions unchanged under the hook freeze) |
| 26 | Amendment (2026-10-27): assess outstanding items before ship (#383, slice 1) | Assess stage: `attempts.assess`, `failures.assess`, `stop_source.assess`, `outstanding_format: 2`, 8-item batches, Sonnet assessor, drop reasons of at least 15 characters; slice 2 (automatic finishing) out of scope | Layer 3 | port, pinned by test |
| 27 | ### Plan step hard-stop on missing agent files (amendment 2026-11-03, #446) | Plan step hard-stops when its agent files are missing; the `flow-assessor` Bash-guard defect is split (see Overlapping-issue dispositions, #446) | Layer 2 | port, pinned by test; the hook defect has been fixed independently (commit f33a8bff, 2026-11-05) |
| 28 | Amendment (2026-10-29): human-attested Accept and continue at the hard stop (#365) | Human-only Accept and continue; default-deny allowlist in `scripts/flow-accept.py`; `accepted (...)` log event | Layer 3 | port, pinned by test |
| 29 | Amendment (2026-10-29): implement/fix receipts name their ledger run (#346) | `RUN_KEY` and `RECEIPT_CONTRACT` in implement and fix receipts | Layer 1 | port, pinned by test |
| 30 | Amendment (2026-10-29): bounded self-heal for check failures and Fix now (#380) | `flow-healer` (one heal per flow, `lint` or deterministic `test` only); Fix now re-spawn; healed run forces the merge gate; `healed` in `state.json` | Layer 3 | port, pinned by test |
| 31 | Amendment 2026-10-30: Issue comments through Ask-based "new comment since plan" relay | Issue comments reach the human through the Ask-based relay | Layer 3 | port, pinned by test |
| 32 | Amendment (2026-11-01): fix-mode coverage reads the Doing-it File cell (#409) | Fix mode reads the Doing-it File cell for coverage | Layer 3 | port, pinned by test |
| 33 | Amendment (2026-10-30): the human ledger is binding (#288) | Directions are binding through `Addressed:`; unaddressed direction is a stop; ledger marks happen only at ship close-out (Q1 = C) | Layer 2 | port, pinned by test |
| 34 | Amendment (2026-11-01): environment chores before the check (#417) | `deps ensure` before worktree, implement, fix and verify; manifest change stops for approval | Layer 2 | port, pinned by test |
| 35 | Amendment (2026-11-02): Pre-run unit sizing and sequential split for multi-unit runs (#419) | `MIN_UNITS` is a lower bound; ceilings of 8 steps, 12 files and 16 findings are provisional; sequential split; `UNIT_CEILINGS:` recorded | Layer 3 | port, pinned by test (ceilings recorded, not enforced, per #325) |
| 36 | Amendment (2026-11-02): one question renderer for flow and fleet (#421) | `scripts/flow-render-questions.py` renders questions for flow and fleet alike | Layer 3 | port, pinned by test |
| 37 | Amendment (2026-11-02) — fleet heal is a further bounded exception (#425) | Fleet heal: one allow-listed `install` per cause in a ticket's worktree; never answers a gate | Layer 4 | port, pinned by test |
| 38 | Amendment (2026-11-04) — Unknown-skill auto-retry is a fifth bounded exception | One automatic retry on an Unknown skill failure, `skill_retries[STEP]`, once per step | Layer 3 | port, pinned by test |
| 39 | Amendment (2026-11-02): resolved File-cell basenames and script-anchored already-done for unknown fix rows (#435) | Resolved File-cell basenames; script-anchored already-done for unknown fix rows | Layer 3 | port, pinned by test |
| 40 | Amendment (2026-11-05, #462): force-push gates run the minimum check; the merge gate is authoritative | Force-push gates run the minimum check; the merge gate runs the full check and is authoritative | Layer 3 | port, pinned by test |
| 41 | Amendment (2026-11-17): human-approved existing-test edits in the fix step (#497) | The fix step sets aside existing-test-edit items, asks per item, and applies approved edits in one approval commit scanned with `--allow-test-edit`; the verifier cross-checks the sha256-pinned answers file | Layer 3 | port, pinned by test |
| 42 | Amendment (2026-11-20): the v2 merge step runs unrestricted (#487) | The v2 merge step runs unrestricted; the human-answered merge gate and the PR-state verification stay in the driver | Layer 3 | port, pinned by test |
| 43 | Amendment (2026-11-20): human-approved existing-test edits and removals in the implement step, with redraft Retry (#511) | The implement step sets aside existing-test-edit and removal items, asks per item in a batched `Test changes` question, and applies approved edits and removals in one approval commit before Round 1; the redraft Retry path resets on approved-test-still-red and redrafts; the verifier cross-checks blob ids and parent invariant; the fix step gains removal items | Layer 3 | port, pinned by test |
| 44 | Amendment (2026-11-23): step agents never end their turn to wait for children (#532) | Step agents and the skills they run collect children in the same turn (foreground return or `wait-files.py` file wait); a child-wait deadline is a FAILED receipt; the top-level orchestrator keeps its end-turn wait | Layer 3 | port, pinned by test (the rule lives in the step commands and `flow-step-contract.md`, which v2 steps run unchanged; `tests/test_step_agent_waits.py`) |
| 45 | Amendment (2026-11-23): zero-unit runs close the implement/fix hatch parity difference (#526) | The implement step no longer stops on a test-only plan; a zero-unit run (`SPLIT_BASIS: zero-units`, `UNITS: 0`) goes straight to Round 2, the fidelity audit does not treat it as a reduced run, and the implement/fix parity table has no remaining intentional difference | Layer 3 | port, pinned by tests/test_zero_unit_wiring.py (ledger render with zero-units, verifier plan cross-check, status and exemption tokens, audit carve-out); the driver does not build the verifier arguments, so its wiring has no test |
| 46 | Amendment (2026-11-24): `/expert-flow --stale` read-only flow classifier | `--stale` classifies flow directories as ACTIVE, STALLED, DEAD or UNKNOWN through `cleanup classify-flows`; read-only, no teardown | Layer 3 | port, pinned by test |
| 47 | Amendment (2026-11-24): every child wait is a background launch plus `wait-files.py` (#553) | No site launches with `run_in_background: false` (the interactive Agent tool has no such parameter); every child is launched in the background, writes a receipt file last, and is collected with `wait-files.py`, keeping per-id retry and stand-ins and adding a deadline at every site | Layer 3 | port, pinned by test (the rule lives in the step commands and `join-barrier-pattern.md`, which v2 steps run unchanged; `tests/test_step_agent_waits.py`, `tests/test_orchestrator_wait_wording.py`) |

Reference for the one `###` section-level amendment tag outside the amendment list:

| Heading (as written) | Rules | Target layer | Disposition |
|---|---|---|---|
| ### 2.2 Receipts are verified against artifacts, not trusted (amendment 2026-10-14, #276) | Receipts are verified against artifacts | Layer 1 | port, pinned by test |

Totals: 43 `##` amendment headings (rows 1 to 26 and 28 to 44), one `###` amendment heading (row 27), and
the section-level `###` amendment tag under ADR section 2.2 (listed above). The ADR's `### Amending §1`
subsection is not an amendment heading and is covered by row 14.
