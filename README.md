# Claude Helpers

An opinionated, personal [Claude Code](https://claude.com/claude-code) configuration: a **multi-persona,
blind-first code review system** plus the planning, hardening, and shipping commands that wrap it.

> 📝 Companion article: [Building an LLM Expert Persona](https://stephenkiers.com/writings/2026/04/04/llm-expert-persona)

This is one developer's working setup, shared in the open. It is **not** a framework with a roadmap or
support guarantees. It's here to be read, stolen from, and forked.

## Steal this. Fork it. Don't merge back.

The intended way to use this repo is to **make it your own**:

- **Fork (or just copy) it and own your fork.** Make it private if you want — the [MIT license](LICENSE)
  lets you. Change the personas' voices, drop the ones you don't like, add your own, retune the model
  routing to your budget. Your fork is yours; you never owe a contribution back.
- **Stay current without merging.** Because forks will diverge (that's the point), don't expect clean
  `git merge`s from upstream. Instead, periodically ask Claude:
  > "Compare my reviewers/commands to `stephenkiers/claude-helpers` upstream and pull in anything new
  > or better — keep my customizations."

  That's a content-level cherry-pick, not a history merge, and it's exactly how I keep my own private
  copy in sync with this public one.
- **Or track upstream directly.** If you'd rather not fork, clone this repo and symlink into it
  (`/setup-local` below); `git pull` gets you my latest.

**Support policy:** none, by design. Issues and PRs are welcome **only for genuine bugs**. Feature
requests, "can you add my persona," and "please support my workflow" → fork it and do it your way.
I won't be offended; that's the whole idea.

## Quick start

```bash
git clone git@github.com:stephenkiers/claude-helpers.git ~/Repositories/claude-helpers
cd ~/Repositories/claude-helpers
claude
# then run:  /setup-local
```

`/setup-local` symlinks `commands/`, `reviewers/`, `prompts/`, and `agents/` into `~/.claude/` at the
file level, so your own personal files can live alongside these. It's idempotent. A no-Claude fallback
is `./install.sh` (add `--with-zsh-keybindings` to opt into Option+Arrow word jumping).

## What's inside

- **28 reviewer personas** (`reviewers/`) — character-driven experts (security, type safety,
  concurrency, DDD, composition/EDA, fragility, contracts, dead code, and more), routed to only the
  parts of a diff they care about.
- **`/expert-review`** — the core: a blind-first, two-pass, multi-persona review with cheap-model
  routing and checkpointed artifacts. An `--effort 1–5` ladder scales it from a 6-scout haiku swarm
  up to the full panel (default 4), and a positional GitHub PR URL reviews a coworker's PR in an
  isolated worktree (ADR-0012).
- **Planning & hardening** — `/expert-plan`, `/expert-review-plan`, `/expert-harden-{types,contracts,tests}`,
  `/expert-pre-mortem`.
- **Lifecycle** — `/track`, `/implement-with-haiku`, `/shipit`, `/cleanup`, `/expert-rebase`, and more.

See [CLAUDE.md](CLAUDE.md) for the full command and persona catalog.

## How it works (and why)

The design decisions are documented as ADRs in [`docs/adr/`](docs/adr/):

- **Progressive disclosure** — 28 personas would be ruinous to load all at once, so experts load lazily;
  a cheap tagger routes diff sections to the few relevant reviewers, and each loads its own context on
  demand. ([ADR-0001](docs/adr/0001-progressive-disclosure.md))
- **Blind-first, two-pass review** — reviewers judge the code before they see the author's rationale,
  then re-evaluate with context. Catches what a rationalizing pass misses.
  ([ADR-0002](docs/adr/0002-blind-first-two-pass-review.md))
- **Tagger routing** ([ADR-0003](docs/adr/0003-tagger-routing.md)) and **model cost routing** — Haiku
  for mechanical work, the strong model for judgment. ([ADR-0004](docs/adr/0004-model-cost-routing.md))
- **Three-layer context cascade** — generic persona → `project.yaml` → per-reviewer local override, so
  the same personas sharpen themselves on any project. ([ADR-0005](docs/adr/0005-three-layer-context-cascade.md))

## A note on cost

A full `/expert-review` with many personas is **not cheap** — it spins up a summarizer, a router,
multiple expert passes, and Q&A subagents. Mechanical steps run on Haiku to keep this sane
([ADR-0004](docs/adr/0004-model-cost-routing.md)); the `--effort 1–5` ladder scales the panel down
to a 6-scout swarm (`--effort 1`) or a focused pair (`--effort 2`) for small diffs
([ADR-0012](docs/adr/0012-effort-ladder-and-pr-mode.md)), and you can scope a run to specific
reviewers (`/expert-review security,types`). Start small before running the whole panel.

## Project context

Drop a `.claude/project.yaml` into any project to give the reviewers your tech stack, ADRs, invariants,
and terminology. Copy [`prompts/project.yaml.template`](prompts/project.yaml.template) to begin.

## Dependencies

The `/shipit` and `/cleanup` commands are gh-stack-aware. `/shipit` establishes the parent→child
chain via `gh pr create --base <parent>` alone — it does **not** auto-run `gh stack link` (that
command is not metadata-only: it repoints the bottom PR's base to trunk, which detaches an
intermediate PR from its real parent when given a subset). The server-side GitHub "stack" entity
is optional cosmetic grouping on top of the base-pointer chain.

- If the GraphQL `stack` field is unavailable, stack metadata is simply not cached or used.
- To opt into the GitHub stack UI grouping manually, link the **full** bottom→top chain in one call
  (never just the parent+child pair): `gh stack link <bottom-pr> … <parent-pr> <child-pr>`. See
  `prompts/shipit-reference.md` § "GitHub stack entity (optional)".

**To install gh-stack** (optional; needed only for `gh stack sync`/`unstack`):
```bash
gh extension install github/gh-stack
```

Both `/shipit` and `/expert-rebase` are layout-aware: they use `gh stack sync` under single-driver layout and manual `--force-with-lease` under per-branch (worktree-per-branch) layout.

## License

[MIT](LICENSE). Copy it, change it, keep it private — no attribution beyond the license text required.
