# Expert Review Panel (shared)

This file contains the shared review panel — Summarizer → Router → Pass 1 → Contrarian Carl →
Haiku Q&A → Pass 2 → Amalgamator (Steps 4–10). Both `/expert-review` and `/expert-review-coworker`
read this file and follow these steps.

**Preconditions — the caller has already set these before invoking this panel:**
- `REVIEW_DIR` — the checkpoint directory, created, with `full-diff.patch` and `diff-index.md` written
- `PANEL_MODEL` — the model for the judgment panel (may be unset → subagents inherit the caller's model)
- `NAMED_SELECTION` — `true` if the user named specific reviewers (Router is bypassed), else `false`
- `NAMED_REVIEWERS` — space-separated lowercased names, when `NAMED_SELECTION=true`
- `PROJECT_CONTEXT`, `DETECTED_LANGUAGES`, project modifiers, and any plan/issue context — detected by the caller
- Reviewer index (`~/.claude/reviewers/index.yaml`) already discovered

All I/O is through `REVIEW_DIR` paths. Reviewer YAMLs live in `~/.claude/reviewers/` regardless of
which repo is under review — this panel is completely repo-agnostic.

The steps below retain their original numbering (Step 4 … Step 10) so cross-references from the
calling commands stay stable.

---

### Step 4: Summarizer → `summary.md`

Spawn one subagent (`subagent_type: "general-purpose"`) with the summarizer prompt
@~/.claude/prompts/summarizer.md, pointing it at `{REVIEW_DIR}/full-diff.patch` (it needs the actual
diff text to summarize) rather than inlining `git diff` output, plus: changed-file list, commit
messages (`git log main...HEAD --format="%s%n%n%b"`), PR description if available, and any
known-issues index. Save its output to `{REVIEW_DIR}/summary.md`. The file contains
`## Technical Summary` (what), `## Business Context` (why), `## Suggested Reviewers`.

### Step 5: Router (sonnet) → `tagged-sections.md`

Spawn a subagent (`subagent_type: "expert-reviewer"`, `run_in_background: false`, `model: "sonnet"` —
model explicitly pinned to sonnet here, a narrow judgment task independent of the panel tier) with the router prompt @~/.claude/prompts/router.md. The router reads:
- `{REVIEW_DIR}/full-diff.patch` (it needs the full patch: the line ranges it emits are offsets into
  that file, which later reviewers use for bounded reads)
- The `{REVIEW_DIR}/summary.md` (Technical Summary and Business Context)
- The plan/issue context (if any)
- `reviewers/index.yaml` **ONLY** — the router must not load persona YAML files; progressive
  disclosure means routing decisions are made from each expert's declared interest (`triggers`,
  `useWhen` in the index), not their full personas

The router outputs `{REVIEW_DIR}/tagged-sections.md` with:
1. `## Panel Decision` — a summary of which reviewers were selected and why, formatted as:
   ```
   | Reviewer | Selected | Reason |
   | ... | Yes | {1-line justification} |
   | ... | No | {1-line justification} |
   ```
2. Per-reviewer sections with line ranges, exactly as today's router outputs them, so Pass 1 reviewers
   can use them for bounded reads.

**Always-run set (never routed, pre-seated):**
- Sam System, Code Rot Cody, Consistency Checker (they get the full diff by domain, not by routing),
- Contrarian Carl (runs last, always).

The router is told these four are pre-seated and to treat them as included for the decision table.

**Named reviewers:** If the user named specific reviewers (Step 3, `NAMED_SELECTION=true`), skip the
router entirely — the user's selection *is* the decision, and all four always-run reviewers still
participate. Step 6 branches on `NAMED_SELECTION`: named/always-run reviewers all read
`{REVIEW_DIR}/full-diff.patch` directly instead of line ranges into it (there is no router output to
offset into). This costs each named reviewer a full-patch read instead of a bounded one — acceptable,
since named mode is already the smaller, more deliberate invocation.

**Synthesize `tagged-sections.md` in named mode.** The Amalgamator (Step 10) reads
`tagged-sections.md` for the `## Panel Decision` table it populates in `final-report.md`. Even
though the router didn't run, synthesize a minimal record so downstream steps have a consistent
input:

```bash
{
  echo "# Routing Decision"
  echo ""
  echo "## Panel Decision"
  echo ""
  echo "| Reviewer | Selected | Reason |"
  echo "|----------|----------|--------|"
  for r in $NAMED_REVIEWERS; do
    echo "| $r | Yes | Named by user |"
  done
  for r in sam-system code-rot-cody consistency-checker contrarian-carl; do
    if ! echo "$NAMED_REVIEWERS" | grep -qw "$r"; then
      echo "| $r | Yes | Always-run |"
    fi
  done
  echo ""
  echo "# Tagged Sections"
  echo ""
  echo "## (Named selection: all reviewers read full-diff.patch directly — no line-range offsets)"
} > "$REVIEW_DIR/tagged-sections.md"
```

### Step 6: Pass 1 Blind Reviews (parallel subagents) → `{reviewer}-pass1.md`

**If `NAMED_SELECTION=true`:** the router did not run; Step 5 synthesized a minimal
`tagged-sections.md` as a routing record (see above). The selected reviewers are exactly the user's
named reviewers plus the always-run four; every one of them reads `{REVIEW_DIR}/full-diff.patch`
in full rather than a line-range offset into it.

**Otherwise:** read `tagged-sections.md` and parse which reviewers were selected by the router.

Launch all selected reviewers (routed by the router OR named by the user OR always-run) in ONE
message. All run as **`subagent_type: "expert-reviewer"`**, `run_in_background: false`,
`model: PANEL_MODEL` from Step 3.

**Launch ALL Pass 1 reviewers in ONE message** (multiple Task calls in a single assistant turn).
One subagent per reviewer. They still run concurrently — the harness caps concurrency — and they have
all returned by the time you continue.

**Why a custom agent, not `general-purpose`.** Twenty concurrent subagents reading persona files and
writing checkpoints outside the working directory would produce twenty near-identical permission
dialogs — the permission system does not deduplicate across concurrent agents, and this command's
`allowed-tools` frontmatter does not propagate to subagents it spawns. `expert-reviewer` is
capability-restricted instead of dialog-gated: `permissionMode: bypassPermissions`, but **no `Edit`
tool and no write-capable Bash**, so a reviewer physically cannot modify the code it is reviewing.
It reads the repo and writes one file. Same for `expert-scout` on the mechanical roles.

`Write` itself is not path-scoped by the tool allowlist — a subagent instructed to write elsewhere
could. When you verify checkpoint files after each join barrier (Steps 6, 8, 9), that check is doing
double duty: confirming the expected file exists *and* implicitly that nothing unexpected showed up
outside `{REVIEW_DIR}`. If you ever see a write outside `{REVIEW_DIR}` — a stray file, a modified
file elsewhere in the repo — treat that run as compromised: stop, do not trust its findings, and
report it rather than silently continuing.

**Pass paths, not contents.** A prompt is self-contained if the subagent can *reach* everything it
needs, not if you paste everything into it. Every `Agent` prompt you write stays in your context for
the rest of the run and is re-read from cache on every turn — so inlining a 11.4KB framework into 20+
prompts costs you ~50k tokens and buys the subagent nothing it couldn't have read itself. Open each
prompt with:

```
Before reviewing, read these files with the Read tool:
  1. ~/.claude/prompts/expert-framework.md  — the canonical output format, response levels,
     severity definitions, scope-expansion and when-not-to-flag rules. Follow it exactly.
  2. ~/.claude/reviewers/{name}.yaml        — your persona. Use `codeReview.prompt` as your
     review lens. If it has a `languageExtensions` key with entries matching any of
     {DETECTED_LANGUAGES}, apply those too, under "Language-Specific Checks ({language})".
  3. {path to {name}-local.yaml}            — only if one was found in Step 2; it augments (2).
```

Then supply inline **only what you alone know** — none of it is on disk for the subagent to find:

- Their tagged sections — as line ranges into `{REVIEW_DIR}/full-diff.patch`, plus:
  ```
  Your sections are line ranges into {REVIEW_DIR}/full-diff.patch. Read ONLY those ranges
  (the Read tool takes offset/limit). Reading whole source files when a finding needs
  surrounding context is expected and correct — reading the whole patch file is not.
  ```
  (`NAMED_SELECTION=true`: no router output exists to offset into — tell the reviewer to read
  `{REVIEW_DIR}/full-diff.patch` in full instead.)
- The **Technical Summary** from `summary.md`
- `PROJECT_CONTEXT`, project modifiers, `DETECTED_LANGUAGES`, and the strict delta-scope rule below
- `{REVIEW_DIR}` and their output path

**The file is the contract (rule #2 above) — never ask a subagent to return its report.** Instruct
each reviewer to Write its full review to `{REVIEW_DIR}/{reviewer}-pass1.md` in the framework's
canonical format, and to return **only a one-line receipt** as its final message:

```
Write your complete review to {REVIEW_DIR}/{reviewer}-pass1.md using the Write tool.
The VERY LAST LINE of the file must be exactly:
  <!-- pass1-end -->
This sentinel lets the join barrier detect a truncated write — its absence means the barrier will
treat your output as failed even if the file exists.

Your final message must be ONLY this receipt line — NOT the review itself:

  {reviewer} | {SKIP|QUICK-SCAN|DEEP-DIVE} | findings: {n} ({c}C/{h}H/{m}M/{l}L) | open-questions: {n} | wrote: {path}
```

The receipt carries everything downstream steps actually branch on (decision level, whether there
are findings, whether there are open questions) — used by Step 8 (Q&A), Step 9 (Pass 2), and Step 10
(Amalgamator).

**Blindness rule: Pass 1 prompts must NOT include Business Context, commit messages, or the PR
description** — only the Technical Summary and the code. This is the point of running them as
fresh subagents.

Strict delta-scope rule (include in every prompt):
```
SCOPE: STRICT DELTA REVIEW — only report issues INTRODUCED or WORSENED by this PR.
Do NOT report pre-existing issues in unchanged code. If the PR makes an existing
issue worse, report it; if it doesn't touch it, skip it.
```

Three reviewers **always run and are never gated** (the router does not route them; their domain is
the whole diff by definition). They get special inputs but run in the same parallel batch — and they
follow the same rules as everyone else: they read their own YAML by path, and they return a receipt,
not a report. (Their output *formats* differ — those formats are defined in their own YAMLs, which
they read themselves; you do not need to know them here.)

- **Sam System** (integration): gets `{REVIEW_DIR}/full-diff.patch` (not tagged sections — his
  domain is the whole diff, so he reads the whole file), the Technical Summary, and any plan context
  as "Known Integration Concerns". He must trace data flow across files — read both ends of every
  factory/event-bus/config connection and flag parameters passed but never used. Output: canonical
  format (he is NOT an ADR-0006 carve-out); each finding's **Issue** field starts with the data-flow
  trace, e.g.
  `Flow createSession (a.ts:12) → createRecordingSession (b.ts:30): eventBus passed but never destructured`.
  Decision is always DEEP-DIVE.

- **Code Rot Cody** (`subagent_type: "expert-scout"`, ADR-0006 carve-out): gets
  `{REVIEW_DIR}/full-diff.patch` + changed-file list. He greps the ENTIRE repo to verify every
  claim — never guesses. New symbols: grep for callers (excluding definition site), flag zero-caller
  symbols DEAD. Removed symbols: grep for lingering references, flag ORPHANED. New config fields:
  verify stored, read, validated, documented. His output format (symbol-inventory table) and his
  `languageExtensions` are in his own YAML, which he reads.

- **Consistency Checker** (`subagent_type: "expert-scout"`, ADR-0006 carve-out): gets
  `{REVIEW_DIR}/full-diff.patch` + the PR description (from cache or `gh pr view --json body`); it
  reads its own persona file for the review lens, like every other subagent. Mechanical pattern
  pass: mixed error types for the same purpose, inconsistent cleanup patterns, PR-description claims
  contradicted by the code. Its output format is defined in its own YAML.

**Join barrier.** All Step 6 agents launched in one message with `run_in_background: false` means
they have all returned by the time you continue. For every selected reviewer, the join condition is
**all three** simultaneously: a receipt was returned AND `{REVIEW_DIR}/{reviewer}-pass1.md` exists
on disk AND the file ends with the sentinel `<!-- pass1-end -->` (which every reviewer appends as its
last line — its absence means the write was truncated, not just missing). If any of the three conditions fails,
**re-run that one reviewer once** — do not try to reconstruct the review from the receipt; the
receipt is a status line, not a report. If the re-run also fails the joint condition, do not retry a
third time — write a stand-in file so downstream globs find something rather than nothing:

```bash
cat > "$REVIEW_DIR/${reviewer}-pass1.md" <<'EOF'
# Pass 1 Review: {reviewer}

## Decision
FAILED

## Reason
Agent returned no output or a truncated write after two attempts.

## Findings
No findings (reviewer failed)

## Summary
- Critical: 0
- High: 0
- Medium: 0
- Low: 0
<!-- pass1-end -->
EOF
```

Then report that reviewer as failed and continue the pipeline without it.

**Never poll.** Do not use `ScheduleWakeup`, `sleep`, or repeated status checks to wait for
subagents. A timed wakeup re-reads your *entire* context from cache and learns nothing you would not
have learned by waiting — in one observed run, 14 such wakeups each re-read ~430k tokens. If a panel
is large enough that you truly want it backgrounded, then **end your turn**: the harness re-invokes
you when the agents finish. Track per-reviewer status by checking for files, never by counting
notifications.

### Step 7: Contrarian Carl (after the barrier) → `contrarian-carl-pass1.md`

Carl runs **last** and is the one reviewer who is not blind to the panel. Spawn one subagent
(`subagent_type: "expert-reviewer"`, `model: PANEL_MODEL`) and, per "Pass paths, not contents",
point him at the files rather than inlining them: his persona (`~/.claude/reviewers/contrarian-carl.yaml`),
the other reviewers' `{REVIEW_DIR}/*-pass1.md` files (including Cody's and the Consistency Checker's).
Supply the diff scope inline. His instruction:

```
You have access to what EVERY other reviewer found. Your job is to find something
DIFFERENT. Do not repeat any finding already raised; look where others didn't look;
question assumptions everyone shared. Raise the strongest concern nobody else
mentioned — or, if after genuine effort you find none, name the strongest candidate
concern you considered and explain why you rejected it. Do NOT manufacture a finding
just to have one.
```

His contrastive output format (What Others Covered / What Everyone Missed / Assumptions I'm
Questioning / The Question Nobody Asked / Verdict) is defined in his own YAML (ADR-0006
carve-out). He writes `{REVIEW_DIR}/contrarian-carl-pass1.md` and returns a receipt only —
`contrarian-carl | findings: {n} | wrote: {path}` — like every other panel agent. He does NOT
participate in Pass 2; his findings are presented as-is.

### Step 8: Haiku Q&A (parallel) → `{reviewer}-questions-answered.md`

Runs BEFORE Pass 2 so the re-evaluation is informed rather than speculative (ADR-0002). For each
reviewer whose Pass 1 receipt reported `open-questions > 0`: spawn a subagent
(`subagent_type: "expert-scout"`) and point it at `{REVIEW_DIR}/{reviewer}-pass1.md` — it reads
the Open Questions itself, so you never have to load them. Supply the reviewer's name and role
summary (from `index.yaml`) and their tagged sections, plus: "Read the hinted files plus whatever
else is needed to answer concretely. If a question can't be settled by static analysis, say so and
name the runtime evidence needed."

It writes `{REVIEW_DIR}/{reviewer}-questions-answered.md` — **Answer** + **Evidence** (`path:line`)
per question — and returns a receipt only: `{reviewer} | answered: {n} | wrote: {path}`. Launch all
Q&A agents in one message.

**Join barrier.** All Q&A agents run with `run_in_background: false`, so they have all returned
before Step 9 starts — Step 9 must not launch a reviewer's Pass 2 until its Q&A file (if one was
expected) exists. Before Step 9, verify `{REVIEW_DIR}/{reviewer}-questions-answered.md` exists for
every reviewer whose Pass 1 receipt reported `open-questions > 0`; re-run just the missing Q&A
agent(s) and wait for them before proceeding — do not let Pass 2 start without the answers it exists
to use.

### Step 9: Pass 2 Re-evaluations (parallel subagents) → `{reviewer}-pass2.md`

**Only for judgment reviewers whose Pass 1 receipt reported findings > 0.** Mechanical roles
(Code Rot Cody, Consistency Checker) skip Pass 2. Carl is not re-evaluated; his findings stand as-is.

Launch one subagent per eligible reviewer, in one message (`subagent_type: "expert-reviewer"`,
`model: PANEL_MODEL`). Pass paths, not contents — each prompt names the files to Read:

- `~/.claude/prompts/pass2-reevaluation.md` — the pass2 prompt and output format
- `{REVIEW_DIR}/{reviewer}-pass1.md` — their own Pass 1
- `{REVIEW_DIR}/{reviewer}-questions-answered.md` — if one exists
- Permission to read any file referenced in their findings, to resolve uncertainty

Supply inline only the **Business Context** section from `summary.md` — revealed now for the first
time, and the whole point of Pass 2. Also supply the plan/issue context if available.

**Reframed as skeptic-verifier (anti-anchoring).** The prompt's framing changes from "continue your
review" to "another engineer submitted this review; given the context, which findings hold up to
your standards?" — third-person, minimal context, explicitly designed to prevent sunk-cost defense.

Each re-evaluates every finding as CONFIRMED / RESOLVED / DOWNGRADED (with reason and final
severity), writes `{REVIEW_DIR}/{reviewer}-pass2.md` in the pass2 prompt's format, and returns a
receipt only:

```
{reviewer} | pass2 | confirmed: {n} | resolved: {n} | downgraded: {n} | wrote: {path}
```

### Step 10: Amalgamator (one expensive agent) → `final-report.md`

**Before spawning the Amalgamator,** verify all expected checkpoint files exist (pass1 for every
routed reviewer, pass2 where findings, questions-answered where open questions).
If any are missing, re-run only those agents **once**. If a re-run still fails to produce its file,
stop retrying — report the specific agent(s) still missing and proceed to the Amalgamator without
them rather than looping. A stuck reviewer should never block or infinitely retry the pipeline.

The Amalgamator is **ONE subagent** (`subagent_type: "expert-reviewer"`, `model: PANEL_MODEL`; this
is the step where `--model fable` earns its cost). Its job: synthesis, not review. It reads:
- All `{REVIEW_DIR}/*-pass1.md` files (including Carl's)
- All `{REVIEW_DIR}/*-pass2.md` files (re-evaluation verdicts)
- All `{REVIEW_DIR}/*-questions-answered.md` files
- The `{REVIEW_DIR}/tagged-sections.md` (router's Panel Decision)
- The plan/issue context (if any)

Its mandate and the `final-report.md` template live in **`~/.claude/prompts/amalgamator.md`**. Pass
the path; do not read it yourself and do not paste it into the prompt (context discipline rule 1).

It writes `{REVIEW_DIR}/final-report.md` and returns a receipt with the finding count summary:

```
amalgamator | final-report written | critical: {n} | high: {n} | medium: {n} | low: {n} | wrote: {path}
```

