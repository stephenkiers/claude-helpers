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
they must remain independent and separately branded so the two systems don't interfere. (See also
ADR-0016's amendment on the run ledger being a control input, not just observational telemetry.)

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
*(2026-10-26: a headless caller now exists, `/expert-fleet` running `/expert-flow --fleet` under `claude -p`; flow-relayed questions park to `fleet-ask.json` instead of hanging (see ADR-0025 "Fleet mode" and commands/expert-flow.md "Fleet mode").*)

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
  run's `command.end` `outcome.status` plus `failure_class` (`gate_not_converged` is the failure that
  matters here).
  *Note: before the telemetry failure-class vocabulary fix (2026-10-09), this kill criterion was evaluated
  on data that could not contain that class — the kebab-case form `gate-not-converged` was rejected by the
  schema and silently dropped.*
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

## Amendment (2026-09-23): the round1-join pause is opt-in via `--pause`

The maintainer no longer finds the round 1 pause valuable, so `/implement-with-haiku` now runs
straight through in one pass by default. The `round1-join` checkpoint (resume line, "continue" /
`/clear` options, HEAD-drift re-check) only runs when `--pause` is passed. The `round1-join` stage-end
and `usage-check` telemetry still run either way.

**Unchanged:** the run still interrupts the human when it genuinely needs them — failed units
(abort vs. proceed with merged units), conflicts on unassigned/shared files, and gate non-convergence.
`--resume-after-round1` still works for runs that were started with `--pause`.

**What's now stale:** earlier amendments describe `round1-join` as the run's "one mandatory
checkpoint"; read that as "the one checkpoint, mandatory only under `--pause`". The kill criterion
above measures the resume arm and only accumulates data from `--pause` runs.

## Amendment (2026-11-04): Round 4 opens with disagreement closure and cleans up tests at test level

A red spec-blind (Round 2) test is a finding, not a fixture to bend. Round 4 now has new
responsibilities, and it runs them in this order. No ledger stage changed: the `round4` row and its
`round4: <D> deleted, <R> relocated` first line keep their shape, and the new state lives in evidence
files (`disagreements.json`, `round4-deletions.md`, `round4-merged.txt`), not in new step ids.

1. **Disagreement closure first (Round 4.0a).** Each red or open spec-blind test is judged against the
   original plan by a Sonnet judge, then by an Opus judge only if Sonnet escalates. A judge may conclude
   "the test is correct, the implementation is wrong", which sends the test to the Haiku fix loop. A
   judge may never rule "spec was wrong". The ladder only prepares the case. Model routing follows
   ADR-0004 (judgment on Sonnet, Opus only on escalation), and how a flow records these calls follows
   ADR-0023. There is no further cap: the ladder is at most two judge calls per disagreement, and the
   fix loop after a `test-correct` verdict is already bounded at K=3.
2. **The human is the only owner of a "spec was wrong" ruling.** Any amendment to, or deletion of, a
   test's oracle needs a human answer through the Ask block (`~/.claude/prompts/flow-reference.md`; a
   plain question outside a flow; fleet parks it), and the answer must carry a reason. Any other edit to a
   failing spec-blind test is fenced: only import lines, call-site names and argument counts may change,
   checked mechanically by `scripts/test-cleanup-check.py fence`. Anything else goes to the human. A
   Round 3 support-edit flip (a shared fixture or helper that turns a red test green) also goes to the human.
   "Take over" is a defined terminal path, and a red test the judges say is correct stays committed red
   and blocks the PR (ruling 1a).
3. **Test-level cleanup with named survivors.** Only once everything is green, except the ruling-1a
   blockers, Round 4 sorts the tests added since `ROUND2_START_SHA` (never pre-existing tests) into
   Exempt, Duplicate, No-op, judgment-delete and Unique. Each deletion names the surviving test that
   still covers the behaviour, or its mechanical proof (`comm` dedup or the reference-check). Unique
   tests, and Exempt tests, are merged into the module's normal test file with their bodies unchanged;
   Exempt tests are never deleted. The merged ids go into the commit body as provenance.
4. **Post-conditions before the commit.** The named-survivor check runs against a fresh collection, and
   a changed-public-behaviour floor requires each public symbol added or changed in the implementation
   to keep at least one test. Any deletion that breaks either is restored and re-merged as Unique. The
   red set must equal exactly the blocking set. `test-cleanup-check.py names` must find no surviving
   `spec-blind` name in the run's own working set. Only then is the cleanup commit made.
5. **Widened working-set invariant.** Round 4 only ever touches files in the working set plus the 4.5
   merge targets recorded in `round4-merged.txt` (recorded before the first write to each target). It
   never deletes or edits a pre-existing test in those targets; it only appends. Resume restores the
   union of the two when a cleanup was interrupted, and stops on any change outside it.

**Deferred to #367:** test budgets and the "fails at base" proof belong to
[#367](https://github.com/stephenkiers/claude-helpers/issues/367), not this amendment. The No-op bucket
is the slot a future at-base proof joins. Nothing here decides a budget or a fails-at-base rule.

**Unchanged:** the earlier amendments' pause semantics (`--pause`, the `round1-join` checkpoint, the
`round4` checkpoint in the table above) are not altered. The table's `round4` row still means "before
Round 4 starts", which now begins with disagreement closure.

**What's now stale:** the 2026-09-12 and earlier descriptions of Round 4 as "delete clearly-junk tests and
relocate the survivors" describe the pre-2026-11-04 cleanup. Read them as the cleanup's classification
step (4.1–4.5), now preceded by 4.0a and bounded by the post-conditions above.

**Operator note:** `scripts/test-cleanup-check.py` is new. Run `/setup-local` after pulling so the
`~/.claude/scripts/` symlink exists. The command calls the helper by its `$HOME/.claude/scripts/` path,
so a machine without the symlink has no helper to call for the Round 3 fence and the Round 4 post-conditions.

## Amendment (2026-11-05): early test-hygiene detection, overlap hints, layout evidence, shared cleanup doctrine

Round 2 and Round 3 authors report every added test's `<new id> | overlaps: <existing id or none>` line, so Round 4.1 can hint at candidates without requiring a rename-before-register gate. A detect-only hygiene scan runs after the Round 2 apply and writes evidence to `round2-hygiene.txt`, making no rename, no commit and no ledger row. The 2026-11-04 sentence "No ledger stage changed ... not in new step ids" still holds and is not superseded. Round 4.3–4.6 now live in `prompts/test-cleanup-reference.md`, read by path, so `/expert-test-cleanup` (#463) can read the same file.

1. **Detect-only hygiene gate after the Round 2 apply.** The orchestrator runs `test-cleanup-check.py names --hygiene --base "$ROUND2_START_SHA" --head HEAD` and records the result in `round2-hygiene.txt`. No rename, no `git mv`, no staging, no commit, no ledger row. Hits are fixed only at Round 4.5. The scan re-runs whenever Round 2 has been applied; on the "Cleanup commit already landed" resume path, no scan re-produces the evidence, and Step 9 records the fields as `null` (not zero) to distinguish resumed from clean runs.

2. **Overlap hints at Round 4.1, never a pre-classification and never a deletion.** Round 2 and Round 3 authors report each added test: `<new id> | overlaps: <existing id or none>`. The overlaps are parsed at Round 4.1 and written to `round4-candidates.md`. Candidates are hints for the 4.3 class 1 (Duplicate) review only. Every deletion still needs its named survivor (4.4) and the 4.6 post-conditions. This stays clear of #367's dedup-at-generation and budgets.

3. **Round 4 does not shrink.** The 4.0a, then cleanup, then post-conditions order is unchanged. Overlap candidates are hints only; the structure and decision points of 4.1–4.6 remain unchanged.

4. **Spec-blind layout evidence from the pre-implementation SHA.** The author prompt may carry file paths and test-definition names (never bodies), extracted by `test-cleanup-check.py layout`, from tests present at the pre-implementation SHA pinned in the unit record. That SHA is the commit before any of this flow's implementation: the `implement` record pins it once in `layout_base_sha`, and the `fix` record and every scoped (Fix now) record inherit it rather than pinning their own. A file absent at that SHA yields zero names, not an error. This ensures spec-blind independence: a fix run never sees names of tests the implement step added.

5. **Round 4.3–4.6 now live in `prompts/test-cleanup-reference.md`.** Read by path from the command doc (no inline copy). The reference doc carries a short **Caller contract** at the top: the inputs the caller supplies and the outputs it produces. `commands/implement-with-haiku.md` replaces 4.3–4.6 with a pointer section and keeps the implement-specific tail (the `round4.md` evidence lines and the `DONE round4` ledger row) under `### 4.6a Record Round 4 evidence`, placed before `## Final check`.

6. **Measurement: per-run hygiene counts.** Every run records `test-hygiene.json` with `round2_hygiene_hits`, `round4_hygiene_hits`, `overlap_candidates`, `overlap_none`, `overlap_unreported`, `round4_deleted`, `round4_relocated`, `layout_names_given` (bool), and `step` (`implement`/`fix`). On the "Cleanup commit already landed" resume path, fields not re-produced are written as `null` (never zero) to distinguish resumed from clean runs. In a flow, `FLOW_DIR/test-hygiene-<STEP>.json` is a sanctioned FLOW_DIR artifact (same precedent as `units/<STEP>/`): the orchestrator writes it with a second `test-cleanup-check.py hygiene-json --out "$FLOW_DIR/test-hygiene-<STEP>.json"` call (no `cp`; a failure prints a warning rather than being hidden). The output paths of `hygiene-json`, `layout-evidence` and `overlaps` share one confinement rule: inside the repo toplevel, `FLOW_DIR` (or the `--flow-dir` given), or the system temp dir where the run's scratch lives. A run that skips the Round 2 or Round 4 scan passes `--skip-round2` / `--skip-round4`, so stale evidence from an earlier invocation reads as `null`. Per-run files will answer a future question on whether a Round 2 rename gate (Q1 A) or a shortened Round 4 (D2 A) earns its cost.

7. **Operator note:** run `/setup-local` after pulling so the new `~/.claude/prompts/test-cleanup-reference.md` symlink exists.
