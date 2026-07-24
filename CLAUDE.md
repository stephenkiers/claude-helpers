# Claude Helpers

An opinionated set of Claude Code commands, reviewer personas, prompts, and agents — built around a
multi-persona, blind-first code review system and a development lifecycle that wraps it.

This repo is meant to be **forked and adapted**. See [README.md](README.md) for the fork-and-adapt
philosophy and [docs/adr/](docs/adr/) for the design decisions behind it.

## Setup (new machine or re-sync)

Run `/setup-local` in this repo (or `./install.sh` for the no-Claude path). It is idempotent — safe to
re-run any time.

It creates **file-level symlinks** from `~/.claude/{dir}/` into this repo for each of `commands`,
`reviewers`, `prompts`, and `agents`. File-level (not directory-level) symlinks let your own personal
or project-specific files coexist in `~/.claude/{dir}/` alongside the repo's files:

- Ensure `~/.claude/{dir}/` exists as a real directory (replace any old directory-level symlink).
- For each file in `{repo}/{dir}/`: if `~/.claude/{dir}/{file}` is already the correct symlink, skip;
  if it's a real file or wrong link, back it up to `.bak`, then symlink; otherwise symlink.

Re-running after new files are added just creates the missing symlinks and leaves everything else
untouched.

Optional (opt-in, zsh + macOS only): Option+Arrow word jumping via
`./install.sh --with-zsh-keybindings`. Never added by default.

## How the review system works

See the ADRs for the full rationale:

- [ADR-0001 Progressive disclosure](docs/adr/0001-progressive-disclosure.md) — experts load lazily;
  only relevant personas run, and each loads its own context on demand.
- [ADR-0002 Blind-first, two-pass review](docs/adr/0002-blind-first-two-pass-review.md)
- [ADR-0003 Reviewer routing](docs/adr/0003-tagger-routing.md) — superseded by a single judgment
  Router (ADR-0003.2); the original keyword tagger + confirm-gate is gone.
- [ADR-0004 Model cost routing](docs/adr/0004-model-cost-routing.md) — Haiku for mechanical work.
- [ADR-0005 Three-layer context cascade](docs/adr/0005-three-layer-context-cascade.md) — amended by
  ADR-0007 with a fourth layer.
- [ADR-0006 Reviewer output-format carve-outs](docs/adr/0006-reviewer-output-format-carve-outs.md)
- [ADR-0007 Triage and decision memory](docs/adr/0007-triage-and-decision-memory.md) — the pipeline
  ends in triage, not synthesis; rulings are recorded and become settled law.

## Triage: the review ends with decisions, not findings

The panel got good enough that reading its output became the expensive part. So `/expert-review` no
longer ends at the Amalgamator's `final-report.md` — a **Triage Chief** (`prompts/triage.md`) reads
that report and writes `action-plan.md`, which is the file a human actually opens. Every CONFIRMED
finding lands in one of **five buckets** — *doing it*, *needs you*, *needs measurement*, *deferred*,
*already settled*:

- **Doing it** — the default, and where ~85% of findings land. Skim it. (No *decision* needed — but
  these still need doing; apply them or hand the plan to `/implement-with-haiku`.)
- **Needs you** — escalations only, under a deliberately narrow test. Over-escalation is treated as
  the failure mode, not the safe default: a *needs you* list long enough to skim is one nobody reads.
  Every escalation must offer *leave as-is* as a real option.
- **Needs measurement** — findings nobody can rule on yet because the honest answer requires running
  something and reading a result back, not picking from options — so it's never put to
  `AskUserQuestion`. The Triage Chief drafts a concrete, runnable command and what result would
  confirm or refute the finding; the ruling stays pending until a human supplies the result.
- **Deferred** — what genuinely should not happen now, each with a reason that survives being read
  aloud.
- **Already settled** — findings a recorded decision covers, split into `(withheld)` (a reviewer
  stayed silent, and said so under `## Suppressed by decision`) and `(raised anyway)`. No CRITICAL or
  security finding is ever here — the floor below forbids it.

The **gut check** is not a bucket; it is the cross-cutting analysis no single-lens reviewer can do:
*do these findings share one bad premise? is this drifting from an ADR? did the panel genuinely
disagree? have we seen this theme before?* A bounded *declined nominations* list watches the opposite
failure — a `**Human Call**` waved through without escalating.

`final-report.md` is unchanged and one click away. It is still the gut-check instrument of record —
triage sits **in front of** it, not over it.

**Decisions are recorded and reviewers obey them.** Rulings land in a repo-keyed `decisions.yaml`
that lives **outside the repo** (`~/.claude/reviews/{owner-repo}/decisions.yaml`; fourth context
layer, template in `prompts/decisions.yaml.template`) — outside so that no diff can add an entry that
silences the review of itself. `prompts/expert-framework.md` instructs every reviewer not to re-raise
what a recorded decision already settles, and to record what it suppressed. That is the loop: each
ruling makes the *next* report shorter, so noise falls as the project's judgment accumulates instead
of rising as the panel improves. A hard floor bounds the mechanism: a decision **demotes, never
deletes** — it can never suppress a CRITICAL or a security finding, and `project.yaml` red lines
outrank it.

The bar for a recorded decision is **patterns and the spirit behind them — never nits.** A
`decisions.yaml` full of nits is worse than an empty one, because reviewers read it as settled law
and it will start suppressing real findings. When a ruling changes how the system is *shaped*, it
becomes an ADR instead. `~/.claude/reviews/{owner-repo}/ledger.jsonl` (keyed on repo identity, beside
the decisions file) records every finding's disposition, and `/review-stats` mines it for recurring
themes — a theme on its third appearance is not three bugs, it is one missing decision.

## Commands

**Review & planning**
- `/expert-review` — multi-persona, blind-first code review; parallel per-reviewer subagents, judgment
  router for reviewer selection (Sonnet), single Amalgamator for synthesis (replaces quadratic
  cross-review), then a **Triage Chief** that turns the report into a decision list and records what
  you rule. Takes `[reviewers...]` and `--model haiku|sonnet|opus|fable` (panel tier; router
  and mechanical roles stay pinned per ADR-0004, Fable is the deliberate expensive step)
- `/expert-plan` — collaborative plan building with expert personas (asks, doesn't assume)
- `/expert-review-plan` — review a plan with the expert panel
- `/expert-pr-comments` — review PR comments, convene an expert huddle on flagged items
- `/pr-comments` — review PR comments and decide how to respond
- `/expert-pre-mortem` — standalone fragility pre-mortem (Fragile Feynman)
- `/expert-rebase` — rebase on origin/main; convene experts on conflicting hunks
- `/review-stats` — aggregate past reviews into per-reviewer confirmed-vs-rejected rates (eval loop)

**Hardening** (take a review persona, switch it to edit mode)
- `/expert-harden-types` — tighten type annotations
- `/expert-harden-contracts` — add contract documentation
- `/expert-harden-tests` — generate property-based tests

**Lifecycle**
- `/setup-local` — symlink this repo's helpers into `~/.claude/`
- `/track`, `/track-and-start` — create a GitHub issue (or local plan) and optionally branch + worktree
- `/implement-with-haiku` — parallel Haiku implementers → orchestrator-owned integration gate (anti-cheat + bounded fix loop) → spec-blind test author → adversary review
- `/shipit` — run CI checks locally, commit, open a PR (`prompts/shipit-reference.md` for details)
- `/expert-implement-with-haiku-and-ship` — run implement → shipit → expert-review in one shot, halting on the first failure; hands the final review back to you
- `/cleanup` — clean up a worktree after a PR is merged
- `/fork-planning` — fork a planning session
- `/handoff` — snapshot the current conversation into `~/.handoff/<timestamp>-<slug>/` so it can be resumed elsewhere (pair with Esc-Esc to rewind)
- `/handoff-resume` — list recent handoffs, or load one (by name/prefix/query) into the current window

**Research & writing**
- `/research-swarm` — deep research on a topic via parallel web agents, optionally cross-checked
  against internal knowledge (via whatever internal-search MCP tools you have configured — Slack,
  Confluence, Notion, Glean, Drive, etc.) if any are available
- `/expert-write` — edit a document with expert writing personas + accumulated prose rules
- `/style-google-doc` — apply a standard Google Doc visual style
- `/search-claude` — search prior Claude Code transcripts; returns resume commands

**Reference docs are not commands.** Every `.md` file in `~/.claude/commands/` is registered as an
invocable slash command — frontmatter or not. So the lazy-loaded companion docs
(`prompts/shipit-reference.md`, `prompts/worktree-reference.md`) live in `prompts/`, and the
commands that need them read them by path. Put a new reference doc in `prompts/`, not `commands/`.

## Reviewers (personas)

28 character-driven reviewers in `reviewers/`, indexed with their triggers in
[`reviewers/index.yaml`](reviewers/index.yaml). Highlights:

- **Uncle Bob** — clean code, SOLID, resource management
- **Security Sage** — security, input validation, failure modes
- **Tara TypeSafe** — type safety, contracts, invariants
- **Rachel** — concurrency, thread safety, race conditions
- **Eric Evans** — DDD, domain boundaries, ubiquitous language
- **Mozart** — composition/orchestration and event-driven architecture
- **Sam System** — cross-file composition and data-flow (receives the full diff)
- **North Star Nick** — alignment with documented ADRs (reads `docs/adr/` here)
- **Fragile Feynman** — pre-mortem fragility analysis
- **Contract Chris** — contract completeness, docstrings, silenced errors
- **Code Rot Cody** — dead code / orphaned-symbol detection (Haiku, full-repo grep)
- …plus Know-It-All Nigel, Vera Verifier, Ariadne, Contrarian Carl, Scope Creep Steve, Penny Pincher,
  Curious Casey, Business Beth, Danielle the Designer, Dependency Skeptic, Data Scientist Dana,
  Frontend Fred, the editor personas (Demosthenes/audience, Shakespeare/cadence, Strunk/signal) +
  Consistency Checker.

## Project context

Create `.claude/project.yaml` in any project to give reviewers and `/shipit` project-specific context.
Copy `prompts/project.yaml.template` to start; see `prompts/project-example-{python,rust,typescript}.yaml`
for full examples, and `*-local-example-*.yaml` for per-reviewer overrides.

## Cascade & overrides

Projects can override any command or reviewer by adding their own `.claude/` directory — project files
take precedence over these defaults (see [ADR-0005](docs/adr/0005-three-layer-context-cascade.md)).

## Shell conventions in command docs

**Never build a JSON object by interpolating a shell variable into a string literal passed to
`--argjson`** (e.g. `jq --argjson pr "{\"url\": \"$URL\"}"`). If the variable contains a quote or
backslash, the resulting JSON is malformed or silently wrong. Always pass one variable per
`--arg`/`--argjson` flag and let the `jq` filter assemble the object — `jq --arg url "$URL" '. +
{pr: {url: $url}}'` — so `jq`, not the shell, owns the escaping. This bug class has been
independently rediscovered three times in this repo's own command docs (`commands/track.md`, twice,
and `commands/shipit.md`); if you find a fourth instance, fix it the same way rather than patching
around it.

## Agents

- `plan-implementer` — implements a step-by-step plan autonomously, type-checks, commits, reports back
  (used by `/implement-with-haiku`).
- `expert-reviewer` — one reviewer persona, one diff, one checkpoint file (used by `/expert-review`
  for Router, Pass 1, Contrarian Carl, Pass 2 skeptic-verifier, and Amalgamator). Model comes from
  the caller (`--model`), except Router which is pinned to sonnet.
- `expert-scout` — the pinned mechanical roles: Q&A (Haiku), Code Rot Cody (Haiku), and Consistency
  Checker (Haiku). Router (Sonnet; narrow judgment) is spawned as expert-reviewer with an explicit
  model override.

**Panel agents are capability-restricted, not dialog-gated.** They run `bypassPermissions` — because
20 concurrent subagents reading personas and writing checkpoints outside the working directory
otherwise means 20 near-identical permission prompts (the permission system does not deduplicate
across concurrent agents, and a command's `allowed-tools` does not propagate to subagents it spawns).
The `tools:` list is the meaningful part of this: **no `Edit` tool and no write-capable Bash**, so a
reviewer cannot modify the code it is reviewing under any circumstances — that half is a real,
technical control. But the plain `Write` tool in that same list is *not* path-scoped (Claude Code's
tool-allowlist syntax has no directory-prefix form for `Write`, unlike the `Bash(git diff:*)`-style
prefix scoping used elsewhere in that list) — so "writes exactly one file" is a prompt-level
convention the subagent is instructed to follow, not something the tool grant enforces. Both agent
prompts tell the subagent to treat diff/PR content as data, not instructions, precisely because that
content is attacker-influenceable and `bypassPermissions` removes the confirmation dialog that would
otherwise catch a stray `Write` elsewhere. Keep both halves — the real `Edit`/Bash restriction and
the prompt-level `Write`-scoping instruction — when adding a panel agent; treat the latter as a
residual, not eliminated, risk.
