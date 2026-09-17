# Reviewer Yield Test Fixtures

## Schema

These fixtures are hand-synthesized `.jsonl` transcript files modeling the real discovered shape:

- Entries with `message.usage` containing fields: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`
- NO `iterations` key (this was the old guard)
- `isSidechain` and `agentId` present on entries
- At least one `Write` `tool_use` entry naming a file like `{reviewer}-pass1.md`
- Leading line `{"type": "_fixture_meta", "schema_version": 1, "modeled_on": "..."}` which parsers must skip without raising

## Modeled On

These fixtures were hand-synthesized on 2026-09-17 to match the post-fix token parsing contract described in ADR and Task 3 of the routing-v2 phase 0 implementation.
