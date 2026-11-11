# v2 inventory: the v1 `/expert-flow` behaviour, rule by rule

This directory is the Layer 0 inventory for the `/expert-flow` v2 driver rewrite (plan Step 1, ticket
#477). It lists every v1 rule the v2 Python driver must reproduce, drop, or change, so that a one-off rule
that lives only in prose (an ADR-0023 amendment, a line in `commands/expert-flow.md`) is not lost in the
rewrite.

## Files

- `rules.md`: one row per v1 rule, with its v1 line range in `commands/expert-flow.md`, its ADR-0023
  section and amendment date, its target layer, and its disposition. It also holds the Fleet state
  contract sub-table, the Overlapping-issue dispositions sub-table, and the ADR-0023 amendment index.

## Purpose

- **Stop silent loss.** The ticket risk is that one-off v1 rules disappear during the rewrite. Every rule
  in v1 has a row; a row is never removed, only re-dispositioned.
- **Key to amendments.** Rules are keyed to ADR-0023 section and amendment date, because the amendments
  are where v1's one-off rules came from.
- **Freeze drift.** Under the seq 1 freeze (no work against v1 during the v2 build), a new ADR-0023
  amendment means someone overrode the freeze. The inventory must absorb it before the build proceeds.

## How to read it

1. Find the v1 area in `rules.md` by its line range in `commands/expert-flow.md`. Line ranges were checked
   against the file when this inventory was written. If v1 is edited, the ranges must be re-checked; v1 is
   frozen for this build, so they should not move.
2. Read the **Disposition** column. There are two values only:
   - **port, pinned by test**: the v2 driver must reproduce the behaviour, and a named test (written in the
     layer that owns it) fails if it does not. The layer is named in the row.
   - **dropped, reason**: the v2 driver deliberately does not reproduce the behaviour. The reason follows
     the word "dropped" and must survive being read aloud. A dropped row without a reason is a defect.
3. The **Target layer** column uses the plan's layers. Layer 1 is state, receipts and resume (Step 8);
   Layer 2 is the step loop, recovery, ledger, stub and the `--list`/`--abandon`/`--retro` subcommands
   (Step 9); Layer 3 is relay, assess, gates and the merge gate (Step 10); Layer 4 is epic and fleet
   (Step 11); Layer 5 is dogfood and v1 deprecation (Step 12). Step 7 is the walking skeleton that comes
   before Layer 1.
4. Riskiest areas come first: the step loop, the `--resume` cascade, the retry / take-over / keep-previous
   tree, the assess stage, the merge gate, and the healed-run forced ask.

## Rule for ADR-0023 amendments

**Every ADR-0023 amendment heading must be referenced under `docs/v2-inventory/`.** This is mechanical,
not a style preference. The drift test (`tests/test_v2_inventory_coverage.py`, plan Step 2) parses the
amendment headings in `docs/adr/0023-expert-flow.md` (their dates and issue numbers) and fails when one of
them is not referenced in this directory. It is checked against the real files and against a synthetic
heading added to a copy.

When a new ADR-0023 amendment lands:

- add a row for it to the amendment index in `rules.md`, quoting the heading exactly as written;
- give each rule it adds a row in the rule table, with a disposition;
- do not change existing rows, except to re-disposition a rule with a recorded reason.

The index is the place the drift test looks; a reference anywhere else in this directory does not count
unless the heading text is quoted exactly.

## Scope

This inventory covers `commands/expert-flow.md` (the v1 orchestrator), ADR-0023, and the two scripts the
v2 driver must keep compatible with: `scripts/fleet.py` (the fleet state fields it reads) and
`scripts/workflow/heal.py` (the stop-key and heal-state fields it reads). It does not cover the restricted
step agents' tool lists, which stay as they are under the hook freeze; those are referenced where a rule
depends on them.
