# Shipit Reference Documentation

This file contains detailed documentation for edge cases, maintenance, and customization. The main `shipit.md` handles the happy path. Read this when:
- A command fails unexpectedly
- You need to customize behavior for a project
- Monthly maintenance is needed (cache > 30 days old)

---

## Extending This Command

This is a **global command** installed in `~/.claude/commands/`. Projects can override or extend it:

### Full Override
Create `.claude/commands/shipit.md` in the project root. This completely replaces the global version for that project.

### Project-Specific Configuration
The global command reads `.claude/repo-cache.json` which stores project-specific settings:
- Detected tooling and commands
- Custom CI check sequences
- Gotchas and workarounds learned from failures

To customize behavior without replacing the entire command:
1. Let the command run once to generate `.claude/repo-cache.json`
2. Edit the cache to adjust commands, add gotchas, or modify parallelization
3. The cache is gitignored by default (each worktree maintains its own)

### When to Override
- Project needs completely different workflow (e.g., different PR format)
- Project has CI checks not detectable from standard config files
- Project requires pre-commit hooks or special validation

---

## Repository Cache

### Full Schema

```json
{
  "version": 2,
  "detected": {
    "packageManager": "bun|npm|yarn|pnpm|none",
    "languages": ["typescript", "rust", "go", "python"],
    "monorepo": true,
    "ciConfig": ".github/workflows/ci.yml",
    "buildTool": "make|just|npm|cargo|go"
  },
  "commands": {
    "install": "bun install",
    "format": "bun run format",
    "lint": "bun run lint",
    "vet": null,
    "typecheck": "bun run typecheck",
    "test": "bun test",
    "build": "bun run build",
    "check": "bun run check"
  },
  "gotchas": [
    {
      "issue": "node_modules symlinks break workspace links",
      "resolution": "always run install, never symlink",
      "addedAt": "2024-01-15T10:30:00Z",
      "lastVerified": "2024-01-15T10:30:00Z",
      "hitCount": 3
    }
  ],
  "parallelizable": ["lint", "typecheck", "test"],
  "lastUpdated": "2024-01-15T10:30:00Z",
  "lastFullRefresh": "2024-01-15T10:30:00Z"
}
```

### Cache Lifecycle

#### Read (every run)
1. Load `.claude/repo-cache.json`
2. If missing -> run full detection
3. If `lastFullRefresh` > 30 days -> run monthly maintenance
4. Otherwise -> use cached values

#### Update (on gotcha)
When a command fails or behaves unexpectedly:
1. Check if gotcha already exists (match on `issue`)
2. If exists -> increment `hitCount`, update `lastVerified`
3. If new -> add gotcha with current timestamp

#### Monthly Maintenance (every 30 days)
When `lastFullRefresh` is >30 days old, run a full refresh:

1. **Re-detect tooling** - Lockfiles or CI config may have changed
2. **Validate commands** - Check if cached commands still exist in package.json/Makefile
3. **Prune stale gotchas** - Remove gotchas that:
   - Reference commands that no longer exist
   - Haven't been verified (`lastVerified`) in 90+ days
   - Have `hitCount: 0` after 60+ days (never actually triggered)
4. **Verify gotchas** - For remaining gotchas, test if still relevant
5. **Update timestamps** - Set `lastFullRefresh` to now

```
Monthly Maintenance Checklist:
[ ] Re-read package.json scripts
[ ] Re-read CI workflow files
[ ] Check each cached command still exists
[ ] Remove gotchas for deleted commands
[ ] Remove unverified gotchas (90+ days)
[ ] Remove never-hit gotchas (60+ days, hitCount: 0)
[ ] Update lastFullRefresh timestamp
```

### Cache Rules

1. **Read cache first** - If `.claude/repo-cache.json` exists, load it
2. **Detect on miss** - If no cache, run full detection
3. **Update immediately on gotcha** - Don't wait; write cache after each discovery
4. **Monthly refresh** - Full re-detection + gotcha pruning every 30 days
5. **Cache is committed** - Shared across worktrees and team via git

---

## Tooling Detection

Detection discovers commands from four sources, in priority order:

### Priority 1: CLAUDE.md

Read the project's `CLAUDE.md`. It often documents the exact commands required before committing (e.g., `make test`, `make lint`). Extract commands from code blocks and command headings.

### Priority 2: Build tool files

| File | Detects | How to parse |
|------|---------|--------------|
| `Makefile` | make targets | `grep -E '^\w+:' Makefile` — map `fmt`→format, `lint`→lint, `vet`→vet, `test`→test, `build`→build |
| `justfile` | just recipes | `just --list` — same mapping as Makefile |
| `package.json` | npm/bun/yarn scripts | Parse `scripts` object — map `check`, `lint`, `format`, `typecheck`, `test`, `build` |
| `Cargo.toml` | rust | `cargo clippy`, `cargo test`, `cargo fmt --check`, `cargo build` |
| `go.mod` | go | Only as fallback if no Makefile — `go vet`, `go test`, `go build` |
| `pyproject.toml` | python | Check for ruff, pytest, mypy |
| `requirements.txt` | python | Same as pyproject.toml |

**Key rule:** Store the build-tool command (e.g., `make lint`), not the underlying tool (e.g., `golangci-lint run`). This way, if the Makefile changes what a target does, we pick it up on next refresh.

### Priority 3: CI config

| File | Detects |
|------|---------|
| `.github/workflows/*.yml` | GitHub Actions steps |
| `.gitlab-ci.yml` | GitLab CI jobs |
| `Jenkinsfile` | Jenkins stages |
| `.circleci/config.yml` | CircleCI jobs |

Cross-reference CI steps against discovered commands. If CI runs a check not yet in the cache (e.g., `golangci-lint` in a GitHub Action but no Makefile target), add it.

### Priority 4: Language defaults (fill gaps only)

Only used for command types not discovered from any other source:

| Language | format | lint | vet | test | build |
|----------|--------|------|-----|------|-------|
| Go | `gofmt -l .` | `golangci-lint run` | `go vet ./...` | `go test -race ./...` | `go build ./...` |
| Rust | `cargo fmt --check` | `cargo clippy -- -D warnings` | - | `cargo test` | `cargo build` |
| TypeScript | - | `npx eslint .` | - | `npx vitest run` | `npx tsc --noEmit` |
| Python | `ruff format --check .` | `ruff check .` | - | `pytest` | - |
| Ruby | `bundle exec rubocop --format quiet` | - | - | `bundle exec rspec` | - |

### Lockfile → Package Manager

| File | Package Manager |
|------|-----------------|
| `bun.lockb` | bun |
| `package-lock.json` | npm |
| `yarn.lock` | yarn |
| `pnpm-lock.yaml` | pnpm |

---

## Dependency Installation

| Package Manager | Check | Install Command |
|-----------------|-------|-----------------|
| bun | `node_modules/.bun` missing | `bun install` |
| npm | `node_modules` missing | `npm ci` |
| yarn | `node_modules` missing | `yarn install --frozen-lockfile` |
| pnpm | `node_modules` missing | `pnpm install --frozen-lockfile` |
| cargo | (handled by cargo) | - |
| go | (handled by go) | `go mod download` |
| python | `.venv` missing | `python -m venv .venv && pip install -r requirements.txt` |

**Note:** Never symlink dependency folders between worktrees. Package managers use internal symlinks that break.

---

## Parallelization Strategy

| Task | Can Parallelize? | Notes |
|------|------------------|-------|
| Cache read | Yes | Fast, do first |
| Git status/diff | Yes | Independent |
| PR existence check | Yes | Independent |
| Dependency install | No | Must complete before checks |
| Format | Depends | Run before lint if lint doesn't auto-fix |
| Lint | Yes | Independent of test/typecheck |
| Typecheck | Yes | Independent of lint/test |
| Test | Yes | Independent of lint/typecheck |
| Build | No | Often depends on typecheck passing |

---

## Self-Correction & Learning

### On Failure: Record Gotcha Immediately

When something fails unexpectedly, update the cache right away:

```json
{
  "gotchas": [
    {
      "issue": "typecheck fails with 'cannot find module'",
      "resolution": "run bun install first, even if node_modules exists",
      "addedAt": "2024-01-20T14:00:00Z",
      "lastVerified": "2024-01-20T14:00:00Z",
      "hitCount": 1
    }
  ]
}
```

**What to record:**
- Command failures with non-obvious causes
- Order dependencies (X must run before Y)
- Environment requirements (env vars, services)
- Workaround for known issues

**Do NOT record:**
- Obvious errors (typos, missing files)
- One-time issues unlikely to recur
- User-specific problems

### On Success After Previous Failure: Verify Gotcha

If a previously-failing command now succeeds:
1. Find the relevant gotcha
2. Update `lastVerified` to now
3. Increment `hitCount` (confirms it's still useful)

### Gotcha Pruning Rules

| Condition | Action |
|-----------|--------|
| References deleted command | Remove |
| `lastVerified` > 90 days | Remove (stale) |
| `hitCount: 0` && `addedAt` > 60 days | Remove (never triggered) |
| `hitCount > 5` && `lastVerified` recent | Keep (proven useful) |

---

## Language-Specific Notes

These are no longer hardcoded fallbacks — all commands are discovered during detection and stored in the cache. These notes cover quirks to be aware of during detection:

- **Go**: Makefile targets are the primary source. If no Makefile, check for `golangci-lint` (which bundles `goimports`, `go vet`, and many linters). Don't assume `go vet` alone is sufficient — most Go projects use `golangci-lint`.
- **Rust with justfile**: `just check` or `just ci` typically orchestrates all checks. Prefer the justfile over raw cargo commands.
- **Rust without justfile**: `cargo clippy -- -D warnings` is stricter than default clippy. `cargo fmt --check` catches formatting without modifying files.
- **Python**: `ruff` is increasingly common and replaces both `flake8` and `black`. Check `pyproject.toml` for `[tool.ruff]` section.
- **TypeScript**: The `check` script in package.json often runs both typecheck and lint. If it exists, prefer it over running lint and typecheck separately.

---

## Commit Message Examples

```
fix(auth): handle expired tokens gracefully
feat(api): add pagination to list endpoints
refactor(db): extract connection pooling
docs(readme): update installation steps
test(utils): add edge case coverage
chore(deps): update dependencies
```

---

## PR Body Example

```markdown
## Summary
- Add pagination support to /users and /posts endpoints
- Default page size: 20, max: 100

## Test plan
- GET /users returns paginated response
- GET /users?page=2&limit=10 works
- Invalid page params return 400
```

---

## Failure Handling

| Scenario | Action |
|----------|--------|
| No cache | Detect tooling, create cache |
| Stale cache (>7 days) | Re-detect, update cache |
| Dependencies missing | Install using detected package manager |
| CI check fails | Stop, report error, do not commit |
| No changes | Report "nothing to commit", stop |
| Already committed | Skip to push/PR |
| PR exists | Report existing PR URL |
| Not on feature branch | Warn user, suggest branching |
| Command not found | Update cache gotchas, try alternatives |
