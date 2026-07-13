# ADR-0004: Model cost routing (Haiku for mechanical work)

**Status:** Accepted

## Context

A full expert review involves many distinct kinds of work. Some require deep judgment (domain
expertise, type design, concurrency reasoning). Others are mechanical (summarize this diff, route
sections by keyword, grep for uncalled symbols, answer a yes/no question about the code). Running
everything on the most capable model is needlessly expensive; running everything on a cheap model
loses the judgment that makes the review worth doing.

## Decision

Route each step to the cheapest model that can do it well:

- **Haiku** for mechanical/throughput steps: summarizer, tagger, consistency checker, Code Rot Cody
  (dead-code grep), and per-reviewer Q&A. These are declared with `model: haiku` (e.g. `shipit`,
  `code-rot-cody`) or run as Haiku subagents.
- **Opus** for the judgment-heavy work where persona expertise actually lives: the `expert-review`
  orchestrator and its per-reviewer Pass 1/Pass 2/cross-review subagents (which inherit the
  orchestrator's model), and `expert-plan`.

Model choice is set per command/reviewer via frontmatter (`model:`), so it stays explicit and tunable.
For `/expert-review` the Haiku pin now lives in the agent definition — `agents/expert-scout.md` carries
`model: claude-haiku-4-5`, and every mechanical role is dispatched to it — rather than being repeated
as a `model:` argument at each spawn site.

**Per-invocation override:** `/expert-review --model <haiku|sonnet|opus|fable>` sets the model for the
**judgment panel only** — Pass 1, escalated Pass 1, Contrarian Carl, Pass 2, cross-review. The
mechanical roles stay pinned to Haiku regardless: the tagger, the confirm-gate (ADR-0003), Q&A, Code
Rot Cody, and the Consistency Checker route and grep, and a larger model does not grep better. So the
flag scales the part of the bill that buys judgment, and only that part.

The tiers, cheapest to dearest (per 1M tokens, input/output): **Haiku 4.5** $1/$5 · **Sonnet 5** $3/$15
· **Opus 4.8** $5/$25 · **Fable 5** $10/$50. Note the shape of that ladder: Fable is the *most
capable and most expensive* model, at 2× Opus — it is not a cheap tier, and the default (inherit the
orchestrator's Opus) is deliberately one rung below it. Verify pricing against the current model
lineup before writing it into a doc; an earlier draft of this ADR's own command had Fable labelled as
the cheap option, which was simply false.

## Consequences

- **Good:** Most token volume (summarizing, routing, grepping, Q&A) runs cheap; expensive models are
  reserved for where they change the outcome.
- **Cost:** Cheap models occasionally mis-summarize or mis-route, feeding the expensive thread slightly
  wrong inputs. Acceptable because the expert passes re-read the actual diff, not just the summary.
- **Cost:** Parallel per-reviewer subagents (ADR-0002) multiply *input* tokens — every reviewer
  re-receives the framework, its persona, and its sections. Narrow triggers plus the Haiku
  confirm-gate keep that multiplier applied to a smaller panel; `--model` scales what remains.
- **Forking note:** If you fork, retune `model:` frontmatter to your budget — there is no global
  switch; it is intentionally per-step.

### The diff-replication problem, and why `diff-index.md` exists

Prompt caching does not cross subagents. The old sequential design ran every reviewer in one main
thread, so the diff was paid for once and every later turn was a cheap cache hit. The parallel
rewrite (ADR-0002) buys blindness and quality, correctly — but each of the ~20+ subagents opens a
*fresh* context, so a diff handed to N subagents is paid for N times at full price. Nobody costed
that when the parallel design shipped.

Measured on `claude-helpers` itself (a branch that rewrites most of `commands/` and `reviewers/`,
so the diff is unusually large relative to the repo — 44k tokens against a 122k-token repo, 36%):
the tagger routed 2 of 21 reviewers and skipped 19, and Step 5b (the confirm-gate) handed each of
those 19 the **full 44k-token diff** — ~1.1M tokens spent on Haiku to produce 19 one-line
"not my domain" verdicts. Cheap per-token (Haiku, ≈$1), but a genuinely wasteful wall-clock and
design smell: broadcasting the whole diff to agents whose entire job is a routing decision.

**The fix is not "pass a path instead of contents."** Passing a path only moves where the tokens are
counted — the receiving subagent pays the identical cost the instant it calls `Read` on that path.
The only real lever on a subagent's cost is handing it something *smaller to read*. That's what
`{REVIEW_DIR}/diff-index.md` is: `git diff --stat` + hunk headers only (each header already carries
its enclosing function/section), about 1/20th the size of the full patch. The confirm-gate (Step 5b)
reads that instead of `full-diff.patch`, with an escape hatch to pull one file's diff if something
looks in-domain. It's a soft control — the gate still *can* read the full patch — but it removes the
default reason to.

The tagger itself still needs the full patch: the line ranges it emits in `tagged-sections.md` are
byte offsets into `full-diff.patch`, which Pass 1 reviewers then use for bounded `Read(offset,
limit)` calls instead of reading the whole file. That's one agent paying full price, not nineteen.
