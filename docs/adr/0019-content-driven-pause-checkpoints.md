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
- Both use `AskUserQuestion` for their checkpoint prompts. The `/implement-with-haiku` UI adds
  a "don't pause again" option not present in `/expert-plan-v2`, giving users mid-run control
  over remaining checkpoints.
- The pause mechanism is **purely prompt-level** — no hook support needed. Hooks cannot see
  step-level semantics; the command doc itself is where the prompt occurs.

## Amendment (2026-09-11): superseded by unconditional stop-and-wait checkpoints

The opt-in `--pause-at` flag, its `all` shorthand, and the `AskUserQuestion`-based three-option UI
described above were removed. All five checkpoints (`round1-join`, `gate-fix-loop`, `pre-fanout`,
`post-fanout`, `pre-round4`) now **always** stop unconditionally: the command prints the
`USAGE-GATE:` line and a one-line status, then stops and waits for the user's next message — no
flag, no `AskUserQuestion`, no proceed/stop decision computed by the tool.

**Why:** the `AskUserQuestion`-based decision (parsing `--pause-at`, evaluating which named
checkpoint fired, presenting three options) was itself burning tokens and adding a decision point
on every run, for a feature that in practice was either always wanted (safety checkpoints between
expensive, hard-to-undo rounds) or not worth the flag's discoverability cost. Removing the
conditional collapses two previously-independent mechanisms (this ADR's content-driven pause,
ADR-0016's data-driven usage gate) at each of the five seams into one unconditional stop that also
prints the usage-gate's token count.

**Consequence for this ADR's specific claims:** the "off by default, safe for non-interactive
callers like `/track-and-start`" guarantee (Decision and Consequences sections above) no longer
holds literally — there is no longer an off state. In practice this is not a functional regression:
every existing call site that invokes `/implement-with-haiku` (all four in `commands/track-and-start.md`)
prints a `cd <worktree> && claude "/implement-with-haiku"` command for a human to copy-paste and run
in their own interactive terminal session — there is no headless/automated invocation anywhere in
this repo today. A human is present at every current call site to reply "continue". If a genuinely
unattended/headless caller is ever added, it will hang at the first checkpoint; that caller would
need its own mechanism (e.g. a flag to skip checkpoints) at the time it's introduced, not before.

**Status:** the `all`/named-checkpoint table, the three-option UI, and the "declined" language above
are historical — read them as describing the pre-2026-09-11 design.

## Amendment (2026-09-12): down to one mandatory checkpoint; the other four removed outright

Of the five unconditional checkpoints from the amendment above, only `round1-join` remains. The
other four (`gate-fix-loop`, `pre-fanout`, `post-fanout`, `pre-round4`) were removed — not made
conditional, deleted — along with their `usage-check` calls.

**Why:** [issue #174](https://github.com/stephenkiers/claude-helpers/issues/174) collected token
counts from 8 real runs. `round1-join` was over threshold in every single one — round 1's parallel
implementer fanout is reliably the biggest context cost in the whole pipeline. The other four seams
never once needed a `/compact` break in that sample. A first attempt tried to keep all five seams
but gate the stop on `usage-check`'s `DECISION:` field; that was reverted (see the issue) because
`DECISION:` measures subagent-only token accounting since the last `round1-join` reset — a different
quantity than the orchestrator's own context size — and that accounting is itself broken today (most
`SubagentStop` hook payloads lack a usable `agent_transcript_path`, degrading `DECISION:` to a
floor guess for the affected agent types — measured post-`97642e1` at 2/1,438 for named agent types
and 0/319 for `plan-implementer` specifically; see #142/#143 for the blank- and `unknown`-`agent_type`
populations this excludes). Given that, keeping four inert `usage-check` Bash calls (tokens and time
spent for a line nobody could act on) had no upside — so they were deleted outright, not just
skipped.

**Telemetry impact:** at the time of this amendment (2026-09-12), `/implement-with-haiku` was not yet
instrumented with the real ADR-0016 `command-begin`/`stage-begin` events — the five `usage-check
--seam ...` calls were always a separate, ephemeral, per-session mechanism (round1-join baseline +
floor logic), not the append-only `events.jsonl` log. That gap was closed by
[#160](https://github.com/stephenkiers/claude-helpers/issues/160), which added real `command-begin`/
`command-end` around the whole command and `stage-begin`/`stage-end` around two named stages,
`round1-join` and `integration-gate` (the fanout and round4 phases were deliberately left
uninstrumented — see #160 for why). The `usage-check --seam` mechanism described above remains a
distinct, unaffected mechanism regardless: it was never reading from `events.jsonl` and still isn't.
Subagent `agent.begin`/`agent.end` telemetry is captured by hooks independently of anything in this
command doc and is unaffected by any of this.

**Consequence:** the final summary's "Usage gate log" now has two rows — `round1-join` and `final`
— instead of five. Re-adding a dropped seam later should come with fresh measured-run data showing
it's actually needed, not a default restoration.

## Amendment (2026-09-15): round1-join now prints a resume command

The `round1-join` checkpoint still stops unconditionally with no flag and no `AskUserQuestion` (this
is unchanged from the prior amendment). The checkpoint is no longer output-free, however. It now:

(a) closes the run's telemetry as `interrupted` (i.e., runs `command-end --outcome interrupted`) before
stopping;

(b) prints a literal `RESUME-AFTER-CLEAR:` invocation when the run's plan reference is durable (i.e.,
a resume command a human can copy, paste after running `/clear`, and use to skip round 1 and re-enter
directly at the Integration Gate);

(c) re-opens telemetry with a `resumed_from` field (a new optional field being added to `command-begin`'s
event, linking a resumed run back to the interrupted one it continues) if the human replies "continue"
in the same context instead of clearing.

This is a continuation of [issue #174](https://github.com/stephenkiers/claude-helpers/issues/174), not
new tracked work.

**Why:** the uninterruptible design of earlier rounds meant large runs frequently hit the `round1-join`
checkpoint with context exhaustion unavoidable. Forcing a full clear-and-restart loses work committing
to triage and decision-making. The resume command provides a path for users to keep their decisions
while recycling context, and telemetry links resumptions back to the originating run so they can be
measured as continuations rather than separate sessions. Measuring the effect of this path against
`/compact`'s token-dropping strategy (captured in issue #174 over 8 real runs) determines whether
resumption is genuinely useful enough to keep.

**What's now stale:** the second amendment's (2026-09-12) claim that the `round1-join` checkpoint is
"output-free" (no emissions besides the usage-gate line) no longer holds — it now emits a command for
the human to paste. The "no flag, no `AskUserQuestion`" and "stops unconditionally" properties remain
unchanged.

**Status:** this amendment's material is now current, not historical.

**Kill criterion (pre-registered, verbatim):**

- *Sample*: the next **8 real `/implement-with-haiku` runs that reach round1-join** (matching #174's
  own sample size so the numbers are comparable), each recorded as a comment on #174 with: arm
  (`clear-resume` | `compact` | `neither`), round-sizing classification, gate-fix-Haiku iteration
  count, and the run's final `command.end` outcome.
- *Metric, readable off existing telemetry* (`~/.claude/telemetry/events.jsonl`): (a) fix-Haiku
  iterations = count of `agent.end` events with `agent_type=plan-implementer` whose timestamp falls
  inside the run's `integration-gate` `stage.begin`/`stage.end` window; (b) terminal outcome = the
  run's `command.end` `outcome.status` plus `failure_class` (`gate-not-converged` is the failure that
  matters here).
- *Refutation*: if the `clear-resume` arm does **not** show both a strictly lower mean fix-Haiku
  iteration count **and** no more failure outcomes than the `compact` arm, the resume branch is
  **deleted** — the `--resume-after-round1` branch and the `RESUME-AFTER-CLEAR:` print come out of
  `commands/implement-with-haiku.md`, and ADR-0019 gets a fourth amendment recording the negative
  result. Deleted, not tuned.
- *Default is removal*: if fewer than 3 runs land in either arm by the 8th recorded run, or if 8
  qualifying runs have not accumulated within **30 days of this amendment's date**, the result is "no
  usable signal" and the branch is deleted on the same terms. Absence of evidence kills it; it does
  not get to ship by default.
- *Who*: the maintainer who ran the runs, at the 8th recorded run or the 30-day mark, whichever comes
  first.
- *Stated honestly in the amendment*: N=8 across heterogeneous runs supports no statistical claim;
  this is a pre-registered decision rule, not an inference. Resumed runs are likely self-selected for
  being large, which biases the comparison against the resume arm, and arm membership is human
  self-reported because nothing in a resumed invocation can detect whether the human actually
  cleared.
