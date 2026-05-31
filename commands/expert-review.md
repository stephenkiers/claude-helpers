---
description: Smart expert code review with triage - works across all projects
argument-hint: [reviewers...] [--full] [--all] [--force]
allowed-tools: Bash(git diff:*), Bash(git branch:*), Bash(git log:*), Bash(git rev-parse:*), Bash(git show:*), Bash(git status:*), Bash(git -C:*), Bash(mkdir:*), Bash(rm:*), Bash(echo:*), Bash(gh:*), Bash(ls:*), Bash(BRANCH=:*), Bash(HASH=:*), Bash(REVIEW_DIR=:*), Read, Glob, Grep, Task, Write
model: opus
---

# Expert Code Review

A checkpoint-based code review system that:
1. **Summarizer** analyzes the diff → saves to `/tmp/code-review/{branch}-{hash}/summary.md`
2. **Tagger** (haiku) routes diff sections to reviewers → saves to `.../{hash}/tagged-sections.md`
3. **Consistency Checker** (haiku) scans for pattern inconsistencies → saves to `.../{hash}/consistency-checker-pass1.md`
4. **Pass 1 reviewers** (main thread, sequential, ALL reviewers) → each saves to `.../{reviewer}-pass1.md` — includes Open Questions and Proposals
5. **Code Rot Cody** (haiku) greps entire repo to verify new symbols have callers and removed symbols are cleaned up → saves to `.../{hash}/code-rot-cody-pass1.md`
6. **Pass 2 re-evaluation** (main thread, sequential) → saves to `.../{reviewer}-pass2.md`
7. **Haiku Q&A** (haiku, per reviewer) → answers each reviewer's open questions → `.../{reviewer}-questions-answered.md`
8. **Expert Cross-Review** (main thread, DEEP-DIVE reviewers only) → each expert reviews all others' findings → `.../{reviewer}-cross-review.md`
9. **Verify** all expected files exist
10. **Amalgamate** from checkpoint files
11. **Cache review metadata** to `.claude/github-cache.json`

**Why checkpoints?** If any agent fails, work from other agents is preserved. Each step produces inspectable artifacts.

**Why subfolders?** Each review gets its own folder (`{branch}-{short_hash}/`), enabling comparison between reviews and avoiding conflicts.

## Arguments

- `$1...`: Reviewer selection (default: all available reviewers)
  - No args: Run all discovered reviewers
  - `--all`: Explicitly run all reviewers
  - Comma-separated names: `contracts,concurrency,callback-safety`
  - Single name: `contracts`

- `--full`: Review entire codebase instead of just changed files

- `--force` (alias `-y`): Skip the re-run confirmation prompt if a prior review exists for this branch in `.claude/github-cache.json`. Without this flag, the command pauses and asks before overwriting prior results.

## File Checkpoint Locations

All artifacts saved to `/tmp/code-review/{branch}-{short_hash}/`:
- `summary.md` - Summarizer output (Technical Summary + Business Context)
- `tagged-sections.md` - Tagger output (section → reviewer routing)
- `consistency-checker-pass1.md` - Consistency Checker output (pattern inconsistencies + PR desc cross-ref)
- `{reviewer}-pass1.md` - Pass 1 blind review output (only for non-skipped reviewers)
- `code-rot-cody-pass1.md` - Code Rot Cody output (dead code, orphaned refs, unused config)
- `{reviewer}-pass2.md` - Pass 2 re-evaluation output (only if findings exist)

Example: `/tmp/code-review/feature-foo-abc1234/`

---

## Instructions

### Step 0: Setup

1. Get branch name and commit hash:
   ```bash
   BRANCH=$(git rev-parse --abbrev-ref HEAD | tr '/' '-')
   HASH=$(git rev-parse --short HEAD)
   REVIEW_DIR="/tmp/code-review/${BRANCH}-${HASH}"
   ```

2. Create checkpoint directory:
   ```bash
   mkdir -p "$REVIEW_DIR"
   ```

3. Determine current working directory and git root

4. **Read `.claude/project.yaml`** (if it exists in the project root):
   - Use the `Read` tool on `.claude/project.yaml`
   - Store as `PROJECT_CONTEXT` — pass it to all reviewer prompts as context
   - Extract `techStack.language` → use as primary language (skips file-extension detection below)
   - Extract `techStack.framework`, `techStack.testing`, `techStack.platform`
   - Extract `fragility.highRiskModules`, `fragility.knownFragilePatterns` → pass to Fragile Feynman
   - Extract `docStyle` → pass to Contract Chris
   - Extract `typeChecker`, `propertyTestingLib` → pass to Tara TypeSafe
   - Extract `adrs`, `invariants`, `redLines`, `terminology` → pass to all reviewers
   - If `project.yaml` not found, fall through to file-extension detection below

5. **Detect project type** (skip if `techStack.language` was set in step 4):
   - Check for `Cargo.toml` → Rust → `DETECTED_LANGUAGES=["rust"]`
   - Check for `package.json` → Node/TypeScript → `DETECTED_LANGUAGES=["typescript"]`
   - Otherwise check file extensions in changed files — majority extension wins:
     - `*.go` → `DETECTED_LANGUAGES=["go"]`
     - `*.rb` → `DETECTED_LANGUAGES=["ruby"]`
     - `*.py` → `DETECTED_LANGUAGES=["python"]`
   - A diff can have multiple languages (e.g. Go + Ruby); collect all that appear

6. Identify project path for reviewer discovery

7. **Detect project modifiers** from CLAUDE.md or `.claude/review-config.md`:
   - Look for `## Review Modifiers` or `## Project Modifiers` section
   - Common modifiers:
     - `greenfield: true` - Pre-release project, backwards compatibility not required
     - `internal: true` - Internal tool, less strict API stability requirements
   - If CLAUDE.md contains phrases like "pre-release", "greenfield", "not yet released", or "backwards compatibility is not a concern", treat as `greenfield: true`
   - Store detected modifiers for use in reviewer context

8. **Store the REVIEW_DIR path** - you'll use it for all file operations

8. **Gather plan/ticket context and review history (cache-first):**
   - **First:** Use the `Read` tool on `.claude/github-cache.json` in the current worktree
     - Parse the JSON content directly (no shell commands needed)
     - If `issue.body` exists → use it as the plan/business context (most reliable source)
     - If `issue.title` exists → include in summarizer prompt
     - If `issue.url` exists → reference in report
     - **Check for prior review:** If `review.lastRun` exists AND `review.branch` matches the current `BRANCH`, **STOP and confirm with the user before proceeding** (unless `--force` / `-y` was passed). Report:
       ```
       ℹ️ Previous review found on this branch:
         Last run: {review.lastRun}
         Commit: {review.commit}{" (current)" if matches HASH else " (older — current is {HASH})"}
         Reviewers: {review.reviewers} (joined)
         Findings: {review.findings.critical}C / {review.findings.high}H / {review.findings.medium}M / {review.findings.low}L
         Checkpoint: {review.reviewDir}
       ```
       Then ask:
       - If `review.commit` matches current `HASH`: "This exact commit was already reviewed. Re-run anyway? (will overwrite prior results)"
       - If `review.commit` differs: "Re-run review for the current commit? (prior results in `{review.reviewDir}` will be preserved; new results go to a different folder)"
       Wait for explicit user confirmation ("yes" / "go" / "proceed") before continuing to Step 1. If the user declines, exit cleanly without running any reviewers.
       If `--force` or `-y` is in the arguments, skip the confirmation and proceed directly (still print the "Previous review found" block for visibility).
   - **Fallback (only if no worktree cache):** Search `~/.claude/plans/*.md` files (read each to see if it mentions this branch or project)
   - Also check for kanban files like `*-kanban.md`, `*-KANBAN.md` in project root or docs/
   - Store relevant context for use in summarizer and Sam System

9. **If plan context found (from cache or files):**
   - Include relevant excerpts in the summarizer prompt (Step 4)
   - Pass them to Sam System as "Known Integration Concerns" (Step 5.5)
   - Cross-reference in final report (Step 8)

### Step 1: Determine Review Scope

1. Check if `--full` is in the arguments
2. If NOT `--full` (delta review):
   - Run: `git diff --name-only main...HEAD` to get changed files
   - If no files changed, inform user and exit
   - Store the file list
3. If `--full`:
   - Review will cover entire `src/` directory

### Step 2: Discover Available Reviewers

1. **Resolve home directory** (tilde doesn't expand in Glob tool):
   ```bash
   echo $HOME
   ```
   Store as `HOME_DIR` for use in paths below.

2. **Load reviewer index** (lightweight meta — used for tagger routing):
   ```
   Read: {HOME_DIR}/.claude/reviewers/index.yaml
   ```
   The index contains `name`, `priority`, `triggers`, `useWhen`, and `note` for every reviewer.
   **Use this for the tagger (Step 4.5)** — no need to load heavy YAML files just for routing.

3. **Load generic reviewers** (full YAML — used for selected reviewers only):
   ```
   Glob: {HOME_DIR}/.claude/reviewers/*.yaml  (excluding index.yaml)
   ```
   Read each selected reviewer's full YAML lazily — only load what the tagger routes to.

4. **Load project-specific reviewer overrides** (if project has them):
   ```
   Glob: {project-root}/.claude/reviewers/*-local.yaml
   ```
   Local overrides extend global reviewers with project-specific checks (see README.md).

5. **Merge**: Project local overrides augment (not replace) the global reviewer of the same base name.

6. **For tagger routing**: Pass only the index entries (name + triggers) — not the full prompts.
   **For review execution**: Load the full YAML for each selected reviewer on demand.

### Step 3: Parse Reviewer Selection

If specific reviewers requested:
- Parse comma-separated or space-separated list
- Match against discovered reviewer names (case-insensitive)
- Error if any requested reviewer not found

If no specific reviewers (or `--all`):
- Use all discovered reviewers

### Step 4: Run Summarizer Agent → Save to File

**Spawn a single summarizer agent.**

Create a Task with `subagent_type: "Explore"` using the summarizer prompt:
@~/.claude/prompts/summarizer.md

Provide to summarizer:
- Git diff output (`git diff main...HEAD` for delta, or description for full)
- List of changed files with categories
- Commit messages (`git log main...HEAD --format="%s%n%n%b"`)
- PR description if available
- Known issues index if project has one

**Wait for summarizer to complete.**

**Save output to `{REVIEW_DIR}/summary.md`** using the Write tool.

The summary file contains:
- **Document A**: Technical Summary (what changed) - under `## Technical Summary`
- **Document B**: Business Context (why it changed) - under `## Business Context`
- **Suggested reviewers** - under `## Suggested Reviewers`

### Step 4.5: Run Tagger Agent → Save to File

**Spawn a tagger agent to route diff sections to reviewers.**

Create a Task with:
- `subagent_type: "general-purpose"`
- `model: "haiku"`

Using the tagger prompt:
@~/.claude/prompts/tagger.md

Provide to tagger:
- Git diff output (`git diff main...HEAD`)
- All reviewer names with their triggers (parsed from reviewer `.yaml` files in Step 2)

**Wait for tagger to complete.**

**Save output to `{REVIEW_DIR}/tagged-sections.md`** using the Write tool.

The tagged sections file contains:
- Sections mapped to each reviewer with line ranges and trigger matches
- A SKIP section listing reviewers with no matching triggers

### Step 4.7: Run Consistency Checker (haiku) → Save to File

**Spawn a Consistency Checker agent to find pattern inconsistencies and cross-reference the PR description.**

This is a mechanical pattern-matching pass, not a domain review. It catches:
- Mixed error types for the same semantic purpose (e.g., `Error` vs `DOMException` for abort)
- Inconsistent cleanup patterns (inline vs helper function)
- PR description claims that contradict the code

Create a Task with:
- `subagent_type: "general-purpose"`
- `model: "haiku"`

Provide to the Consistency Checker:
- The full diff (`git diff main...HEAD`)
- The PR description (from `.claude/github-cache.json` issue body, or `gh pr view --json body`)
- The Consistency Checker's `codeReview.prompt` from `consistency-checker.yaml`

**Wait for completion.**

**Save output to `{REVIEW_DIR}/consistency-checker-pass1.md`** using the Write tool.

**Important**: Consistency Checker findings are included in the final report like any other reviewer. They participate in Pass 2 re-evaluation if they have findings. Unlike Contrarian Carl, their findings ARE subject to business context re-evaluation since a pattern inconsistency might be intentional.

### Step 5: Run Pass 1 Reviews (Main Thread, Sequential) → Save to Files

**First, read the tagged sections:**
1. Read `{REVIEW_DIR}/tagged-sections.md`
2. Parse which reviewers have sections assigned vs. which are in SKIP

**For reviewers in SKIP section:**
- Do NOT skip them entirely — run them with the **full diff** at `QUICK-SCAN` priority
- They may still self-SKIP if they decide the changes are truly outside their domain, but they must explicitly decide
- Note in final report: "{reviewer} ran on full diff (no tagger match)" vs "{reviewer} self-SKIPped after reviewing full diff"

**For all reviewers, iterate sequentially in main thread:**

For each reviewer with tagged sections:

1. **Load context**:
   - Read the expert framework: @~/.claude/prompts/expert-framework.md
   - Read the reviewer's `codeReview.prompt` from their `.yaml` file
   - If reviewer has tagged sections: extract ONLY those sections from `tagged-sections.md`
   - If reviewer had no tagger match (was in SKIP section): provide the **full diff** and set initial priority to `QUICK-SCAN`
   - Include any project modifiers detected in Step 0.6
   - **Language extensions**: If the reviewer YAML has a `languageExtensions` field, check each key against `DETECTED_LANGUAGES`. For any match, append to the prompt:
     ```
     ## Language-Specific Checks ({language})
     - [each entry verbatim as a bullet]
     ```
     Only append for languages that appear in the diff — unmatched languages are skipped entirely (zero token cost).

2. **Apply the reviewer persona** and generate Pass 1 review:
   - Adopt the reviewer's perspective from their prompt
   - Review tagged sections (or full diff if no tagger match)
   - You may read additional files if the code references them and you see risk indicators
   - For delta review, remember: ONLY report issues INTRODUCED or WORSENED by this PR

3. **Output in this format**:
   ```markdown
   # Pass 1 Review: {Reviewer Name}

   ## Decision
   [SKIP | QUICK-SCAN | DEEP-DIVE]

   ## Reason
   [Brief explanation of triage decision]

   ## Files Examined
   - [file1]
   - [file2]

   ## Findings

   ### [SEVERITY] Finding Title
   - **File**: path/to/file:123
   - **Issue**: Description
   - **Impact**: What could go wrong
   - **Recommendation**: How to fix

   [repeat for each finding, or "No findings" if none]

   ## Summary
   - Critical: N
   - High: N
   - Medium: N
   - Low: N
   ```

4. **Save to checkpoint file**: Write output to `{REVIEW_DIR}/{reviewer}-pass1.md`

5. **Move to next reviewer** and repeat

**Important**: Do NOT include Business Context yet - this is blind review (WHAT changed, not WHY).

For delta review, apply this scope rule to each review:
```
SCOPE: STRICT DELTA REVIEW - Only report issues INTRODUCED or WORSENED by this PR.

RULES:
1. ONLY report issues in the changed lines or new code
2. Do NOT report pre-existing issues in unchanged code
3. If you see an existing issue that the PR makes worse, report it
4. If you see an existing issue that the PR doesn't touch, SKIP IT
```

**If project modifiers are present, include them in reviewer context:**
```
PROJECT MODIFIERS:
- greenfield: [true/false] - If true, backwards compatibility is NOT a concern.
  Do NOT flag: breaking API changes, removed exports, renamed functions,
  changed signatures, or any "breaking change" issues.
- internal: [true/false] - If true, relaxed API stability requirements.
```

### Step 5.5: Run Sam System Integration Review (Main Thread) → Save to File

**Sam System is a special reviewer that examines cross-file composition and data flow.**

Unlike other reviewers who receive only their tagged sections, Sam System receives:
1. The full Technical Summary from `{REVIEW_DIR}/summary.md`
2. The **full diff** (not just tagged sections) - they need to trace data across files
3. Any plan context files found in Step 0.8 as "Known Integration Concerns"

**Process:**

1. **Check if Sam System is in the reviewer list**
   - Sam System is `critical` priority, so included by default
   - Can be explicitly requested: `/expert-review sam-system`
   - Can be excluded by specifying other reviewers: `/expert-review tara-typesafe,uncle-bob`

2. **Load Sam System context**:
   - Read the expert framework: @~/.claude/prompts/expert-framework.md
   - Read Sam System's `codeReview.prompt` from `sam-system.yaml`
   - Include the full diff (NOT just tagged sections)
   - Include any plan context files as "Known Integration Concerns"

3. **Critical instruction for Sam System**:
   ```
   You MUST trace data flow across files. If you see:
   - A factory passing parameters to another factory
   - An event bus being created and passed
   - Config objects flowing through multiple layers

   You MUST read the related files to verify:
   - Parameters passed are actually destructured and used
   - Event buses are connected (emitters have subscribers)
   - Config options are respected, not ignored
   ```

4. **Sam System reviews composition**:
   - Focus on factory composition (what gets passed vs used)
   - Trace event bus wiring (emitters connected to subscribers?)
   - Check config parameter usage (all passed options respected?)
   - Look for the critical pattern: "Parameter passed but ignored"

5. **Output format** (same as other reviewers):
   ```markdown
   # Pass 1 Review: Sam System

   ## Decision
   [DEEP-DIVE - Sam System always does integration analysis]

   ## Reason
   [Brief explanation of integration concerns found or not found]

   ## Files Examined
   - [file1] - reason for examining
   - [file2] - reason for examining

   ## Findings

   ### [SEVERITY] Integration Issue: [Title]
   - **Flow**: A → B → C (trace the data path)
   - **File A** (`path/to/file.ts:line`): What it passes/expects
   - **File B** (`path/to/file.ts:line`): What it actually does
   - **Expected**: What should happen
   - **Actual**: What the code does
   - **Impact**: What breaks as a result
   - **Recommendation**: How to fix

   [repeat for each finding, or "No integration concerns found" if none]

   ## Summary
   - Critical: N
   - High: N
   - Medium: N
   - Low: N
   ```

6. **Save to checkpoint file**: Write output to `{REVIEW_DIR}/sam-system-pass1.md`

**Note**: Sam System participates in Pass 2 re-evaluation like other reviewers if they have findings.

### Step 5.6: Run Code Rot Cody (Main Thread) → Save to File

**Code Rot Cody is a mechanical dead-code and orphan detector. He greps the entire repo to verify every new symbol is called and every removed symbol is fully cleaned up.**

Unlike domain reviewers who evaluate design and correctness, Cody does one thing: "Find All References" on every symbol the PR introduces or removes. He catches what no domain reviewer looks for — functions defined but never called, config fields stored but never read, removed APIs with lingering references.

**Process:**

1. **Check if Code Rot Cody is in the reviewer list**
   - Cody is `high` priority, so included by default
   - Can be explicitly requested: `/expert-review code-rot-cody`
   - Can be excluded by specifying other reviewers: `/expert-review uncle-bob,rachel`

2. **Load Cody's context**:
   - Read Cody's `codeReview.prompt` from `code-rot-cody.yaml`
   - Include the full diff
   - **Language extensions**: If `DETECTED_LANGUAGES` includes a language Cody has extensions for (go, rust, typescript), append those language-specific checks
   - Include the list of changed files

3. **Cody's critical instruction**:
   ```
   You MUST use grep to verify every claim. Do NOT guess whether something has callers.

   For each new symbol in the diff:
   1. Grep the ENTIRE repo for usage (exclude the definition site)
   2. Note whether callers are in production code, test code, or both
   3. Flag zero-caller symbols as DEAD CODE

   For each removed symbol:
   1. Grep the ENTIRE repo for lingering references
   2. Flag any remaining references as ORPHANED

   For each new config field:
   1. Verify it is stored, read, validated, and documented
   2. Flag stored-but-never-read as DEAD CONFIG
   ```

4. **Output format**:
   ```markdown
   # Code Rot Report

   ## Symbol Inventory
   | Symbol | File | Type | Status |
   |--------|------|------|--------|
   | `FuncName` | path:line | New function | [CONNECTED / DEAD / TEST-ONLY] |

   ## Findings

   ### [SEVERITY] [Finding Type]: [Symbol Name]
   - **Defined at**: path/to/file:line
   - **Expected callers**: [where you'd expect it to be used]
   - **Actual callers**: [what grep found, or "none"]
   - **Evidence**: [grep command and result summary]
   - **Recommendation**: [remove it / add caller / make private]

   ## Clean Symbols (no issues)
   - `symbol1` — N callers found across M files
   - `symbol2` — used in production and test code

   ## Summary
   - Dead code: N
   - Orphaned references: N
   - Dead config: N
   - Unused parameters: N
   - Clean symbols: N
   ```

5. **Save to checkpoint file**: Write output to `{REVIEW_DIR}/code-rot-cody-pass1.md`

**Note**: Cody participates in Pass 2 re-evaluation if he has findings (dead code might be intentionally staged for a follow-up PR). His findings are also visible to Contrarian Carl.

### Step 5.7: Run Contrarian Carl (Main Thread, Last) → Save to File

**Contrarian Carl runs LAST and receives ALL prior findings. His job is to find what everyone else missed.**

Unlike other reviewers who do blind review, Carl explicitly sees what others found and must find something DIFFERENT.

**Process:**

1. **Check if Contrarian Carl is in the reviewer list**
   - Carl is `low` priority, so runs last by default
   - Can be explicitly excluded: `/expert-review uncle-bob,rachel` (doesn't include Carl)
   - Can be explicitly requested: `/expert-review contrarian-carl`

2. **Gather all prior findings**:
   - Read ALL `{REVIEW_DIR}/*-pass1.md` files
   - Compile a summary of what each reviewer found
   - Note which areas/files each reviewer examined

3. **Load Carl's context**:
   - Read the expert framework: @~/.claude/prompts/expert-framework.md
   - Read Carl's `codeReview.prompt` from `contrarian-carl.yaml`
   - Include the full diff
   - Include the compiled summary of all prior findings

4. **Carl's special instruction**:
   ```
   You have access to what EVERY other reviewer found.
   Your job is to find something DIFFERENT.

   DO NOT repeat any finding already raised.
   DO look where others didn't look.
   DO question assumptions everyone shared.

   You MUST raise at least one concern nobody else mentioned.
   It's okay if your concern gets rejected later — that's expected.
   Your value is ensuring we CONSIDERED the angle.
   ```

5. **Output format**:
   ```markdown
   # Contrarian Review: Carl

   ## What Others Covered
   [Summary of themes from prior reviewers]

   ## What Everyone Missed

   ### [SEVERITY] [Issue Title]
   - **The gap**: What wasn't examined
   - **My concern**: What could go wrong
   - **Confidence**: Low/Medium/High
   - **Verification**: How to prove/disprove

   ## Shared Assumptions I'm Questioning
   - [Assumption that might be wrong]

   ## The Question Nobody Asked
   - [Probing question]

   ## Verdict
   [BLOCKING / WORTH DISCUSSING / PROBABLY FINE BUT...]
   ```

6. **Save to checkpoint file**: Write output to `{REVIEW_DIR}/contrarian-carl-pass1.md`

**Note**: Carl does NOT participate in Pass 2 re-evaluation. His findings are presented as-is for the team to accept or reject. His value is raising the question, not defending the answer.

### Step 6: Run Pass 2 Re-evaluations (Main Thread, Sequential) → Save to Files

**Only for reviewers WITH findings from Pass 1.**

For each reviewer that has findings in their `-pass1.md` file, iterate sequentially in main thread:

1. **Load context**:
   - Read `{REVIEW_DIR}/{reviewer}-pass1.md` (their Pass 1 findings)
   - Read the Business Context section from `{REVIEW_DIR}/summary.md`
   - Read the pass2-reevaluation prompt: @~/.claude/prompts/pass2-reevaluation.md

2. **Re-evaluate findings**:
   - Consider the Business Context (WHY the changes were made)
   - For each finding from Pass 1, determine if it's still valid given the intent
   - You may read referenced files if needed to resolve uncertainty

3. **Output in this format**:
   ```markdown
   # Pass 2 Re-evaluation: {Reviewer Name}

   ## Business Context Received
   [Summary of the business context]

   ## Additional Context Gathered
   [If any files were read to resolve uncertainty, list them. Otherwise: "None needed"]

   ## Re-evaluated Findings

   ### Finding: [Original Title]
   - **Original Severity**: [CRITICAL/HIGH/MEDIUM/LOW]
   - **Re-evaluation**: [CONFIRMED | RESOLVED | DOWNGRADED]
   - **Reason**: [Why this assessment changed or stayed the same]
   - **Final Severity**: [Same or new severity if downgraded]

   [repeat for each finding]

   ## Summary
   - CONFIRMED: N
   - RESOLVED: N
   - DOWNGRADED: N
   ```

4. **Save to checkpoint file**: Write output to `{REVIEW_DIR}/{reviewer}-pass2.md`

5. **Move to next reviewer** and repeat

### Step 6.5: Haiku Question Answering → Save to Files

**For each reviewer whose `pass1.md` contains Open Questions (not "None"):**

1. Extract all questions from `{REVIEW_DIR}/{reviewer}-pass1.md` under `## Open Questions`
2. Create a Task with:
   - `subagent_type: "general-purpose"`
   - `model: "haiku"`
3. Provide to the Haiku agent:
   - The reviewer's name and role summary (from their `.yaml`)
   - The open questions verbatim (including any file hints)
   - The reviewer's tagged sections (or full diff if they had no tagger match)
   - Instruction: "Read the files indicated by file hints, plus any other files needed to answer these questions concretely. For each question, provide a definitive answer. If a question cannot be determined from static code analysis, say so clearly and explain what runtime evidence would be needed."
4. Haiku may use Read/Grep tools to investigate.
5. **Save output to `{REVIEW_DIR}/{reviewer}-questions-answered.md`**

**Output format:**
```markdown
# Questions Answered: {Reviewer Name}

## Q: {Question verbatim}
- **Answer**: [concrete answer, or "Cannot determine from static analysis — needs runtime verification"]
- **Evidence**: path/to/file:line — [what was read to reach this answer]

[repeat per question]
```

Run all Haiku Q&A agents sequentially in main thread (cheap; keep it simple).

### Step 6.7: Expert Cross-Review → Save to Files

**For each reviewer who returned `DEEP-DIVE` in their `pass1.md`:**

1. **Gather all other reviewers' findings:**
   - Read all `{REVIEW_DIR}/*-pass1.md` files **except** this reviewer's own
   - Read all `{REVIEW_DIR}/*-pass2.md` files for re-evaluation context

2. **Run the reviewer in main thread** with:
   - Their full persona from their `.yaml`
   - The full diff
   - Compiled findings from all other reviewers
   - This cross-review instruction:
     ```
     You have now seen what every other expert found. From your domain ({domain}):
     - AGREE with or CHALLENGE each finding that intersects your domain
     - Identify SYNERGIES: findings that compound each other
     - Identify GAPS: angles no reviewer covered from your domain perspective
     Keep it focused — skip findings that don't touch your domain at all.
     ```

3. **Save to `{REVIEW_DIR}/{reviewer}-cross-review.md`**

**Output format:**
```markdown
# Cross-Review: {Reviewer Name}

## Agreements
- [{Other Reviewer} / {Finding Title}]: [1-sentence agreement + any reinforcement]

## Challenges
- [{Other Reviewer} / {Finding Title}]: [1-sentence challenge with reasoning]

## Synergies
- [{Finding A} + {Finding B}]: [Why these compound and what the combined impact means]

## Gaps Nobody Covered (from my domain)
- [What angle was missed from this reviewer's domain perspective]
```

Only DEEP-DIVE reviewers run cross-review (reviewers who self-SKIPped or did QUICK-SCAN with no findings add little value here).

Run sequentially in main thread.

### Step 7: Verify Checkpoint Files

Before amalgamation, verify all expected files exist:

1. Check `{REVIEW_DIR}/summary.md` exists
2. For each selected reviewer: check `{REVIEW_DIR}/{reviewer}-pass1.md` exists
3. If Code Rot Cody was in the reviewer list: check `{REVIEW_DIR}/code-rot-cody-pass1.md` exists
4. For each reviewer with findings: check `{REVIEW_DIR}/{reviewer}-pass2.md` exists
5. For each reviewer with Open Questions: check `{REVIEW_DIR}/{reviewer}-questions-answered.md` exists
6. For each DEEP-DIVE reviewer: check `{REVIEW_DIR}/{reviewer}-cross-review.md` exists
7. After Step 8.5: check `{REVIEW_DIR}/final-report.md` exists

If any are missing, report which agents failed and suggest retry.

### Step 8: Amalgamate Results from Files

1. Read all `{REVIEW_DIR}/*-pass1.md` files
2. Read all `{REVIEW_DIR}/*-pass2.md` files (for reviewers with findings)
3. Read all `{REVIEW_DIR}/*-questions-answered.md` files
4. Read all `{REVIEW_DIR}/*-cross-review.md` files
5. Collect all CONFIRMED findings (from Pass 2) and all findings from reviewers who SKIPPED Pass 2 (no findings = no re-evaluation needed)
6. Group by severity (Critical > High > Medium > Low)
7. De-duplicate overlapping findings
8. Cross-reference against known issues (if project has them)
9. Flag matches as "Known: #XXX"
10. Note which reviewers SKIPPED (and why) or found no issues
11. Include answered questions and cross-review commentary in the final report

### Step 8.5: Write consolidated `final-report.md` to checkpoint dir

The amalgamated report from Step 8 must be saved as a single file so the user can re-open it without re-running the review or opening every per-reviewer checkpoint file.

1. **Write to `{REVIEW_DIR}/final-report.md`** using the Write tool. Use the same template as the "Output Format" section below, but with all findings expanded with full detail (not the truncated in-conversation version).

2. **Include in the file:**
   - Executive summary (counts, verdict)
   - Every Medium/High/Critical finding with: what, where (file:line), why it matters, fix options (concrete choices, not just "do something"), recommendation
   - Every Low finding grouped by theme (documentation, test ergonomics, code style, defensive/observability, out-of-scope), each with the same structure as Mediums but more concise
   - Open Questions Resolved (from `questions-answered.md`)
   - Cross-Review consensus themes (from `*-cross-review.md` files)
   - Sign-off checklist at the end so the user can mark which findings to act on

3. **Do NOT write the report to the worktree root** — it pollutes `git status`. Keep it in `/tmp/code-review/{branch}-{hash}/` where the rest of the checkpoints live.

4. **The in-conversation response (the assistant message after running this command)** must be much shorter than the file:
   - Brief actionable summary (counts + the 1–3 most important findings)
   - Top 3–5 recommended next actions
   - End the response with: `📄 Full report: /tmp/code-review/{branch}-{hash}/final-report.md`

   Do NOT inline the full report in the response — the user can `cat` or open the file if they want detail. The link is the contract.

### Step 9: Cache Review Metadata

After amalgamation, write review metadata to `.claude/github-cache.json` so `/shipit` and future `/expert-review` runs know a review was performed.

1. **Read** the existing `.claude/github-cache.json` (may already have `issue`, `pr`, `branch` sections)
2. **Merge** a `review` section into it, preserving all other sections:
   ```json
   {
     "review": {
       "lastRun": "2024-01-15T10:30:00Z",
       "commit": "abc1234",
       "branch": "feature-foo",
       "reviewDir": "/tmp/code-review/feature-foo-abc1234",
       "reviewers": ["uncle-bob", "security-sage", "tara-typesafe"],
       "scope": "delta",
       "findings": {
         "critical": 0,
         "high": 1,
         "medium": 2,
         "low": 0
       }
     }
   }
   ```
3. **Write** the merged JSON back:
   ```bash
   EXISTING=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
   echo "$EXISTING" | jq --argjson review "$REVIEW_JSON" '. + {review: $review}' > .claude/github-cache.json
   ```
   Where `$REVIEW_JSON` is constructed from the review's actual values:
   - `lastRun`: current ISO 8601 timestamp
   - `commit`: the `HASH` from Step 0
   - `branch`: the `BRANCH` from Step 0
   - `reviewDir`: the `REVIEW_DIR` path
   - `reviewers`: array of reviewer names that actually ran (not self-skipped)
   - `scope`: `"delta"` or `"full"` based on `--full` flag
   - `findings`: severity counts from the amalgamated results

**Important**: Use `jq` merge (`. + {review: $review}`) to PRESERVE existing `issue`, `pr`, and `branch` sections.

---

## Output Format

There are **two** outputs from this command:

1. **`{REVIEW_DIR}/final-report.md`** — written in Step 8.5 — contains the full report (template below).
2. **The in-conversation assistant message** — must be SHORT: actionable summary (counts, top 3 findings, top 3–5 recommended actions) followed by a closing line `📄 Full report: {REVIEW_DIR}/final-report.md`.

Do NOT inline the full template below in the assistant message. The user will open the file if they want detail.

### Template for `final-report.md`

```markdown
# Code Review Report

**Date**: YYYY-MM-DD
**Branch**: [current branch name]
**Commit**: [short hash]
**Project**: [detected project name]
**Scope**: Delta from main | Full codebase
**Files Reviewed**: N files
**Checkpoint Directory**: /tmp/code-review/{branch}-{hash}/

## Executive Summary

- **Reviewers Run**: N (list names)
- **Reviewers Skipped**: N (list names with reasons)
- **Total Findings**: N
  - Critical: N
  - High: N
  - Medium: N
  - Low: N
- **Context Re-evaluation**:
  - CONFIRMED: N
  - RESOLVED: N
  - DOWNGRADED: N

## Technical Summary

[Include the summarizer's Technical Summary - what changed]

## Findings by Severity

### Critical

#### [Finding Title]
- **Reviewer**: [name]
- **File**: path/to/file:123
- **Issue**: Description
- **Impact**: What could go wrong
- **Recommendation**: How to fix
- **Context Re-evaluation**: CONFIRMED | RESOLVED | DOWNGRADED
- **Re-evaluation Notes**: [if changed after seeing business context]
- **Known Issue**: #NNN (if matches)

[repeat for each finding]

### High
[same format]

### Medium
[same format]

### Low
[same format]

## Reviewer Summary

| Reviewer | Decision | Findings | Re-evaluated | Cross-Review | Notes |
|----------|----------|----------|--------------|--------------|-------|
| Contracts | DEEP-DIVE | 2 | 2 CONFIRMED | Yes | Found invariant violations |
| Concurrency | FULL-DIFF-QUICKSCAN | 0 | - | No | No triggers matched, self-cleared after full diff |
| Security-Input | QUICK-SCAN | 0 | - | No | Checked tagged sections, no concerns |
| Resource-Cleanup | SELF-SKIP | 0 | - | No | Reviewer chose to skip after reviewing full diff |
| Code Rot Cody | CODE-ROT | 1 | 1 CONFIRMED | No | Found dead code: `unusedFunc()` |
| Contrarian Carl | CONTRARIAN | 1 | N/A | No | Found observability gap |

**Decision legend**:
- `FULL-DIFF-QUICKSCAN`: No tagger triggers matched; reviewer ran on full diff at QUICK-SCAN priority
- `SELF-SKIP`: Reviewer ran on full diff and explicitly decided to skip (no relevant changes)
- `QUICK-SCAN`: Reviewer did a quick review of tagged sections
- `DEEP-DIVE`: Reviewer did thorough investigation
- `CODE-ROT`: Mechanical grep-based verification of symbol connectivity (dead code, orphans, unused config)
- `CONTRARIAN`: Ran last with all prior findings, found something different (no re-eval)

## Answered Questions

| Reviewer | Question | Answer |
|----------|----------|--------|
| [name] | [question text] | [answer or "needs runtime verification"] |

(omit table if no reviewers raised open questions)

## Cross-Review Commentary

### {Reviewer A} — Cross-Domain Observations
**Agreements**: ...
**Challenges**: ...
**Synergies**: ...
**Gaps**: ...

[repeat for each DEEP-DIVE reviewer with cross-review content]

(omit section if no reviewers did DEEP-DIVE)

## Checkpoint Files

All review artifacts saved to `{REVIEW_DIR}/`:
- `summary.md` - Summarizer output (Technical Summary + Business Context)
- `tagged-sections.md` - Tagger output (section → reviewer routing)
- `consistency-checker-pass1.md` - Consistency Checker output (pattern + PR desc cross-ref)
- `{reviewer}-pass1.md` - Pass 1 outputs (all reviewers run; tagger-unmatched get full diff)
- `code-rot-cody-pass1.md` - Cody's dead code / orphan report (grep-verified, participates in pass2)
- `{reviewer}-pass2.md` - Pass 2 re-evaluations (only for reviewers with findings)
- `contrarian-carl-pass1.md` - Carl's contrarian findings (no pass2, presented as-is)
- `{reviewer}-questions-answered.md` - Haiku's answers to each reviewer's open questions
- `{reviewer}-cross-review.md` - DEEP-DIVE reviewer cross-domain commentary on all other findings

## Recommended Next Steps

1. [Prioritized action items based on CONFIRMED findings]
2. ...

## Sign-off Checklist

| Item | Severity | Recommendation | Decision |
|------|----------|---------------|----------|
| [Finding 1 title] | M / L | [my recommendation] | ☐ |
| ... | ... | ... | ... |
```

### Template for the in-conversation assistant message

```
{Brief one-paragraph verdict: ship-blocking? polish-only? regressions vs prior?}

**Findings**: 0 Critical, 0 High, N Medium, M Low.

**Top items requiring attention**:
1. [most important Medium/High] — file:line — one-sentence what
2. [next] — file:line — one sentence
3. [next, optional] — file:line — one sentence

**Top recommended actions** (in priority order):
1. ...
2. ...
3. ...

📄 Full report: /tmp/code-review/{branch}-{hash}/final-report.md
```

---

## Example Usage

```bash
# Review changes from main with all available reviewers
/expert-review

# Review with specific reviewers only
/expert-review contracts,concurrency

# Review entire codebase
/expert-review --full

# Specific reviewers on full codebase
/expert-review callback-safety,panic-safety --full
```

---

## Reviewer Locations

**Generic Reviewers** (user config, all projects):
- `$HOME/.claude/reviewers/`
- Note: Use `echo $HOME` to resolve path - tilde doesn't expand in Glob tool

**Project-Specific Reviewers** (override generic):
- `{project-root}/.claude/reviewers/`

Project reviewers with the same filename override the generic ones.

---

## Recovery from Failures

If an agent fails mid-review:

1. **Check what completed**: `ls {REVIEW_DIR}/`
2. **Retry failed step**: Re-run only the missing reviewer(s)
3. **Resume from Pass 2**: If Pass 1 files exist, skip directly to Pass 2
4. **Inspect artifacts**: Read any `-pass1.md` file to see what was found

The checkpoint pattern means you never lose completed work.

---

## Comparing Reviews

Each review gets its own subfolder, enabling comparison:

```bash
# Compare two reviews
diff /tmp/code-review/main-abc1234/ /tmp/code-review/feature-bar-def5678/

# List all reviews
ls /tmp/code-review/

# Clean up old reviews
rm -rf /tmp/code-review/
```

---

## Notes

- **Subfolder per review**: `{branch}-{short_hash}/` enables comparison, avoids conflicts
- **File checkpoints**: Each step produces artifacts that can be inspected, retried, resumed
- **Tagger routes sections, no longer skips reviewers**: Tagger-unmatched reviewers now receive the full diff at QUICK-SCAN priority rather than being dropped; they may still self-SKIP if truly irrelevant
- **Consistency Checker** (haiku): Mechanical pattern-matching pass that catches mixed error types, inconsistent cleanup patterns, and PR description vs code contradictions. Runs on haiku for cost efficiency. Participates in Pass 2 re-evaluation.
- **Code Rot Cody** (haiku): Mechanical dead-code and orphan detector. Greps the entire repo to verify every new symbol has callers, every removed symbol is cleaned up, and every new config field is end-to-end connected. Runs after Pass 1 domain reviewers (Step 5.6) so his findings are available to Contrarian Carl. Participates in Pass 2 re-evaluation (dead code might be intentionally staged for a follow-up PR).
- **Blind-first review**: Reviewers see WHAT changed before WHY, to catch rationalized issues
- **Main thread execution**: Pass 1, Pass 2, Haiku Q&A, and Cross-Review all run sequentially in main thread
- **Open Questions + Proposals**: Every Pass 1 review ends with explicit open questions (for Haiku to answer) and concrete implementation proposals for HIGH/CRITICAL findings
- **Haiku Q&A** (Step 6.5): After Pass 2, a Haiku agent reads the actual files to answer each reviewer's open questions — turning uncertainty into evidence
- **Expert Cross-Review** (Step 6.7): After Haiku Q&A, each DEEP-DIVE reviewer reads all other experts' findings and cross-comments from their domain — surfacing synergies, conflicts, and gaps
- **Scope expansion**: Reviewers can read referenced files if they see risk indicators
- **Delta review is strict**: Only issues introduced or worsened by the PR
- **Project modifiers**: Detected from CLAUDE.md or `.claude/review-config.md` to adjust review scope (e.g., `greenfield` skips backwards compatibility concerns)
