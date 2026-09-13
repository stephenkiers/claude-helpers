---
description: Review PR comments from all reviewer types (Copilot, Olive, human) and decide whether each should be addressed, ignored, or discussed.
allowed-tools: Bash(gh api:*), Bash(gh pr:*), Bash(gh pr diff:*), Bash(gh pr view:*), Bash(cat:*), Bash(jq:*), Read, Glob, Grep
---

# PR Comments

Review all unresolved PR review comments — from GitHub Copilot, olive-agent, and human reviewers — and decide whether each should be addressed, ignored, or discussed. The shared triage steps live in `~/.claude/prompts/pr-comment-triage-reference.md` — read it now, then follow Steps 1–7 below, each of which names the section to run from that file.

**Arguments:** $ARGUMENTS (optional PR number, URL, or `owner/repo#number` — if omitted, use cached PR or detect from current branch)

## Step 1: Identify the PR

Run the **PR Identification** section from `~/.claude/prompts/pr-comment-triage-reference.md`.

## Step 2: Fetch all unresolved review threads

Run the **Fetch All Unresolved Review Threads** section from `~/.claude/prompts/pr-comment-triage-reference.md`.

## Step 3: Gather context

Run the **Gather Context** section from `~/.claude/prompts/pr-comment-triage-reference.md`.

## Step 4: Evaluate each comment

Run the **Evaluation Criteria** section from `~/.claude/prompts/pr-comment-triage-reference.md`.

## Step 5: Categorize and present

Run the **Categorize and Present (Format)** section from `~/.claude/prompts/pr-comment-triage-reference.md`.

## Step 6: Offer to act

Run the **Offer to Act** section from `~/.claude/prompts/pr-comment-triage-reference.md`.

## Step 7: Resolve threads on GitHub

Run the **Resolve Threads on GitHub** section from `~/.claude/prompts/pr-comment-triage-reference.md`.
