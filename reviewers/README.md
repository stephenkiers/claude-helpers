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
| `~/.claude/reviewers/` | `index.yaml` | Lightweight meta index (router reads this only) |
| `project/.claude/` | `project.yaml` | Project-wide context — all experts + /shipit (local, gitignored) |
| `project/.claude/reviewers/` | `{expert}-local.yaml` | Expert-specific project overrides (local, gitignored) |

**Note:** `project.yaml` and `{expert}-local.yaml` are local, project-specific state. Like `.claude/*.json` cache files (`.claude/github-cache.json`, `.claude/repo-cache.json`, `.claude/effort-heuristic.yaml`), these files should be added to `.gitignore` and never committed to the shared repository.

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
      `character` + `voice`), `principles`, and `codeReview.prompt` (containing an `INVESTIGATE:`
      body). Do **not** add a `triggers` block to the persona file — routing triggers live only in
      [`index.yaml`](index.yaml) (ADR-0003), on the persona's index entry.
- [ ] Do **not** add a per-persona `OUTPUT FORMAT` block — the canonical format lives in
      [`prompts/expert-framework.md`](../prompts/expert-framework.md). Only add one if your persona
      genuinely needs a different shape (see the existing carve-outs).
- [ ] Do **not** add a per-persona "Load Project Context" block unless you load context differently
      from the default — that step is centralized in `expert-framework.md`.
- [ ] Add a matching entry to [`index.yaml`](index.yaml) (the router reads only this index; a
      persona missing here never runs). If you're migrating a persona's existing `triggers:` block
      into its index entry rather than writing one from scratch, diff the two — a prior migration
      silently dropped keywords for five personas (Fragile Feynman, Uncle Bob, Tara TypeSafe,
      Frontend Fred, Rachel) before it was caught in review. A later migration additionally dropped
      keywords for **Contract Chris** (lost: `precondition`, `postcondition`, `assert`) and
      **Scope Creep Steve** (lost: `state-machine`, `timeout`, `keepalive`) — verify both entries if
      you touch them. `index.yaml` is now the *sole* signal the judgment router sees for that persona's
      domain (there is no fallback gate to catch a miss), so a dropped keyword is a silent routing
      blind spot, not a cosmetic loss.
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
  See [ADR-0006](../docs/adr/0006-reviewer-output-format-carve-outs.md) for the bar a persona must
  clear to qualify.

**Editorial personas** (`/expert-write`) use an `editReview.focusAreas` block instead of
`codeReview.prompt`. The current three are **Demosthenes** (`editor-audience.yaml`, audience fit),
**Shakespeare** (`editor-cadence.yaml`, rhythm and pacing), and **Strunk** (`editor-signal.yaml`,
signal over noise). They have **no diff
`triggers`**, so the `/expert-review` router never selects them — their `index.yaml` `note` marks
them `for /expert-write`. A persona with only an `editReview` block is not a valid `/expert-review`
target; do not request one there.

## Creating Project Context

When setting up a new project, create `.claude/project.yaml`.
Copy `~/.claude/prompts/project.yaml.template` and fill in what's relevant.
See `~/.claude/prompts/project-example-{python,rust,typescript}.yaml` for full examples.

**Before committing, add it to `.gitignore` so it never gets tracked in the shared repository:**

```bash
# 1. Ignore it (do this FIRST — an interruption here leaves the harmless status quo:
#    ignored-but-still-tracked, rather than untracked-and-unignored).
printf '%s\n' '.claude/project.yaml' >> .gitignore

# 2. Only if it is already tracked: .gitignore does nothing for a file git already knows about.
git ls-files --error-unmatch .claude/project.yaml >/dev/null 2>&1 && git rm --cached .claude/project.yaml || true     # keeps your local copy on disk

# 3. One commit carrying BOTH changes, so the two never ship apart.
git add .gitignore
git commit -m "Untrack .claude/project.yaml (local-only reviewer context)"

# 4. Verify (--no-index is required; a bare check-ignore is silent for tracked paths):
git check-ignore -v --no-index .claude/project.yaml   # expect your .gitignore line, exit 0
```

**Important notes:**
- `.gitignore` alone does not affect files already tracked by git; that's why step 2 (git rm --cached) is necessary. Any teammate who pulls this commit will see `.claude/project.yaml` deleted from their checkout and should save their own local copy first — this is a tracking change against the shared repository, not a purely additive documentation edit.
- Untracking does not remove the file from existing git history; old commits will still have it.
- The cascade loads the literal path `.claude/project.yaml` and nothing else. If your project file has a different name, you must rename it to `.claude/project.yaml` exactly; adding a differently-named file to `.gitignore` will not enable it to be loaded.

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

Only create `{expert}-local.yaml` when you need expert-specific project knowledge beyond `project.yaml`. Like `project.yaml`, these files should be added to `.gitignore` and never committed to the shared repository (this repo's own `north-star-nick-local.yaml` is a deliberate, documented exception for dogfooding):

**Before committing, add it to `.gitignore` so it never gets tracked in the shared repository:**

```bash
# 1. Ignore it (do this FIRST — an interruption here leaves the harmless status quo:
#    ignored-but-still-tracked, rather than untracked-and-unignored).
printf '%s\n' '.claude/reviewers/*-local.yaml' >> .gitignore

# 2. Only if it is already tracked: .gitignore does nothing for a file git already knows about.
git ls-files --error-unmatch .claude/reviewers/{expert-name}-local.yaml >/dev/null 2>&1 && git rm --cached .claude/reviewers/{expert-name}-local.yaml || true     # keeps your local copy on disk

# 3. One commit carrying BOTH changes, so the two never ship apart.
git add .gitignore
git commit -m "Untrack .claude/reviewers/{expert-name}-local.yaml (local-only expert overrides)"

# 4. Verify (--no-index is required; a bare check-ignore is silent for tracked paths):
git check-ignore -v --no-index .claude/reviewers/{expert-name}-local.yaml   # expect your .gitignore line, exit 0
```

**Important notes:**
- `.gitignore` alone does not affect files already tracked by git; that's why step 2 (git rm --cached) is necessary. Any teammate who pulls this commit will see the file deleted from their checkout and should save their own local copy first — this is a tracking change against the shared repository, not a purely additive documentation edit.
- Untracking does not remove the file from existing git history; old commits will still have it.
- The cascade loads `{expert}-local.yaml` from `.claude/reviewers/` only. If your file has a different name or location, the cascade will not find it.

Example structure:

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
