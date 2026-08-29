---
name: shipit
description: Use when user says "/shipit", "ship it", "commit and pr", "create pr", or wants to commit changes and create a pull request. Detects tooling, runs CI checks locally, creates a minimal commit, and creates or refreshes the PR.
model: haiku
---

# Shipit

Run CI checks, commit, and create or refresh the PR — every run regenerates the title and body
from the whole branch, and refreshing an already-open PR is normal, ongoing behavior, not an edge
case. For edge cases and maintenance, read `~/.claude/prompts/shipit-reference.md`.

## 0. Telemetry: mark command start

Telemetry is local, observational, and best-effort — it must never block or fail `/shipit`.
Every call is non-fatal (see docs/metrics.md's telemetry call-site conventions for why `*-begin` uses `|| true` for non-fatal best-effort):

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" command-begin --command shipit >/dev/null 2>&1 || true
```

Correlation between stages and commands is automatic via a session-scoped state file keyed on the `CLAUDE_CODE_SESSION_ID` environment variable (which persists across separate Bash tool calls). This means explicit `--command-id` and `--stage-id` flags are now optional — they're resolved automatically when omitted. Shell variables set in one Bash tool call do **not** survive into a later, separate Bash tool call, which is why this doc no longer threads `TELEMETRY_CMD_ID`/`TELEMETRY_STAGE_ID` through shell variables across call boundaries. See `docs/metrics.md`'s "Telemetry Call-Site Conventions" section for the full mechanism.

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

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage run-checks >/dev/null 2>&1 || true
```

**Skip if recently run:** If lint/typecheck/test were run earlier in this conversation and all passed, skip re-running them. Trust the prior results.

Run every non-null command from the cache in this order: format → check (composite, replaces lint+typecheck if present) → parallelizable group → build.

```bash
# Run checks via deterministic CLI
CHECK_RESULT=$(python3 -m scripts.workflow.cli checks run - < <(cat .claude/repo-cache.json 2>/dev/null || echo '{}'))
CHECK_PASSED=$(printf '%s' "$CHECK_RESULT" | jq -r '.all_passed // false')
FAILED_AT=$(printf '%s' "$CHECK_RESULT" | jq -r '.failed_at // empty')
```

**If a command is null in the cache, skip it.** Don't fall back to language defaults at runtime — all defaults were already resolved during detection and written to the cache.

**On failure:** Stop immediately, report error, record gotcha in cache. Do NOT commit.

```bash
if [ "$CHECK_PASSED" != "true" ]; then
  # Check failed
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage run-checks --outcome failure --failure-class test_failure 2>/dev/null || true
  python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command shipit --outcome failure --failure-class test_failure 2>/dev/null || true
  exit 1
fi
```

On success, continue below and close out `run-checks` there.

## 4. Commit

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage run-checks --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage commit >/dev/null 2>&1 || true
```

Write commit message:
- Format: `<type>(<scope>): <short description>`
- Types: feat, fix, refactor, docs, test, chore
- Under 72 chars
- **NEVER mention Claude, AI, LLM, or add Co-Authored-By**

(The wrapper's modeling step handles this; the CLI just commits the message already written.)

## 5. Push & PR

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage commit --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage create-pr >/dev/null 2>&1 || true
```

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
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-pr --outcome failure --failure-class other 2>/dev/null || true
  python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command shipit --outcome failure --failure-class other 2>/dev/null || true
  exit 1
fi

# Determine the base to diff the whole branch against: the stacked parent if stacked,
# otherwise the repo's default branch (same detection pattern used in worktree-reference.md).
if [ "$STACK_IS_STACKED" = "true" ]; then
  DIFF_BASE="$STACK_PARENT_BRANCH"
else
  DIFF_BASE=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|^refs/remotes/origin/||')
  DIFF_BASE="${DIFF_BASE:-main}"
fi

git log "$DIFF_BASE"..HEAD --oneline
git diff "$DIFF_BASE"...HEAD --stat
```

**Compute the PR title and body by reasoning over the branch, not by running more bash.** The
commands above give you the whole branch's commit history and diff shape — use them (not just the
latest commit) so the title and body stay accurate as commits accumulate across repeated `/shipit`
runs on the same PR. Gather:

- **Title**: a concise (<70 char), imperative summary of the whole branch's diff — same register as
  a commit subject (`add X`, `fix Y`, `rewrite Z`), not a restatement of the last commit alone.
  Assign it to `$TITLE`.
- **Why this PR exists**: if `$ISSUE_NUM` is set, run `gh issue view "$ISSUE_NUM" --json
  title,body` (cache-first against `.claude/github-cache.json`'s `.issue` section, same as the
  `ISSUE_NUM` lookup above); otherwise derive it from the user's original request earlier in this
  conversation. 1-3 sentences on the problem/need being addressed.
- **What it does**: a bulleted list of concrete behavior/code changes, derived from the `git log`
  / `git diff --stat` output above.
- **What major decisions were made**: pull from a plan file's "Decisions Made" section if one
  exists for this work (e.g. an `/expert-plan` output), or notable tradeoffs surfaced during
  implementation. If genuinely none, **omit this whole heading** — do not write "None".
- **What a reviewer should pay attention to**: a bulleted list naming specific files/areas from the
  branch diff worth a closer look (security-sensitive files, concurrency, migrations, new external
  dependencies) and why.
- **How to verify**: a bulleted list of concrete steps/commands to confirm the change works — same
  content the old "Test plan" section held.

**Guardrail:** you are synthesizing this body from conversation context, which may contain
internal-only detail (credentials, other people's names, private ticket notes). Write PR-body prose
as if it will be public — don't restate secrets or non-public context.

```bash
# Prepare temp file for PR body (multi-line content too risky to inline)
TMP_BODY=$(mktemp)
echo "TMP_BODY=$TMP_BODY"
```

**The block below is a shape to fill in, not a command to run verbatim.** Do not execute the
`cat > "$TMP_BODY"` heredoc as written — its bracketed placeholders are not real content. Instead,
write `$TMP_BODY` yourself (e.g. with the Write tool, or your own heredoc with the placeholders
replaced) using the five headings in this exact order, each followed by the real content you
reasoned out above. Omit the "What major decisions were made" heading entirely if there were
none — do not leave a heading with an empty or "None" body.

**Important:** Note the exact path printed by the `echo "TMP_BODY=$TMP_BODY"` line above; reuse
that path verbatim for the Write-tool step below and every later bash fence in this flow. Do not
re-run `mktemp` and do not guess the path.

```
## Why this PR exists
<1-3 sentences: the problem/need this addresses>

## What it does
<bulleted list of concrete behavior/code changes>

## What major decisions were made
<bulleted list of notable tradeoffs or choices — omit this whole section if none>

## What a reviewer should pay attention to
<bulleted list of specific files/areas worth a closer look, and why>

## How to verify
<bulleted list of how to confirm the change works>
```

Once you've written that real content to `$TMP_BODY`, continue:

```bash
# Prepend "Closes #N" if issue is set
if [ -n "$ISSUE_NUM" ]; then
  {
    echo "Closes #$ISSUE_NUM"
    echo ""
    cat "$TMP_BODY"
  } > "${TMP_BODY}.with-issue"
  mv "${TMP_BODY}.with-issue" "$TMP_BODY"
fi

# Append "Stacked on #N" if applicable
if [ "$STACK_IS_STACKED" = "true" ] && [ -n "$STACK_PARENT_PR" ]; then
  {
    cat "$TMP_BODY"
    echo ""
    echo "Stacked on #$STACK_PARENT_PR"
  } > "${TMP_BODY}.with-stack"
  mv "${TMP_BODY}.with-stack" "$TMP_BODY"
fi

# Check if PR already exists (cache-first)
PR_NUM=""
if [ -f ".claude/github-cache.json" ]; then
  PR_NUM=$(jq -r '.pr.number // empty' < .claude/github-cache.json 2>/dev/null)
fi
if [ -z "$PR_NUM" ]; then
  # Fallback: try gh pr view
  PR_NUM=$(gh pr view --json number -q '.number' 2>/dev/null || echo "")
fi
```

**If the PR already exists, merge into its current body before handing it to the CLI — never
blind-overwrite.** This is the firm rule the rewrite exists for: a human may have hand-edited the
description since the last `/shipit` run, and that work must not be silently destroyed.

```bash
if [ -n "$PR_NUM" ]; then
  TMP_BODY_CURRENT=$(mktemp)
  if ! gh pr view "$PR_NUM" --json body -q '.body' > "$TMP_BODY_CURRENT"; then
    echo "ERROR: failed to fetch the existing PR body — stopping rather than risking a blind overwrite." >&2
    exit 1
  fi
  echo "TMP_BODY_CURRENT=$TMP_BODY_CURRENT"
fi
```

Note: There is a window between fetching the current PR body and the final apply-step write
during which a human could edit the PR description concurrently (e.g. while the agent is
reasoning over the diff and drafting the merge); this implementation does not guard against that
race (no optimistic-concurrency / `updatedAt` check) — it is an accepted, documented trade-off,
not an oversight.

If `$PR_NUM` was set above, note the exact path printed by `echo "TMP_BODY_CURRENT=$TMP_BODY_CURRENT"`
and reuse it verbatim — do not re-run `mktemp` and do not guess the path. Read `$TMP_BODY_CURRENT`
(the PR's live body) and apply this ingest-then-merge policy, rewriting `$TMP_BODY` in place with
the result:

1. **Split the current body into recognized vs. novel content.** Recognized content is exactly:
   the `Closes #N` line, the five standard headings (`## Why this PR exists`, `## What it does`,
   `## What major decisions were made`, `## What a reviewer should pay attention to`,
   `## How to verify`), the `Stacked on #N` line, and a prior
   `## Notes carried over from a previous description` section. Everything else — an unfamiliar
   heading, a checklist, free text a human added — is **novel content**.
2. **Recognized sections are always regenerated** from the `$TMP_BODY` template you just built —
   discard the old text of those sections entirely.
3. **Small, clearly-delimited novel content** (its own heading or clearly separate block, not
   interleaved inside one of the five recognized sections) → carry it forward verbatim under a
   `## Notes carried over from a previous description` heading, appended at the end of the new
   body (merge with any existing `## Notes carried over…` section rather than duplicating it).
4. **Large or structurally ambiguous novel content** (e.g. free text mixed into the middle of a
   recognized section, so it can't be cleanly separated) → do not guess. Stop and ask the user
   (via a direct question, not a silent decision) how to proceed before overwriting.
5. Write the final merged body — regenerated recognized sections plus any carried-over novel
   content — back to `$TMP_BODY`, then clean up: `rm -f "$TMP_BODY_CURRENT"` (only if it was
   created above).

**With `$TMP_BODY` finalized, use the deterministic CLI to execute push and PR creation/update:**

```bash
# Plan the shipit operation (captures current state: branch, HEAD SHA, cache hash)
TMP_MSG=$(mktemp)
# Write the commit message to TMP_MSG here (if not already done above)

SHIPIT_PLAN=$(python3 -m scripts.workflow.cli shipit plan "$TMP_MSG" --body-file "$TMP_BODY" --title "$TITLE")
PLAN_OK=$(printf '%s' "$SHIPIT_PLAN" | jq -r '.plan_hash // empty')
if [ -z "$PLAN_OK" ]; then
  echo "ERROR: Failed to plan shipit" >&2
  exit 1
fi

# Apply the plan (stages, commits, pushes, creates/edits PR, writes cache).
# The CLI detects pr_exists from the same cache read above and calls `gh pr edit`
# instead of `gh pr create` accordingly — it does not re-decide create-vs-edit itself.
SHIPIT_RESULT=$(echo "$SHIPIT_PLAN" | python3 -m scripts.workflow.cli shipit apply -)
SHIPIT_OK=$(printf '%s' "$SHIPIT_RESULT" | jq -r '.success // false')
if [ "$SHIPIT_OK" != "true" ]; then
  ERROR=$(printf '%s' "$SHIPIT_RESULT" | jq -r '.error // "Unknown error"')
  echo "ERROR: shipit apply failed: $ERROR" >&2
  exit 1
fi

rm -f "$TMP_BODY" "$TMP_MSG"
```

**The CLI's `shipit apply` step already writes `pr` and `stack` (isStacked/parentBranch/parentPr)
data back to `.claude/github-cache.json`** — no separate manual cache-write step is needed here.

If stacked, look up the remote stack number (only needed for the `/cleanup` restack runbook;
the CLI does not compute or cache this field):

```bash
if [ "$STACK_IS_STACKED" = "true" ]; then
  PR_URL=$(printf '%s' "$SHIPIT_RESULT" | jq -r '.pr_url // empty')
  PR_NUM=$(echo "$PR_URL" | grep -oE '[0-9]+$')
  REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || echo "")
  if [ -n "$REPO" ] && [ -n "$PR_NUM" ]; then
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
    if [ -n "$STACK_NUM" ]; then
      TMP=$(mktemp .claude/github-cache.json.XXXXXX)
      jq --argjson stackNum "$STACK_NUM" '.stack.stackNumber = $stackNum' < .claude/github-cache.json > "$TMP" \
        && mv "$TMP" .claude/github-cache.json \
        || { rm -f "$TMP"; echo "WARNING: failed to write stackNumber to .claude/github-cache.json." >&2; }
    fi
  fi
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

**If PR exists:** Fetch current body, preserve novel content, regenerate recognized sections, update PR with refreshed title and body, then report URL.
**If on main/master:** Warn user, suggest creating a branch.
**If branch has issue number:** Include "Closes #N" in PR body to auto-close issue on merge.

### Telemetry: mark command end (success path)

Once the PR is created (or confirmed to already exist) and reported to the user, close out
`create-pr` and the overall command as success — non-fatal, same as every telemetry call above:

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-pr --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command shipit --outcome success 2>/dev/null || true
```

## On Failure

Record gotcha in cache: `{"issue": "what failed", "resolution": "how to fix"}`

If failure happened during push/PR creation (Step 5) rather than the earlier checks step, close
out telemetry for that failure too — never let this block reporting the failure to the user:

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-pr --outcome failure --failure-class other 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command shipit --outcome failure --failure-class other 2>/dev/null || true
```

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
| PR exists | Refresh title and body (preserving novel content), report URL |
| On main branch | Warn, suggest branching |
| Branch is stacked | Create PR with `--base "$STACK_PARENT_BRANCH"` (this establishes the chain — do **not** auto-run `gh stack link`, it destructively repoints the bottom PR's base to trunk). Cache stack metadata; push via `gh stack sync` (single-driver) or `--force-with-lease` (per-branch) |
| Branch not stacked | Create PR with repo default base, mark `stack.isStacked=false` in cache |
