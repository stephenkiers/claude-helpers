# Reviewer Yield Test Fixtures

## Schema

These fixtures are hand-synthesized `.jsonl` transcript files modeling the real discovered shape:

- Entries with `message.usage` containing fields: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`
- NO `iterations` key (this was the old guard)
- `isSidechain` and `agentId` present on the assistant entries (the `_fixture_meta` and trailing `user` lines carry neither)
- At least one `Write` `tool_use` entry naming a file like `{reviewer}-pass1.md`
- Leading line `{"type": "_fixture_meta", "schema_version": 1, "modeled_on": "..."}` which parsers must skip without raising

## Modeled On

These fixtures were hand-synthesized on 2026-09-17 to match the post-fix token parsing contract described in ADR and Task 3 of the routing-v2 phase 0 implementation.

## Consumers

`sample-uncle-bob-pass1.jsonl` is loaded by `tests/test_routing_metrics_phase_0.py` through the real
`parse_tokens_from_subagent` and `_subagent_reviewer_for_review_dir`. Expected totals: input 1850,
output 875, cache_read 150, cache_creation 75 (the second `msg-1` entry is a deliberate duplicate id
and the last assistant entry has no id, so it is counted). The Write `tool_use` targets a path under
`feature-123/`, so the fixture anchors to `uncle-bob` only for a review dir named `feature-123`.
