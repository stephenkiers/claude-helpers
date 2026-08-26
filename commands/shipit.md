---
name: shipit
description: Use when user says "/shipit", "ship it", "commit and pr", "create pr", or wants to commit changes and create a pull request. Detects tooling, runs CI checks locally, creates a minimal commit, and opens a PR.
model: haiku
---

# Shipit

Run CI checks, commit, and open PR. For edge cases and maintenance, read
`~/.claude/prompts/shipit-reference.md`.

## 1. Load Cache & Detect Tooling

Start these in parallel:
- Read `.claude/repo-cache.json`
- Read `.claude/github-cache.json` (worktree cache — issue/PR data)
- `git status && git diff --stat`

**PR existence check (cache-first):**
1. If `.claude/github-cache.json` has `pr.url` → PR already exists, skip API call
2. If no cache or no `pr` section → fall back to `gh pr view --json url 2>/dev/null`

**If no cache or cache is stale (>30 days):** Run full detection (Step 1.5) and write cache.

**If cache exists and fresh:** Use cached commands.

### Cache Schema

```json
{
  "version": 2,
  "detected": {
    "packageManager": "bun|npm|yarn|pnpm|none",
    "languages": ["go", "typescript", "rust", "python", "ruby"],
    "monorepo": false,
    "ciConfig": ".github/workflows/ci.yml",
    "buildTool": "make|just|npm|cargo|go"
  },
  "commands": {
    "install": "go mod download",
    "format": "make fmt",
    "lint": "make lint",
    "vet": "make vet",
    "typecheck": null,
    "test": "make test",
    "build": "make build",
    "check": null
  },
  "gotchas": [],
  "parallelizable": ["lint", "vet"],
  "lastUpdated": "ISO date",
  "lastFullRefresh": "ISO date"
}
```

**Worktree cache (.claude/github-cache.json)** also stores stacked-PR metadata (transient):
```json
"pr": {
  "number": 42,
  "url": "https://github.com/…/pull/42",
  "state": "OPEN"
},
"stack": {
  "isStacked": true,
  "parentBranch": "stephenkiers/pps-166-…",
  "parentPr": 14,
  "stackNumber": 18,
  "layout": "single-driver"
}
```

`layout` is `"single-driver"` or `"per-branch"` (optional; absent on old caches → always re-detect via the "Detect layout" block).

## 1.5. Full Detection (No Cache or Stale Cache)

**Goal:** Discover every check the project runs and store it so we never miss one again.

**Detection order matters** — more specific sources override less specific:

### Source 0: `.claude/project.yaml` (highest priority)

Read `.claude/project.yaml` at project root. If a `commands` section exists, it **wins immediately** — skip all other sources for any key that is explicitly set.

```yaml
# Example: these commands are used as-is, no further detection needed
commands:
  format: ruff format .
  lint: ruff check .
  typecheck: mypy src/
  test: pytest
  build: null    # null = not applicable, skip this step
  check: null    # null = not applicable
```

Rules:
- Any key present (even `null`) overrides detection for that key
- A `null` value means "this step doesn't exist for this project" — do not run it
- Keys omitted from `commands` still fall through to Sources 1–4 below
- Write the resolved commands back to `repo-cache.json` as usual

### Source 1: CLAUDE.md

Read `CLAUDE.md` at project root. Extract any commands documented under headings like `## Commands`, `## Verification`, `## Development`, or backtick-fenced code blocks containing build/test/lint commands.

Look for patterns:
- `make <target>` commands
- `go test`, `go vet`, `cargo test`, `npm run`, `bun run`, `pytest`, etc.
- Any command described as required before committing/submitting

### Source 2: Build tool files

**Makefile** (if exists):
```bash
# Extract all phony targets
grep -E '^\w+:' Makefile | sed 's/:.*//'
```
Map known target names to command types:
- `fmt` / `format` → `commands.format`
- `lint` → `commands.lint`
- `vet` / `check` → `commands.vet`
- `test` → `commands.test`
- `build` → `commands.build`
- `install` / `deps` → `commands.install`

Store as `make <target>` (e.g., `"lint": "make lint"`).

**justfile** (if exists): Same approach — `just --list` to discover recipes.

**package.json** (if exists): Parse `scripts` object. Map:
- `check` → `commands.check` (composite — may replace lint+typecheck)
- `lint` → `commands.lint`
- `format` / `fmt` → `commands.format`
- `typecheck` / `type-check` → `commands.typecheck`
- `test` → `commands.test`
- `build` → `commands.build`

Prefix with detected package manager: `bun run lint`, `npm run lint`, etc.

**Cargo.toml** (if exists):
- `commands.lint` = `cargo clippy -- -D warnings` (if clippy available)
- `commands.test` = `cargo test`
- `commands.build` = `cargo build`
- `commands.format` = `cargo fmt --check`

**go.mod** (if exists — only as fallback if no Makefile/justfile):
- `commands.vet` = `go vet ./...`
- `commands.test` = `go test -race ./...`
- `commands.build` = `go build ./...`
- Check if `golangci-lint` is available: `which golangci-lint` → `commands.lint` = `golangci-lint run`

**pyproject.toml / requirements.txt** (if exists):
- Check for ruff: `commands.lint` = `ruff check .`, `commands.format` = `ruff format --check .`
- Check for pytest: `commands.test` = `pytest`
- Check for mypy: `commands.typecheck` = `mypy .`

### Source 3: CI config (validation)

Read `.github/workflows/*.yml` (or `.gitlab-ci.yml`, etc.) to cross-reference. If CI runs a check that isn't in the discovered commands, add it. This catches things like `golangci-lint` that aren't in the Makefile but are in CI.

### Source 4: Language defaults (lowest priority, fill gaps only)

Only use these for command types that weren't discovered from any other source:

| Language | format | lint | vet | test | build |
|----------|--------|------|-----|------|-------|
| Go | `gofmt -l .` | `golangci-lint run` | `go vet ./...` | `go test -race ./...` | `go build ./...` |
| Rust | `cargo fmt --check` | `cargo clippy -- -D warnings` | - | `cargo test` | `cargo build` |
| TypeScript | - | `npx eslint .` | - | `npx vitest run` | `npx tsc --noEmit` |
| Python | `ruff format --check .` | `ruff check .` | - | `pytest` | - |
| Ruby | `bundle exec rubocop --format quiet` | - | - | `bundle exec rspec` | - |

### Write Cache

After detection, write `.claude/repo-cache.json` with all discovered commands. Set `null` for command types that don't apply (e.g., `typecheck: null` for Go).

**Important:** The cache captures what the project actually uses, not what the language could use. If the project has `make lint` that runs `golangci-lint`, store `"lint": "make lint"` — not the raw `golangci-lint` command. This way, if the Makefile changes what `make lint` does, we pick it up on next refresh.

## 2. Dependencies

If `node_modules` missing (or `node_modules/.bun` for bun): run cached install command.

## 3. Run Checks

**Skip if recently run:** If lint/typecheck/test were run earlier in this conversation and all passed, skip re-running them. Trust the prior results.

Run every non-null command from the cache in this order:

1. **Format** (if `commands.format` exists): Run first — formatting fixes may prevent lint errors
2. **Check** (if `commands.check` exists): Composite command — may replace lint+typecheck. Run instead of separate lint/typecheck if present.
3. **Parallel** (from `parallelizable` list): Typically lint, vet, typecheck, test
4. **Sequential**: build (if exists)

**If a command is null in the cache, skip it.** Don't fall back to language defaults at runtime — all defaults were already resolved during detection and written to the cache.

**On failure:** Stop immediately, report error, record gotcha in cache. Do NOT commit.

## 4. Commit

```bash
git add -A
git diff --staged
```

Write commit message:
- Format: `<type>(<scope>): <short description>`
- Types: feat, fix, refactor, docs, test, chore
- Under 72 chars
- **NEVER mention Claude, AI, LLM, or add Co-Authored-By**

## 5. Push & PR

### Stack Detection (if stacked)

Before pushing and creating the PR, run the **Stack Detection → Is-stacked** block from
`~/.claude/prompts/worktree-reference.md` to detect whether this branch's parent is another
worktree's branch. This resolves `STACK_IS_STACKED`, `STACK_PARENT_BRANCH`, and `STACK_PARENT_PR`.
(See that reference doc for the full detection logic.)
Then also run the **"Detect layout"** block from `~/.claude/prompts/worktree-reference.md` to resolve `STACK_LAYOUT`. (Skip if `STACK_IS_STACKED` is false AND the branch has no stacked descendants — but note a stack root is exactly that case: unstacked itself, yet heading a stack. The descendant-sync gate below needs `STACK_LAYOUT` for roots too, so when the branch may have children, run layout detection anyway.)

### Create PR (with correct base for stacked PRs)

```bash
# Detect issue number: cache-first, then branch name regex fallback
BRANCH=$(git branch --show-current)
GITHUB_CACHE=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
# printf, not echo: zsh's builtin echo interprets backslash escapes, turning a stored
# \n back into a literal newline and making the JSON invalid before jq ever sees it.
ISSUE_NUM=$(printf '%s' "$GITHUB_CACHE" | jq -r '.issue.number // empty' 2>/dev/null)
if [ -z "$ISSUE_NUM" ]; then
  ISSUE_NUM=$(echo "$BRANCH" | grep -oE '^[0-9]+' || echo "")
fi

if [ "$STACK_IS_STACKED" != "true" ]; then
  git push -u origin "$BRANCH"
else
  # Run the "Push a stacked branch (new local work)" block from
  # ~/.claude/prompts/worktree-reference.md (routes on STACK_LAYOUT: gh stack sync for
  # single-driver, git rebase --onto + --force-with-lease for per-branch; stops on unknown).
  echo "ERROR: stacked push block not expanded — run the 'Push a stacked branch (new local work)' block from ~/.claude/prompts/worktree-reference.md here." >&2
  exit 1
fi

# Build PR body with issue link (and "Stacked on" note if applicable)
PR_BODY="## Summary
- <what changed>

## Test plan
- <how to verify>"

if [ -n "$ISSUE_NUM" ]; then
  PR_BODY="Closes #$ISSUE_NUM

$PR_BODY"
fi

if [ "$STACK_IS_STACKED" = "true" ] && [ -n "$STACK_PARENT_PR" ]; then
  PR_BODY="$PR_BODY

Stacked on #$STACK_PARENT_PR"
fi

# Create PR with correct base (stacked PRs use parent branch; non-stacked use repo default)
if [ "$STACK_IS_STACKED" = "true" ]; then
  gh pr create --title "<commit subject>" --base "$STACK_PARENT_BRANCH" --body "$PR_BODY"
else
  gh pr create --title "<commit subject>" --body "$PR_BODY"
fi
```

### Write PR Data to Worktree Cache

After `gh pr create` succeeds, write PR data (including stack metadata) to `.claude/github-cache.json`:

```bash
# Extract PR number and URL from creation output
PR_URL=$(gh pr view --json url -q '.url')
PR_NUM=$(echo "$PR_URL" | grep -oE '[0-9]+$')

# If stacked, look up the remote stack number (needed for cleanup restack runbook)
STACK_NUM=""
if [ "$STACK_IS_STACKED" = "true" ]; then
  REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || echo "")
  if [ -n "$REPO" ]; then
    REPO_OWNER=$(echo "$REPO" | cut -d/ -f1)
    REPO_NAME=$(echo "$REPO" | cut -d/ -f2)
    STACK_NUM=$(gh api graphql -F owner="$REPO_OWNER" -F name="$REPO_NAME" -F number="$PR_NUM" -q '.data.repository.pullRequest.stack.number // empty' 2>/dev/null << 'GRAPHQL'
query ($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      stack {
        number
      }
    }
  }
}
GRAPHQL
)
  fi
fi

# Merge PR data into existing cache (preserves branch + issue sections)
EXISTING=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
# Write to a temp file and mv on success so a jq failure never truncates the existing cache
# (a bare `> github-cache.json` redirect truncates the file before jq runs).
TMP=$(mktemp .claude/github-cache.json.XXXXXX)
# printf, not echo: zsh's builtin echo interprets backslash escapes, turning a stored
# \n back into a literal newline and making the JSON invalid before jq ever sees it.
if [ "$STACK_IS_STACKED" = "true" ]; then
  # Stacked: write full stack metadata
  printf '%s' "$EXISTING" | jq \
    --argjson number "$PR_NUM" \
    --arg url "$PR_URL" \
    --arg state "OPEN" \
    --arg parentBranch "$STACK_PARENT_BRANCH" \
    --arg parentPr "$STACK_PARENT_PR" \
    --arg stackNum "$STACK_NUM" \
    '. + {pr: {number: $number, url: $url, state: $state}, stack: {isStacked: true, parentBranch: $parentBranch, parentPr: (if $parentPr != "" then ($parentPr | tonumber) else null end), stackNumber: (if $stackNum != "" then ($stackNum | tonumber) else null end)}}' > "$TMP" && mv "$TMP" .claude/github-cache.json || { rm -f "$TMP"; echo "WARNING: failed to update .claude/github-cache.json (jq/mv error); cache left unchanged." >&2; }
else
  # Not stacked: write isStacked=false
  printf '%s' "$EXISTING" | jq \
    --argjson number "$PR_NUM" \
    --arg url "$PR_URL" \
    --arg state "OPEN" \
    '. + {pr: {number: $number, url: $url, state: $state}, stack: {isStacked: false}}' > "$TMP" && mv "$TMP" .claude/github-cache.json || { rm -f "$TMP"; echo "WARNING: failed to update .claude/github-cache.json (jq/mv error); cache left unchanged." >&2; }
fi

# Do NOT auto-link a server-side GitHub "stack" entity here.
# `gh pr create --base "$STACK_PARENT_BRANCH"` above already establishes the
# parent→child relationship — GitHub derives "Stacked on #N" from the base-branch
# pointer alone; no server stack entity is needed for the chain to work.
#
# `gh stack link <parent> <child>` is NOT metadata-only: it repoints the FIRST
# arg's base to the trunk (master). Linking only a parent+child SUBSET detaches
# the parent from ITS parent (observed 2026-08-25: `gh stack link 67 77`
# repointed #67 from pps-223 → master and locked its base under a new server
# stack, requiring `gh stack unstack` + `gh pr edit --base` to repair).
#
# The server "stack" entity is optional cosmetic grouping on top of the
# base-pointer chain. If the user explicitly wants it, link the FULL
# bottom→top chain — see "GitHub stack entity (optional)" in
# ~/.claude/prompts/shipit-reference.md. Never link just the new pair.
```

### Sync stacked children (per-branch ongoing)

**Gate on descendants, not on this branch being stacked.** A stack root
(`STACK_IS_STACKED=false`, based on the repo default branch) still has children that need
ongoing sync — the old `STACK_IS_STACKED` gate silently skipped exactly the canonical
2-level stack. Detect descendants with this branch as the pivot: first resolve `WORKTREE_PARENT`
(steps 2–3 of the **Project Detection** block from `~/.claude/prompts/worktree-reference.md` —
the worktree-list derivation; the full block is not needed here), then run the **Stack
Detection → Find children** block. If it reports `GH_CHILD_LOOKUP_FAILED=true`, print a loud
WARNING that the descendant set may be incomplete — never treat a failed lookup as "no
descendants" and silently skip the sync.

If the branch has descendants AND `STACK_LAYOUT="per-branch"`, the push above advanced this
branch past where its descendants branched off; per-branch children were NOT updated by the
push. Print one line first so the descendant force-push is not silent, then invoke the
`stack-sync` skill via the `Skill` tool, passing `--yes` so the push confirmation does not
block the ship flow:

> Syncing N stacked descendant(s) via /stack-sync
> /stack-sync --yes "$BRANCH"

stack-sync rebases each descendant onto this branch's updated tip inside each child's own
worktree, runs the project check gate, and force-pushes with `--force-with-lease`. If the
branch has no descendants, skip the invocation (stack-sync would be a clean no-op on a leaf).
Skip when `STACK_LAYOUT="single-driver"` (single-driver already cascaded via `gh stack sync`).

**If PR exists:** Report URL and stop.
**If on main/master:** Warn user, suggest creating a branch.
**If branch has issue number:** Include "Closes #N" in PR body to auto-close issue on merge.

## On Failure

Record gotcha in cache: `{"issue": "what failed", "resolution": "how to fix"}`

Then retry. For complex failures, see `~/.claude/prompts/shipit-reference.md`.

## Quick Reference

| Scenario | Action |
|----------|--------|
| No cache | Detect tooling from project files, create cache |
| Stale cache (>30 days) | Re-detect, update cache |
| Cache exists | Use cached commands directly |
| Dependencies missing | Run install command |
| Checks ran earlier in session | Skip, trust prior results |
| Check fails | Stop, report, record gotcha, don't commit |
| No changes | Report "nothing to commit" |
| PR exists | Report URL |
| On main branch | Warn, suggest branching |
| Branch is stacked | Create PR with `--base "$STACK_PARENT_BRANCH"` (this establishes the chain — do **not** auto-run `gh stack link`, it destructively repoints the bottom PR's base to trunk). Cache stack metadata; push via `gh stack sync` (single-driver) or `--force-with-lease` (per-branch) |
| Branch not stacked | Create PR with repo default base, mark `stack.isStacked=false` in cache |
