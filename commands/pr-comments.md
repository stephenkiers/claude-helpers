---
description: Review PR comments from all reviewer types (Copilot, Olive, human) and decide whether each should be addressed, ignored, or discussed.
allowed-tools: Bash(gh api:*), Bash(gh pr:*), Bash(gh pr diff:*), Bash(gh pr view:*), Bash(cat:*), Bash(jq:*), Read, Glob, Grep
---

# PR Comments

Review all unresolved PR review comments — from GitHub Copilot, olive-agent, and human reviewers — and decide whether each should be addressed, ignored, or discussed.

**Arguments:** $ARGUMENTS (optional PR number, URL, or `owner/repo#number` — if omitted, use cached PR or detect from current branch)

## Step 1: Identify the PR

Use the **cache-first** pattern (same as /shipit):

1. If `$ARGUMENTS` contains a PR number or URL, use that.
2. Otherwise, check `.claude/github-cache.json` for cached PR metadata:
   ```bash
   cat .claude/github-cache.json 2>/dev/null | jq '.pr'
   ```
   If `pr.number` and `pr.state == "OPEN"` exist, use them.
3. Fall back to detecting from the current branch:
   ```bash
   gh pr view --json number,title,headRefName,baseRefName,url 2>/dev/null
   ```
4. If no PR is found, ask the user.

Also load the cached issue context if available (for understanding the plan/intent):
```bash
cat .claude/github-cache.json 2>/dev/null | jq '.issue'
```

Store the owner/repo and PR number for API calls.

## Step 2: Fetch all unresolved review threads

Use the GraphQL API to fetch all unresolved review threads, including the author of each comment so we can categorize by reviewer type:

```bash
gh api graphql -f query='
  query($owner: String!, $repo: String!, $pr: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100) {
          nodes {
            isResolved
            id
            comments(first: 10) {
              nodes {
                author { login }
                body
                path
                line
                url
                diffHunk
                createdAt
              }
            }
          }
        }
      }
    }
  }
' -f owner='{owner}' -f repo='{repo}' -F pr={pr_number} \
  --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | {threadId: .id, comments: .comments.nodes} | select(.comments | length > 0)]'
```

Also fetch review-level (non-inline) comments and their states:

```bash
gh api repos/{owner}/{repo}/pulls/{pr_number}/reviews --paginate \
  --jq '[.[] | select(.state != "DISMISSED") | {id, state, body, user: .user.login}]'
```

Categorize each thread's comments by reviewer type:
- **Bot - Copilot**: `copilot-pull-request-reviewer[bot]` or login matches `copilot` (case-insensitive)
- **Bot - Olive**: login matches `olive` (case-insensitive)
- **Human**: any other login

If no unresolved comments are found at all, tell the user and stop.

## Step 3: Gather context

Build a full picture before evaluating comments:

1. **The PR diff** — understand what changed:
   ```bash
   gh pr diff {pr_number}
   ```

2. **The plan** — check multiple sources for intent:
   - The cached issue from `.claude/github-cache.json` (title + body = the original task)
   - Active plans in the conversation context
   - Plan-related files in the working directory (PLAN.md, TODO.md, etc.)
   - Commit messages for intent:
     ```bash
     gh pr view {pr_number} --json commits --jq '.commits[].messageHeadline'
     ```

3. **PR description** — the stated purpose:
   ```bash
   gh pr view {pr_number} --json body --jq '.body'
   ```

4. **Read relevant source files** for any comment that references specific code, so you can see the full context around the flagged lines.

## Step 4: Evaluate each comment

For each comment, consider:

- **Validity**: Is this a real issue, or a false positive / misunderstanding of intent?
- **Severity**: Bug risk? Security concern? Or purely stylistic?
- **Alignment with plan**: Does the suggestion conflict with deliberate architectural or design choices? Does the cached issue context explain why a choice was made?
- **Cost/benefit**: Is the fix trivial (just do it) or would it require significant rework for marginal benefit?
- **Scope**: Is the reviewer suggesting work beyond the PR's scope? (Feature creep disguised as review feedback)
- **Source weight**: Human reviewer comments carry more weight than bot comments — treat them as likely intentional unless clearly wrong. Bot comments (Copilot, Olive) are probabilistic and more likely to be false positives.

## Step 5: Categorize and present

Group comments by reviewer type, then present each comment in one of three sub-categories:

### By reviewer group

#### 🤖 Copilot
#### 🫒 Olive
#### 👤 Human Reviewers (grouped by reviewer login)

### Sub-categories for each comment

**Address** — valid and worth fixing:
- Quote the comment (abbreviated)
- File and line reference
- Why it's worth addressing
- Suggested fix (brief)

**Ignore** — should be dismissed:
- Quote the comment (abbreviated)
- File and line reference
- Why it's safe to ignore (false positive, out of scope, conflicts with plan, etc.)

**Discuss** — unsure or needs user judgment:
- Quote the comment (abbreviated)
- File and line reference
- The tradeoff or ambiguity
- Your leaning and why

## Step 6: Offer to act

After presenting the assessment, offer:

1. **Fix all "Address" items** — apply the fixes directly
2. **Fix selectively** — let the user pick which to address

Do NOT automatically apply fixes — present the assessment first and wait for direction.

## Step 7: Resolve threads on GitHub

After the user approves and fixes are applied, **resolve ALL review threads** on GitHub that have been addressed or dismissed — so the PR is clean.

For bot threads (Copilot, Olive): resolve all of them — addressed, ignored, and discussed alike.
For human threads: only resolve ones the user has explicitly approved resolving.

To resolve threads:
1. Query for unresolved threads via GraphQL (`reviewThreads` on the PR, `isResolved == false`)
2. Call `resolveReviewThread` mutation for each approved thread
3. Report how many were resolved
