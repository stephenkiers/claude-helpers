# Proposal: `/expert-flow` — one-ticket (and one-epic) lifecycle orchestrator with a question relay

**Status:** Accepted and implemented (Phases 0–2 and 4 landed in one PR on 2026-10-09; Phase 3
dogfooding — #252 — is in progress). Originally posted as the body of #245 on 2026-10-08 and
moved here when #245 became an epic.
**Date:** 2026-10-08 (updated 2026-10-09)
**Companion ADR:** `docs/adr/0023-expert-flow.md` — numbered 0023, not 0022 as the original text
below says, because `docs/adr/0022-expert-spike.md` took 0022 first.
**Engine decision (from the #246 spike, harness 2.1.296):** subagent-per-step. See
`docs/plans/expert-flow-spike-findings.md`; the constraints it found are summarized in §2 below.
**Prior art in this repo:** `/expert-implement-with-haiku-and-ship` (added 080bdb2, deleted in #71 as
unused; `CLAUDE.md` still lists it — stale). It chained implement → shipit → review from the main
thread and stopped at the first question. It was unused because it solved the wrong half of the
problem: the human's *clearing* was never the bottleneck, the human's *answering* was. This proposal
is about the answering half.

## 1. Goal

Replace this manual loop:

```
/expert-plan <issue>  →  answer questions  →  /clear
/track-and-start <plan>                     →  /clear, cd worktree
/implement-with-haiku                       →  /clear
/expert-review  →  answer rulings           →  /clear
/implement-with-haiku <claude-action-plan>  →  /clear
run tests / measurements
/shipit                                     →  /clear
/merge-and-cleanup
```

with one command, run from the main worktree in a window the human leaves open:

```
/expert-flow <issue-url|number> [--plan-effort 2|3] [--review-effort N] [--auto-merge]
             [--resume <flow-dir|issue#>] [--list] [--epic]
```

that

- runs each step in its **own subagent context window** (so nobody has to `/clear`; the orchestrator
  sees only receipts — ADR-0018),
- **stops only for questions the step would have asked anyway**, surfaced in the orchestrator window
  through the exact `AskUserQuestion` payload the step would have used,
- **stops on any hard failure** with *Retry / Take over / Abort*, never auto-fixes past a failing
  gate,
- can be **paused and resumed from disk** at any step (`--resume`), so the human can `cd` into the
  worktree, work by hand, and hand it back,
- keeps the **merge as a human gate by default** (`--auto-merge` is the opt-in), and
- in `--epic` mode, runs the same ticket flow across an ordered set of sub-issues (sequential first,
  stacked later — see §9).

## 2. Why this is realistic now, and what it hinges on

Verified against the Claude Code docs on 2026-10-08, and **confirmed empirically by the #246 spike on
2026-10-09** (harness 2.1.296) — the four previously-undocumented items below are now facts, not
assumptions:

| Fact | Consequence for the design |
|---|---|
| Subagents get their own context window and auto-compaction | Each step's heavy context lives and dies in its subagent. This *is* the `/clear`. |
| `AskUserQuestion` is stripped from subagents even if listed in `tools` | No step can ask the human directly. **Every** `AskUserQuestion` site in the chain must learn to write its question to disk instead. This is the bulk of the work (§5). |
| A parent can `SendMessage` a finished subagent; it resumes with full history | The step can *end its turn* after writing a question, and be woken with "answers are at `<path>`". No re-running the step, no lost state. |
| Nesting depth limit 3; concurrency cap 20 (env-tunable) | Orchestrator → step → panel/implementer is depth 2. OK, but the panel's concurrency now shares the cap with the orchestrator. |
| Workflow tool: "No mid-run user input" | Rejected as the engine. |
| `claude -p`: `AskUserQuestion` behavior undocumented; background commands capped at 10 min in `-p`; `--max-turns` | Rejected as the engine (reviews run for hours). |
| Background sessions (`claude --bg`, agent view) have a *Needs input* state and notify, but answering a dialog requires attaching | Not the engine, but the natural **"take over" escape hatch**: the worktree is a normal worktree; the human can always open it. |
| Compaction preserves CLAUDE.md, memory, plan-mode plan, 5 recent files; everything else is summarized | The orchestrator must keep **all** flow state in `state.json` on disk, never in conversation. The human may `/compact` the orchestrator window at will. |

Four things were **not** documented and were proven in the Phase 0 spike (#246; §11) before anything
was built — all four held, with the constraints recorded in `docs/plans/expert-flow-spike-findings.md`
(concurrency cap of 20 *includes* the step agent, so a step may launch at most 19 children; a new
command is `Skill`-invocable only after a turn boundary; the orchestrator must verify the answers
file exists before `SendMessage`-resuming a step; #262 tracks the one Q4 wrinkle — the track CLI
lacks an existing-issue pivot, so the step pivots by hand per the command doc):

1. A subagent can invoke a slash command via the `Skill` tool and run it to completion.
2. A step subagent can itself launch the step's own subagents (panel, implementers) — and whether
   the join-barrier's "end the turn, no polling" idiom works one level down, or those children must
   run in the foreground when nested.
3. `SendMessage` to a subagent that ended its turn on a sentinel resumes it and it reads the answers
   file correctly.
4. `/track-and-start --issue N --plan-file P` pivots the issue body with **zero** prompts.

Kill rule as applied by the spike: switch engines on a Q1 fail, a Q3 fail, or a *hard* Q2 fail
(nested children cannot run at all). A soft Q2 result (children must run in the foreground, or the
concurrency cap binds) is a constraint to design around, not a kill. (The original text disagreed
with itself — "1–3" here, "1 and 3" in §11, "1 or 3" in the #246 ticket — and the spike resolved it
as stated.) The fallback engine, had it been needed, was **one `claude --bg` session per step** with
the orchestrator polling `claude agents --json` for `blocked`, and the same disk question protocol
(§5) — slower to build, same contract. The question protocol is engine-independent by design. The
fallback was not needed.

## 3. What exists that this reuses (no reinvention)

| Need | Existing piece | How it's reused |
|---|---|---|
| Orchestrator skeleton, telemetry pairing, invocation IDs | `commands/expert-plan.md` Step 0 + "Every Exit Path" | Copied structurally |
| Orchestrator sees paths + one-line receipts only | ADR-0018 checkpoint isolation | Each step writes `steps/<n>-<name>.md` ending in a sentinel; orchestrator reads only that |
| Sentinel-terminated files, stand-ins on failure, no polling | `prompts/join-barrier-pattern.md` | One step at a time, so N=1; same ok/bad/stand-in rules |
| Project / worktree detection | `prompts/worktree-reference.md` § Project Detection | `REPO_KEY`, `WORKTREE_PARENT`, `MAIN_WORKTREE` |
| Per-worktree local state | `.claude/github-cache.json` (gitignored tier) | New sibling marker `.claude/flow-run` → path of the flow dir |
| Step-to-step handoffs | Already on disk: `~/.claude/plans/<slug>-<id>.md` → `github-cache.issue` → commits → `~/.claude/reviews/<repo>/<dir>/claude-action-plan.md` + `github-cache.review.reviewDir` → `github-cache.pr` | Orchestrator reads the same fields the commands already write; no new handoff format |
| Interrupt-and-resume telemetry | ADR-0019 `command-end --outcome interrupted`, `--resumed-from` | Each question pause closes the step as `interrupted`, resume reopens with `--resumed-from` |
| Pending-ruling drain | `/verify-queue` | "Needs measurement" items are *not* relayed; they stay in the queue exactly as today |
| Machine enforcement of prose rules | ADR-0008; `tests/test_command_doc_shell_conventions.py` | New lint: no bare `AskUserQuestion` in a flow-aware command outside the canonical Ask block |
| Structural tests | `tests/test_expert_plan_v3.py` | Cloned as `tests/test_expert_flow.py` |

## 4. Architecture

```
┌─ main worktree, human's window ──────────────────────────────────────────────┐
│ /expert-flow 123            (Sonnet; dispatch + relay + state, no judgment)  │
│   state.json ◄──────────────────────────────────────────┐                    │
│   │                                                     │                    │
│   ├─ Agent(step=plan)      ──► runs /expert-plan 123 ───┤ receipt / question │
│   ├─ Agent(step=track)     ──► /track-and-start --issue 123 --plan-file …    │
│   ├─ Agent(step=implement, cwd=worktree) ──► /implement-with-haiku           │
│   ├─ Agent(step=review)    ──► /expert-review --effort N --force             │
│   ├─ Agent(step=fix)       ──► /implement-with-haiku <dir>/claude-action-plan.md
│   ├─ Agent(step=verify)    ──► repo-cache.json commands.check (+ measurements)
│   ├─ Agent(step=ship)      ──► /shipit                                        │
│   └─ [human gate: Merge?]  ──► Agent(step=merge) ──► /merge-and-cleanup       │
│                                                                              │
│  question file appears ──► AskUserQuestion(same payload) ──► answers file    │
│                        ──► SendMessage(step agent, "answers at <path>")      │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Orchestrator model:** `model: sonnet` in frontmatter (same reasoning as `/expert-plan`: the main
  thread does no judgment work). Steps inherit their own frontmatter pins (`/shipit` haiku,
  `/expert-review` sonnet shell, etc.) — the flow changes no model choices.
- **One step at a time.** No parallelism inside a ticket. The join barrier degenerates to N=1.
- **The orchestrator never edits code, never runs git mutations.** Its `allowed-tools` are `Task`,
  `SendMessage`, `AskUserQuestion`, `Read`, `Write` (flow dir only, by convention — same residual
  risk CLAUDE.md already documents for panel agents), and read-only `git`/`gh`/`ls`/`cat`. All
  mutations happen inside the steps, under the steps' existing guardrails.
- **Step subagent type:** `general-purpose` (needs `Skill` + the step's own tool surface). The step
  prompt is one line: *"Run `/<command> <args>` to completion in cwd `<path>`. You are inside an
  `/expert-flow` run; FLOW_DIR is `<dir>`. Follow `prompts/flow-reference.md`."*

## 5. The question relay (the load-bearing piece)

New `prompts/flow-reference.md` with three canonical blocks every flow-aware command includes by
reference (lazy-loaded, path-read, never argument-substituted):

**5a. Resolve FLOW_DIR.** Flag `--flow <dir>` wins; else `.claude/flow-run` in the cwd (one line:
absolute path of the flow dir); else *not in a flow* → every block below is a no-op and the command
behaves exactly as today. `/expert-plan` runs before the worktree exists, so it gets the flag;
everything after uses the marker that `/track-and-start` writes into the new worktree.

**5b. Ask.** Replaces every `AskUserQuestion` call site:

```
If FLOW_DIR is unset: call AskUserQuestion as written. Otherwise:
  1. SEQ = next integer under ${FLOW_DIR}/questions/ for this step
  2. Write ${FLOW_DIR}/questions/<step>-<SEQ>.json — the AskUserQuestion input, verbatim
     (questions[], options[], recommended first, multiSelect), plus {"step","context_path"}
     pointing at the artifact the human should skim (plan.md, claude-action-plan.md, …)
  3. Append to the step receipt: "<!-- awaiting-answers: questions/<step>-<SEQ>.json -->"
  4. run-metrics command-end --outcome interrupted; END THE TURN. Do not poll. Do not sleep.
On resume (a message naming ${FLOW_DIR}/answers/<step>-<SEQ>.json exists):
  5. run-metrics command-begin --resumed-from; read answers; continue exactly as if
     AskUserQuestion had returned them.
```

The orchestrator side: sees the sentinel, reads the question JSON, **calls `AskUserQuestion` with
that same payload** (so the human's experience is identical to running the command by hand, plus a
one-line "skim: `<context_path>`" preface), writes `answers/<step>-<SEQ>.json` in the harness's
`answers` map shape, and `SendMessage`s the step agent. Free-text "Other" answers pass through
untouched.

**5c. Receipt.** Every step ends by writing `${FLOW_DIR}/steps/<n>-<step>.md`: the command's
*existing* final summary block (implement-with-haiku's ROUND lines, expert-review's counts,
shipit's PR line) + `Decision: OK | FAILED | AWAITING` + one reason line + `<!-- step-end -->`.
Nothing else crosses into the orchestrator.

**Call sites to convert (from the seam map).** Each becomes a 5b reference; the wording of the
question does not change:

| Command | Site | Becomes |
|---|---|---|
| `/expert-plan` | Step 5 checkpoint (2–4-option Qs) | relayed |
| | Step 5 open-ended / >4-question themes (markdown + conversation) | relayed as one free-text question per theme — **this is the one UX change**; today it's a chat exchange |
| | Step 8 repair ask; missing-ticket ask; plan-mode guard | relayed / orchestrator always supplies the ticket / orchestrator never enters plan mode |
| `/track-and-start` | pivot, duplicate, base-branch | **avoided**: orchestrator passes `--issue N --plan-file P` (skips pivot+duplicate detection today); base-branch only exists in tracker mode |
| `/implement-with-haiku` | failed units Abort/Proceed; incomplete-report menu; conflict on shared file; gate not converged | relayed (first two) / relayed as FAILED-with-options (last two) |
| `/expert-review` | "Re-run anyway?"; "Escalate to opus?"; **Step 12 rulings** (≤4 per call) | `--force` / orchestrator passes explicit `--effort` so the prompt is skipped **except** when the heuristic floors to 4 on a risk keyword — then relay *that* as a question (see memory: effort-4 floor retro) / relayed, one call per batch, edits `claude-action-plan.md` in place exactly as today |
| `/shipit` | force-push ask; ambiguous PR body | relayed / relayed |
| `/merge-and-cleanup` → `/cleanup` → `/stack-sync` | queue-setup offer; non-MERGED confirm; verify-queue `done/defer/ignore`; stack-sync pre-force-push confirm | relayed / relayed / **not relayed** (already defaults to `defer`) / relayed |

**Enforcement:** `tests/test_flow_ask_sites.py` fails if any command listed in `flow-reference.md`'s
roster contains `AskUserQuestion` outside a line that references the Ask block. ADR-0008: prose
constraints on autonomous agents are machine-enforced or they are not constraints.

## 6. State and resume

`FLOW_DIR = ~/.claude/flows/<REPO_KEY>/<issue>-<slug>-<FLOW_ID>/`

```
state.json        {issue, slug, worktree, branch, step, status: running|awaiting|paused|failed|done,
                   attempts{step:n}, plan_path, review_dir, pr, started, updated, flags{...}}
steps/            01-plan.md 02-track.md 03-implement.md 04-review.md 05-fix.md 06-verify.md
                  07-ship.md 08-merge.md   (receipts, sentinel-terminated)
questions/ answers/   <step>-<seq>.json
log.md            append-only one line per transition (human-readable audit)
```

- **Resume** (`--resume <dir|issue#>`) reads `state.json`, then **re-verifies from disk** before
  re-entering the step: worktree exists? `github-cache.issue` present? `review_dir/claude-action-plan.md`
  has no `pending-decision` items? `github-cache.pr.state`? If disk disagrees with `state.json`,
  disk wins and the orchestrator asks one question ("state says *fix*, disk says PR already open —
  continue from *ship*?"). This is what makes **take-over** safe: the human can do anything in the
  worktree by hand and hand back.
- **Pause** is just `status: paused` + the orchestrator ending its turn with the `cd` line printed.
- **Attempts** are capped at 2 per step; a third failure is a hard stop, not a question.
- Flow dirs are listed by `--list` and never auto-deleted (same policy as review dirs).

## 7. Step table

| # | Step | Invocation (inside step agent) | cwd | Hard stop → human options |
|---|---|---|---|---|
| 1 | plan | `/expert-plan <issue> --flow $FLOW_DIR [--effort]` | main worktree | plan context empty / copy failed → Retry · Abort |
| 2 | track | `/track-and-start --issue <N> --plan-file <plan>` then write `<wt>/.claude/flow-run` | main worktree | worktree exists already → Resume there · Abort |
| 3 | implement | `/implement-with-haiku` | worktree | failed units, gate non-convergence → Retry · Take over · Abort |
| 4 | review | `/expert-review --force --effort <N>` | worktree | zero CONFIRMED findings is **not** a stop; skip step 5 |
| 5 | fix | `/implement-with-haiku <review_dir>/claude-action-plan.md` | worktree | pending-decision items remain → relay them first (they should already be answered in step 4) |
| 6 | verify | `repo-cache.json` `commands.check` (fallback: `commands.test` + `lint`); plus any "needs measurement" command the action plan drafted that is runnable without the human | worktree | any non-zero → Retry · Take over · Abort. Never "fix and retry" automatically |
| 7 | ship | `/shipit` | worktree | check failure, rebase conflict (→ `/expert-rebase` is *suggested*, not run) → Take over · Abort |
| — | **merge gate** | `AskUserQuestion`: "PR #N: review clean, checks green. Merge? [Merge · Hold (leave PR open, end flow) · Take over]" | — | always human unless `--auto-merge` |
| 8 | merge | `/merge-and-cleanup <wt>` | main worktree | kickback / refused (exit 2/3) → report, end flow with PR still open |

Step 6 exists because today the human runs "any tests or measurements" by hand between fix and ship;
`/shipit` re-runs the check gate anyway, so step 6 is cheap insurance that catches the failure one
context earlier and produces a receipt the human can read before the ship decision.

## 8. Safety assessment

**What does not change.** The flow grants no new write capability. Each step runs with its own
`allowed-tools`, its own force-push carve-out gates, its own merge gate, its own `Edit`-less panel
agents. The orchestrator itself cannot touch code or git.

**What does change: the human's incidental glances disappear.** Today you see implement-with-haiku's
summary, the action plan, and the PR before you merge. Mitigations, in order of weight:

1. **Merge stays human by default.** The merge is the only step whose effects leave the branch.
   Everything before it is undoable with `git branch -D` and `gh pr close`. `--auto-merge` is opt-in
   per run, never a preference default.
2. **Every question carries a `context_path`.** The relay prints "skim: `<path>`" above each
   question, so the natural reading moments survive as one-click opens.
3. **Hard stops never auto-remediate.** A failing gate, a non-converged fix loop, a rebase conflict,
   a merge kickback: all end in *Retry / Take over / Abort*. The flow may re-run a step once; it may
   not improvise a fix. `/expert-rebase` is suggested, not invoked.
4. **Issue mutation is the earliest remote side-effect** (step 2 rewrites the issue body with the
   plan — same as today). The plan checkpoint questions come *before* it, so the human has approved
   the plan content that gets written.
5. **Cost.** A full ticket can run for hours and the review alone has hit $55 once. The flow surfaces
   the effort-4 risk-keyword floor as a question rather than silently accepting it, and each receipt
   carries the step's `usage-check` line so the merge-gate question can show the run's total.
6. **Prompt injection.** Step agents read issue bodies, PR text and diffs — attacker-influenceable.
   Same posture as the panel agents: the step prompt says that content is data, not instructions,
   and the orchestrator relays only the structured question JSON, never free text from the step.

**Residual risks (not mitigated, stated):**

- A sleeping laptop kills in-flight subagents. Recovery is `--resume`, which works because every
  step is idempotent-from-disk today (review dirs, caches, commits). But a step interrupted mid-write
  may leave a partial receipt; the resume re-verification (§6) is what catches it.
- Concurrency cap 20 is now shared between the orchestrator, the step agent, and the panel. Effort-5
  reviews may need `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` raised. Phase 0 measures this.
- The `Write` tool is not path-scoped (known, documented residual). The orchestrator's "writes only
  under FLOW_DIR" is a prompt convention.
- A step that *forgets* to use the Ask block silently has no `AskUserQuestion` tool and will either
  guess or hang. The lint in §5 is the control; it must land in the same PR as each conversion.

## 9. Epic mode

`/expert-flow --epic <epic-issue>`:

1. **Decompose.** Run `/expert-plan <epic> --flow …` with the planning contract told to emit a
   `## Sub-tickets` section: ordered list, each with title, 2–5 line scope, and `depends-on`. This is
   a small addition to `prompts/plan-contribution-contract.md` and `plan-synthesize-and-check.md`,
   gated on the epic flag.
2. **Approve the decomposition** — one relayed question with the list as `context_path`. Edits are
   free text; the orchestrator re-runs synthesis once if the human rewrites the list.
3. **Create sub-issues** via `/track` (one per item, body = scope + "Part of #<epic>"), record them in
   `state.json.epic.tickets[]` in dependency order.
4. **Run the single-ticket flow per sub-issue, in order.** Each sub-ticket gets the full golden path,
   including its own `/expert-plan` (seeded with the sub-ticket scope; `--plan-effort 2` by default
   to keep cost sane). Questions relay exactly as in §5.
5. **Sequential by default.** Ticket N+1 branches from main *after* ticket N has merged. Because the
   flow already ends each ticket in `/merge-and-cleanup`, a stack buys nothing unless the merge gate
   is held open by the human. The epic-level `state.json` is just the ticket list plus a pointer to
   the current ticket's flow dir, so `/compact` and `--resume` work unchanged.

**`--stack` (Phase 5, optional).** Lets ticket N+1 start on top of ticket N while N waits at the
merge gate ("Hold" at the gate means "continue stacked"). What it needs that does not exist today:

- `/track-and-start` GitHub mode has no `--base`; `track.py` cuts the branch from the main worktree's
  HEAD. Add `--base <branch>`, and have it write `github-cache.stack.parentBranch` at creation rather
  than waiting for `/shipit` to infer it by ancestry.
- The merge queue refuses non-default-base PRs, so stacked children take the direct merge route until
  the parent merges and `/cleanup` → `/stack-sync` restacks them (that path already exists, per-branch
  layout, with its force-push confirmation relayed). Whether the restack also retargets the child
  PR's base to main (`gh pr edit --base`) must be verified; if not, it is a one-line addition to the
  Restack-a-child block.
- Layout `unknown` in `/stack-sync` is a STOP today and stays one: it relays as a hard stop.

Recommendation: ship sequential epics first and watch whether "Hold" is ever chosen. If the human
always merges at the gate, `--stack` is never needed.

## 10. User-facing contract

```
/expert-flow <issue-url|number>
    [--plan-effort 2|3]        default: heuristic (scripts/plan-effort.py), as today
    [--review-effort 1-5]      default: heuristic; a risk-keyword floor to 4 is relayed as a question
    [--auto-merge]             skip the merge gate (never a stored default)
    [--from <step>]            start at a later step for a ticket already in flight by hand
    [--resume <flow-dir|issue#>]
    [--list]
    [--epic]  [--stack]        §9
```

Every stop prints two lines the human can act on without reading anything else:

```
▶️ expert-flow #123 — paused at: review (awaiting 3 rulings)   skim: ~/.claude/reviews/…/claude-action-plan.md
   take over:  cd ~/Repositories/<repo>/worktrees/123-slug     resume: /expert-flow --resume 123
```

## 11. Phasing and kill criteria

**Phase 0 — spike (≈1 day, throwaway branch). Done: #246, merged as #260.** Proved §2's four
unknowns with a stub command that asked one question and spawned one background subagent. Kill
criterion (as applied — see §2): Q1 fail, Q3 fail, or hard Q2 fail switches the engine to
`claude --bg` sessions. None fired; engine = subagent-per-step. Measured: depth 2 works, the
concurrency cap of 20 includes the step agent (19 children max), nested background children work
and their completion notifications re-invoke the step (one hand-back reaches the orchestrator).

**Phase 1 — the relay (2–3 PRs).** `prompts/flow-reference.md` (5a/5b/5c), `tests/test_flow_ask_sites.py`,
then convert call sites command by command, roster growing with each PR so the lint is always
green: `/expert-plan` + `/expert-review` first (they hold the questions you actually answer), then
`/implement-with-haiku`, `/shipit`, `/merge-and-cleanup`/`/cleanup`/`/stack-sync`. Each conversion
is behavior-neutral outside a flow (FLOW_DIR unset → identical to today) and lands with its test.

**Phase 2 — `/expert-flow` single ticket (1 PR).** `commands/expert-flow.md` (Sonnet), §6 state,
§7 steps, merge gate, telemetry stages `plan track implement review fix verify ship merge`,
`tests/test_expert_flow.py`. Fix the stale `CLAUDE.md` chain-command line in the same PR.
ADR-0023 (renumbered; 0022 was taken by `/expert-spike`).

**Phase 3 — dogfood (3 real tickets in this repo, then 3 elsewhere).** Log per run: questions
relayed, hard stops, manual take-overs, wall-clock, cost line. **Kill criterion:** if more than one
take-over per ticket is needed for reasons other than a genuine code problem (i.e. the *flow* broke,
not the ticket), stop and fix the relay before adding epic mode. Mirrors ADR-0019's 8-run test.

**Phase 4 — `--epic` sequential (1 PR).** §9 steps 1–5 + the `## Sub-tickets` contract addition.

**Phase 5 — `--stack` (optional, only if "Hold" is chosen in practice).** `--base` in track-and-start,
PR-base retarget check, ADR-0011 amendment.

## 12. Decisions needed from you before Phase 1

**Resolved on #245 (2026-10-08):** (1) accept — open-ended plan questions become free-text
`AskUserQuestion`s, the one UX change; (2) human merge gate by default, `--auto-merge` per-run only;
(3) epic v1 sequential, `--stack` deferred; (4) verify runs `commands.check` only — auto-running
"needs measurement" commands stays open. The original questions follow for the record.

1. **Open-ended plan questions become free-text `AskUserQuestion`s** instead of a chat exchange
   (§5 table, first row). Accept, or keep `/expert-plan` outside the flow for themes with >4
   questions (the flow would pause and hand you the planning window)?
2. **Merge gate default = human.** Confirm, or default to `--auto-merge` with the gate only when the
   review had any *needs you* item?
3. **Epic v1 sequential, `--stack` deferred.** Confirm, or is continuing on top of an unmerged
   parent a day-one requirement?
4. **Step 6 (verify) scope.** Just `commands.check`, or also auto-run "needs measurement" commands
   the Triage Chief drafted when they are plainly runnable (no human reading needed)?
