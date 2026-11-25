# ADR-0018: Parallel planning with checkpoint-based isolation

**Status:** Accepted

## Context

`/expert-plan` (v1) runs experts sequentially in the main thread, accumulating their contributions
to the orchestrator's context. With 5-7 experts per planning session, context balloons quickly —
the user waits 5+ minutes watching an ever-growing conversation before seeing a plan.

Two improvements are desirable but conflict under v1's architecture:

1. **Parallelism** — spawn multiple experts at once, halving wall-clock time
2. **Isolation** — each expert blind to others' input, so reasoning stays independent

Both require subagents, and subagents writing to the orchestrator's context defeats the goal
(context doubles: once in the tool result, again in the completion notification).

## Decision

Create `/expert-plan-v2` as a parallel-only architecture with checkpoint-based communication:

- Each expert runs as a **separate subagent** (isolated from every other)
- Subagents write to **checkpoint files** in `~/.claude/plan-sessions/{slug}/`
- The orchestrator reads only **file paths**, never pasted content
- All subagents return **one-line receipts**, never full reports
- The checkpoint format is specified by `plan-contribution-contract.md` — a parallel input to persona YAMLs for planning roles

**A/B deployment:** Both v1 and v2 coexist. Users choose by preference. v1 remains the default
(`/expert-plan`); v2 is opt-in (`/expert-plan-v2`). No deprecation or retirement of v1.

## Consequences

- **Faster planning** (v2) for teams that can tolerate seeing experts' independent perspectives
  before synthesis (some projects prefer v1's sequential consolidation for single-lens reasoning).
- **Architecture mirrors code review** — `/expert-review`'s subagent blind-first panel
  (`expert-review-panel.md`) is the template; planning borrows its join-barrier pattern and
  "the file is the contract" discipline (documented in `agents/expert-reviewer.md`).
- **Effort ladder** (per ADR-0012) applies to both v1 and v2; v2's implementation covers efforts 1-5
  fully today, including swarm/pod modes for efforts 1-3.
- **Known limitation (issue #122):** v2's final plan path can collide across concurrent runs; fixed
  in v3 — see ADR-0020's "Collision-Resistant Session Paths" section for the mechanism and fix.

## Amendment — Plan Mode guard and multi-checkpoint write restriction (2026-09-10)

Plan Mode, when active in a session, restricts the `Write` tool to a single designated plan file. This works well for v1 (`/expert-plan`), which runs sequentially in the main thread and produces exactly one artifact. v2, however, is fundamentally different: parallel subagents each write their own checkpoint file under `~/.claude/plan-sessions/` (the router writes `selected-experts.md`, each contributor writes `{expert}-contribution.md`, the digest writes `open-questions.md`, synthesis writes `plan.md`, and the alignment pass writes `{expert}-alignment.md`). If Plan Mode were left active when v2 runs, the first write action attempting to create its checkpoint file would hit the single-file restriction and fail, blocking the entire pipeline outright.

**Decision: Fix in place (option B).** v2's Step 0 guard explicitly exits Plan Mode before any subagent work begins (if the invoking session is already in Plan Mode). This guard is documented in detail in `commands/expert-plan-v2.md` Step 0's Plan Mode guard paragraph. The guard's failure-path behavior: if the user declines `ExitPlanMode`, stop cleanly; if `ExitPlanMode` errors after detecting Plan Mode, stop and ask the user to press Shift+Tab and re-run; if no Plan Mode signal is found, proceed without calling the tool.

v2 no longer enters or ends in Plan Mode. Step 6 (the hard-stop checkpoint with `AskUserQuestion`) is v2's human gate, serving the role that Plan Mode serves for v1. The orchestrator's writes are confined to `plan-sessions/` and `~/.claude/plans/` — no code changes at any point. Subagent write-scope enforcement is documented as a prompt convention (residual risk, not a tool-enforced guarantee) in CLAUDE.md's "Panel agents are capability-restricted, not dialog-gated" section.

The guard belongs here (to v2's architecture decision) rather than to a cross-cutting session-management ADR because it is specific to the tension between v2's multi-file checkpoint pipeline and Plan Mode's single-file constraint.

## Amendment — Harness-agnostic barrier waiting (2026-09-23)

**Prior assumption (removed):** Join barriers were documented as if subagents always returned their results in the launching turn — "they have all returned by the time you continue." This assumption held only for older harness versions returning results inline. Observed transcripts (2026-09) show every Agent call returns a launch acknowledgement, with the final report arriving separately: either a hand-back message or a task notification in a later turn. The false premise created confusion about what "waiting" meant.

**Decision: End-turn protocol.** Waiting means ending the turn and re-evaluating per-id state on each return or notification. The receipt (the id's final report) is the first message carrying it — a synchronous result containing the report, an `<agent-message from="{id}">` hand-back, or a task notification whose `<result>` contains the report. A synchronous result that only says the agent was launched ("Async agent launched", "working in the background") is a launch acknowledgement, not a return.

Per-id state progresses: `launched → returned → ok | bad`. The barrier closes when every id is `ok` or stood-in. At most one 1800s status-only `ScheduleWakeup` per phase (one dispatch batch) is permitted as a fallback; it tells the user which ids are missing, never retries or writes stand-ins.

The canonical text lives in `prompts/join-barrier-pattern.md` § "Waiting for the barrier". All commands referencing join barriers now point at that section via a fixed pointer sentence, replacing earlier contradictory inline wording.

**Superseded for subagents and skills (2026-11-23, #532):** step agents and the skills they run no longer end their turn to wait; they launch in the background and block on a foreground `wait-files.py` wait over the children's receipt files (since 2026-11-24, #553; the earlier foreground-return option does not exist in interactive sessions), and never use `ScheduleWakeup` for children. Only the top-level `/expert-flow` orchestrator keeps the end-turn wait. See ADR-0023 amendment 2026-11-23.

Observed in sampled transcripts (2026-09), not a harness guarantee: calls return a launch ack regardless of `run_in_background` flag value, a notification follows for nearly all launched agents, and the flag made no observable waiting difference. The receipt/file/sentinel contract is unchanged.

> **Superseded (2026-11-23, #532):** the observation above is superseded by the #532 probe (`prompts/headless-wait-probe.md`), which found nested Tasks launched with `run_in_background: false` block and return synchronously. See ADR-0023 amendment 2026-11-23. That held only under `claude -p`; interactively the flag does not exist and launches are asynchronous, as observed here (ADR-0023 amendment 2026-11-24, #553).

## Amendment — Third sanctioned write prefix for /expert-spike (2026-10-09)

This section is the single full statement of the rule. It is the reciprocal of ADR-0022 §4 and §13, which introduced the prefix for `/expert-spike`.

**Decision: a third write prefix, scoped to one file per spike role.** Subagents may write to `${PROJECT_ROOT}/spikes/<spike-dir>/`, and only to the single file the orchestrator names in that directory. It applies only to `expert-reviewer` spike roles (contribution, assessment, audit). It is not a blanket `spikes/` grant.

- **`spike-researcher` is excluded.** It has no `Write` tool. It returns its research body, and the orchestrator writes that body to `research/<id>.md` (ADR-0022, 2026-10-09 amendment).
- **`Write` stays prompt-scoped, not tool-scoped.** The residual risk is unchanged (ADR-0022 §4). A hook does not close it, and the sanctioned-prefix list is a convention that the subagent is instructed to follow, as it is for the two existing prefixes (CLAUDE.md, "Panel agents are capability-restricted, not dialog-gated").
- **The PreToolUse hook is optional, per-machine, and a guard rather than a boundary.** The example in `agents/expert-reviewer.md` uses `<PROJECT_ROOT>` as a literal placeholder that the user replaces with an absolute path. The example uses raw `startswith` with no normalization, so it is a shape to adapt, not hardened code. A hardened matcher should resolve the path to an absolute form and compare against the prefix with a trailing `/`, so that `spikes-evil/` and `../` escapes fail.

**Failure modes (documented behavior):**

- **No hook configured:** nothing enforces the `spikes/` prefix. The rule is prompt-level only.
- **Hook configured without the `spikes/` prefix:** expert writes into the spike directory are rejected. The missing artifact leaves that stage as the manifest's resume point, because VERIFY fails (`commands/expert-spike.md`, Verify block). The spike stops visibly at that stage instead of continuing with a gap. State this as the documented behavior; if dogfood shows otherwise, file an issue rather than changing the behavior in the PR that records it.

See ADR-0022 §4 (write prefixes and residual risk) and §13 (relationship to this ADR).

## Amendment — Fourth sanctioned write prefix for /expert-flow flow dirs (2026-10-13)

This section documents the rule for `/expert-flow` step agents' `Write` scope, parallel to the
prior amendments covering `/expert-spike` spikes and the original two prefixes
(`.claude/plan-sessions/` and `.claude/plans/`).

**Decision: a fourth write prefix, scoped to flow-directory files only.** Step agents spawned by
`/expert-flow` may write to `~/.claude/flows/<repo>/<issue>-<slug>-<id>/` — the flow directory
tree the orchestrator creates — and **only to receipt files and answer markers named by the
orchestrator in that directory**. It applies to step agents when they invoke commands like
`/merge-and-cleanup`, `/stack-sync`, and `/implement-with-haiku` with `FLOW_DIR` set (from
the `.claude/flow-run` marker or relay flag). The orchestrator itself writes flow-run markers
(`~/.claude/flows/<repo>/<issue>-<slug>-<id>/.claude/flow-run`), question files
(`<FLOW_DIR>/questions/...`), and answer files (`<FLOW_DIR>/answers/...`).

- **`Write` stays prompt-scoped, not tool-scoped.** The residual risk and lack of a hardened
  PreToolUse hook are unchanged from the spike amendment. The sanctioned-prefix list is a
  convention the step agent is instructed to follow in its prompt (e.g., "Write is used ONLY
  under a validated FLOW_DIR (receipt/question files), never for repo content").
- **Step agents record receipts under `<FLOW_DIR>/steps/`.** The orchestrator names the receipt
  path in the step prompt (e.g., `RECEIPT_PATH="${FLOW_DIR}/steps/03-implement.md"`). The step
  command (`implement-with-haiku`, `merge-and-cleanup`, etc.) documents this in a hard-rule
  sentence near the Resolve FLOW_DIR block.
- **No answer writes from step agents.** Step agents read answer files (`${FLOW_DIR}/answers/*.json`)
  after relay, but never write them. The orchestrator owns answer-file writes (via the `Ask` relay
  pattern in `flow-reference.md`).
- **CLI-owned writes (cross-reference, 2026-11-01).** Writes outside this prefix that the `deps ensure` and `checks` CLI make (the main `project.yaml`, `repo-cache.json`, the deps stamp) and its install subprocesses are not step-agent writes. They are bounded in ADR-0013 Amendment 10.

**Failure modes (documented behavior):**

- **No hook configured:** nothing enforces the flow-directory prefix. The rule is prompt-level only.
- **Hook configured without the flow-prefix rule:** step-agent writes into flow directories are
  rejected. The missing receipt leaves that step as the manifest's resume point, because VERIFY
  fails (the receipt check); the step stops visibly there instead of continuing with a gap.

## Amendment — Fifth sanctioned write prefix for v2 flow dirs (2026-11-17)

This is the reciprocal of ADR-0026 Decision 7, which cites an "ADR-0018 write-prefix amendment for v2". It records the
rule the human approved for the `/expert-flow` v2 driver (ADR-0026, Layer 2 #486).

**Decision: the fourth prefix also covers v2 flow directories.** Step agents spawned by the v2 driver may write to
`~/.claude/flows-v2/<repo>/<issue>-<slug>-<id>/`, the v2 flow directory the driver creates. The rule is the same as the
fourth prefix: flow-directory files only, and only the receipt, marker and question files the driver names in the step
prompt. The fourth prefix is otherwise unchanged. Its `~/.claude/flows/` path still covers v1 flows.

**Conditionality.** This grant is conditional on ADR-0026 acceptance. It takes effect only with that acceptance, and
no hook change accompanies it: no PreToolUse hook is added, widened or re-pointed for `~/.claude/flows-v2/`. Until a
hook is configured, the prefix is enforced by the prompt only, as for the fourth prefix.

- **Scope is the same as the fourth prefix.** Step agents write receipts under `<FLOW_DIR>/steps/`. The driver writes
  the question, answer, `state.json` and `fleet-ask.json` files, and step agents never write them.
- **`Write` stays prompt-scoped, not tool-scoped.** As for the other prefixes, the residual risk is unchanged
  (CLAUDE.md, "Panel agents are capability-restricted, not dialog-gated").
- **v1 flow directories are unaffected.** The v1 root and v1 relay format do not change.

**Scope of this amendment.** It widens the sanctioned prefix in writing. It does not by itself make a real step command
resolve a v2 `FLOW_DIR`. That is the ADR-0026 Step 6 work, and ADR-0026's Layer 2 amendment records its status.

## Pointer — /expert-flow (2026-10-09)

`--flow`/`--epic` handling, the orchestrator's `Write` scope, and the full-capability step agents are recorded in ADR-0023 (§1, §2, §2.1, §5).
