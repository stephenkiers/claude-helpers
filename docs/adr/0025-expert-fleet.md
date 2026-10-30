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
| Ledger enforcement | #288 | hard-stop and merge-gate answers via prose |
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
| `init` can raise `FileExistsError` when two fleets are initialised in the same second | none | Narrow window; verbatim POC code. Tracked by this table. |
| `question_file` / `report_file` are joined to the flow directory without a containment check | #381 (comment) | Names come from `fleet-ask.json`, which the flow orchestrator writes from model-influenced content. |
| `wait` takes its baseline when called, so a receipt that settled before the call never reads as `settled`; each call can burn the full timeout | none | (`--since` baseline) Severity pending a live measurement: LOW if `wait` returns promptly after a step finishes, MEDIUM if it blocks for the full 540s. Tracked by this table. |
| Flocks are not inherited by the `claude` child | #381 (comment) | LOW. Verbatim POC code. |
| No wall-clock bound on `claude -p` or the wait loop | none | Needs new timeout and park-gate behaviour. Tracked by this table. |
| `--max` is not validated | none | One-line fix, deferred to keep `fleet.py` byte-identical. Tracked by this table. |
| `init` validation raises tracebacks | none | Verbatim POC code. Tracked by this table. |
| An unreadable `state.json` is treated as "no flow" | none | Narrow window, limited by the flock and pid. Tracked by this table. |
| A stale `session` is kept after an unparsed run | none | Verbatim POC code. Tracked by this table. |
| `find_flow` counts older flows as progress | none | Low confidence; the code fix breaks the byte-identical claim. Tracked by this table. |
| `fleet.py` is outside the mypy gate; state is untyped | none | Adding it to the gate conflicts with the byte-identical claim. Follow-up ticket. Tracked by this table. |
| `run()` mixes responsibilities; `range(3)` is unexplained | none | Readability refactor of verbatim code. Tracked by this table. |
| The lock handle is never closed and is undocumented | none | Matters only to in-process callers. Tracked by this table. |
| `wait` and `run` exit codes are undocumented | none | The exit-code contract is written with the follow-up. Tracked by this table. |
| Error and crash paths are untested | none | Verbatim POC tests; tests not yet written. Tracked by this table. |
| `SETTLED` constant is unused in `fleet.py` | none | Not removed here (byte-identical). Remove or use when the Hold fix lands. Tracked by this table. |

## Consequences

- Each `/expert-flow` invocation holds zero knowledge of the others. Ticket order, dependencies, and the inbox are fleet-level orchestration, not baked into any step.
- Prose rules (never answer for the human, merge gate from human reply) are enforced by review, not by code. Answer replay testing and the audit of how answers reach decision points are untracked gaps in #381 (comment).
- Headless step agents are `general-purpose` Task subagents (per ADR-0023 §1) and run with full capability, including `Write` and full-capability `Bash`. This inherits ADR-0008's residual risk: a subagent could write outside the flow directory despite prompt-level scoping. The risk is stated as a prerequisite for understanding that `/expert-fleet` cannot sandbox step work more strictly than `/expert-flow` does.
- Symbol names, not line numbers, are cited in the known-gaps table and in the ADR itself (e.g. `claude_cmd`, `ready()`, "resume rule 9") so later edits to those symbols do not rot the references. This pattern prevents the kind of line-number rot that #369 addresses (wrong line refs, line-number refs rotting).

## Amendment (2026-10-26) — landing preparation

This ADR documents the implementation landing on main at a known state:

- New files: `commands/expert-fleet.md`, `scripts/fleet.py`, `tests/test_fleet.py`. `scripts/fleet.py` is byte-identical to the POC base 8e97cbf (equals commit 982a75e, this branch's verbatim POC commit). `commands/expert-fleet.md` and `tests/test_fleet.py` are the POC plus review-round edits (not byte-identical). The test suite adds `tests/test_fleet_invariants.py` (new file) to pin parser and default-mode invariants, and `tests/test_fleet_docs_contract.py` (new file) to pin the ADR and command-doc contract.
- Fleet mode ported onto `commands/expert-flow.md` with every cross-reference re-pointed to main's step and rule numbers. All changes are conditional on `FLEET` / `flags.fleet`.
- `tests/test_expert_flow.py` is changed by this landing: commit eb2985b (on this branch, after the verbatim POC commit 982a75e) added the `fleet.py wait` entry to `READ_ONLY_BASH` and a regression `test_result` assertion (about +9 lines versus fa642e2). No `NO_GATES` count changes.
- ADR-0023 pointer at §4 item 3 (sequential epic execution is a separate layer; `/expert-fleet` is parallel; `--epic` itself stays sequential).
- ADR-0019 pointer after the "no headless invocation" amendment (a headless caller now exists; questions park to `fleet-ask.json`).
- Two follow-up issues: #381 (catch-all for unticketed gaps) and #382 (`bypassPermissions` risk).
