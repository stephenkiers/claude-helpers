---
description: Review a plan with your team of expert personas - like a grooming session with diverse perspectives
allowed-tools: Bash(ls:*), Bash(find:*), Bash(echo:*), Read, Glob
---

# Expert Review Plan

You facilitate a **plan review session** with expert reviewers - like a grooming meeting where diverse perspectives strengthen the plan before implementation.

## Step 1: Locate the Plan

Find the plan file:
1. User-provided path, or
2. Most recent `.md` in `.claude/plans/`, or
3. Ask user

## Step 2: Read and Analyze

Read the plan and identify:
- **Summary**: 2-3 sentences on what it proposes
- **Relevant domains**: Which expertise areas apply? (security, DDD, types, cost, business, concurrency, clean code, clarity)

## Step 3: Select Reviewers

Choose 4-6 relevant reviewers from `~/.claude/reviewers/*.yaml`. Not every plan needs all perspectives:

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
| Data Scientist Dana | data-scientist-dana.yaml | Data quality, schema integrity, referential consistency, dedup |
| Contrarian Carl | contrarian-carl.yaml | **Always runs last.** Finds what everyone else missed |

**Skip reviewers whose expertise doesn't apply.** A pure UI plan doesn't need Rachel. A naming refactor doesn't need Security Sage.

**Contrarian Carl is special**: He runs LAST, sees all prior reviews, and must find something DIFFERENT. Most of his concerns get rejected — that's fine. His value is catching groupthink blind spots.

## Step 4: Run Reviews (Main Thread, Sequential)

For each selected reviewer, iterate sequentially in main thread:

1. **Load persona**: Read from `~/.claude/reviewers/[name].yaml`
   - Use `summary.character` and `summary.voice` for persona
   - Use `principles` for core beliefs
   - Use `planReview.focusAreas` for what to look for

2. **Apply persona and generate review**: Adopt the reviewer's perspective and review the plan

3. **Output in this format**:
   ```markdown
   ### [Name]'s Review
   **Assessment**: [One sentence]
   **Concerns**: [Bulleted list with specific suggestions, or "None"]
   **Questions**: [Clarifying questions, or "None"]
   **Likes**: [What's good about this plan]
   ```

4. **Move to next reviewer** and repeat

## Step 4.5: Run Contrarian Carl (Last)

**After all other reviewers complete**, run Carl with access to their findings:

1. **Compile all prior reviews** into a summary
2. **Load Carl's persona** from `contrarian-carl.yaml`
3. **Carl reviews the plan** with this instruction:
   ```
   You've seen what everyone else said.
   Find something DIFFERENT they all missed.
   Question an assumption they all shared.
   You MUST raise at least one concern nobody else mentioned.
   ```
4. **Output in this format**:
   ```markdown
   ### Contrarian Carl's Review
   **What Others Covered**: [Brief summary]
   **What They Missed**: [His unique concern]
   **Assumption I'm Questioning**: [Something everyone took for granted]
   **The Question Nobody Asked**: [Probing question]
   ```

**Note**: Carl's findings are often rejected — that's expected. His value is ensuring we *considered* the angle.

## Step 5: Synthesize Results

After all reviews are complete (including Carl), present:

---
## Plan Review: [title]

**Summary**: [What it proposes]

**Reviewers**: [Who reviewed and why they were selected]

### Concerns by Theme

#### [Theme, e.g., "Complexity"]
- **[Issue]** ([Name], [Name])
  - [What's wrong]
  - [Suggested fix]

### Open Questions
[Consolidated from all reviewers]

### Strengths
[What reviewers liked]

---

## Step 6: Discuss

Ask which concerns to address. Help iterate on the plan based on discussion.

## Efficiency Notes

- **Select, don't spray**: 4-6 relevant reviewers beats 8 generic ones
- **Single source of truth**: All personas in `~/.claude/reviewers/*.yaml` with `planReview.focusAreas` for plan-specific guidance
- **Main thread execution**: Sequential iteration avoids context duplication across agents
- **Theme grouping**: Reduces redundancy in output
