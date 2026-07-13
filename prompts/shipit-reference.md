# Shipit Reference Documentation

<!-- Lives in prompts/, not commands/: every .md file in ~/.claude/commands/ is registered as an
     invocable slash command regardless of frontmatter, and this is a reference doc, not a command. -->

This file contains detailed documentation for edge cases, maintenance, and customization. The main `shipit.md` handles the happy path. Read this when:
- A command fails unexpectedly
- You need to customize behavior for a project
- Monthly maintenance is needed (cache > 30 days old)

---

## Extending This Command

`/shipit` is a **global command** installed in `~/.claude/commands/` (this reference doc lives in
`~/.claude/prompts/`). Projects can override or extend it:

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

The full cache schema lives in `shipit.md` (Step 1). The one field detailed only here is the
`gotchas` array — each entry is `{issue, resolution, addedAt, lastVerified, hitCount}` (see
Self-Correction & Learning below).

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

The detection sources, priority order, and language-default tables live in `shipit.md`
(Step 1.5, Sources 0–4). Only the lockfile mapping below is not covered there.

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

The common scenarios are in `shipit.md`'s Quick Reference. Additional cases:

| Scenario | Action |
|----------|--------|
| Already committed | Skip to push/PR |
| Command not found | Update cache gotchas, try alternatives |
