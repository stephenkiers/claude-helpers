# ADR-0008: Machine-enforced agent guardrails

**Status:** Accepted

## Context

During a real `/implement-with-haiku` run, staged work was wiped by a `git reset` mid-run while
the run reported success — an instance of the "self-reports overstate success" failure mode
already documented in `commands/implement-with-haiku.md`'s Orchestrator verification lessons. The
work harvested via `git diff --staged` was gone by the time the orchestrator went to apply it.

Two contributing gaps, both worth fixing regardless of which one caused the specific incident:

1. `agents/plan-implementer.md` grants `Bash(git *)` under `permissionMode: bypassPermissions`,
   with the "no destructive git" rule enforced only as prose in the Constraints section. Prose is
   ignorable — an agent under pressure to "make it work" can rationalize past it, and plain
   `git reset` (no flags) isn't even literally covered by the "reset --hard" wording.
2. The orchestrator itself runs `git checkout "$START_SHA" -- <file>` / `git checkout HEAD --
   <file>` in the **main worktree** during the Part C mutation smoke — which wipes the index and
   worktree for that file if staged work happens to be sitting there uncommitted.

`Bash(...)` glob patterns in an agent's `tools:` frontmatter are **not a documented enforcement
mechanism** in Claude Code — they scope what shows up in a permission prompt, and
`permissionMode: bypassPermissions` skips that prompt entirely. The deterministic, bypass-proof
mechanism is a per-agent **PreToolUse hook**: it runs before permission evaluation, and exiting
non-zero blocks the tool call outright, regardless of permission mode.

**Threat model: a careless agent, not an adversarial one.** This guardrail assumes the agent is
trying to do the right thing and occasionally reaches for the wrong command under pressure to
reach "green" — not that it is trying to evade a sandbox. Shell-wrapper evasions (`sh -c "git
reset"`, npm/cargo scripts that shell out to git, `xargs git`, etc.) are explicitly out of scope.

## Decision

Prose constraints on an autonomous agent must be **machine-enforced** whenever violating them is
destructive (data loss, not just a style deviation). The mechanism is an agent-frontmatter
`PreToolUse` hook that inspects the command before it runs and blocks disallowed ones with a
nonzero exit.

Applied here:

- `scripts/plan-implementer-git-guard.py` — a PreToolUse hook wired into
  `agents/plan-implementer.md`. It parses every `Bash` command's `git` invocations (including ones
  inside `$(...)`, compound commands, and multi-line commands) and blocks (exit 2) any subcommand
  outside an explicit allowlist (`rev-parse`, `add`, `status`, `diff`, `log`, `show`, `ls-files`,
  `grep`) — **allowlist, not blocklist**, so a git subcommand nobody thought to ban is
  blocked by default rather than slipping through. It fails closed: anything it can't confidently
  parse is treated as blocked, not allowed.
- The orchestrator's own mutation smoke now requires `git status --porcelain -- <file>` to be
  clean before it runs its bounded `git checkout` — if the file has uncommitted state sitting on
  it, the smoke is skipped and flagged rather than risking a wipe.
- The orchestrator's `allowed-tools` grant for `git checkout` is narrowed from `Bash(git
  checkout:*)` to the two literal forms it actually uses, rather than a wildcard covering every
  checkout invocation.
- An empty harvest that the report claims is a success (`STAGED: yes` with nothing actually
  staged) is now treated as an anomaly — the inverse of the existing "empty diff + `STAGED: no`,
  go look for unstaged work" rule — routed through the same Incomplete-report menu rather than
  silently accepted as "no changes needed."

The `tools:` frontmatter pattern (`Bash(git *)`) stays as-is on `plan-implementer.md` — narrowing
it would create a second, drifting copy of the same allowlist with no additional enforcement
value, since it was never a real gate to begin with. The hook is the single source of truth.

## Consequences

- **Good:** the "no destructive git" rule for plan-implementer is now enforced by an exit code,
  not a sentence the agent has to remember to obey. `tests/test_git_guard.py` cross-checks that
  the hook's allowlist and the agent's Constraints prose stay in sync, and exercises the guard
  script directly (including fail-closed cases) so drift is caught by the test suite (run manually or via the `/shipit` command's local checks before committing), not in a future incident.
- **Good:** the mutation smoke's precondition and the empty-harvest anomaly rule close the
  orchestrator-side half of the incident, independent of whether the hook alone would have caught
  it.
- **Cost:** the guard is a regex/whitespace approximation of shell parsing, not a real shell
  parser — it can be fooled by a determined adversary (hence the threat-model carve-out above).
  It is a guardrail against a careless agent making an honest mistake, not a sandbox.
- **Cost:** every `Bash` call from `plan-implementer` now pays one extra subprocess hop for the
  hook. Negligible relative to the agent turns it gates.
- **Forking note:** if you fork this repo and add other autonomous, `bypassPermissions` agents
  that can reach genuinely destructive commands (not just git), apply the same pattern: an
  allowlist-based, fail-closed `PreToolUse` hook, not a prose rule alone.

## Amendment: Cwd drift as a residual risk (2026-07-30)

Real incidents (e.g., session 7F17AECE) exposed a second half of the lost-work failure:
an agent's cwd drifting, causing its edits to land in the main worktree instead of its assigned
worktree, while the run still reported success (`STAGED: yes` or `STAGED: no` with unstaged work).

**Why this is not hook-enforceable:** The PreToolUse hook is static per-agent (bound at agent
definition time), has no visibility into per-invocation state like the assigned worktree path
(prompt-level only), and cannot influence the orchestrator's assignment of cwd to the main
worktree before spawning the agent. A path-guard hook that blocked writes to main would also
block the legitimate single-unit fallback and Round 3 follow-up pass, which run in the main
worktree by design.

**Root cause, found later:** the drift wasn't a spawning-mechanism limitation at all — the
orchestrator passed the assigned path as a plain "Working directory: X" prompt line, but
`plan-implementer`'s own instructions never consumed it (no `cd`, ever). Claude Code's Bash tool
persists cwd across a single agent's calls, so a `cd` at the start of the agent's own instructions
closes this for good — no hook, no orchestrator-side change needed. `plan-implementer.md` now has
this as its first, load-bearing step: `cd` to the given path, verify with `pwd`, halt on mismatch.
(A second, compounding cause was nesting worktrees under `main/.claude/worktrees/`, inside build
tool workspace roots that then silently misresolved — fixed by defaulting to flat sibling
placement instead.)

**Detection + salvage remains as defense in depth**, not the primary fix: orchestrator-side
detection at every harvest point (Step 4c, Round 2 apply, Round 3 follow-up apply) checks the
main worktree state (`git status --porcelain` on the unit's owned files) against the agent's
assigned worktree, and a **codified salvage procedure** (Step 4c, "Salvage procedure for cwd
drift") applies the drifted work in place if found. Before offering Re-run as a response to an
empty worktree with a success-claiming trailer, the orchestrator checks main first — real
incidents wasted duplicate runs by Re-running without salvaging from main first.

The fixed destructive-git half (this ADR's main scope), the `cd`-at-start prevention in
`plan-implementer.md`, flat worktree placement, and the detection + salvage half (in
`implement-with-haiku.md`) together close the incident class.

## Amendment: Named out-of-cwd exceptions for report files (2026-09-09)

The working-directory boundary constraint ("only touch files inside the current working directory")
was refined with two named exceptions, both at orchestrator-designated absolute paths:

1. **Report-file write** — a report file at an absolute path the orchestrator's prompt explicitly names
   for the agent to `Write` — required when named, not a code edit, does not change `STAGED:` status.
2. **Prior-report reads** — one or more input files at absolute paths the orchestrator's prompt
   explicitly names as a prior pass's report for the agent to `Read` — not a violation of the
   working-directory constraint.

These exceptions apply to the `/implement-with-haiku` workflow's multi-round structure: each round
reads the prior round's report file and writes its own, all at orchestrator-named absolute paths
in a shared scratch directory. The exceptions close a legitimate pattern, not a loophole — reads
and writes are always at the path the orchestrator names in the prompt, never agent-chosen paths.

The `plan-implementer.md` Constraints section now explicitly documents these exceptions and closes
with: "These are the **only out-of-cwd exceptions** this agent is permitted: writing its own report
file, and reading a prior round's report file, both at orchestrator-named absolute paths. Any other
out-of-cwd read or write is not permitted and should be treated as an anomaly." This statement
clarifies the boundary so the agent and future maintainers (or anomaly detectors) know what
out-of-cwd activity is expected and what is unexpected.

## Standing Exception: /expert-flow step agents (2026-10-13)

`/expert-flow` step agents are `general-purpose` Task subagents with full capability, not restricted
panel agents (ADR-0023 §1). They run without the PreToolUse hook and machine-enforced constraints
documented in this ADR's Consequences section. The decision is that this capability is acceptable for
the `/expert-flow` orchestrator's step agents because (a) they run only when the user explicitly
invokes `/expert-flow` (not by default), (b) they operate inside the user's own worktrees and flow
directories under explicit user control, not reading arbitrary diff content as untrusted data, and
(c) the orchestrator's own prompt-scoped `Write` rule (confined to `FLOW_DIR` and marker files; see
ADR-0023 §1 and ADR-0018 2026-10-13 amendment) mirrors the cascade of prompt-level conventions
already governing other multi-step commands.

**Decision:** No machine-enforced guardrails are required for `/expert-flow` step agents. No follow-up
issue is filed — this is a standing exception, not a temporary deferral.

**Narrowed by:** Amendment 2026-10-26 restricts this exception to general-purpose agents only; see that amendment.

## Amendment 2026-10-26 (#336)

The earlier amendment (2026-07-30) stated that "the `cd`-at-start prevention in `plan-implementer.md`,
flat worktree placement, and the detection + salvage half (in `implement-with-haiku.md`) together
close the incident class" — specifically incident 7F17AECE where an agent's cwd drifted. This
amendment corrects that statement and records the actual prevention contract.

**Definitions:**
- **Primary checkout** — the first entry from `git worktree list` in the repo root; the human's
  regular checkout where they commit code. One per repo.
- **Ticket worktree** — the worktree path passed to an agent as `Working directory: <path>` in the
  orchestrator's prompt. One per agent invocation.

**Drift frequency finding (from receipt analysis, 2026-10-26):**
- Method: grep of `~/.claude/flows` receipts for `salvaged-from-main` and `cwd-drift`, 0 hits
  dated 2026-10-23 to 2026-10-26; hits for #293 (2026-10-19) and #287 (2026-10-20).
- Limitation: receipts only. A drift the orchestrator never noticed leaves no receipt line, so the
  true rate is unknown. That limit is why the detector exists.

**Cause investigation (#293, 2026-10-19, docs unit transcript analysis):**
The drifted #293 unit's first Bash call was `date +%s > "$(git rev-parse --git-dir)/iwh-agent-start"`.
The output printed `~/.../worktrees/main/.git/worktrees/293-...-haiku-docs` (the primary checkout path).
This was the FIRST appearance of the primary checkout path in the agent's context. The unit then Read
and Edited absolute paths under the primary checkout (worktrees/main), despite the assigned Working
directory being the ticket worktree.

**Cause class:** (d) Other — the agent derived an absolute path from the output of `git rev-parse
--git-dir`, not from a cwd reset, not from the environment block, not from "main worktree" wording
in Block A. A linked worktree's git-dir lives under the primary checkout's `.git/worktrees/`, and
printing that output leaked the primary checkout's path, allowing the agent to build paths into it.

**Prevention contract:**
The sentence "Your Bash tool's working directory persists across your subsequent calls" in the
earlier amendment was wrong. The Bash tool's cwd may reset between calls. The prevention contract
is:

1. **Baseline snapshot at Step 2** — before any implementer agent runs, the orchestrator snapshots
   the primary checkout's git status via `primary-checkout-drift.py snapshot`.
2. **Agent-level prevention** — every Bash call must start with `cd <Working directory> &&`. Every
   Write/Edit/Read `file_path` must be absolute and start with the assigned Working directory. Never
   derive paths from git output (`--git-dir`, `--git-common-dir`, `git worktree list`,
   `--show-toplevel`). These rules are stated in `agents/plan-implementer.md` (D2 fallback prevention,
   targeted to cause class (d)).
3. **Orchestrator-side detection at barriers** — after all Round 1 units complete (merged with the
   integration gate compare), after each serial pass (single-unit fallback, Round 2 apply, Round 3
   follow-up apply), and once after the last agent pass (before the final summary), the orchestrator
   compares the primary checkout's status against the baseline via `primary-checkout-drift.py compare`.
4. **Fail condition** — fail only on the planned file set (all units' owned files plus the ticket
   branch's changes since START_SHA). Warn on other new dirt. Skip the check when the primary
   checkout's realpath equals the orchestrator cwd.
5. **Fail-closed on missing baseline** — a missing baseline exits with code 2, reported as a FAILED
   receipt with Reason `primary-checkout-drift: baseline-unavailable` and failure class `cwd-drift`.
6. **Never touch the human's checkout** — the detector is report-only; it never writes to or reverts
   anything in the primary checkout. On exit 1 (drift detected), the ticket-worktree check in Step 4c
   saves a diff patch, and the FAILED receipt names the patch and the drifted files. The human
   decides what to do by hand (no auto-salvage).

Spelling convention: receipt `FAILURE_CLASS:` value is hyphenated `cwd-drift`; telemetry failure
class is underscored `cwd_drift`. This is a hard stop (see ADR-0023 for cwd-drift as a never-auto-remediated
incident class in the lifecycle).

**Earlier amendment corrections:**
- The sentence near line ~115 saying the measures "together close the incident class" was
  incomplete — the baseline and barrier detects (new in #336) are also required.
- The phrase near line ~107 saying the detection "checks the main worktree state" was imprecise.
  It checks the **primary checkout** state (first `git worktree list` entry), not "main worktree",
  which was overloaded. See primary-checkout-drift.py in #336's implementation.

**Related tickets:**
- #358 (expert-flow: flow-review + flow-implement step agents) — adds a **PreToolUse guard on
  Write/Edit** to block writes outside the Working directory at tool invocation time. Complements
  but does not replace #336's prompt-level prevention and detection.
- #327 (expert-flow: takeover leaves uncommitted work) — adds a **pre-spawn cleanliness check**
  of the ticket tree before implementer/fixer agents start. Complements but does not replace #336's
  primary-checkout detection.
- The two (#327 and #336) are not merged by accident — #336's probe detects primary-checkout drift
  (caused by guide paths), #327's check ensures the ticket tree is clean (preventing overlap in
  receipt interpretation).

**Known gaps:**
- Sibling worktrees under the repo root are not inspected. W1 deferred this — it multiplies false
  positives from concurrent flows. The helper accepts a list of paths.
- Re-edits of already-dirty planned files may only appear as WARN (not FAIL) if the same-XY file
  edits do not create measurably-new drift outside the baseline status. Mitigation: a `WARN
  pre-dirty-planned` line makes the gap visible on each run. This is rare and only occurs when a file
  was dirty before the agent started, the agent edits it, and the result is not detectably different
  from the pre-agent state.
- The detector baseline (#336) and barrier checks are required to prevent drift class incidents (#338
  implements a `just check` timeout addressing a consequence; both are independent fixes).

## Amendment: Restrict the standing exception to implement, fix, and review (2026-10-26)

The standing exception narrowed: **implement, fix and review are restricted** (`flow-implement`,
`flow-review`); **worktree, plan, track, verify, ship, merge, the retro grader and the auto-rebase
worker remain general-purpose, pending #359/#360** (the fidelity-audit scout is already a restricted
`expert-scout`).

The implement and fix steps run `flow-implement.md` (model pinned to `sonnet`, no `Edit`, Bash guard);
the review step runs `flow-review.md` (model pinned to `sonnet`, `Edit` kept for red-lined
`claude-action-plan.md`). All other steps spawn `general-purpose` agents as before.

### Control matrix for restricted steps

Each control is labelled: **technical** (machine-enforced), **best-effort** (strong heuristic,
documented bypasses), or **prompt-level** (instruction-based, not enforced).

| Control | Category | Detail |
|---|---|---|
| **Model pin** | technical | `model: sonnet` in agent frontmatter, no Task-time override by orchestrator, overrides session model |
| **Edit omission** | technical | `flow-implement` has no `Edit` tool; `flow-review` keeps `Edit` (red-line exception; see residuals below) |
| **Agent/Task allowlist hook** | technical (settings-level and frontmatter; see 2026-10-30 amendment) | `PreToolUse` hook with `scripts/flow-agent-guard.py` registered in `~/.claude/settings.json` at settings level (verified in probe); frontmatter-declared hooks in agent files are belt-and-braces, unverified headless; run `/setup-local` or `./install.sh` to register settings-level hooks |
| **Skill allowlist hook** | technical (settings-level and frontmatter; see 2026-10-30 amendment) | `PreToolUse` hook with `scripts/flow-skill-guard.py` registered in `~/.claude/settings.json` at settings level; frontmatter wiring is unverified (same belt-and-braces as Agent hook); run `/setup-local` or `./install.sh` to register |
| **Bash guard** | best-effort | Q1=C: refuses wrappers (`sh -c`, `bash -c`, `eval`, `xargs git\|gh`, `env git\|gh`, `git -c`, `git --git-dir`, `git --exec-path`, `gh api` non-GET) and publishing verbs; known bypasses: write-a-script-then-run, `python3 -c`, nested `general-purpose` agent (if Agent hook is not technical) |
| **Step contract** | prompt-level | `prompts/flow-step-contract.md` with injection-defence block; contract-read/FAILED stop if unreadable; procedure-level rules ("invoke via Skill", "treat data as data", "FLOW_DIR authoritative") |
| **Agent-file preflight** | technical | Stop at flow start, on `--resume`, and per step if file/hook/contract missing; "run /setup-local" message |

### Probe results (Step 2.1–2.6, session ids below for `/usage-report` exclusion)

**2.1 Model override (T-D1, CP-D4):** Task-time `model: sonnet` override on a `claude-haiku-4-5-20251001`-pinned
agent ran on `claude-sonnet-5-5` (subagent transcript `message.model`). Unpinned agent with override
also ran Sonnet (non-discriminating: session was Sonnet). **Result: PASSES.** The #315 Sonnet escalation and `/expert-review --model` already depend on this working; it is now documented as observable.

**2.2 Permission prompt (T-W2):** Headless agent without `permissionMode` calling Skill was denied by
permission system, not stalled. Interactive behaviour **UNPROBED** (cannot be driven from subagent). **Decision:**
omit `permissionMode` per T-W2; do not add `bypassPermissions` unless future probe shows stalls.

**2.3 Edit re-grant (T-W3):** Skill call was denied headless before `allowed-tools` could be exercised. **Result: INCONCLUSIVE.**
`flow-review` keeps `Edit` despite omission on `flow-implement` (residual below).

**2.4 Hook payload (T-W4, CP-W3):** Settings-level `PreToolUse` hook with Bash guard showed exit 2 blocks
on denylisted verbs and off-list Skill names. Observed payload for Agent/Task: `tool_input = {description, prompt, subagent_type, run_in_background}`; for Skill: `tool_input = {skill: "<name>"}`. Subagent calls carry top-level `agent_id` and `agent_type`; main-session calls do not. **Frontmatter hooks in project-scoped agents did NOT fire headless** (no payload logged); settings-level hooks fired with `agent_type` payload. **Result:** Settings-level wiring is verified technical; frontmatter wiring is UNVERIFIED as a technical control (user-scoped agents like plan-implementer may differ, not probed). Per CP-W3: ship both script + settings-level form and agent-body prompt-level allowlists, labelled as such.

**2.5 Unregistered subagent_type (CP-W2):** Agent call with type `does-not-exist` returned immediately,
error text `"Agent type 'does-not-exist' not found."` No subagent transcript or agent id created (only
valid spawns have transcripts). **Result: never-started signal IS observable.** Hot-load lag (file
created after session start) not separately probed; assumed to produce the same error.

**2.6 Q1 confounder:** `just check` recipes run by just's shell, not agent Bash tool top-level;
`commands/implement-with-haiku.md` has no `bash -c`/`sh -c`. No legitimate wrapper found. **Result:**
no narrowing of Bash guard required.

### Known bypasses and residuals

**Best-effort bypasses:** The Bash guard can be bypassed by (a) writing a script to a temp file and
invoking it, (b) `python3 -c "..."`, (c) nested `general-purpose` agent if the Agent hook is not technical
(Q1 confounder: the Agent hook wiring path is unverified frontmatter-only for project agents).

**Residual risks:**
- `Write` tool is not path-scoped; writes outside the worktree are undetected (open issue #353).
- `plan-implementer` keeps `Edit` and `bypassPermissions` (unchanged from ADR-0008 design).
- `flow-review` keeps `Edit` solely for red-lining the `claude-action-plan.md` file produced by triage. This is prompt-scoped, not path-scoped: the agent is instructed to use `Edit` only on that file, not a machine-enforced boundary. A scripted, field-scoped rulings writer is pending as a follow-up (file #CP-W1).
- Nested `general-purpose` agent spawning from a restricted step gets `Edit` and no hook.

### Wrapper set and publishing verbs (drift guard targets)

<!-- flow-bash-wrapper-set:begin -->
sh -c
bash -c
zsh -c
eval
xargs git
xargs gh
env git
env gh
git -c
git --git-dir
git --exec-path
gh api -X non-GET
<!-- flow-bash-wrapper-set:end -->

<!-- flow-bash-publishing-verbs:begin -->
git push
git remote add
git remote set-url
gh pr create
gh pr merge
gh pr edit
gh pr comment
gh pr close
gh pr review
gh issue create
gh issue edit
gh issue comment
gh issue close
gh release create
gh repo create
gh repo delete
gh gist create
gh workflow run
<!-- flow-bash-publishing-verbs:end -->

### Pattern for #359

Q1 = C guard wiring and Q2 = A contract placement are the pattern #359 copies:
- Q1 = C: named constant wrapper set in a marked block (drift guard target), explicit verb denylist, and fail-closed guard script (exit 2 on parse failure).
- Q2 = A: one shared contract file with marked injection-defence block, inlined byte-checked into agent system prompts, preflight enforcement.
**#359 must not copy an unprobed hook** (frontmatter-declared Agent/Skill hooks are unverified as technical controls).

## Amendment 2026-10-30 (#402): live probe of the guard wiring

Probed from an interactive session by spawning the real `flow-implement` and `flow-review` agents
(after the hook-frontmatter fix in b99020d) and having each attempt allowed and disallowed calls.
This supersedes the 2.2, 2.3 and 2.4 results above where they differ.

| Probe | Result |
|---|---|
| Hooks fire for step agents | **Pass.** `flow-implement`: `bash -c`, `git push`, `general-purpose` spawn and `Skill cleanup` blocked; `echo`, `plan-implementer` spawn and `Write` allowed. `flow-review`: `general-purpose` spawn and `Skill cleanup` blocked; `expert-scout` spawn, `Edit` and `Bash` allowed. |
| Frontmatter hooks | **Fire.** Block messages cite both the settings-level form (`--agent auto`) and the frontmatter form (`--agent flow-implement` / `--agent flow-review`). Both run and the first denial reported wins, so a Bash block may name either. Contradicts the 2.4 headless result; the b99020d frontmatter fix is the likely difference (not isolated). |
| `flow-review` Edit grant | **Granted.** `Edit` on a scratch file succeeded with no prompt or hook block. Scope to `claude-action-plan.md` remains prompt-level only (residual unchanged). |
| Permissions (`bypassPermissions`) | **Partial.** No permission prompt or non-hook denial for any call in either agent. Not probed: the allowed `/implement-with-haiku` Skill path, and an interactive-session prompt (cannot be driven from a subagent). |
| Hot-load lag | **Not instant, about 20 s.** A temporary hook added to `~/.claude/settings.json` missed a Read ~2 s later and fired on a Read ~23 s later; other sessions picked it up within ~30 s of first registration. Coarse (two samples). Consequence: run `/setup-local` and wait before the first flow. |

Open: the Skill-allowed path and interactive permission behaviour are still unprobed; "machine-enforced"
is now evidenced for the hook-denied paths only.
