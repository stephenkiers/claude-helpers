# Expert Reviewers

Global expert personas for code and plan reviews.

## Hybrid Context Loading Pattern

Experts follow a two-layer context loading pattern:

```
┌─────────────────────────────────────────────────────────────┐
│ GLOBAL (~/.claude/reviewers/{expert}.yaml)                  │
│ • Expert personality, voice, principles                     │
│ • Generic domain knowledge (EDA, concurrency, security)     │
│ • Universal red lines and green flags                       │
│ • Prompt template with context loading instructions         │
└─────────────────────────────────────────────────────────────┘
                              ↓
                    "Load project context"
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ PROJECT (project/.claude/project.yaml)                      │
│ • Tech stack (language, framework, testing, platform)       │
│ • Build/test/lint commands (used by /shipit)                │
│ • docStyle, typeChecker, propertyTestingLib                 │
│ • fragility.highRiskModules, fragility.knownFragilePatterns │
│ • ADRs and their summaries                                  │
│ • Project-wide invariants, red lines, terminology           │
└─────────────────────────────────────────────────────────────┘
                              ↓
              "Load expert-specific overrides"
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ LOCAL (project/.claude/reviewers/{expert}-local.yaml)       │
│ • Expert-specific project checks                            │
│ • Additional triggers for this project                      │
│ • Project-specific red lines for this expert's domain       │
│ • Technology-specific patterns (RxJS, Tokio, etc.)          │
└─────────────────────────────────────────────────────────────┘
```

## Context Loading

Project-context loading is **centralized** in `prompts/expert-framework.md` under
"Load Project Context (REQUIRED, all reviewers)". Every persona inherits it — they read
`.claude/project.yaml` and `.claude/reviewers/{expert}-local.yaml` automatically. **Do not**
add a per-persona "Load Project Context" / STEP 1 block; that duplication is what the framework
now owns (and contradicts the "Adding a New Reviewer" checklist).

Add a context block to a persona **only** when it loads context *differently* from the default
— e.g. North Star Nick, which additionally reads strategic documents (ADR index, issues index)
whose paths come from its local override. In that case, reference the already-loaded context
rather than re-listing the generic files.

## File Conventions

| Location | Naming | Purpose |
|----------|--------|---------|
| `~/.claude/reviewers/` | `{expert}.yaml` | Global expert definition |
| `~/.claude/reviewers/` | `index.yaml` | Lightweight meta index (tagger routing) |
| `project/.claude/` | `project.yaml` | Project-wide context — all experts + /shipit |
| `project/.claude/reviewers/` | `{expert}-local.yaml` | Expert-specific project overrides |

Templates for `project.yaml` and `{expert}-local.yaml` are in `~/.claude/prompts/`:
- `project.yaml.template` — canonical schema with all fields documented
- `project-example-python.yaml` — Python/FastAPI example
- `project-example-rust.yaml` — Rust/Axum example
- `project-example-typescript.yaml` — TypeScript/Next.js example
- `tara-typesafe-local-example-{python,rust,typescript}.yaml` — Tara overrides by language
- `fragile-feynman-local-example-python.yaml` — Feynman overrides for Python

## Current Experts

The authoritative, always-current roster lives in [`index.yaml`](index.yaml) — each entry has the
expert's `name`, `file`, `triggers`, and `useWhen`. Browse that file (or the `*.yaml` files in this
directory) for the full list rather than a table here that drifts out of date.

All personas inherit project-context loading from the framework (see [Context Loading](#context-loading)
above), so there is no longer a per-persona "has context loading" distinction.

## Adding a New Reviewer

Before writing a new persona, clear the **novelty guardrail**: a new reviewer must own a lens that
no existing persona already covers. Most "I need a reviewer for X" cases are better served by a
`{expert}-local.yaml` override or a project-specific trigger than by a whole new persona — the panel
is deliberately small so blind-first review stays cheap and routing stays sharp.

**Novelty check (answer before creating the file):**

1. Scan [`index.yaml`](index.yaml) — does an existing persona's domain already cover this concern?
   If yes, extend it (triggers or a `-local.yaml`) instead of adding a persona.
2. Is the lens genuinely orthogonal to the existing 27, or just a narrower slice of one?
3. Would this persona ever reach DEEP-DIVE on a real diff, or would it almost always SKIP?
   A persona that rarely fires is routing noise, not coverage.

**Why these rules exist:** the `OUTPUT FORMAT` and "Load Project Context" rules below both exist to
keep personas thin and consistent. The output format is canonical so Pass 2 re-evaluation and the
amalgamation step can parse every reviewer the same way — a per-persona format breaks that contract.
Load Project Context is centralized so a change to how context loads happens in one place, not 27.

**If it clears the guardrail, the checklist:**

- [ ] Create `reviewers/{kebab-name}.yaml` with: `name`, `priority`, `summary` (with nested
      `character` + `voice`), `triggers` (`filePatterns` / `keywords` / `riskIndicators`),
      `principles`, and `codeReview.prompt` (containing an `INVESTIGATE:` body).
- [ ] Do **not** add a per-persona `OUTPUT FORMAT` block — the canonical format lives in
      [`prompts/expert-framework.md`](../prompts/expert-framework.md). Only add one if your persona
      genuinely needs a different shape (see the existing carve-outs).
- [ ] Do **not** add a per-persona "Load Project Context" block unless you load context differently
      from the default — that step is centralized in `expert-framework.md`.
- [ ] Add a matching entry to [`index.yaml`](index.yaml) (the tagger routes from this index; a
      persona missing here never runs).
- [ ] Run `python3 tests/test_invariants.py` — the index/file mapping must stay bidirectional and
      the count invariant must hold.
- [ ] Re-run `/setup-local` so the new file gets symlinked into `~/.claude/reviewers/`.

## Persona Schemas

Personas live in one place (`reviewers/`) but are tagged by **what they review**. The `index.yaml`
entry — its `triggers` and `note` — declares whether a persona runs in code review, prose review, or
both, and the YAML body carries the matching review block.

**Code-review personas** (`/expert-review`) use a `codeReview` block, in two shapes:

- **Standard** (the default): `codeReview.prompt` with an `INVESTIGATE:` body; output format is
  inherited from `expert-framework.md`.
- **Self-formatting carve-outs** (`code-rot-cody`, `contrarian-carl`, `consistency-checker`): keep
  their own `OUTPUT FORMAT` block because their output structure differs from the standard template.

**Editorial personas** (`/expert-write`) use an `editReview.focusAreas` block instead of
`codeReview.prompt`. The current three are **Demosthenes** (`editor-audience.yaml`, audience fit),
**Shakespeare** (`editor-cadence.yaml`, rhythm and pacing), and **Strunk** (`editor-signal.yaml`,
signal over noise). They have **no diff
`triggers`**, so the code-review tagger never routes to them — their `index.yaml` `note` marks them
`for /expert-write`. A persona with only an `editReview` block is not a valid `/expert-review`
target; do not request one there.

## Creating Project Context

When setting up a new project, create `.claude/project.yaml`.
Copy `~/.claude/prompts/project.yaml.template` and fill in what's relevant.
See `~/.claude/prompts/project-example-{python,rust,typescript}.yaml` for full examples.

Minimal example:

```yaml
project: my-project
description: Brief description

techStack:
  language: TypeScript       # python | rust | typescript | go | ruby
  framework: react
  testing: vitest

commands:                    # used by /shipit — overrides auto-detection
  format: bun run format
  lint: bun run lint
  typecheck: bun run typecheck
  test: bun run test
  build: null                # null = not applicable

docStyle: jsdoc              # used by Contract Chris
typeChecker: tsc             # used by Tara TypeSafe
propertyTestingLib: fast-check

fragility:                   # used by Fragile Feynman
  highRiskModules:
    - src/auth/
  knownFragilePatterns:
    - "stale closures in useEffect"

adrs:
  ADR-001:
    title: State Management
    summary: Use Zustand for global state
    location: docs/adr/

invariants:
  someRule:
    rule: "Description of the rule"
    scope: where-it-applies
    why: "Reason for the rule"

redLines:
  - "Project-wide thing to never do"

terminology:
  widget: "Our name for X"
```

## Creating Expert-Local Overrides

Only create `{expert}-local.yaml` when you need expert-specific project knowledge beyond `project.yaml`:

```yaml
extends: {expert-name}

triggers:
  keywords:
    - project-specific-keyword

projectChecks:
  - "Project-specific check for this expert"

redLines:
  - pattern: "Something bad in this expert's domain"
    why: "Why it's bad in this project"
    fix: "How to fix it"

greenFlags:
  - "Good pattern specific to this project"
```
