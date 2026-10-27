# ADR-0025: /expert-fleet — Parallel epic execution via headless dispatcher

**Status:** Proposed

## Context

ADR-0023's §4 item 3 chose sequential epic execution: each ticket runs in a flow after the previous one merges. The proof-of-concept `/expert-fleet` dispatcher ran epic #122 (Todoed, decomposed into 9 tickets) from 2026-10-16 to 2026-10-25 with **parallel ticket execution** — up to 3 headless `/expert-flow` processes running at once, their questions batched and asked together, and dependants unblocking as soon as their dependencies merged.

The POC was exercised via fresh launches against base 8e97cbf only through 2026-10-23. After that date, `~/.claude/commands/expert-flow.md` and `prompts/flow-reference.md` pointed at main, so later runs were answer-resumes of POC-era flows running against main's current text. This ADR documents the landed implementation and the gaps known at landing.

## Decision

### 1. Layering

`/expert-fleet` is a layer over `/expert-flow`, which is a layer over the individual lifecycle commands. Each layer stays independently usable: `/expert-flow` without `--fleet` behaves exactly as before, and every lifecycle command still runs on its own. The diff verifies this: every expert-flow change is conditional on `--fleet` / `flags.fleet`.

### 2. The model

`scripts/fleet.py` launches one headless `claude -p` process per sub-issue (`claude_cmd` function, `--output-format json`, stdin closed) running `/expert-flow <N> --fleet` or `/expert-flow --resume <N> --fleet`. Up to `max_parallel` tickets run at once (default 3, enforced by `ready()` function), and dependants launch once their deps are merged or CLOSED on GitHub.

A per-ticket `flock` allows at most one runner per ticket, and `main.lock` serializes fresh worktree creation from the main worktree. A ticket that needs the human **parks** by writing `fleet-ask.json` in its flow directory and exiting. The flow's orchestrator is the single writer of `fleet-ask.json`; `fleet.py` only reads it.

Each flow has one open ask at a time. The `/expert-fleet` session batches open asks to the human and resumes the ticket's own session with `fleet.py run --message-file`. For the `fleet-ask.json` schema and lifecycle, see `commands/expert-flow.md` "Fleet mode"; this ADR does not copy it, as siblings (#365, #367, #370) will change it.

Delivery of answers to tickets is not exactly-once: a crash between consuming an answer and closing the ask can leave a ticket re-parked on the same question. This is recorded in the catch-all issue #381.

### 3. Merge gate: split enforcement

The gate is split into two halves:

- **Code-enforced:** `fleet.py`'s `claude_cmd` function builds argv without `--auto-merge` (it is never added). The `/expert-flow` parse block rejects `--fleet` together with `--auto-merge`. This is pinned by `tests/test_fleet_invariants.py`.
- **Prose-enforced:** the merge gate parks as a `gate` ask. The answer that reaches the ticket is text composed by the `/expert-fleet` model session from the human's reply. No code distinguishes a human-chosen Merge from one the model wrote. "A fleet run never answers for the human" is a prose rule. Resume rule 9 (resume forcing `auto_merge` false) is also prose-enforced.

Claim nothing stronger than ADR-0023 §4 item 2 about merge-gate enforcement; it does not require code proof.

### 4. Permissions

The default is `auto`, and `auto`, `acceptEdits`, `bypassPermissions`, and `dontAsk` are all selectable at `fleet.py init`. Ticket bodies are attacker-influenceable input to unattended agents (same threat class as ADR-0008/ADR-0018). `bypassPermissions` is **unsupported, at your own risk** (see issue #382). `/expert-fleet`'s own `fleet.py init` call passes no `--permission-mode`, so it defaults to `auto`.

### 5. Never idle while a step runs (known workaround)

The 540-second `fleet.py wait` loop is a known workaround. Headless sessions abort their background agents about ten minutes after the session's own turn goes idle (observed 2026-10-16: a plan step killed at exactly 10:00). In fleet mode, the orchestrator calls `fleet.py wait` in the foreground after every step launch and `SendMessage`, repeating until it prints `settled` or a completion notification arrives. This is not a constraint on future implementations; it sits beside the "headless step agents idle" gap (see §6).

### 6. Known gaps

The table below lists gaps fixed elsewhere (in numbered sibling issues or the catch-all) and gaps deferred to later work. Parent epic #371. None are fixed in this landing; all are marked `not fixed here`.

| Gap | Issue | Qualifies |
|-----|-------|-----------|
| Accept-and-continue at hard-stop gates | #365 | "Not exercised end to end" if untested |
| Ledger enforcement | #288 | hard-stop and merge-gate answers via prose |
| Non-code plan obligations | #366 | `depends-on` linking, external merges |
| Review/triage not reading human answers; commits after review unreviewed; test bloat | #367 | plan/review answers reaching code decisions |
| Shared test DB / cross-ticket safety / sibling cross-checks; external merges and dynamic dependency edges | #368 | cross-ticket consistency; #368 covers both |
| Plan/review accuracy (wrong line refs, line-number refs rotting, report renderer dropping D items) | #369 | cited in ADR-0025; see "Symbols over line numbers" in Consequences |
| Mandatory Validate phase and fact-check scout | #370 | answer verification before proceeding |
| `bypassPermissions` on unattended agents | #382 | prose-enforced merge gate; default is `auto` |
| Run-sequence race; unbounded growth of `fleet.json` `runs` and `runs/*.json`/`.err`; `pid_alive` pid recycling; no `schema_version` on `fleet.json`; answer delivery not exactly-once; Pat's write-prefix conflict; `fleet.py --from`/`--plan-file`; CLAUDE.md "Agents" text does not mention fleet's headless processes; global `~/.claude` symlinks as shared mutable state (2026-10-23 `/setup-local` swap); `runs/*.json`/`.err` files written with default umask and may hold model output | #381 | all unticketed items; each a separate concern |
| **Fleet mode on main: not exercised end to end** | none | Validation counts only when at least one fleet child is launched **fresh** on main and reaches its first park. Unknown include whether `flow-verify-receipt.py --flow-dir` accepts a plan report shown through `fleet-report-<SEQ>.md`, and how the main-only ask sites from the Fleet mode section behave (specifically: resume rule 3b and hard-stop item 4 (Keep previous attempt), both reachable via `--resume --fleet` on a flow with prior redo history and covered only by the general Park rule; resume rule 0 and the redo-note follow-up are unreachable under `--fleet` because the argument parser rejects `--redo`). |
| Headless step agents are `general-purpose` with full capability (ADR-0008 standing exception) | none | Noted. In-flight #358 may narrow this. |

## Consequences

- Each `/expert-flow` invocation holds zero knowledge of the others. Ticket order, dependencies, and the inbox are fleet-level orchestration, not baked into any step.
- Prose rules (never answer for the human, merge gate from human reply) are enforced by review, not by code. #367 owns the testing of answer replay and the audit of how answers reach decision points.
- Headless step agents are `general-purpose` Task subagents (per ADR-0023 §1) and run with full capability, including `Write` and full-capability `Bash`. This inherits ADR-0008's residual risk: a subagent could write outside the flow directory despite prompt-level scoping. The risk is stated as a prerequisite for understanding that `/expert-fleet` cannot sandbox step work more strictly than `/expert-flow` does.
- Symbol names, not line numbers, are cited in the known-gaps table and in the ADR itself (e.g. `claude_cmd`, `ready()`, "resume rule 9") so later edits to those symbols do not rot the references. #369 tracks this as a cross-ADR concern.

## Amendment (2026-10-26) — landing preparation

This ADR documents the implementation landing on main at a known state:

- New files: `commands/expert-fleet.md` (174 lines), `scripts/fleet.py` (499 lines), `tests/test_fleet.py` (196 lines), verbatim from the POC base 8e97cbf. `fleet.py` stays byte-identical; the test suite adds `tests/test_fleet_invariants.py` (new file) to pin parser and default-mode invariants.
- Fleet mode ported onto `commands/expert-flow.md` with every cross-reference re-pointed to main's step and rule numbers. All changes are conditional on `FLEET` / `flags.fleet`.
- `tests/test_expert_flow.py` changes: `fleet.py wait` added to `READ_ONLY_BASH`; POC regression test added; no `NO_GATES` count changes.
- ADR-0023 pointer at §4 item 3 (sequential epic execution is a separate layer; `/expert-fleet` is parallel; `--epic` itself stays sequential).
- ADR-0019 pointer after the "no headless invocation" amendment (a headless caller now exists; questions park to `fleet-ask.json`).
- Two follow-up issues: #381 (catch-all for unticketed gaps) and #382 (`bypassPermissions` risk).
