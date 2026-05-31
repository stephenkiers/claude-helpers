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
