# Reviewer Selection Audit

This document defines the methodology for auditing and tuning `/expert-review`'s reviewer selection (`useWhen`/trigger text in `reviewers/index.yaml`) using the offline harness `scripts/reviewer-selection-audit.py`.

## Running the Harness and Reading Its Reports

The harness exposes three subcommands:

### `attendance`

Reports per-reviewer and panel-wide inclusion rates. Each run answers: "Of all runs in the corpus, how many matched this reviewer's `useWhen` triggers?"

Output includes:
- **Per-reviewer attendance**: raw match count and percentage of corpus runs
- **Router yes/no breakdown**: how many times the router affirmed vs. rejected the `useWhen` match
- **Structural gate no count**: runs excluded by structural pre-gates (if any apply to this reviewer)
- **Per-repo breakdown**: same metrics stratified by repo, so you can see if a reviewer is over- or under-represented in particular codebases

Usage: `scripts/reviewer-selection-audit.py attendance [--reviewer <slug>]`

### `yield`

Reports severity-weighted finding counts across confirmed (not disputed or deferred) findings only, stratified by reviewer and repo.

Scoring: CRITICAL=8, HIGH=4, MEDIUM=2, LOW=1. Yield = total severity weight / run count (average severity-adjusted findings per run attended).

Output includes:
- **Per-reviewer yield**: average severity-weighted confirmed findings across all runs they attended
- **Contribution count**: raw count of findings the reviewer raised (CRITICAL, HIGH, MEDIUM, LOW)
- **Per-repo yield**: same metrics by repository

Usage: `scripts/reviewer-selection-audit.py yield [--reviewer <slug>]`

### `simulate`

Simulates a proposed change to trigger matching before shipping. Takes a candidate index (YAML file with reviewer trigger changes) and optional reviewer slug.

Two modes:

1. **Trigger-list delta** (reviewer triggers added/removed): outputs before-and-after attendance for runs that flipped in/out of the reviewer's set. For each flipped-out run, outputs the reviewer's confirmed CRITICAL and HIGH findings in that run (if any), so you can manually verify none were lost. The tool exits with nonzero if any CRITICAL findings would be silently excluded; fatal failures must be resolved before shipping.

2. **Prose-only change** (only `useWhen` description text changed, no trigger list delta): outputs a 60-run repo-stratified sample of runs that *would still match* the reviewer under the new wording, plus a replay prompt suitable for passing to Haiku (model-cost-routing per ADR-0004, mechanical classification work). The sample and prompt are attached to the change commit-message or plan so a human can dispatch the replay without re-running the full harness.

Usage: `scripts/reviewer-selection-audit.py simulate --candidate-index <path> [--reviewer <slug>]`

## Diagnosis: Attendance as Symptom, Yield as Diagnostic

High attendance alone does not mean a reviewer's triggers are too broad. The diagnostic is **severity-weighted yield** (confirmed findings per run attended).

**Fragile Feynman case study**: 82% attendance, 2.52 severity-weighted yield, 20 CRITICAL confirmed findings across the corpus. Despite high attendance, the yield is correctly high and the CRITICAL findings are in-domain — the reviewer was correctly calibrated and no narrowing was warranted. Attendance alone would have suggested over-inclusion; yield and CRITICAL breakdown together showed the inclusion was justified.

**Decision rule**: Do not narrow a reviewer's `useWhen` based on attendance alone. If attendance is high but yield is also proportionally high, and CRITICAL/HIGH findings are consistently in-domain, the reviewer is correctly tuned. Narrowing based on attendance is disguised as precision work but is actually a cost-cutting move (see Labeling Rule below).

## Labeling Rule: Precision Fix vs. Cost Reduction

Every proposed reviewer-selection change must be explicitly labeled as one of:

### (a) Precision Fix
Same domain, but the `useWhen`/triggers describe it more correctly or narrowly. The reviewer was sometimes firing on out-of-domain diffs; the change prevents that. Examples:
- "Tara TypeSafe was matching Go interfaces; we're narrowing to Rust trait-objects where type inference is the pain point"
- "Rachel was matching all concurrency keywords; we're narrowing to explicit thread/channel creation since that's where race conditions hide"

### (b) Cost Reduction
Explicitly a budget decision: the reviewer's domain remains valid, but panel cost or token budget constrains inclusion. Must be named as such, never laundered through `useWhen` wording as if it were a precision improvement.

Example (cost reduction):
- "Uncle Bob's attendance and yield are both correctly high, but panel token cost targets require us to reduce his inclusion from 55% to 40%. We're widening the architectural-smell trigger to increase selectivity."

Precision fixes belong in a `chore: improve reviewer selection precision` commit; cost reductions must be marked in the commit message or ADR as an explicit budgetary tradeoff.

## The Mandatory Narrowing Gate

Any `useWhen`/trigger change that **narrows** a reviewer's inclusion (reduces their attendance) requires:

1. **Run `simulate --candidate-index <path> [--reviewer <slug>]`** to identify all runs that would be flipped out
2. **Manual cross-check**: For each flipped-out run, review the reviewer's confirmed CRITICAL and HIGH findings (if any). Ensure none were in-domain and merely misclassified elsewhere.
3. **Attach evidence**: Include the `simulate` output and manual-check results in the commit message, plan description, or attached document before shipping.

If `simulate` exits nonzero (any CRITICAL findings in flipped-out runs), the change is **fatal** — do not ship without resolving the loss.

**WIDENING a reviewer's inclusion** is exempt from the CRITICAL/High cross-check (widening cannot cause a silent false negative). However, still report:
- The attendance delta (old % → new %)
- Panel cost implication (additional token budget per run, if known)

### Prose-Only Narrowing

When a change only alters `useWhen` prose description (no trigger-list delta), the harness outputs a 60-run repo-stratified sample + replay prompt. You must:

1. Dispatch the replay prompt to Haiku (or equivalent mechanical classifier)
2. Capture the Haiku replay output
3. Attach the result to the commit or plan before shipping

This is mechanical classification work per ADR-0004; Haiku cost is appropriate.

## Precondition and Failure-Mode Statement

Every shipped `useWhen` change should record, in its commit message or a plan's rationale:

- **What diff shape the new wording is meant to admit**: "We're narrowing to diffs that mention 'goroutine', 'channel', or 'sync.Mutex'."
- **What it means when a genuinely in-domain diff no longer matches**: "A diff that races on shared state but doesn't mention those keywords would be missed."

This statement survives into the codebase so a future reader auditing reviewer selection can understand the original intent and check if it's still valid.

## Structural Pre-Gate Denylist and Conditions

Four reviewers are permanently ineligible for Sam-System-style structural pre-Router exclusion:

- **security-sage**, **rachel**, **tara-typesafe**, **contract-chris**

These domains (security, concurrency, type/contract safety) cannot be ruled out by diff shape alone. A diff with no SQL keywords can still have a SQL injection; a diff with no concurrency keywords can still race. Structural gates cannot be used to pre-exclude them.

### Eligibility Conditions for Any Other Structural Gate

A structural pre-gate exclusion must satisfy both conditions:

1. **The exclusion signal is structural**: computable from the diff-index.md (file types, extension patterns, keyword presence). Not inferred from triage rulings, reviewer past behavior, or other non-structural data.
2. **The gate can only turn Yes into No, never reverse**: If the gate fires, the reviewer is excluded. If the gate misses (malformed input, missing diff-index), the gate must fail open — the reviewer is included as if the gate didn't fire.

### Corpus Limitations

The harness learns from corpus review runs with these constraints (carried forward without re-validation in this audit):

- **Repo concentration**: lotl-co-lotl 32.9%, stephenkiers-InsuranceTracking 12.7%, claude-helpers 9.1%; top 3 repos > 54% of corpus
- **Branch repetition**: moderate; 78–80% unique branches per-repo
- **Effort level reconstruction**: effort-scout.json exists in only 1 of 772 review dirs, so per-run effort is not reliably reconstructible for historical analysis

These limitations are acknowledged here so a future auditor does not mistake correlation (high attendance in a particular repo) for truth (universally high attendance).

## Known Limitation: Circularity in Severity-Weighted Yield

Severity-weighted yield may be partly circular: if triage confirms findings from familiar (frequently-included) reviewers slightly more readily than from unfamiliar (rare) reviewers, yield both rewards and is rewarded by attendance.

**This is untested and unmitigated.** Yield should not be treated as unexamined ground truth. A future reader analyzing reviewer selection should be aware that yield may partly reflect triage bias toward familiar reviewers, not pure quality signal.

No mechanism is proposed to fix this; doing so would require a confirmation-rate study (comparative triage outcomes by reviewer familiarity) beyond the scope of this work.

## Post-Ship Re-Check and Revert Threshold

After a Vera Verifier / Curious Casey `useWhen` precision redraw and an Uncle Bob widening ship (in a later, separate pass of issue #202), someone should monitor corpus drift. Re-run `attendance` and `yield` once **40 new review runs** have accumulated across the corpus since the change lands.

### Baselines (as of 2026-09-17)

These baselines were captured before any selection tuning in issue #202 shipped:

- **Vera Verifier**: 82% attendance, 1.92 severity-weighted yield
- **Curious Casey**: 83% attendance, 2.53 severity-weighted yield
- **Uncle Bob**: 55% attendance, 1.87 severity-weighted yield

### Revert Threshold

Revert or widen the change if ANY of:

**(a)** Vera Verifier's or Curious Casey's severity-weighted yield (on runs they still attend) falls below its pre-change baseline on **two consecutive re-checks** (e.g., 1.85 and 1.84 vs. 1.92 baseline).

**(b)** Any run in the newly-excluded shape produces a CRITICAL or HIGH finding in the excluded reviewer's domain, but that finding is credited to a different reviewer in the final action plan instead.

**(c)** Uncle Bob's attendance lands outside the 70–85% target band on two consecutive re-checks. (Target band acknowledges his selectivity while guarding against accidental over-narrowing.)

### Re-Check Ownership and Record

The re-check is owned by **the person running the next audit** — no fixed individual. When re-checking:

1. Record today's attendance and yield for each reviewer in this document (update the Baselines section with a dated entry)
2. Compare against the baselines listed above
3. If any revert threshold is crossed, file a new issue or mark the original change for revert discussion

This doc preserves the baselines so the comparison never depends on memory.
