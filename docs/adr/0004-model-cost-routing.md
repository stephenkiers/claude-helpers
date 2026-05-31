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
- **Opus** for the judgment-heavy main review thread (`expert-review`, `expert-plan`) where persona
  expertise actually lives.

Model choice is set per command/reviewer via frontmatter (`model:`), so it stays explicit and tunable.

## Consequences

- **Good:** Most token volume (summarizing, routing, grepping, Q&A) runs cheap; expensive models are
  reserved for where they change the outcome.
- **Cost:** Cheap models occasionally mis-summarize or mis-route, feeding the expensive thread slightly
  wrong inputs. Acceptable because the expert passes re-read the actual diff, not just the summary.
- **Forking note:** If you fork, retune `model:` frontmatter to your budget — there is no global
  switch; it is intentionally per-step.
