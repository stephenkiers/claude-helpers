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

- `scripts/plan-implementer-git-guard.sh` — a PreToolUse hook wired into
  `agents/plan-implementer.md`. It parses every `Bash` command's `git` invocations (including ones
  inside `$(...)`, compound commands, and multi-line commands) and blocks (exit 2) any subcommand
  outside an explicit allowlist (`rev-parse`, `add`, `status`, `diff`, `log`, `show`, `ls-files`,
  `grep`, `rm`, `mv`) — **allowlist, not blocklist**, so a git subcommand nobody thought to ban is
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
  script directly (including fail-closed cases) so drift is caught in CI, not in a future incident.
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
