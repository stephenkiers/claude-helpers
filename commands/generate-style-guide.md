---
description: Auto-generate a personal style-guide.json from your review history, with human confirmation before writing.
allowed-tools: Bash(gh api:*), Bash(gh search:*), Bash(gh pr:*), Read, Write, Glob, Grep
---

# Generate Style Guide

Auto-generate a personal `~/.claude/style-guide.json` file from your PR review history. The result is a portable tone/style guide capturing how you actually write code review comments — useful for `/pr-comment-guide` to model on, and as a shared reference for review consistency across your team.

**Arguments:** $ARGUMENTS (optional: a repo name, a comma-separated repo list, or `--org {org}` to scope the search — defaults to the current repo if not specified)

## Step 1: Identify the invoking user

Fetch the GitHub login of the invoking user:
```bash
gh api user -q '.login'
```

## Step 2: Resolve scope

Parse `$ARGUMENTS` to determine which repos to search:
- If `$ARGUMENTS` is empty or `.` → use the current repo (detected via `gh api repo -q '.full_name'` or similar).
- If `$ARGUMENTS` contains `--org {org}` → search the named org (but list repos first via `gh search repos --owner {org} --limit 100` to stay bounded).
- Otherwise, treat `$ARGUMENTS` as a comma-separated `repo-list`, where each item is either an `owner/repo` or just a repo name (prepend current org if needed).

**Critical:** Never crawl all of GitHub — scope is always bounded to what's given or the current repo. State this explicitly in your narration.

## Step 3: Find candidate PRs

For each scoped repo, search for PRs the user has commented on:
```bash
gh search prs --repo {owner}/{repo} --commenter {login} --state all --limit 30
```

Collect these PR URLs for the next step.

## Step 4: Fetch user's own review-thread comments

Use the GraphQL API to fetch all review-thread comments on the found PRs, filtered client-side to `author.login == $login` (only your own comments, never anyone else's):

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
  --jq '[.data.repository.pullRequest.reviewThreads.nodes[].comments.nodes[] | select(.author.login == $login)]'
```

Adapt this pattern from `commands/pr-comments.md`'s GraphQL section for `reviewThreads`.

## Step 5: Mechanically pre-filter

Drop trivial/short comments (fewer than ~20 words), "LGTM"-style noise, bot artifacts, and comments that are not substantive review feedback. Keep only real code review observations/questions.

## Step 6: LLM distillation step (in-conversation)

Analyze the collected comments and:
1. **Pick 6-12 representative examples** — verbatim or lightly trimmed (never rewritten into a different register). These should span different concern types (e.g., some are security-focused, some are architecture, some are testing) so the examples show your full review voice, not just one flavor.
2. **Draft 3-6 `toneNotes`** — short freeform observations about what these examples have in common. Things like: "prefers questions over assertions," "no hedging or preamble," "names fixes directly," "no reviewer attribution," etc.
3. **Flag and genericize sensitive content** — if any example contains employer names, internal URLs, proprietary jargon, or client names, explicitly call it out (e.g., "Example 4 references internal tool X—stripping it for portability"). Do this explicitly, never silently rewrite it. If an example is too tied to proprietary context to genericize cleanly, drop it and pick a different one.

## Step 7: Show the full draft and get confirmation

Present the full draft to the user — all 6-12 examples and all tone notes — as a JSON preview (legible formatted JSON, not raw string dump). Explicitly state:
> "This draft is derived from your actual review comments and shapes how `/pr-comment-guide` will model tone going forward. Please review for accuracy and sensitive content, then confirm to write it to `~/.claude/style-guide.json`."

This is the **load-bearing safeguard** — machine-derived files that shape review tone must be human-reviewed before they're trusted. Direct lineage from ADR-0007's lesson about `decisions.yaml`: auto-appended machine files were reverted in favor of hand-authored/confirmed config. Never auto-write without explicit confirmation.

Wait for user confirmation. If they decline, do not write anything.

## Step 8: Write to `~/.claude/style-guide.json`

If confirmed, write the file with:
- `"version": 1`
- `"source": "generated"`
- `"generatedAt": "<ISO-8601 timestamp>"` (e.g., `"2026-09-02T15:30:45Z"`)
- `"scope": {"repos": ["owner/repo", ...]}` listing the repos searched
- `"examples": [...]` with the confirmed examples
- `"toneNotes": [...]` with the confirmed notes

No `_instructions` field (that's only in the template). Validate the JSON before writing:
```bash
python3 -c "import json; json.dump(...); json.load(...)"
```

Report success: "Wrote `~/.claude/style-guide.json` with 12 examples and 5 tone notes, scoped to ['acme/backend', 'acme/frontend']."

## Constraints

- Never crawl all of GitHub — always bound scope to user arguments or current repo.
- Never collect or include comments from other reviewers — only the invoking user's own comments.
- Fetch only the invoking user's review-thread comments (`author.login == $login`), not all comments on a PR.
- Require explicit user confirmation before writing anything to disk (ADR-0007 lineage).
- Genericize sensitive content explicitly, never silently.
- Do not rewrite examples into a different register or tone — keep them verbatim.
