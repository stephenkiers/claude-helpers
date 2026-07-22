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

```bash
# Detect issue number: cache-first, then branch name regex fallback
BRANCH=$(git branch --show-current)
GITHUB_CACHE=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
ISSUE_NUM=$(echo "$GITHUB_CACHE" | jq -r '.issue.number // empty' 2>/dev/null)
if [ -z "$ISSUE_NUM" ]; then
  ISSUE_NUM=$(echo "$BRANCH" | grep -oE '^[0-9]+' || echo "")
fi

git push -u origin "$BRANCH"

# Build PR body with issue link if detected
if [ -n "$ISSUE_NUM" ]; then
  gh pr create --title "<commit subject>" --body "$(cat <<'EOF'
Closes #<issue_num>

## Summary
- <what changed>

## Test plan
- <how to verify>
EOF
)"
else
  gh pr create --title "<commit subject>" --body "$(cat <<'EOF'
## Summary
- <what changed>

## Test plan
- <how to verify>
EOF
)"
fi
```

### Write PR Data to Worktree Cache

After `gh pr create` succeeds, write PR data to `.claude/github-cache.json`:

```bash
# Extract PR number and URL from creation output
PR_URL=$(gh pr view --json url -q '.url')
PR_NUM=$(echo "$PR_URL" | grep -oE '[0-9]+$')

# Merge PR data into existing cache (preserves branch + issue sections)
EXISTING=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
# Write to a temp file and mv on success so a jq failure never truncates the existing cache
# (a bare `> github-cache.json` redirect truncates the file before jq runs).
TMP=$(mktemp .claude/github-cache.json.XXXXXX)
echo "$EXISTING" | jq --argjson pr "{\"number\": $PR_NUM, \"url\": \"$PR_URL\", \"state\": \"OPEN\"}" \
  '. + {pr: $pr}' > "$TMP" && mv "$TMP" .claude/github-cache.json || rm -f "$TMP"
```

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
