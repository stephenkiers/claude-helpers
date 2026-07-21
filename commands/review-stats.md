---
name: review-stats
description: Temporarily disabled — the ledger machinery was removed from /expert-review.
model: haiku
---

# Review Stats — Temporarily Non-functional

The ledger machinery that powered this command (`~/.claude/reviews/{repo-key}/ledger.jsonl`) has been removed from `/expert-review` as part of simplifying the decision-recording system. This command is currently non-functional.

If you want to re-enable reviewer stats and recurring-theme tracking in the future, restore the ledger machinery per the ADRs and prior commit history. The schema and aggregation logic can be re-established at that point.
