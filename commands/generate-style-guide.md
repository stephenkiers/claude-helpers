---
description: Auto-generate a personal style-guide.json from your review history, with human confirmation before writing.
allowed-tools: Bash(gh api user*), Bash(gh api graphql*), Bash(gh search prs*), Bash(gh repo view*), Bash(gh search repos --owner*), Bash(cp*), Bash(python3*), Bash(test*), Read, Write, Glob, Grep
---

# Generate Style Guide

Auto-generate a personal `~/.claude/style-guide.json` file from your PR review history. The result is a portable tone/style guide capturing how you actually write code review comments — useful for `/pr-comment-guide` to model on, and as a shared reference for review consistency across your team.

**Arguments:** $ARGUMENTS (optional: a repo name, a comma-separated repo list, or `--org {org}` to scope the search — defaults to the current repo if not specified)

## Step 1: Identify the invoking user

Fetch the GitHub login of the invoking user:
```bash
gh api user -q '.login'
```

If this command fails or returns an empty login, stop immediately and report the `gh` error to the user — do not proceed to later steps with an unresolved login.

## Step 2: Resolve scope

Parse `$ARGUMENTS` to determine which repos to search:
- If `$ARGUMENTS` is empty or `.` → use the current repo (detected via `gh repo view --json nameWithOwner -q '.nameWithOwner'`).
- If `$ARGUMENTS` contains `--org {org}` → search the named org (but list repos first via `gh search repos --owner {org} --limit 100` to stay bounded).
- Otherwise, treat `$ARGUMENTS` as a comma-separated `repo-list`, where each item is either an `owner/repo` or just a repo name (prepend current org if needed).

**Critical:** Never crawl all of GitHub — scope is always bounded to what's given or the current repo. State this explicitly in your narration.

**Cost estimate for `--org` mode:** Before running Steps 3-4 across every discovered repo, narrate an estimate of the total API calls involved (repo count × PR search limit 30 × per-PR GraphQL fetch) and ask the user to confirm or narrow the scope before proceeding. This is soft guidance, not a hard cap — the user may proceed with a large org if they choose to, but they should never be surprised by how long or how expensive it runs.

## Step 3: Find candidate PRs

For each scoped repo, search for PRs the user has commented on:
```bash
gh search prs --repo {owner}/{repo} --commenter {login} --state all --limit 30
```

Collect these PR URLs for the next step.

## Step 4: Fetch user's own review-thread comments

Use the GraphQL API to fetch all review-thread comments on the found PRs, filtered client-side to your own comments only — never anyone else's:

```bash
gh api graphql -f query='
  query($owner: String!, $repo: String!, $pr: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100) {
          nodes {
            comments(first: 10) {
              nodes {
                author { login }
                body
                path
                line
                url
              }
            }
          }
        }
      }
    }
  }
' -f owner={owner} -f repo={repo} -F pr={pr_number} \
  --jq '[.data.repository.pullRequest.reviewThreads.nodes[].comments.nodes[] | select(.author.login == "{login}")]'
```

Adapt this pattern from `commands/pr-comments.md`'s GraphQL section for `reviewThreads`. `{login}` here is the value resolved in Step 1 — interpolate it directly into the `--jq` filter string exactly like `{owner}`/`{repo}`/`{pr_number}` above, so the comparison actually runs (a bare `$login` inside the `--jq` expression is not a bound variable and would silently match nothing, or error).

**This command is read-only.** Never construct or execute a GraphQL `mutation { ... }` operation — only the `query` shown above.

If a given PR's GraphQL fetch errors, skip that PR and continue with the rest; note the count of skipped PRs in the final report.

## Step 5: Mechanically pre-filter

Drop noise, not short comments: bot markers, exact "lgtm"/"nit"-only comments, and exact duplicates of another kept comment. Do **not** filter by word count — a load-bearing feature of this file is capturing the invoking user's actual tone, and short, direct comments (e.g. "why not just delete this?") are exactly the voice worth keeping. Keep only real code review observations/questions.

If fewer than ~4-6 substantive comments survive this filter, tell the user explicitly (e.g. "Only 3 substantive comments found across the searched scope — style guide quality will be limited") and offer to widen scope (more repos, `--org`, or removing scope restrictions) rather than proceeding silently with too little material.

## Step 6: LLM distillation step (in-conversation)

Analyze the collected comments and:
1. **Pick 6-12 representative examples** — verbatim or lightly trimmed (never rewritten into a different register). These should span different concern types (e.g., some are security-focused, some are architecture, some are testing) so the examples show your full review voice, not just one flavor.
2. **Draft 3-6 `toneNotes`** — short freeform observations about what these examples have in common. Things like: "prefers questions over assertions," "no hedging or preamble," "names fixes directly," "no reviewer attribution," etc.
3. **Flag and genericize sensitive content** — if any example contains employer names, internal URLs, proprietary jargon, or client names, explicitly call it out (e.g., "Example 4 references internal tool X—stripping it for portability"). Do this explicitly, never silently rewrite it. If an example is too tied to proprietary context to genericize cleanly, drop it and pick a different one.

## Step 7: Show the full draft and get confirmation

Before presenting the draft, check whether `~/.claude/style-guide.json` already exists:
```bash
test -f ~/.claude/style-guide.json && echo exists || echo none
```
If it exists, read it and summarize for the user how it compares to the new draft — how many examples/toneNotes it currently has, and whether it looks hand-edited (`"source": "manual"` or missing `"source"`) vs. previously generated (`"source": "generated"`). If it looks hand-edited, say so explicitly: "Your current `~/.claude/style-guide.json` does not look machine-generated — overwriting it will discard any manual edits."

Present the full draft to the user — all 6-12 examples and all tone notes — as a JSON preview (legible formatted JSON, not raw string dump). Explicitly state, naming the exact destination path and what happens to any existing file:
> "This draft is derived from your actual review comments and shapes how `/pr-comment-guide` will model tone going forward. Please review for accuracy and sensitive content, then confirm to write it to `~/.claude/style-guide.json`[, replacing your existing file there — a backup will be saved to `~/.claude/style-guide.json.bak`]."

This is the **load-bearing safeguard** — machine-derived files that shape review tone must be human-reviewed before they're trusted. This continues the confirm-before-write pattern ADR-0007 established for `decisions.yaml`: that pattern was human-confirmed from inception, so this design doesn't fix a gap in it, it reuses it. Never auto-write without explicit confirmation.

Wait for user confirmation. If they decline, do not write anything.

## Step 8: Write to `~/.claude/style-guide.json`

If confirmed:

1. **Back up any existing file first.** If `~/.claude/style-guide.json` exists:
   ```bash
   cp ~/.claude/style-guide.json ~/.claude/style-guide.json.bak
   ```
2. **Construct the JSON** with:
   - `"version": 1`
   - `"source": "generated"`
   - `"generatedAt": "<ISO-8601 timestamp>"` (e.g., `"2026-09-02T15:30:45Z"`)
   - `"scope": {"repos": ["owner/repo", ...]}` listing the repos searched
   - `"examples": [...]` with the confirmed examples
   - `"toneNotes": [...]` with the confirmed notes

   No `_instructions` field (that's only in the template).
3. **Validate, then write atomically** — write to a temp file first, then move it into place, so an interrupted write never leaves a partial or corrupt file at the real path:
   ```bash
   python3 << 'EOF'
   import json, os
   data = {
       "version": 1,
       "source": "generated",
       "generatedAt": "2026-09-02T15:30:45Z",
       "scope": {"repos": ["owner/repo"]},
       "examples": [...],
       "toneNotes": [...],
   }
   json.dumps(data)  # validate before touching disk
   path = os.path.expanduser("~/.claude/style-guide.json")
   tmp_path = path + ".tmp"
   with open(tmp_path, 'w') as f:
       json.dump(data, f, indent=2)
   os.replace(tmp_path, path)  # atomic on POSIX
   EOF
   ```

Report success, including whether a backup was made: "Wrote `~/.claude/style-guide.json` with 12 examples and 5 tone notes, scoped to ['acme/backend', 'acme/frontend']. Your previous file was backed up to `~/.claude/style-guide.json.bak`."

## Constraints

- Never crawl all of GitHub — always bound scope to user arguments or current repo.
- Never collect or include comments from other reviewers — only the invoking user's own comments.
- Fetch only the invoking user's review-thread comments (`author.login == {login}`), not all comments on a PR.
- Never issue a GraphQL mutation — this command only ever runs `query` operations, never `mutation`.
- Require explicit user confirmation before writing anything to disk (continues the confirm-before-write pattern from ADR-0007).
- Back up and never silently overwrite an existing `~/.claude/style-guide.json` — always confirm, echo the destination, and back up first (see Steps 7-8).
- Genericize sensitive content explicitly, never silently.
- Do not rewrite examples into a different register or tone — keep them verbatim.
