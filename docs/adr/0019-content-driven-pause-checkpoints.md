# ADR-0019: Content-driven pause checkpoints

**Status:** Accepted

## Context

`/implement-with-haiku` runs as an uninterruptible multi-round pipeline: Round 1 (implementation),
Integration Gate, Round 2–3 (tests and adversary review), and Round 4 (test cleanup). Users wanted
the ability to pause at specific named steps — not after every step, but only at points where work
is stable and committed. The [`/expert-plan-v2` checkpoint](../commands/expert-plan-v2.md#step-6-checkpoint-hard-stop)
already demonstrated a single hard-stop pattern with user confirmation (a modal `AskUserQuestion`
with a decline option); this ADR generalizes that single-checkpoint pattern by adding multiple
checkpoints and a "proceed and don't pause again" dismissal, needed only for multi-checkpoint runs.

The mechanism differs from [ADR-0016](0016-usage-yield-telemetry.md)'s usage gate: the usage gate is
**data-driven** (token totals trigger a threshold-crossing ask), whereas this is **content-driven**
(user declares via `--pause-at` which named steps should prompt). Both are stop-and-ask mechanisms;
they must remain independent and separately branded so the two systems don't interfere.

## Decision

**Add an opt-in `--pause-at <name>[,<name>...]` flag** to `/implement-with-haiku`, with an `all`
shorthand. Default is off, preserving existing caller behavior (notably, `/track-and-start`'s
non-interactive use passes no arguments and must not hit an `AskUserQuestion`).

**Four named checkpoints** map onto step boundaries that already host usage-gate seams, so the
command gains a second, clearly-separate check at each point without adding new seam locations:

| Name | Location | Reason |
|---|---|---|
| `gate` | After Round 1 join (Step 4d), before Integration Gate | All units merged and committed; gate hasn't run |
| `fanout` | After Integration Gate passes, before Round 2/3 fan-out | Gate is clean and committed; tests haven't run |
| `round4` | After Rounds 2–3 complete, before Round 4 (test cleanup) | All fixes committed; cleanup hasn't run |

An `all` value expands to `gate,fanout,round4`.

**Decline = clean stop, no rollback.** When a user chooses "Stop here" at any checkpoint, the
command exits with status 0 immediately, printing the current `git status` and a one-line note that
the checkpoint was reached and work is committed. Nothing is reverted; no pending work is lost.
(This matches the real per-round commit structure — Round 1 is already committed before the gate
runs, tests are already committed before Round 3 follow-up runs, etc.)

**Three-option UI shape:**
- **Proceed** — continue to the next stage
- **Stop here** — exit cleanly, work committed
- **Proceed and don't pause again this run** — clears all remaining checkpoints from `$PAUSE_AT`
  and continues, so no further prompts interrupt the run

The third option enables the user to dismiss all future pauses mid-run without modifying the
command or re-invoking it.

**Validation and off-by-default.** Flag parsing happens in Step 0 before plan detection. Unknown
checkpoint names are rejected with an error before any work runs. The default (`$PAUSE_AT` empty)
is fully backward-compatible — no code path reaches `AskUserQuestion` when the flag is absent,
making this safe for non-interactive callers like `/track-and-start`.

## Consequences

- Pause checkpoints are **independent of the usage gate** — the two mechanisms can both be active
  without interfering. An `AskUserQuestion` prompt at a checkpoint is unrelated to ADR-0016's
  token-threshold ask. Both mechanisms share the `run-metrics.py` telemetry script and may be
  active at the same seam (usage-gate `AskUserQuestion` + pause `AskUserQuestion`); stacked prompts
  at a shared seam are accepted as-is without merging — the orchestrator may encounter both, or the
  user's decision at one gate may make the other moot (e.g., "stop here" exits before the next prompt).
- The pause mechanism is **off by default**, so existing non-interactive use (e.g. in
  `/track-and-start`) is unaffected.
- **Clean stops preserve work** — a user who chooses "Stop here" gets committed code/tests they can
  review or continue manually. This matches the real per-round commits already in place; nothing
  new is lost if the session ends.
- **Three-option UI matches `/expert-plan-v2`'s checkpoint** UI, reducing cognitive load when
  users move between the two commands.
- The pause mechanism is **purely prompt-level** — no hook support needed. Hooks cannot see
  step-level semantics; the command doc itself is where the prompt occurs.
