# v2 inventory: the v1 `/expert-flow` behaviour, rule by rule

This directory is the Step 1 inventory for the `/expert-flow` v2 driver rewrite (plan Step 1, ticket
#477; parity catch-up, ticket #538). It lists every v1 rule the v2 Python driver must reproduce, drop, or
change, so that a one-off rule that lives only in prose (an ADR-0023 amendment, a line in
`commands/expert-flow.md`) is not lost in the rewrite.

## Files

- `rules.md`: one row per v1 rule, with its v1 line range in `commands/expert-flow.md`, its ADR-0023
  section and amendment date, its target layer, and its disposition. It also holds the Fleet state
  contract sub-table, the Overlapping-issue dispositions sub-table, and the ADR-0023 amendment index.
- `v1-content-pins.md`: the content-hash watch list for the v1 files that the rules depend on (`sha256`,
  reviewed date, tracking issue). A pin mismatch means a watched v1 file changed without its rows being
  reviewed.
- `skeleton-runbook.md`: the measurement procedure for the walking skeleton's interactive and headless
  runs, evidence collection, definitions, cleanup and scrubbing (Step 8).
- `skeleton-findings.md`: the measured (or `NOT MEASURED`) results of the walking-skeleton runs, one row per
  item, with provenance and the coverage disclaimer (Step 10).

## Purpose

- **Stop silent loss.** The ticket risk is that one-off v1 rules disappear during the rewrite. Every rule
  in v1 has a row; a row is never removed, only re-dispositioned.
- **Key to amendments.** Rules are keyed to ADR-0023 section and amendment date, because the amendments
  are where v1's one-off rules came from.
- **Feature parity.** v1 is not frozen. Every v1 behaviour change lands with v2 feature parity: either the v2
  driver code reproduces it, or a `deferred to …` row says where it will land. A watched v1 doc edit bumps
  its content pin in the same PR (see `v1-content-pins.md`); a pin bump is a human-only edit. The rule is
  recorded in the ADR-0026 2026-11-23 amendment. A new ADR-0023 amendment must be absorbed into this
  inventory before the next change to the v2 driver.

## How to read it

1. Find the v1 area in `rules.md` by its line range in `commands/expert-flow.md`. Line ranges were checked
   against the file when this inventory was written. If v1 is edited, the ranges must be re-checked in the
   same PR; they move only when v1 moves.
2. Read the **Disposition** column. Each cell begins with one of the following closed set of prefixes:
   - `port, pinned by test` — the v2 driver must reproduce the behaviour, and it must name at least one
     `tests/<file>.py` whose named test fails if it does not. The layer is named in the row.
   - `port, test not named (backfill #N)` — the behaviour is ported, but no named test is cited yet; `#N`
     is the tracking issue that adds one.
   - `landed in #N (…)` — the behaviour is in the code named in the cell. If the cell says "pinned by", it
     names a `tests/` path; a "no test yet" tail is not a pin.
   - `landed as: <what>` — the behaviour landed in a different form, and the form is named.
   - `re-pointed to <layer or follow-up> (<reason>)` — the rule moved to another layer or follow-up.
   - `deferred to Layer N (#issue)` or `deferred to <named follow-up> (#issue)` — not yet built.
   - `not ported: <reason>` — v2 does not reproduce the behaviour yet, or at all, and the reason is given.
   - `dropped: <reason>` — the v2 driver deliberately does not reproduce the behaviour. The reason must
     survive being read aloud. A dropped row without a reason is a defect.
   - `refused by design (<reason>)` — v2 refuses the input rather than reproducing it.

   A cell that does not begin with one of these prefixes is a defect. "Named" means defined: a cited test
   must be `def <name>` in the cited file. Registration in the run list is not checked.
3. The **Target layer** column uses the plan's layers. Layer 1 is state, receipts and resume (Step 8);
   Layer 2 is the step loop, recovery, ledger, stub and the `--list`/`--abandon`/`--retro` subcommands
   (Step 9); Layer 3 is relay, assess, gates and the merge gate (Step 10); Layer 4 is epic and fleet
   (Step 11); Layer 5 is dogfood and v1 deprecation (Step 12). Step 7 is the walking skeleton that comes
   before Layer 1.
4. Riskiest areas come first: the step loop, the `--resume` cascade, the retry / take-over / keep-previous
   tree, the assess stage, the merge gate, and the healed-run forced ask.
5. `ref` in `v1-content-pins.md` and `landed in #N` mean the tracking issue, not the PR.

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

The ADR-0026 2026-11-23 amendment adds three checks on top of this, which the same drift test enforces:
row truth (every cited test is defined in the cited file), the closed disposition set above, and the
content pin in `v1-content-pins.md`. The amendment extends the sentence above; it does not replace it.

## Pins and human-only edits

- A pin mismatch in `v1-content-pins.md` is not fixed by re-hashing. The bump procedure is: re-read the
  changed v1 file, add or update the `rules.md` rows it affects, then update the hash, reviewed date and
  `ref` in the same PR as the v1 edit. Only a human edits the pin file. An automated worker (implementer,
  `flow-healer`, rebase worker) that hits a pin mismatch stops with `NEEDS_HUMAN` / `Decision: FAILED`
  (W2; follow-up #543 adds the pin file to the never-edit rules of those workers).
- The pin watch list has three files, and that is deliberate. The plan rules out widening it to the agent
  files and the guard scripts. "Pin them" was considered and is not taken: those files change through the
  restricted-agent and guard work, which has its own review path.

## Closed sets

Two value sets in the driver are closed: the StopCodes and the Decision values. Their source of truth is
`scripts/flow_driver/steps.py`, and the code comments there say the sets are closed. A new value must come
with a `rules.md` row that says where the behaviour is recorded, or the drift check does not see it.

## Pre-parity flows

v2 flows started before #538 are pre-parity: they ran without the restricted step agents and the parity
rows above. They are not D6-eligible and never count toward the exit criterion (W4). The existing pre-#538
v2 flow is abandoned or restarted by the human.

## Scope

This inventory covers `commands/expert-flow.md` (the v1 orchestrator), ADR-0023, and the two scripts the
v2 driver must keep compatible with: `scripts/fleet.py` (the fleet state fields it reads) and
`scripts/workflow/heal.py` (the stop-key and heal-state fields it reads). It does not cover the restricted
step agents' tool lists; those are referenced where a rule depends on them, and the guard-coverage limits are
recorded in the ADR-0026 2026-11-23 amendment and pointed to #489 for ADR-0008 text.
