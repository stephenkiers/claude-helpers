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
- [ADR-0003 Tagger-based routing](docs/adr/0003-tagger-routing.md)
- [ADR-0004 Model cost routing](docs/adr/0004-model-cost-routing.md) — Haiku for mechanical work.
- [ADR-0005 Three-layer context cascade](docs/adr/0005-three-layer-context-cascade.md)

## Commands

**Review & planning**
- `/expert-review` — multi-persona, blind-first code review with tagger routing and triage
- `/expert-plan` — collaborative plan building with expert personas (asks, doesn't assume)
- `/expert-review-plan` — review a plan with the expert panel
- `/expert-pr-comments` — review PR comments, convene an expert huddle on flagged items
- `/pr-comments` — review PR comments and decide how to respond
- `/expert-pre-mortem` — standalone fragility pre-mortem (Fragile Feynman)
- `/expert-rebase` — rebase on origin/main; convene experts on conflicting hunks

**Hardening** (take a review persona, switch it to edit mode)
- `/expert-harden-types` — tighten type annotations
- `/expert-harden-contracts` — add contract documentation
- `/expert-harden-tests` — generate property-based tests

**Lifecycle**
- `/setup-local` — symlink this repo's helpers into `~/.claude/`
- `/track`, `/track-and-start` — create a GitHub issue (or local plan) and optionally branch + worktree
- `/implement-with-haiku` — background Haiku flow: implement, spec-blind test author, adversary review
- `/shipit` — run CI checks locally, commit, open a PR (`/shipit-reference` for details)
- `/expert-implement-with-haiku-and-ship` — run implement → shipit → expert-review in one shot, halting on the first failure; hands the final review back to you
- `/cleanup` — clean up a worktree after a PR is merged
- `/fork-planning` — fork a planning session

**Research & writing**
- `/research-swarm` — deep research on a topic via parallel web agents, optionally cross-checked
  against internal knowledge (via whatever internal-search MCP tools you have configured — Slack,
  Confluence, Notion, Glean, Drive, etc.) if any are available
- `/expert-write` — edit a document with expert writing personas + accumulated prose rules
- `/style-google-doc` — apply a standard Google Doc visual style
- `/search-claude` — search prior Claude Code transcripts; returns resume commands

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

## Agents

- `plan-implementer` — implements a step-by-step plan autonomously, type-checks, commits, reports back
  (used by `/implement-with-haiku`).
