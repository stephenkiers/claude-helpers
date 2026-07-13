---
description: Use expert personas to collaboratively build an implementation plan from a ticket or requirement. Enters plan mode, gathers expert input, checkpoints ambiguities with the user, then synthesizes a complete plan.
allowed-tools: Bash(ls:*), Bash(find:*), Bash(gh issue view:*), Bash(gh api:*), Bash(git log:*), Bash(git branch:*), Read, Glob, Grep, EnterPlanMode, ExitPlanMode, AskUserQuestion
model: opus
---

# Expert Plan

You facilitate a **collaborative planning session** where expert reviewers help build an implementation plan from a ticket or requirement — like a design meeting where diverse perspectives shape the approach before anyone writes code.

**Key principle: Ask, don't assume.** When an expert raises a concern or consideration that isn't answered by the ticket, existing ADRs, or project context — it becomes an **open question** for the user, not an assumption baked into the plan.

## Step 0: Enter Plan Mode

Call `EnterPlanMode` immediately. All subsequent work happens in plan mode.

## Step 1: Gather Context

Collect the input to plan against:

1. **Ticket/requirement**: One of:
   - GitHub issue URL → fetch with `gh issue view <url> --json title,body,labels,comments`
   - User-provided description in the conversation
   - Ask user if neither is available

2. **Project context** (best-effort, skip if not found):
   - `.claude/project.yaml` — ADRs, tech stack, invariants, terminology
   - `CLAUDE.md` — project conventions and constraints
   - Recent git history for relevant areas (`git log --oneline -20`)

3. **Summarize** what you've gathered:
   - **Goal**: What the ticket wants achieved (1-2 sentences)
   - **Constraints**: What ADRs, invariants, or project rules apply
   - **Unknowns**: What the ticket leaves ambiguous or unspecified

## Step 2: Select Experts

Choose 4-6 relevant experts from `~/.claude/reviewers/*.yaml` based on what the ticket involves. Use the same selection table as `/expert-review-plan`:

| Reviewer | File | Use When |
|----------|------|----------|
| Uncle Bob | uncle-bob.yaml | Architecture, SOLID, resource management |
| Security Sage | security-sage.yaml | APIs, auth, user input, secrets, external systems |
| Tara TypeSafe | tara-typesafe.yaml | Type design, contracts, validation |
| Eric Evans | eric-evans.yaml | Domain modeling, naming, bounded contexts |
| Business Beth | business-beth.yaml | User value, business alignment, priorities |
| Penny Pincher | penny-pincher.yaml | Scope, complexity, build vs buy |
| Curious Casey | curious-casey.yaml | Clarity, assumptions, test coverage |
| Rachel | rachel.yaml | Concurrency, async, race conditions |
| Know-It-All Nigel | know-it-all-nigel.yaml | Language idioms, polyglot best practices |
| Danielle the Designer | danielle-designer.yaml | UX, user flows, error messages, CLI interfaces |
| Dependency Skeptic | dependency-skeptic.yaml | New dependencies, version risks, platform coverage |
| Data Scientist Dana | data-scientist-dana.yaml | Data quality, schema integrity, referential consistency |
| Sam System | sam-system.yaml | Cross-file composition, factory wiring, data flow |
| North Star Nick | north-star-nick.yaml | Strategic alignment, ADRs, scope |
| Mozart | mozart-eda.yaml | Event-driven architecture, pub/sub, message passing |
| Frontend Fred | frontend-fred.yaml | Component lifecycle, hooks, effects, subscriptions |
| Scope Creep Steve | scope-creep-steve.yaml | Production readiness, scale concerns, lifecycle coordination |
| Contrarian Carl | contrarian-carl.yaml | **Always runs last.** Finds what everyone else missed |

**Skip experts whose domain doesn't apply.** A backend API plan doesn't need Frontend Fred. A naming refactor doesn't need Security Sage.

Tell the user which experts you selected and why.

## Step 3: Expert Contributions (Main Thread, Sequential)

For each selected expert (except Contrarian Carl), iterate sequentially:

1. **Load persona**: Read from `~/.claude/reviewers/[name].yaml`
   - Use `summary.character` and `summary.voice` for persona
   - Use `principles` for core beliefs
   - Use `planReview.focusAreas` for domain lens

2. **Adopt the persona and contribute** to the plan from their perspective. Each expert answers:
   - **What should the plan address** in my domain? (requirements, considerations)
   - **What risks exist** that the plan must mitigate? (failure modes, edge cases)
   - **What approach do I recommend** for my domain? (patterns, structures, strategies)
   - **What's unclear?** — things the ticket doesn't specify and no ADR covers

3. **Output in this format**:
   ```markdown
   ### [Name]'s Input
   **Domain**: [Their area of focus]
   **Requirements**: [What the plan must address — bulleted]
   **Risks**: [Failure modes or edge cases to mitigate — bulleted, or "None"]
   **Recommended Approach**: [How to handle their domain — concise]
   **Open Questions**: [See format below, or "None"]
   ```

4. **Open question format** — each open question must include:
   ```markdown
   - **[Question]**
     - _Why it matters_: [What this decision affects in the plan — be specific about consequences]
     - _Recommendation_: [The expert's suggested answer, based on their domain expertise]
     - _Confounders_: [Things that could make the recommendation wrong — other constraints, trade-offs, or context the expert doesn't have. "None" if straightforward.]
     - _Source_: [silent | ambiguous | disagreement] — why this is a question, not a decision
   ```

   The recommendation is the expert's *informed opinion*, not a decision. The human still decides. Confounders are critical — they surface the "yeah, but what about..." that the expert can see from their lane but can't resolve without broader context.

5. **Classify each open question's source**:
   - **silent** — the ticket doesn't mention this at all
   - **ambiguous** — the ticket mentions it but could be read multiple ways
   - **disagreement** — experts disagree (note which experts and their positions)

6. **Move to next expert** and repeat

### The "Ask, Don't Assume" Rule

This is the most important rule in this command.

When an expert raises a consideration where:
- The ticket doesn't specify a preference
- No ADR or project convention covers it
- Multiple valid approaches exist

**It MUST become an open question.** Do NOT pick an approach and bake it into a recommendation. Do NOT say "we should probably..." for something the ticket is silent on.

Examples of what becomes an open question:
- "Should errors be surfaced to the user or logged silently?"
  - _Recommendation_: Surface them (Danielle) — users need feedback to self-correct
  - _Confounders_: If this is a background job, surfacing errors may not be possible
- "Should this be a new service or extend the existing one?"
  - _Recommendation_: Extend (Penny Pincher) — new service adds operational cost
  - _Confounders_: Existing service may already be too large; team ownership boundaries
- "Do we need backwards compatibility with the old format?"
  - _Recommendation_: Yes (Tara) — consumers may not upgrade simultaneously
  - _Confounders_: If there's only one consumer and you control it, migration is simpler
- "What's the expected scale — 10 users or 10,000?"
  - _Recommendation_: None — this is pure context the experts don't have
  - _Confounders_: Scale affects every architectural choice downstream

Examples of what does NOT need to be an open question:
- "Use the project's existing error handling pattern" (covered by convention)
- "Follow the ADR on database access" (covered by ADR)
- "Use TypeScript since the project is TypeScript" (obvious from context)

## Step 3.5: Contrarian Carl (Last)

After all other experts, run Carl with access to their contributions:

1. **Compile all prior expert input** into a summary
2. **Load Carl's persona** from `contrarian-carl.yaml`
3. **Carl contributes** with this instruction:
   ```
   You've seen what everyone else wants in the plan.
   Find a requirement, risk, or consideration they ALL missed.
   Question an assumption they all shared.
   You MUST raise at least one thing nobody else mentioned.
   ```
4. **Output in this format**:
   ```markdown
   ### Contrarian Carl's Input
   **What Others Covered**: [Brief summary]
   **What They Missed**: [Requirement or risk nobody raised]
   **Assumption I'm Questioning**: [Something everyone took for granted]
   **Open Question Nobody Asked**:
   - **[Question]**
     - _Why it matters_: [What goes wrong if this isn't addressed]
     - _Recommendation_: [Carl's take — often the contrarian position]
     - _Confounders_: [Why the mainstream assumption might actually be correct]
   ```

Carl's open questions follow the same "Ask, Don't Assume" rule — and his confounders should honestly acknowledge when the consensus might be right.

## Step 4: Checkpoint — Resolve Open Questions

**STOP here and present all open questions to the user.** Do not proceed to synthesis until these are answered.

Collect every open question from all experts (including Carl) and present them grouped by theme:

```markdown
## Open Questions

Before I synthesize the plan, I need your input on these questions.
Each includes the expert's recommendation and what might complicate it.

### [Theme, e.g., "Error Handling"]

**1. [Question]** — raised by [Expert Name]
> **Why it matters**: [What this decision affects — specific consequences]
> **[Expert Name] recommends**: [Their suggestion and reasoning]
> **Confounders**: [Trade-offs or context that could change the answer]

**2. [Question]** — raised by [Expert Name(s)]
> **Why it matters**: [Consequences]
> **[Expert Name] recommends**: [Suggestion] / **[Other Expert] recommends**: [Different suggestion]
> **Confounders**: [What makes this a hard call]

...
```

When experts disagree on the same question, present both recommendations side-by-side so the user can see the tension.

Use `AskUserQuestion` for questions with clear discrete options (2-4 choices). For open-ended questions or when there are more than 4 questions in a theme, present them as markdown and let the user respond in conversation.

**Wait for the user to answer before proceeding.** If the user says "you decide" or "use your judgment" for a specific question, THEN you may make a recommendation — but note it in the plan as "Decision: [choice] (AI-recommended, not specified in ticket)".

## Step 5: Synthesize the Plan

With ticket + expert input + user decisions, write the implementation plan. Structure:

```markdown
# [Plan Title]

## Goal
[What this achieves — from the ticket]

## Decisions Made
[List any questions that were resolved in Step 4, with the chosen answer.
This creates an audit trail of what was decided and by whom.]

## Approach
[High-level strategy — 2-4 sentences]

## Implementation Steps

### Step 1: [Title]
- **What**: [Description]
- **Why**: [Rationale — which expert input or user decision drives this]
- **Files**: [Expected files to touch, if known]

### Step 2: [Title]
...

## Risks and Mitigations
[From expert input — only risks that survived the discussion]

## Testing Strategy
[What to test and how — informed by expert input]

## Out of Scope
[Things explicitly deferred — from Penny Pincher, Business Beth, or user decisions]
```

### Plan Quality Rules

- Every non-obvious decision in the plan traces back to: the ticket, an ADR, a project convention, or a user answer from Step 4
- No "I assumed..." statements — if something was assumed, it should have been asked
- Implementation steps are ordered by dependency, not by expert
- Risks only include things that weren't fully mitigated by the approach

## Step 6: Present for Approval

Call `ExitPlanMode` to present the synthesized plan to the user. They can:
- Approve and proceed to `/track-and-start`
- Request changes (iterate on specific steps)
- Run `/expert-review-plan` for a formal validation pass

## Efficiency Notes

- **Select, don't spray**: 4-6 relevant experts beats running all of them
- **Main thread execution**: Sequential iteration avoids context duplication
- **Checkpoint early**: Resolving ambiguity before synthesis prevents rework
- **Composable**: This command produces a plan → `/expert-review-plan` validates it → `/track-and-start` ships it
- **ADR-aware**: Experts should reference existing ADRs rather than re-debating settled decisions
