# Plan Contribution Contract

This file defines the **output format** for expert contributors to `/expert-plan-v2` planning
sessions. This is a **format specification**, not a persona or lens — read this alongside your
assigned reviewer persona YAML, not instead of it.

## How This File Works With Personas

- **Your persona YAML** (e.g., `security-sage.yaml`) defines your **voice, principles, and planning
  domain lens** — who you are and what you care about (see that file's `summary` and `planReview.focusAreas`).
- **This contract** defines your **output format and structure** — the shape and rules all
  contributors follow, regardless of domain.

Read your persona YAML first to understand your domain lens. Then follow this contract to structure
your contribution.

## Contribution Format

Write your complete planning contribution following this exact structure:

```markdown
### [Your Name]'s Input

**Domain**: [Your area of focus — pulled from your persona's `planReview.focusAreas`]

**Requirements**: [What the plan must address in your domain — bulleted list]

**Risks**: [Failure modes or edge cases your domain is concerned about — bulleted list, or "None"]

**Recommended Approach**: [How to handle your domain — concise, 2-3 sentences]

**Open Questions**: [See format below, or "None"]
```

### Requirements Section

List 3-5 key requirements your domain identifies. These are **must-haves** for the plan in your area
of focus. Be specific about what the plan needs to address.

Example (Security Sage):
- "Trust boundaries must be explicitly defined between user input and internal processing"
- "All external APIs must be treated as untrusted; responses must be validated"
- "Secrets must be stored and transmitted following the project's secret management policy"

### Risks Section

List 2-4 failure modes or edge cases your domain worries about. These are **things that could go
wrong** if your requirements aren't addressed. Be specific about consequences, not vague warnings.

Example (Uncle Bob):
- "If components are tightly coupled, changing the authentication strategy requires changes across 5 files"
- "Resource cleanup on error paths is missing — connections could leak in production"

If your domain has no specific risks for this plan, write "None" and move on.

### Recommended Approach Section

Give your **informed opinion** on how the plan should handle your domain. This is your expert
recommendation — the human still decides. Be concise: 2-3 sentences is the target. If you have
multiple alternatives, name them but recommend one.

Example (Tara TypeSafe):
- "Use a sealed interface for the user session to enforce compile-time verification of role-based access. This prevents runtime auth bypass and makes the type system the first line of defense."

Example (Business Beth):
- "Prioritize user-facing messaging — surface validation errors to users immediately, not in logs. This reduces support load and improves onboarding."

### Open Questions Section

List questions the ticket doesn't answer, no ADR covers, and where multiple valid approaches exist.
Each open question follows this exact format:

```markdown
- **[Question]**
  - _Why it matters_: [What this decision affects in the plan — be specific about consequences]
  - _Recommendation_: [Your suggested answer, based on your domain expertise — NOT a decision, the human still decides]
  - _Confounders_: [Things that could make your recommendation wrong — other constraints, trade-offs, or context your domain doesn't have. "None" if straightforward.]
  - _Source_: [silent | ambiguous | disagreement] — why this is a question, not a decision
```

All four fields are required. Do not skip `_Confounders_` — this is where you name the limits of
your own domain expertise and surface the trade-offs others must consider.

#### Source Classification

Classify why each question is a question, not a decision:

- **silent**: The ticket doesn't mention this at all. Your domain identified a gap.
- **ambiguous**: The ticket mentions it but could be read multiple ways. You're surfacing the ambiguity.
- **disagreement**: Multiple experts disagree on this point (and you're listing it when your
  recommendation differs from another expert's). Name the other expert(s) and their position in the
  question or recommendation.

#### Open Question Examples

**Silent** (ticket doesn't mention it):
```markdown
- **Should the service authenticate on every request or cache the session?**
  - _Why it matters_: Caching improves performance but creates a window where revoked sessions still have access. This affects both user security and operational response time.
  - _Recommendation_: Cache for 5 minutes with explicit revocation checks on sensitive operations. This balances security and performance.
  - _Confounders_: If users frequently disconnect/reconnect, 5 minutes is too long. If the service is internal-only, caching is safe.
  - _Source_: silent
```

**Ambiguous** (ticket mentions it but in multiple ways):
```markdown
- **Is "high availability" 99.9% or 99.99% uptime?**
  - _Why it matters_: This drives infrastructure costs, deployment strategy, and fallback complexity. The difference is 8.6 hours/year vs 52 minutes/year of acceptable downtime.
  - _Recommendation_: 99.9% for MVP, with a plan to upgrade to 99.99% if usage grows. Start conservative.
  - _Confounders_: If this is a payment system, even 99.9% might not be acceptable. If we don't know expected scale, we might over-engineer.
  - _Source_: ambiguous
```

**Disagreement** (experts differ):
```markdown
- **Should this be a new service or extend the existing API service?**
  - _Why it matters_: This affects scalability, team ownership, deployment cadence, and operational complexity.
  - _Recommendation_: Extend the existing service (Uncle Bob + Penny Pincher position). New service adds operational overhead unless scale justifies it.
  - _Confounders_: The existing service is already at 80% load. If this feature is security-critical, isolation justifies a new service (Security Sage's position). Scope Creep Steve would argue for new service to enable independent scaling later.
  - _Source_: disagreement (Penny Pincher vs. Security Sage)
```

## The "Ask, Don't Assume" Rule

This is the most important rule in planning contributions.

When you raise a consideration where:
- The ticket doesn't specify a preference
- No ADR or project convention covers it
- Multiple valid approaches exist

**It MUST become an open question.** Do NOT pick an approach and bake it into your Recommended
Approach. Do NOT say "we should probably..." for something the ticket is silent on.

**Ask, don't assume.** Your domain expertise is valuable precisely because it surfaces options the
ticket author didn't consider. Your job is to raise the question, not to hide the decision in your
recommendation.

When something IS covered by convention or ADR, mention it in Recommended Approach and do NOT make
it an open question. Example: "Use the project's existing error handling pattern" doesn't need an
open question — it's a statement of fact.

## Your Output Location

Write your complete contribution to this file:
```
{PLAN_SESSION_DIR}/{your-name}-contribution.md
```

Replace `{your-name}` with your reviewer name in kebab-case (e.g., `security-sage-contribution.md`,
`uncle-bob-contribution.md`).

The file location tells the orchestrator which expert's perspective it is; the path is semantic.

## Receipt Format

After writing your contribution file, return **only a one-line receipt** as your final message:

```
{your-name}-contribution.md written — {n} requirements, {n} risks, {n} open questions
```

Example: `security-sage-contribution.md written — 4 requirements, 2 risks, 1 open question`

Do not return your contribution itself. Your report is the file, not the message. Returning the
full contribution in your final message would double-load the orchestrator's context (once from the
file, once from your message), defeating the parallel design. The orchestrator reads the file you
wrote.

This follows the same principle as the code-review framework: **the file is the contract**. See
`agents/expert-reviewer.md` under "The file is the contract" for the full rationale.

## Scope Discipline

Raise only considerations that belong in your domain:
- Requirements your domain cares about (trust boundaries, type design, business value, etc.)
- Risks your domain is responsible for catching (security, performance, scalability, etc.)
- Questions where your expertise meaningfully informs the answer

Do not raise issues that belong to another persona's domain. They have their own perspective, and
duplicate concerns dilute the panel. (The digest will see all contributions and can flag agreement.)

## Quality Checklist

Before returning your receipt:
- [ ] Your Domain describes your area from the persona YAML
- [ ] Requirements are specific and actionable (not "make it secure")
- [ ] Each risk includes a consequence, not just a name
- [ ] Recommended Approach is 2-3 sentences, not a full design doc
- [ ] Every open question has all four fields: Why it matters, Recommendation, Confounders, Source
- [ ] Confounders are honest — they name where your domain advice could be wrong
- [ ] Open questions follow "Ask, Don't Assume" — they're not hidden assumptions
- [ ] No open question is duplicated in your contribution
- [ ] Your receipt line is one line only, with counts accurate to your contribution
