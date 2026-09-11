---
description: Focused isolated-expert planning (v3) — v1's cheap main-thread orchestration and dependency-ordered synthesis, with genuinely isolated per-expert contributions and a mandatory consistency check instead of v2's pods/router/digest pipeline. Effort 2 (default) or 3 (+ one independent auditor) only.
argument-hint: [--effort 2|3] [--models balanced|opus]
allowed-tools: Bash(ls:*), Bash(find:*), Bash(gh issue view:*), Bash(gh api:*), Bash(git log:*), Bash(git branch:*), Bash(mkdir:*), Bash(cp:*), Bash(date:*), Bash(python3:*), Read, Glob, Grep, Task, Write, AskUserQuestion, ExitPlanMode
model: sonnet
---

# Expert Plan v3

Focused isolated-expert planning: effort 2 (default, 3 experts + consistency check) or 3 (+ independent auditor).

## Why v3 alongside v2 and v1?

Real A/B data showed: `/expert-plan` (v1) costs $4.37 for a 92-line plan but allowed indeterminate-status decisions to drift between steps. `/expert-plan-v2` effort 4 costs $13.39 and its alignment pass catches cross-domain risks (a probe assumed side-effect-free, an FFI timeout budget) — but at 3x the cost, with fixes appended rather than reconciled. v2 effort 2 cost almost the same as v1 ($4.20) but used 39% more tokens, because its "pods" still load full shared context per pod and gain nothing from isolation.

This command combines: v1's cheap main-thread orchestration and dependency-ordered synthesis, v2's genuine per-expert isolation (no pods), and Carl's second-pass challenge — but drops v2's router/digest/synthesis subagents and its 5-level ladder in favor of **two effort levels** (2 = focused baseline; 3 = baseline + independent auditor). Goal: get a working, opt-in v3 that is cheaper than v2 effort 4 and more internally consistent than v1, without regressing v1's cost floor.

v3 is an alternative approach pending evaluation; v2 remains the comprehensive reference implementation for the full 5-level ladder.

## Model Policy

The command itself runs at `model: sonnet` (fixed in the frontmatter — this is orchestration work only: gathering context, picking experts, building the decision index, copying the final plan). The main thread does **no judgment work** — it routes, organizes, and coordinates. Every genuinely judgment-heavy step is dispatched to its own one-shot Opus subagent:

- **Step 6 — Synthesize**: reads all contributions + decisions, writes the plan template. Dispatched Opus subagent.
- **Step 7 — Consistency check**: reads plan + all context, fixes requirement/decision tracking and error handling agreement. Dispatched Opus subagent. This is a self-check, not an independent audit; the receipt explicitly says so.
- **Step 8 — Audit** (effort 3, or effort-2 escalation): independently checks requirement fidelity, assumption validity, contradictions, verification adequacy. Dispatched Opus subagent.

Contributors (Step 3) and Carl (Step 4) are dispatched according to `--models`:
- `--models balanced` (default): contributors run Sonnet unless marked as needing Opus (Step 2 assessment). Carl, Synthesize, Consistency-check, and auditor (if applicable) are Opus.
- `--models opus`: escalates every dispatched subagent (contributors, Carl, Synthesize, Consistency-check, auditor) to Opus. Note: this flag does NOT and mechanically CANNOT escalate the main-thread orchestration shell itself (the command's frontmatter `model: sonnet` is fixed before the command body runs). If the user wants the main shell on Opus too, they switch their own session's model before invoking the command.

## Arguments

- `--effort <2|3>`: effort level. Effort 2 (default): 3 focused experts + consistency check, no independent auditor. Effort 3: baseline + independent auditor. Effort 1, 4, and 5 are not supported — v3 does not implement them. If `--effort` is omitted, defaults to 2.

- `--models <balanced|opus>`: model tier for dispatched contributors and roles. Balanced (default): contributors Sonnet-by-default-unless-marked-difficult, Carl/Synthesize/Consistency/Auditor always Opus. Opus: all subagents escalated to Opus. The main-thread orchestration shell remains Sonnet (fixed by frontmatter).

Reject `--effort 1`, `--effort 4`, `--effort 5`, and any unknown flags with a one-line error naming the two supported levels.

A GitHub issue URL can be passed as a positional argument (same as v2); the command resolves the ticket and proceeds.

Note: `--view` (a summary presentation mode) is deliberately deferred — not built in v3 yet. This is a visible deferral, not a silent gap.

## Checkpoint Files

All artifacts live in `{SESSION_DIR}` = `~/.claude/plan-sessions/{REPO_KEY}/{SLUG}-{INVOCATION_ID}/`
(persists across reboots; one subfolder per **invocation** — the invocation ID ensures two overlapping runs on the same ticket never collide):

| File | Written by | When |
|------|-----------|------|
| `context.md` | Orchestrator (Step 1) | Requirements, constraints, scope, unknowns |
| `selected-experts.md` | Orchestrator (Step 2) | Expert names, concerns, model assignment, reasoning |
| `{expert}-contribution.md` | Each contributor (Step 3) | One per selected expert, requirements/risks/approach/open-questions |
| `contrarian-carl-contribution.md` | Carl (Step 4) | After seeing all Step 3 contributions, checks cost and unverified premises |
| `decisions.md` | Orchestrator (Step 5) | Decision index and checkpoint results |
| `plan.md` | Synthesize (Step 6), refined by Consistency-check (Step 7) | Final synthesized plan in template form |
| `audit.md` | Audit (Step 8, optional) | Findings on requirement fidelity, assumptions, contradictions, adequacy — or "No findings." |

Final deliverable: `~/.claude/plans/{SLUG}-{INVOCATION_ID}.md` — the plan copied by the orchestrator to its final path (outside `plan-sessions/`), with the invocation ID included to prevent collisions.

## Plan Mode (guard and reconciliation)

Unlike v1, this command does **not** call `EnterPlanMode`. v3's whole mechanism is subagents writing checkpoint files in `~/.claude/plan-sessions/` — if Plan Mode were active, the first subagent that tried to write its checkpoint would fail.

**If the invoking session is already in Plan Mode**, Step 0 (the Plan Mode guard, documented below) checks for this explicitly and exits Plan Mode deterministically before any subagent work begins. The checkpoint at Step 5 (reading decision-index entries and asking for material questions only) is v3's human-in-the-loop gate — the real decision point before Synthesis and Consistency-check run.

---

## Instructions

### Step 0: Setup & Plan Mode Guard

**Plan Mode guard (first action):** Check the harness's Plan Mode system message for this turn and record the result once as `WAS_IN_PLAN_MODE` (0 or 1). This predicate is the Plan Mode system message itself, never `ExitPlanMode` tool availability.

If `WAS_IN_PLAN_MODE=1`: explain the situation to the user in a chat message (without writing files), then call `ExitPlanMode` right away:

"This session is already in Plan Mode. `/expert-plan-v3` is a planning pipeline that writes working artifacts (context, expert contributions, decisions, synthesized plan) to `~/.claude/plan-sessions/` and its final deliverable to `~/.claude/plans/{slug}-{invocation-id}.md`. The `ExitPlanMode` dialog will appear — approve to exit Plan Mode and proceed with the pipeline. When the dialog appears, pick an option that does **not** clear context (e.g., 'Manual Edit Approval' or 'No', not 'Clear Context'). The pipeline's own Step 5 checkpoint is the real decision gate before synthesis."

Wait for the user's approval. If they decline, stop cleanly (no telemetry yet). When they approve, proceed.

**Failure-path behavior:** When `WAS_IN_PLAN_MODE=1` and the guard calls `ExitPlanMode`:
  - If the user **declines** the dialog: stop cleanly (no telemetry call needed).
  - If `ExitPlanMode` **errors**: report "Plan Mode is still active. Press Shift+Tab to switch modes, then re-run `/expert-plan-v3`" and stop.

Only when `WAS_IN_PLAN_MODE=0` from the start does the run proceed without calling `ExitPlanMode` at all.

Then set up telemetry and parse arguments:

```bash
set -euo pipefail

# Mark command start for telemetry timing
python3 "$HOME/.claude/scripts/run-metrics.py" command-begin --command expert-plan-v3 2>/dev/null || true

# Derive slug from ticket title (or default)
TICKET_TITLE="${TICKET_TITLE:-plan}"
SLUG=$(printf '%s\n' "$TICKET_TITLE" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/-\+/-/g; s/^-\|-$//' | cut -c1-50)
[ -n "$SLUG" ] || SLUG="plan"

# REPO_KEY identifies the repository
PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
REPO_KEY=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null | tr '/' '-')
[ -z "$REPO_KEY" ] && REPO_KEY=$(basename "$PROJECT_ROOT")

# Generate collision-resistant invocation ID
INVOCATION_ID="$(date +%Y%m%dT%H%M%S)-$(printf '%05d' $RANDOM)"

# Create session directory
SESSION_DIR="$HOME/.claude/plan-sessions/${REPO_KEY}/${SLUG}-${INVOCATION_ID}"
mkdir -p "$SESSION_DIR"

# Final plan output path (includes invocation ID to prevent collisions)
FINAL_PLAN_PATH="$HOME/.claude/plans/${SLUG}-${INVOCATION_ID}.md"

# Print the final path immediately
echo "Plan will be written to: $FINAL_PLAN_PATH"

# Parse --effort and --models flags
EFFORT=2
MODELS="balanced"

if [ $# -gt 0 ]; then
  for arg in "$@"; do
    case "$arg" in
      --effort=*)
        EFFORT="${arg#--effort=}"
        ;;
      --effort)
        # Handled by next iteration
        ;;
      --models=*)
        MODELS="${arg#--models=}"
        ;;
      --models)
        # Handled by next iteration
        ;;
      *)
        # Skip; will process flags properly in second pass
        ;;
    esac
  done

  # More careful parsing for space-separated flags
  i=1
  while [ $i -le $# ]; do
    eval "arg=\${$i}"
    case "$arg" in
      --effort)
        i=$((i + 1))
        if [ $i -le $# ]; then
          eval "EFFORT=\${$i}"
        fi
        ;;
      --models)
        i=$((i + 1))
        if [ $i -le $# ]; then
          eval "MODELS=\${$i}"
        fi
        ;;
    esac
    i=$((i + 1))
  done
fi

# Validate --effort
case "$EFFORT" in
  2|3)
    # Valid
    ;;
  *)
    echo "ERROR: --effort must be 2 or 3, got '$EFFORT'" >&2
    python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage gather-context --outcome interrupted 2>/dev/null || true
    python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command expert-plan-v3 --outcome interrupted 2>/dev/null || true
    exit 1
    ;;
esac

# Validate --models
case "$MODELS" in
  balanced|opus)
    # Valid
    ;;
  *)
    echo "ERROR: --models must be 'balanced' or 'opus', got '$MODELS'" >&2
    python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage gather-context --outcome interrupted 2>/dev/null || true
    python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command expert-plan-v3 --outcome interrupted 2>/dev/null || true
    exit 1
    ;;
esac

# Export for use in subsequent steps
export SESSION_DIR REPO_KEY SLUG PROJECT_ROOT EFFORT MODELS FINAL_PLAN_PATH INVOCATION_ID

python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage gather-context >/dev/null 2>&1 || true
```

### Step 1: Gather Context

Collect the input to plan against:

1. **Ticket/requirement**: One of:
   - GitHub issue URL → fetch with `gh issue view <url> --json title,body,labels,comments`
   - User-provided description in the conversation
   - Ask user if neither is available

2. **Project context** (best-effort):
   - `.claude/project.yaml` — ADRs, tech stack, invariants, terminology
   - `CLAUDE.md` — project conventions and constraints
   - Recent git history (`git log --oneline -20`)

3. **Summarize** what you've gathered:
   - **Goal**: What the ticket wants achieved (1-2 sentences)
   - **Constraints**: What ADRs, invariants, or project rules apply
   - **Unknowns**: What the ticket leaves ambiguous or unspecified

Write `{SESSION_DIR}/context.md` with the requirements, explicit user constraints verbatim, relevant existing behavior with file refs, known unknowns, and starting scope.

On failure (cannot read ticket, context load times out): emit failure telemetry and stop.

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage gather-context --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage select-experts >/dev/null 2>&1 || true
```

### Step 2: Select Panel (Main Thread, No Router Subagent)

Read `~/.claude/reviewers/index.yaml` directly. Using a coverage checklist as a mental rubric — user-visible behavior, domain/data assumptions, contracts/types, trust/side effects, integration, failure behavior — pick 3 experts for effort 2. Carl is always added, always last, never counted as a domain specialist.

For any contributor whose task looks like novel architecture, concurrency, security-sensitive trust, or an irreversible migration, record `model: opus` for that one contributor with a one-line reason; everyone else gets the `--models` default (sonnet unless `--models opus`).

Write `{SESSION_DIR}/selected-experts.md` (expert, concern, model, reason). Example structure:

```markdown
# Selected Panel (Effort 2)

## Experts

| Expert | Concern | Model | Reason |
|--------|---------|-------|--------|
| North Star Nick | Architectural alignment and ADR fit | sonnet | Scope clarification |
| Tara TypeSafe | Contract and boundary design | sonnet | State invariant tracking |
| Security Sage | Trust boundaries and failure modes | sonnet | Input validation surfaces |
| Contrarian Carl | Cost, premises, and smaller-is-better | opus | Always present, fresh pass over all input |
```

Tell the user who's participating — no approval gate for ordinary selection.

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage select-experts --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage expert-contributions >/dev/null 2>&1 || true
```

### Step 3: Dispatch Contributors (Parallel, Isolated, One Message)

For each selected expert (not Carl), dispatch one `Task` call, `subagent_type: "expert-reviewer"`, all in a single message per the join-barrier pattern (`~/.claude/prompts/join-barrier-pattern.md`). Each prompt names: the persona YAML path, `~/.claude/prompts/plan-contribution-contract.md`, `{SESSION_DIR}/context.md`, and its output path `{SESSION_DIR}/{expert}-contribution.md`. Model per Step 2's record.

No proposed design, no other expert's report, no digest. Expected receipt format (from the contract):
```
{expert}-contribution.md written — {n} requirements, {n} risks, {n} open questions
```

**Join barrier.** All Step 3 agents launched in one message with `run_in_background: false` means they return by the time you continue. Apply `~/.claude/prompts/join-barrier-pattern.md`'s pattern: receipt validation, file existence, sentinel (`<!-- contribution-end -->`), retry once on failure, stand-in file on second failure. The orchestrator never hangs.

If a contributor fails after two retries, write a stand-in `{PLAN_SESSION_DIR}/{expert}-contribution.md` with `Decision: FAILED`. Report the missing domain.

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage expert-contributions --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage contrarian >/dev/null 2>&1 || true
```

### Step 4: Carl (Fresh Agent, Sees Everything)

One `Task` call after all Step 3 contributions land: persona `~/.claude/reviewers/contrarian-carl.yaml` + `~/.claude/prompts/plan-contribution-contract.md` + `{SESSION_DIR}/context.md` + all `{expert}-contribution.md` paths.

Inline addition to the dispatch prompt: "Also examine the cost of the panel's recommendations — which risks already existed vs. which are introduced by a proposed retry/abstraction/API change, and whether a smaller adequate design avoids them; check for shared unverified premises (e.g., treating a probe as a pure read)."

Output: `{SESSION_DIR}/contrarian-carl-contribution.md`. Expected receipt:
```
contrarian-carl-contribution.md written — {n} requirements, {n} risks, {n} open questions
```

Model: Opus (always) unless `--models opus` overrides, in which case it stays Opus anyway.

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage contrarian --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage checkpoint >/dev/null 2>&1 || true
```

### Step 5: Decision Index and Checkpoint (Main Thread)

Main thread reads all contribution files once, builds a compact decision index directly. When experts propose materially different scopes, present them side by side with concrete differences (extra behavior, added components, affected repos, testing burden) rather than picking a default silently.

Present to the user:
1. **Decision index** — a summary of expert recommendations, disagreements, and scope options
2. **Every original expert block** — one passage per expert, including Carl, showing their full input

Use `AskUserQuestion` for 2-4-option questions; markdown + conversation for open-ended ones or themes with >4 questions. Wait for answers.

Write `{SESSION_DIR}/decisions.md` with the decision index and checkpoint results.

If the user declines to continue at this checkpoint, emit:
```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage checkpoint --outcome interrupted 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command expert-plan-v3 --outcome interrupted 2>/dev/null || true
```
Then exit cleanly.

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage checkpoint --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage synthesize-plan >/dev/null 2>&1 || true
```

### Step 6: Synthesize (Dispatched Opus Subagent)

One `Task` call, `subagent_type: "expert-reviewer"`, `model: opus`, role prompt `~/.claude/prompts/plan-synthesize.md`, given:
- `{SESSION_DIR}/context.md`
- All `{expert}-contribution.md` files (by path)
- `{SESSION_DIR}/contrarian-carl-contribution.md`
- `{SESSION_DIR}/decisions.md`

The subagent writes `{SESSION_DIR}/plan.md` using this template:

```markdown
## Goal
## Selected Scope
## Decisions Made
## Approach
## Implementation Steps
## Risks and Mitigations
## Testing Strategy
## Out of Scope
## Requirement and Decision Coverage
## Open Items
```

Expected receipt: `plan.md written — {n} implementation steps` (no file content in the message).

Main thread copies nothing here; subagent writes within its checkpoint dir.

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage synthesize-plan --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage consistency-check >/dev/null 2>&1 || true
```

### Step 7: Consistency Check (Always, Dispatched Opus Subagent — Self-Check, Not Audit)

One more `Task` call, `subagent_type: "expert-reviewer"`, `model: opus`, role prompt `~/.claude/prompts/plan-consistency-check.md`, given:
- `{SESSION_DIR}/context.md`
- All `{expert}-contribution.md` files
- `{SESSION_DIR}/contrarian-carl-contribution.md`
- `{SESSION_DIR}/decisions.md`
- The drafted `{SESSION_DIR}/plan.md`

The subagent verifies:
- Every requirement/decision reaches an actual step
- Error/status handling agrees across producer/consumer/test mentions
- No superseded branch survives a later decision
- Optional work stays out of scope

Then applies fixes directly to `plan.md` in place.

Expected receipt: must include the phrase `main-thread consistency check; no independent audit` so the command's final message can quote it verbatim.

The main thread never claims this is a second opinion.

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage consistency-check --outcome success 2>/dev/null || true
```

### Step 8: Independent Audit (Conditional)

**Conditional on effort level:**
- **Effort 3**: Always dispatch one fresh `Task` (`subagent_type: "expert-reviewer"`, `model: opus`, role prompt `~/.claude/prompts/plan-audit.md`) with:
  - `{SESSION_DIR}/context.md`
  - All `{expert}-contribution.md` files
  - `{SESSION_DIR}/contrarian-carl-contribution.md`
  - `{SESSION_DIR}/decisions.md`
  - `{SESSION_DIR}/plan.md`

  It reads original contributions itself. Writes `{SESSION_DIR}/audit.md`: actionable discrepancies with evidence and affected section, or a compact "No findings." result.

- **Effort 2**: No automatic auditor. Escalate the same role prompt, scoped to one concrete question, only if a material disagreement remains unresolved after checking sources, or synthesis introduced a mechanism no expert reviewed. State the question and why existing work can't settle it before spawning it — this is a recorded exception, not silent scope creep. One retry on join-barrier failure, same as contributors.

Stage `audit-plan` is only emitted when Step 8 actually runs (telemetry rule: never emit a stage nobody entered). Document this conditionality explicitly: "effort 3, or a recorded effort-2 escalation."

```bash
if [ "$EFFORT" -eq 3 ]; then
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage audit-plan >/dev/null 2>&1 || true
  
  # Dispatch auditor subagent
  # [Task: role prompt plan-audit.md, reads context + all contributions + plan, writes audit.md]
  # [Receipt format: plan-audit.md written — {n} findings]
  # [One retry on failure; stand-in on second failure]
  
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage audit-plan --outcome success 2>/dev/null || true
fi
```

### Step 9: Repair (Conditional on Step 8 Producing Findings)

Apply audit corrections directly to affected plan sections (never just append a contradicting note). If a correction needs a new user judgment call, ask, then update the plan. Allow exactly one recheck of the specific changed sections; do not loop.

Only emit telemetry if Step 8 ran and found something:

```bash
if [ "$EFFORT" -eq 3 ] && [ -f "$SESSION_DIR/audit.md" ]; then
  # Check if audit found any non-empty findings
  if ! grep -q "No findings" "$SESSION_DIR/audit.md"; then
    python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage repair-plan >/dev/null 2>&1 || true
    
    # [Apply corrections to plan.md, ask user if needed, recheck]
    
    python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage repair-plan --outcome success 2>/dev/null || true
  fi
fi
```

### Step 10: Deliver

Copy the final plan to its output location:

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage present >/dev/null 2>&1 || true

mkdir -p "$HOME/.claude/plans"
if ! cp "$SESSION_DIR/plan.md" "$FINAL_PLAN_PATH"; then
  echo "ERROR: Failed to copy plan.md to $FINAL_PLAN_PATH" >&2
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage present --outcome interrupted 2>/dev/null || true
  python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command expert-plan-v3 --outcome interrupted 2>/dev/null || true
  exit 1
fi
```

Print the path, selected scope, effort, panel + model choices, and review status (one of: "main-thread consistency check only," "targeted independent check: <question>," "independent final audit"). Suggest `/track-and-start` or `/expert-review-plan` as next steps.

Example closing message:

```
✅ Plan complete.

📄 Plan: $FINAL_PLAN_PATH

Panel: [list of experts + models]
Effort: 2 (baseline + consistency check) | 3 (+ independent audit)
Review status: [consistency check only | targeted escalation: <question> | independent final audit]

Next steps:
  - `/expert-review-plan $FINAL_PLAN_PATH` — validation pass (optional)
  - `/track-and-start $FINAL_PLAN_PATH` — create issue branch and worktree for implementation
```

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage present --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command expert-plan-v3 --outcome success 2>/dev/null || true
```

### Every Exit Path

Every code path that CAN reach an exit must emit failure/interrupted telemetry before stopping. Examples:
- User declines Plan Mode guard → `stage-end --outcome interrupted`, `command-end --outcome interrupted`
- User declines checkpoint at Step 5 → `stage-end --outcome interrupted`, `command-end --outcome interrupted`
- Contributor fails after retry → `stage-end --outcome failure`, `command-end --outcome failure`

---

## All Stage Names (for telemetry pairing check)

These stages must each appear in a `stage-begin`/`stage-end` pair:
- `gather-context`
- `select-experts`
- `expert-contributions`
- `contrarian`
- `checkpoint`
- `synthesize-plan`
- `consistency-check`
- `audit-plan` (conditional, effort 3 only or escalation)
- `repair-plan` (conditional, only if Step 8 found findings)
- `present`

---

## References

This command references four role prompts:
- `~/.claude/prompts/plan-contribution-contract.md` — output-format contract read alongside a persona YAML by each contributor and Carl
- `~/.claude/prompts/plan-synthesize.md` — role prompt for Step 6 (Synthesize subagent)
- `~/.claude/prompts/plan-consistency-check.md` — role prompt for Step 7 (Consistency check subagent)
- `~/.claude/prompts/plan-audit.md` — role prompt for Step 8 (Audit subagent, effort 3 or escalation)

Subagents in this command run as `subagent_type: "expert-reviewer"`, exactly like `/expert-review` reviewers. The agent has `permissionMode: bypassPermissions`, no `Edit` tool, no write-capable Bash — it can only Read/Grep/Glob/Write-one-file.

---

## Out of Scope — Do Not Do These

- Do not implement v1/v2 retirement or deprecation banners in this command.
- Do not implement prior-plan-session cache reuse — every run is fresh.
- Do not build `--view summary` mode — it's deliberately deferred as a future feature.
