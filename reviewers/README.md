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

## Adding Context Loading to an Expert

Add this block at the START of the expert's `codeReview.prompt`:

```yaml
codeReview:
  prompt: |
    You are {EXPERT_NAME} reviewing {DOMAIN} in this code.

    ## STEP 1: Load Project Context (REQUIRED)
    BEFORE reviewing, you MUST check for and read these files in order:
    1. `.claude/project-context.yaml` - Project-wide ADRs, invariants, tech stack
    2. `.claude/reviewers/{expert}-local.yaml` - Expert-specific project overrides

    If either file exists, incorporate its knowledge into your review:
    - Apply project-specific invariants as additional red lines
    - Reference relevant ADRs in your findings
    - Use project terminology consistently
    - Check project-specific patterns

    ## STEP 2: Apply {DOMAIN} Review
    [... rest of expert-specific review instructions ...]
```

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

| Expert | File | Domain | Has Context Loading |
|--------|------|--------|---------------------|
| Mozart | `mozart-eda.yaml` | Event-Driven Architecture | Yes |
| North Star Nick | `north-star-nick.yaml` | Strategic Alignment | Yes |
| Rachel | `rachel.yaml` | Concurrency | Yes |
| Sam System | `sam-system.yaml` | System Composition | Yes |
| Fragile Feynman | `fragile-feynman.yaml` | Pre-mortem Fragility Analysis | Yes |
| Contract Chris | `contract-chris.yaml` | Contract Documentation | Yes |
| Tara TypeSafe | `tara-typesafe.yaml` | Type Safety + Tighten-Types | Yes |
| Uncle Bob | `uncle-bob.yaml` | Clean Code / SOLID | Not yet |
| Security Sage | `security-sage.yaml` | Security | Not yet |
| ... | ... | ... | ... |

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

Two `codeReview` shapes are supported:

- **Standard** (the default): `codeReview.prompt` with an `INVESTIGATE:` body; output format is
  inherited from `expert-framework.md`.
- **Self-formatting carve-outs** (`code-rot-cody`, `contrarian-carl`, `consistency-checker`): keep
  their own `OUTPUT FORMAT` block because their output structure differs from the standard template.

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

Only create `{expert}-local.yaml` when you need expert-specific project knowledge beyond `project-context.yaml`:

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
