# ADR-0025: /expert-fleet — Parallel epic execution via headless dispatcher

**Status:** Proposed

## Context

ADR-0023's §4 item 3 chose sequential epic execution: each ticket runs in a flow after the previous one merges. The proof-of-concept `/expert-fleet` dispatcher ran epic #122 (Todoed) from 2026-10-16 to 2026-10-25 with **parallel ticket execution** — up to 3 headless `/expert-flow` processes running at once, their questions batched and asked together, and dependants unblocking as soon as their dependencies merged. Per the author's POC notes, the dispatcher was exercised via fresh launches only through 2026-10-23. This ADR documents the landed implementation and the gaps known at landing.

## Decision

### 1. Layering

`/expert-fleet` is a layer over `/expert-flow`, which is a layer over the individual lifecycle commands. Each layer stays independently usable: `/expert-flow` without `--fleet` behaves exactly as before, and every lifecycle command still runs on its own. Every expert-flow change is conditional on `--fleet` / `flags.fleet`, so the non-fleet path is intended to be unchanged. No test compares non-fleet behaviour before and after the change; that claim rests on review.

### 2. The model

`scripts/fleet.py` launches one headless `claude -p` process per sub-issue (`claude_cmd` function, `--output-format json`, stdin closed) running `/expert-flow <N> --fleet` or `/expert-flow --resume <N> --fleet`. Up to `max_parallel` tickets run at once (default 3, enforced by `ready()` function), and dependants launch once their deps are merged or CLOSED on GitHub.

The cap gates fresh admission only. `ready()` counts `running` tickets against `max_parallel`; an answer resume (`run --message-file`) continues a ticket that was already admitted, so it is exempt from the cap and can briefly run more than `max_parallel` processes at once.

A per-ticket `flock` allows at most one runner per ticket, and `main.lock` serializes fresh worktree creation from the main worktree against other fresh launches only. Nothing checks that a ticket's dependencies are merged into MAIN before its worktree is cut (see §6). A ticket that needs the human **parks** by writing `fleet-ask.json` in its flow directory and exiting. The flow's orchestrator is the single writer of `fleet-ask.json`; `fleet.py` only reads it.

Each flow has one open ask at a time. The `/expert-fleet` session batches open asks to the human and resumes the ticket's own session with `fleet.py run --message-file`. For the `fleet-ask.json` schema and lifecycle, see `commands/expert-flow.md` "Fleet mode"; this ADR does not copy it, as siblings (#365, #367, #370) will change it. (#365 is resolved by ADR-0023's 2026-10-29 amendment.)

Delivery of answers to tickets is not exactly-once: a crash between consuming an answer and closing the ask can leave a ticket re-parked on the same question. This is recorded in the catch-all issue #381.

### 3. Merge gate: split enforcement

The gate is split into two halves:

- **Code-enforced:** `fleet.py`'s `claude_cmd` function builds argv without `--auto-merge` (it is never added). A shell block in `commands/expert-flow.md` rejects `--fleet` together with `--auto-merge`. This is pinned by `tests/test_fleet_invariants.py`.
- **Prose-enforced:** the merge gate parks as a `gate` ask. The answer that reaches the ticket is text composed by the `/expert-fleet` model session from the human's reply. No code distinguishes a human-chosen Merge from one the model wrote. "A fleet run never answers for the human" is a prose rule. Resume rule 9 (resume forcing `auto_merge` false) is also prose-enforced.

Claim nothing stronger than ADR-0023 §4 item 2 about merge-gate enforcement; it does not require code proof.

### 4. Permissions

The default is `auto`, and `auto`, `acceptEdits`, `bypassPermissions`, and `dontAsk` are all selectable at `fleet.py init`. Ticket bodies are attacker-influenceable input to unattended agents (same threat class as ADR-0008/ADR-0018). `bypassPermissions` is **unsupported, at your own risk** (see issue #382). `/expert-fleet`'s own `fleet.py init` call passes no `--permission-mode`, so it defaults to `auto`.

### 5. Never idle while a step runs (known workaround)

The 540-second `fleet.py wait` loop is a known workaround. Headless sessions abort their background agents about ten minutes after the session's own turn goes idle (observed 2026-10-16: a plan step killed at exactly 10:00). In fleet mode, the orchestrator calls `fleet.py wait` in the foreground after every step launch and `SendMessage`, repeating until it prints `settled` or a completion notification arrives. This is not a constraint on future implementations; it sits beside the "headless step agents idle" gap (see §6).

### 6. Known gaps

The table below lists gaps fixed elsewhere (in numbered sibling issues or the catch-all) and gaps deferred to later work. Parent epic #371. None are fixed in this landing; each row's Qualifies column states the disposition.

| Gap | Issue | Qualifies |
|-----|-------|-----------|
| Accept-and-continue at hard-stop gates | #365 | "Not exercised end to end" if untested — resolved by ADR-0023 amendment (2026-10-29) |
| Ledger enforcement | #288 | ADR-0023 amendment (2026-10-30) |
| Non-code plan obligations | #366 | `depends-on` linking (ordering obligations as dependency edges) |
| Post-review commits and test budget | #367 | #367 covers post-review commits, test budget, and fails-at-base proof. Answer replay testing and answer-path audit are untracked gaps in #381 (comment). |
| Shared test DB / cross-ticket safety / sibling cross-checks; external merges and dynamic dependency edges | #368 | cross-ticket consistency; #368 covers both |
| Plan/review accuracy (wrong line refs, line-number refs rotting) | #369 | cited in ADR-0025; see "Symbols over line numbers" in Consequences |
| Mandatory Validate phase and fact-check scout | #370 | answer verification before proceeding |
| `bypassPermissions` on unattended agents | #382 | prose-enforced merge gate; default is `auto` |
| Run-sequence race; unbounded growth of `fleet.json` `runs` and `runs/*.json`/`.err`; `pid_alive` pid recycling; no `schema_version` on `fleet.json`; answer delivery not exactly-once; Pat's write-prefix conflict; `fleet.py --from`/`--plan-file`; CLAUDE.md "Agents" text does not mention fleet's headless processes; global `~/.claude` symlinks as shared mutable state (2026-10-23 `/setup-local` swap); `runs/*.json`/`.err` files written with default umask and may hold model output | #381 | all unticketed items; each a separate concern |
| **Fleet mode on main: not exercised end to end** | none | Validation counts only when at least one fleet child is launched **fresh** on main and reaches its first park. Unknown include whether `flow-verify-receipt.py --flow-dir` accepts a plan report shown through `fleet-report-<SEQ>.md`, and how the main-only ask sites from the Fleet mode section behave (specifically: resume rule 3b and hard-stop item 4 (Keep previous attempt), both reachable via `--resume --fleet` on a flow with prior redo history and covered only by the general Park rule; resume rule 0 and the redo-note follow-up are unreachable under `--fleet` because the argument parser rejects `--redo`). |
| Headless step agents are `general-purpose` with full capability (ADR-0008 standing exception) | none | Noted. In-flight #358 may narrow this. |
| Merge-gate Hold in a fleet strands the ticket and its dependants: the flow is `paused at merge` (reported `held`, not `LAUNCHABLE`), no ask is open, dependants stay `blocked`. Decision: manual hand-off. The human runs `/expert-flow --resume <N>`; the fleet does not re-ask. | #381 (comment) | Decided. Recovery is manual and was undocumented until this landing. The automatic re-ask part is tracked in #381 (comment). |
| A failed launch is relaunched and `run()` exits 0 | #381 (comment) | Not fixed here; `run()` reports success although a launch failed. |
| Answered-but-undelivered asks are re-listed by `inbox`: there is no delivery record | #381 (comment) | A crash between delivery and closing the ask re-lists it. Same family as the exactly-once gap above. |
| `main.lock` covers fresh launches only; a dependency merge is not checked against MAIN before a worktree is cut | none | Guarantee is fresh-vs-fresh only (see §2). Tracked by this table. |
| Report reply (the body of a `FLEET-REPLY #N` block) is passed as a bare positional argv to `claude -p`; the `--` end-of-options terminator is unverified | #381 (comment) | `claude --help` does not document `--`, and a report body that starts with `-` could be read as an option. Relay and gate messages are not affected: they start with a fixed `Fleet answer for` header. Verify on a live `claude` before relying on `--`, or prefix report messages with a fixed line. |
| `gh_issue_closed` maps any lookup failure (auth, network, timeout) to not-closed, so a dependant stays blocked | none | Fail-safe: a dependant is never launched early on an unknown state. Documented; no follow-up scheduled. |
| `run()` re-asks a held ticket if it is launched without a guard: the expert-flow resume rule re-parks the merge gate | #381 (comment) | Same family as the Hold row above. Launched without a guard, `run()` resumes a held ticket and the resume rule re-parks the merge gate. |
| Concurrent `--fleet` telemetry correlation is unchecked | ADR-0023, "Consequences" | Recorded from ADR-0023 "Consequences", "Telemetry correlation (expected, to be confirmed in #252 dogfooding)"; not checked at landing. |
| `init` can raise `FileExistsError` when two fleets are initialised in the same second | none | Narrow window; POC code as landed on 2026-10-26. Tracked by this table. |
| `question_file` / `report_file` are joined to the flow directory without a containment check | #381 (comment) | Names come from `fleet-ask.json`, which the flow orchestrator writes from model-influenced content. |
| `wait` takes its baseline when called, so a receipt that settled before the call never reads as `settled`; each call can burn the full timeout | none | (`--since` baseline) Severity pending a live measurement: LOW if `wait` returns promptly after a step finishes, MEDIUM if it blocks for the full 540s. Tracked by this table. |
| Flocks are not inherited by the `claude` child | #381 (comment) | LOW. Verbatim POC code. |
| No wall-clock bound on `claude -p` or the wait loop | none | Needs new timeout and park-gate behaviour. Tracked by this table. |
| `--max` is not validated | none | One-line fix, deferred at the 2026-10-26 landing to keep `fleet.py` byte-identical. That claim is retired by the 2026-11-02 amendment, so the deferral no longer applies. Tracked by this table. |
| `init` validation raises tracebacks | none | POC code as landed on 2026-10-26; the current code may differ. Tracked by this table. |
| An unreadable `state.json` is treated as "no flow" | none | Narrow window, limited by the flock and pid. Tracked by this table. |
| A stale `session` is kept after an unparsed run | none | POC code as landed on 2026-10-26; the current code may differ. Tracked by this table. |
| `find_flow` counts older flows as progress | none | Low confidence. The byte-identical claim that once blocked the code fix is retired (2026-11-02 amendment). Tracked by this table. |
| `fleet.py` is outside the mypy gate; state is untyped | none | Follow-up ticket. The heal's typed logic lives in `scripts/workflow/heal.py`, which is in the mypy gate; `fleet.py` itself is not. Tracked by this table. |
| `run()` mixes responsibilities; `range(3)` is unexplained | none | Readability refactor of the 2026-10-26 POC code. Tracked by this table. |
| The lock handle is never closed and is undocumented | none | Matters only to in-process callers. Tracked by this table. |
| `wait` and `run` exit codes are undocumented | none | The exit-code contract is written with the follow-up. Tracked by this table. |
| Error and crash paths are untested | none | POC tests as landed on 2026-10-26; tests not yet written. Tracked by this table. |
| `SETTLED` constant is unused in `fleet.py` | none | Not removed at the 2026-10-26 landing. Remove or use when the Hold fix lands. Tracked by this table. |
| Heal nomination evidence is read from ticket-writable `state.json`: `step`, `status`, `stop_reason[step]` and the `stop_key` built from them come from the flow's own `state.json`, which the ticket's flow writes. The probe is read-only and checks the worktree, but a ticket can write the evidence that nominates a tool for its own stop. Only `install` ships, and its own gates still apply (manifest unchanged against the base, one attempt per cause). | none | Accepted residual for the one shipped tool, not a fix. Revisit before a second tool or any automatic retry ships. |

## Consequences

- Each `/expert-flow` invocation holds zero knowledge of the others. Ticket order, dependencies, and the inbox are fleet-level orchestration, not baked into any step.
- Prose rules (never answer for the human, merge gate from human reply) are enforced by review, not by code. Answer replay testing and the audit of how answers reach decision points are untracked gaps in #381 (comment).
- Headless step agents are `general-purpose` Task subagents (per ADR-0023 §1) and run with full capability, including `Write` and full-capability `Bash`. This inherits ADR-0008's residual risk: a subagent could write outside the flow directory despite prompt-level scoping. The risk is stated as a prerequisite for understanding that `/expert-fleet` cannot sandbox step work more strictly than `/expert-flow` does.
- Symbol names, not line numbers, are cited in the known-gaps table and in the ADR itself (e.g. `claude_cmd`, `ready()`, "resume rule 9") so later edits to those symbols do not rot the references. This pattern prevents the kind of line-number rot that #369 addresses (wrong line refs, line-number refs rotting).

## Amendment (2026-10-26) — landing preparation

This ADR documents the implementation landing on main at a known state:

- New files: `commands/expert-fleet.md`, `scripts/fleet.py`, `tests/test_fleet.py`. `scripts/fleet.py` was byte-identical to the POC base 8e97cbf (equals commit 982a75e, this branch's verbatim POC commit) at this landing; that claim is retired by the 2026-11-02 amendment, which changes `fleet.py`. `commands/expert-fleet.md` and `tests/test_fleet.py` are the POC plus review-round edits (not byte-identical). The test suite adds `tests/test_fleet_invariants.py` (new file) to pin parser and default-mode invariants, and `tests/test_fleet_docs_contract.py` (new file) to pin the ADR and command-doc contract.
- Fleet mode ported onto `commands/expert-flow.md` with every cross-reference re-pointed to main's step and rule numbers. All changes are conditional on `FLEET` / `flags.fleet`.
- `tests/test_expert_flow.py` is changed by this landing: commit eb2985b (on this branch, after the verbatim POC commit 982a75e) added the `fleet.py wait` entry to `READ_ONLY_BASH` and a regression `test_result` assertion (about +9 lines versus fa642e2). No `NO_GATES` count changes.
- ADR-0023 pointer at §4 item 3 (sequential epic execution is a separate layer; `/expert-fleet` is parallel; `--epic` itself stays sequential).
- ADR-0019 pointer after the "no headless invocation" amendment (a headless caller now exists; questions park to `fleet-ask.json`).
- Two follow-up issues: #381 (catch-all for unticketed gaps) and #382 (`bypassPermissions` risk).

## Amendment (2026-11-01) — launch preflight: check resolution and deps (#417)

Before launch, `/expert-fleet` runs a check-resolution preflight against the main worktree: `checks ensure-check --worktree <MAIN> --main-worktree <MAIN>`. The preflight lives in `commands/expert-fleet.md`; `fleet.py` is not the gate. ADR-0023's 2026-11-01 amendment defines the chores and the ask-once rules this relies on.

- **Resolved:** nothing is asked and the launch proceeds. A `package.json` `scripts.check` resolves automatically and is written to the main `repo-cache.json` with provenance.
- **Unresolved:** the question is asked once per epic. With `--prepass`, it joins the pre-pass `AskUserQuestion` batch under the header `epic`. On an interactive launch without `--prepass`, it is asked alone. Its options are the `CLAUDE.md` suggestion (labelled unverified, pre-filled only), typing a command, or setting `checkOptOut` with a reason. The answer is persisted with `checks record-check-answer`.
- **Still unresolved after the answer** (no answer, or persistence refused): the fleet refuses to launch and prints the exact line to add to the main `.claude/project.yaml`. A ticket never parks on this question, because tickets resolve the check from main.
- **Single writer.** The preflight is the only writer of the main check configuration for an epic, so no two tickets race on `project.yaml`.
- **Deps chore in each ticket.** Each `/expert-flow --fleet` ticket runs the deps precondition unattended. A `DEPS: current` or `installed` result never parks. A `failed` or `manifest-changed` result parks the ticket (fail closed), and the human answers through the usual fleet relay. Fleet install inherits the outcome of #382.
- When #370's Validate phase lands, this preflight moves there.

## Amendment (2026-11-02) — fleet heal harness (#425)

When a ticket's process exits stopped or failed on a known environmental cause, the fleet may run one allow-listed tool in that ticket's worktree before the stop reaches the human. The code is `scripts/workflow/heal.py` (the `heal probe` and `heal run` CLI, called by `fleet.py` with a fixed argv and `cwd` set to the helpers directory). The hook is `heal_pass()`, called once in `run()` after the launch paths finish and before the pid clears. This amendment records the harness, the one tool that ships, and its bounds.

- **Retired claim.** The 2026-10-26 landing said `scripts/fleet.py` was byte-identical to the POC base. That claim is retired: this amendment changes `fleet.py` (the cap, the heal record, the inbox skip, the status row and the `FLEET` line). The §6 rows that say "verbatim POC code" describe the 2026-10-26 landing, not current code. Rows that deferred a fix because of the byte-identical claim no longer rest on it.
- **Closed table, no model.** Tools live in a closed table in code (`TOOLS` in `scripts/workflow/heal.py`). There is no plugin loader, no chaining, no planning loop and no model call. Matching is deterministic. Structured fields in the flow directory nominate a tool, and a read-only probe of real state confirms it. Free text, including ticket text, never authorizes a run. Each stop produces at most one tool, one run and one record. A stop that matches nothing bubbles as it does today.
- **One tool: `install`.** `install` is a thin wrapper over #417's `ensure_deps`, run with `--base` always set, stored approval stamps ignored and `approve_manifest` off. It runs only when the manifest is unchanged. A heal never approves a manifest. Its statuses map as follows: `installed` is a heal that changed something; `current` is a no-op; `manifest-changed`, `ambiguous`, `skipped` and a missing base are refusals; `failed`, and any unknown status, is a failed heal. Refusals and no-ops are not heals.
- **Approval class.** Each tool declares `permission` and `approval`. A tool that runs new code waits for approval, returns `refused` with reason `awaiting-approval`, and its runner is never called. No such tool ships.
- **Q4: unchanged inputs.** A `harness`-gated tool runs only when each declared input is unchanged against the merge-base, or ships in the claude-helpers arsenal. The arsenal is the claude-helpers code itself, never a file in the ticket's repo, even when the fleet runs on claude-helpers. A changed, unreadable or uncomparable input refuses. **`install` is the one named exception.** It carries `gate: ensure-deps-exception`, and only `install` may carry it; the registry test pins that. Residual: scripts that the package manager runs from lifecycle hooks can change on the branch, because the gate compares named config files only. No later tool gets this exception.
- **Full environment for `install`.** `install` runs with the operator's full environment. Scrubbing it would break registries that need tokens, so scrubbing yields for this tool. The heal code does not read secrets and does not log environment values. The installer and the lifecycle scripts it runs do see the environment. This is an accepted residual, not a claim that the environment is isolated.
- **§3 is preserved: the fleet never answers for the human.** The heal never writes a gate answer, never selects an option, never passes `--approve-manifest`, and never touches the flow ledger or the flow directory. Retry stays the human's choice. The inbox item shows the heal line before the form. A `Retry recommended` line appears only when the gate offers Retry and the tool changed something. It never appears at `manifest-changed`. A finished heal that is not retry-recommended names Take over; an interrupted attempt, and a stop with no heal record, name neither. `inbox()` skips a ticket whose pid is alive, and attaches a heal only when its stop key matches the open ask, so no recommendation sits beside a later gate.
- **W1: attempts only while `failures[STEP] < 2`.** The harness nominates nothing once a step has two failures. The flow-side `failures` accounting is unowned: neither #417 nor this ticket decides it. It must be revisited before any switch to fresh resume, which would otherwise continue a step whose count the flow never reset.
- **D1: write-ahead cap.** The cap is one attempt per cause per ticket, counted at attempt start. `heal_write_ahead()` writes `outcome: attempted` under `update_fleet` before the tool runs. A crash spends the attempt, and the entry renders as "heal interrupted", which is the same bubble-up as today. A refused `heal run` spends the cap, so a refused state is not re-attempted. A probe refusal spends nothing. A `heal_note` line is overwritten at each stop, because the probe is read-only and is re-evaluated every time. Absent `heals` means nothing spent; a malformed entry counts as spent. Refusals are excluded from heal counts everywhere.
- **`after_heal` seam.** `after_heal(result, evidence)` is a pure function that returns the recommendation as data. It returns `retry-recommended` only when the result is healed, the gate offers Retry, the tool changed something, and `last_deps_result` is not `manifest-changed`. Every other row, including refused, failed and no-op, returns `take-over`. No automatic retry ships. A later fresh-resume ticket adds one branch here, the `run()` open-ask guard change, and a stale-ask sentence in `commands/expert-flow.md`'s Fleet mode. It must first prove that resume re-drives without a gate answer, which is unproven today. It depends on the resume path (#424) and must also work wherever a flow continues, not only under `fleet.py`.
- **Override.** `fleet.heal.tools.<name>` is read from the main worktree's `.claude/project.yaml` only, never the ticket worktree. It accepts `enabled` and the tool's declared parameters, with their bounds. It cannot add a tool or any behaviour, and it takes no raw argv. Any failure fails closed: an unreadable file, a parse error, an unknown tool or key, an out-of-range value. No tool runs for that heal, and a fixed line says why. `project.yaml` is per-machine, so the override is not shared with collaborators; defaults live in tracked code.
- **Bounds on the run.** `heal run` gets the probe's effective `timeout_secs` plus a fixed margin. On expiry the CLI receives SIGTERM, a grace period, then SIGKILL, so the installer's process group is cleaned up before the pid clears. Output tails go only to a mode 0600 file under `FLEET/heals/`. They never reach status, the inbox, the summary or a resume message.
- **Causes without a tool.** The ownership table in #425 is diagnosis only for causes 2 to 6: fixed lines that never nominate a tool. Their owners are #417 at launch, or follow-up tickets.
