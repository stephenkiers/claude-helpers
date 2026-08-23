# ADR-0005: Context cascade

**Status:** Accepted

> Titled "Three-layer context cascade" when accepted; [ADR-0007](0007-triage-and-decision-memory.md)
> added a fourth layer (see the Amendment below), so the heading now reads "Context cascade." The
> filename is unchanged to avoid breaking inbound links.

## Context

The same personas need to work across wildly different projects — a Rust audio engine, a TypeScript
web app, a Python data pipeline. A persona hard-coded to one project's conventions is not reusable; a
persona with no project knowledge gives generic, low-value feedback. We need personas that are generic
by default but sharpen themselves with project-specific knowledge when it exists.

## Decision

Context cascades through three layers, each optional and each overriding the one above:

1. **Global persona** — `reviewers/{name}.yaml` in this repo (symlinked into `~/.claude/`). The
   reusable character and checklist. Knows nothing about your project.
2. **Project context** — `.claude/project.yaml` in the project being reviewed (`techStack`, `adrs`,
   `invariants`, `redLines`, `terminology`, plus per-reviewer fields like `fragility.*`, `docStyle`).
   See `prompts/project.yaml.template` and the `project-example-*.yaml` files.
3. **Per-reviewer local override** — `.claude/reviewers/{name}-local.yaml` in the project, for
   project-specific extensions to a single persona (e.g. `north-star-nick-local.yaml` pointing at the
   project's ADR index; `mozart-local.yaml` for project-specific event patterns).

Each reviewer reads only the layers relevant to it, on demand (see [ADR-0001](0001-progressive-disclosure.md)).

## Consequences

- **Good:** One set of personas works everywhere; projects add precision without forking the personas.
  Cascade overrides also let a project replace any command/reviewer wholesale by shadowing it in its
  own `.claude/`.
- **Cost:** Three places a fact could live. Convention: generic truth in the persona, project truth in
  `project.yaml`, single-persona project truth in the local override.
- **Dogfooding:** This repo wires its own `.claude/reviewers/north-star-nick-local.yaml` →
  `docs/adr/` so North Star Nick reviews changes here against these very ADRs.

## Amendment — a fourth layer (ADR-0007)

[ADR-0007](0007-triage-and-decision-memory.md) adds a `decisions.yaml` layer, logically between
layers 2 and 3: the rulings a human made during a previous review's triage, which reviewers must
treat as settled and not re-raise.

It is a separate file from `project.yaml` on purpose. `project.yaml` is **hand-authored** and
describes what the project *is*; `decisions.yaml` is **machine-appended** (with approval) and records
what a human *ruled*. Merging them would let a tool churn a hand-curated file.

Note the one way this fourth layer breaks the cascade's shape: it does **not** live in the project's
`.claude/`. Per ADR-0007's own amendment, it sits outside the working tree at a repo-keyed path
(`~/.claude/reviews/{owner-repo}/decisions.yaml`), because a decision suppresses findings and an
in-tree file would let a branch license the review of itself. The orchestrator passes each reviewer
the path; conceptually it is still the "decided truth" layer, it just cannot be a tracked file.

The cascade convention extends accordingly: generic truth in the persona, project truth in
`project.yaml`, **decided truth** in `decisions.yaml`, single-persona project truth in the local
override.

**Note (chore/29 amendment):** The fourth layer (`decisions.yaml`) was subsequently removed. The
cascade reverts to three layers. See [ADR-0007](0007-triage-and-decision-memory.md)'s second
amendment for rationale.

## Amendment — user preferences as a global lens layer

Preferences.yaml adds a fourth layer at global-user scope, sitting **above** the three-layer
project cascade. A reviewer loads preferences after project context but before per-reviewer local
overrides. This supersedes the removed `decisions.yaml` fourth layer from ADR-0007's amendment.

The cascade is now again four layers:

1. **Global persona** — `reviewers/{name}.yaml` (generic, reusable).
2. **Project context** — `.claude/project.yaml` (project-wide knowledge).
3. **User preferences** — `~/.claude/preferences.yaml` (personal code review taste/style lens).
4. **Per-reviewer local override** — `.claude/reviewers/{name}-local.yaml` (per-domain project overrides).

Preferences are a **lens**, not a suppression list. They shape how findings are worded and
prioritized but never eliminate findings. The hard floor is identical to what `decisions.yaml`
held: preferences never dilute CRITICAL or security-flagged finding severity. Project-level rules
win on conflict — preferences are personal defaults that projects can override.

See [ADR-0007](0007-triage-and-decision-memory.md)'s second amendment, which removed `decisions.yaml`,
for the rationale behind moving to a user-preference-based approach.

## Amendment — project-context filename fallback

Layer 2 (Project context) is now resolved as an ordered candidate list (`.claude/project.yaml`, then `.claude/project-context.yaml`), first match wins, no merge.

This provides flexibility in project context file naming while maintaining full backward compatibility. The primary filename `.claude/project.yaml` is checked first; if it does not exist, the system falls back to `.claude/project-context.yaml`. Existing projects are unaffected. If both files exist in a project, only the primary is read.
