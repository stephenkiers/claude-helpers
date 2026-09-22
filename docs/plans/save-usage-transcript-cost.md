# Design note: compute cost from transcripts instead of pasted `/usage` panels

**Status:** not started. Written after implementing the session-scoping fix
(`session_id`/`commands` fields, `[[save-usage-commands-telemetry]]`-style change) and the `d`
(days) `parse_duration` bug fix in `scripts/save-usage.py`. This note covers the third,
longer-term suggestion: derive cost directly from transcripts rather than from a pasted
`/usage`/`/cost` panel.

## Why

`/save-usage` today depends on a human running `/usage`, copying its output, and pasting it in.
Two structural problems follow from that, independent of labeling:

1. **Session scoping.** The `/usage` panel totals the *whole Claude Code session*, not a single
   command. A session that ran `expert-plan` then `expert-review` produces one cost number that
   can't be split between them. The `commands` field just added tells you *what ran*, not *how
   much each one cost*.
2. **Manual step required.** Nothing gets logged unless a human remembers to run `/usage` and
   paste it. Most sessions never get recorded.

Claude Code transcripts (`~/.claude/projects/<project>/<session-id>.jsonl`, one file per session)
already contain per-turn `usage` blocks (input/output/cache-read/cache-write tokens) for the
parent session and, in a multi-agent run, for every subagent transcript too — each subagent gets
its own transcript file, linked to the parent by `agent_id`/`session_id` metadata that
`scripts/telemetry_schema.py`'s `parse_transcript_tokens` (used by
`scripts/claude-transcript-metrics.py`) already knows how to read.

If cost is computed straight from transcripts:
- **No paste required.** A `/save-usage` (or a new report command) run at any time can read back
  every session's transcript and compute cost after the fact — nothing needs to happen during
  the session itself.
- **Parent vs. subagent cost splits out naturally**, since each transcript file is already scoped
  to one agent.
- **Per-command cost becomes possible**, by using the `command.begin`/`command.end`
  timestamps already in `events.jsonl` to slice a session transcript into per-command token
  windows (the same session-scoping problem, just solved by slicing instead of by asking a human
  to paste more carefully).

## What's missing today

`parse_transcript_tokens` extracts token *counts*, not dollar cost — no cost is in the transcript
JSONL, because Claude Code doesn't write per-turn pricing to it. To get dollars, the calculation
needs a **price table** (cost per input/output/cache-read/cache-write token, per model) applied to
the extracted token counts.

That's exactly where the 134 existing pasted `usage-log.jsonl` records are useful: each one has a
per-model breakdown of token counts *and* the real `$` cost the `/usage` panel reported for that
breakdown. That's a calibration set — enough to solve for (or just cross-check against published)
per-token prices per model, and enough to validate that transcript-derived counts + a price table
reproduce the pasted panel's `$` figure within noise (cache read/write pricing tiers are the most
likely source of drift, since those have the most model-specific variation).

## Rough shape (not a committed plan)

1. Extend `parse_transcript_tokens` output (or a new function) to also return per-model token
   breakdowns (it already tracks `tokens` totals; check whether model attribution survives per
   turn — if the transcript's `usage` blocks are per-turn and turns can use different models in
   the same session, e.g. after `/model` switching or in a multi-agent run with mixed tiers, this
   needs to stay a per-model dict, not a flat total).
2. Build a small price table keyed by model name (the same `claude-sonnet-5`/`claude-haiku-4-5`
   style strings already seen in pasted `usage-log.jsonl` records) — cost per million tokens for
   input/output/cache-read/cache-write.
3. Calibrate: for each of the 134 pasted records, recompute cost from the token counts already
   stored in that record (no transcript needed for calibration — the pasted records already have
   both token counts and `$` cost per model) and solve for/verify the price table. Flag any model
   where reconstructed cost doesn't match pasted cost within a small tolerance — that's either a
   pricing-tier detail (e.g. batch vs. real-time, or a cache-write TTL tier) or a parsing bug.
4. Once the price table is trustworthy, apply it to transcript-derived token counts (not the
   pasted records) to get cost for sessions that were never pasted at all.
5. For per-command splitting: use `command.begin`/`command.end` timestamps from `events.jsonl`
   to bucket a session transcript's turns into command windows, then sum cost per window. Turns
   outside any command window (e.g. ad hoc chat before the first command) fall into an
   "unattributed" bucket rather than being silently dropped or misattributed.
6. Decide the output shape: most likely a new read-only report (parallel to
   `scripts/claude-transcript-metrics.py`, not a change to how `/save-usage` itself works, since
   `/save-usage` is explicitly opt-in/manual per its own docs) — or a `save-usage --from-transcript
   <session-id>` mode that backfills `usage-log.jsonl` without a paste. Needs a decision before
   implementation, not during it.

## Open questions to resolve before implementing

- Does a transcript's per-turn `usage` block reliably carry the model name, or does the model
  need to be inferred from context (e.g. the surrounding `command.begin` event's `model` field)?
  This determines whether step 1 is straightforward or needs cross-referencing `events.jsonl`.
- How stable is Anthropic's per-model pricing across the calibration set's time range? If any of
  the 134 records predate a price change, calibrating a single price table against all of them
  will silently blend two different price regimes. Worth checking record timestamps for a price
  discontinuity before trusting the calibration.
- Cache-write pricing usually has multiple TTL tiers (5-minute vs. 1-hour, in this repo's own
  session-cache guidance) — does the transcript distinguish which tier a given cache-write turn
  used, or does it need to be assumed?
- Whether subagent transcripts are reliably discoverable and linkable to a parent session (file
  naming/location, and whether `agent_id` correlation is already solid) — worth a quick spike
  against a handful of known multi-agent sessions before committing to the parent/subagent split
  as a headline feature.
