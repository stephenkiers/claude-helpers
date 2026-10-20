#!/usr/bin/env python3
"""
Spec-blind contract tests for Precedent Pat role prompt, ADR-0024, and ADR-0007 amendment.

Tests verify the documented behaviors and wiring of Precedent Pat without reading or
understanding the implementation logic. Each test anchors on distinctive text from the
actual files and fails if the rule is deleted or fundamentally changed.

Tests cover:
1. Input schema validation rules
2. Output path validation
3. Output heading format specification
4. Citation logic and ordering
5. Failure arms (bad digest, bad questions, unwritable output)
6. Display-only invariant across the pipeline
7. Worked examples and receipt reconciliation

Run with: python3 tests/test_precedent_pat_contract.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

# File paths
PROMPT = REPO_ROOT / "prompts" / "precedent-pat.md"
ADR = REPO_ROOT / "docs" / "adr" / "0024-precedent-pat.md"
ADR_0007 = REPO_ROOT / "docs" / "adr" / "0007-triage-and-decision-memory.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
AGENT_FILE = REPO_ROOT / "agents" / "expert-reviewer.md"


def read_file(path):
    """Read file, return empty string if missing (so tests fail cleanly, not crash)."""
    try:
        return path.read_text()
    except OSError:
        return ""


# ============================================================================
# Load all files at suite start (so missing files fail early)
# ============================================================================
h = Harness("PRECEDENT PAT CONTRACT TEST SUITE")
test = h.test_result

prompt_text = read_file(PROMPT)
adr_text = read_file(ADR)
adr_0007_text = read_file(ADR_0007)
claude_md_text = read_file(CLAUDE_MD)
agent_text = read_file(AGENT_FILE)

# ============================================================================
# INVARIANT 1: File presence
# ============================================================================
print("[Invariant 1] All required files exist and are non-empty")

test("prompts/precedent-pat.md exists and is non-empty",
     bool(prompt_text),
     "File missing or empty")

test("docs/adr/0024-precedent-pat.md exists and is non-empty",
     bool(adr_text),
     "File missing or empty")

test("docs/adr/0007-triage-and-decision-memory.md exists and is non-empty",
     bool(adr_0007_text),
     "File missing or empty")

test("CLAUDE.md exists and is non-empty",
     bool(claude_md_text),
     "File missing or empty")

test("agents/expert-reviewer.md exists and is non-empty",
     bool(agent_text),
     "File missing or empty")

# ============================================================================
# INVARIANT 2: CLAUDE.md documents Precedent Pat
# ============================================================================
print("\n[Invariant 2] CLAUDE.md documents Precedent Pat")

test("CLAUDE.md has a '## Precedent Pat' section",
     "## Precedent Pat" in claude_md_text,
     "Section header not found")

test("CLAUDE.md describes Pat as display-only with no decision-logic reads",
     re.search(r"display-only.*no.*decision|decision.*logic.*reads.*it", claude_md_text, re.I),
     "display-only with decision-logic restriction not clearly documented")

test("CLAUDE.md says triage never reads Precedent Pat output",
     re.search(r"triage.*never.*read|triage never reads it", claude_md_text, re.I),
     "triage/never-read contract not clearly stated")

test("CLAUDE.md mentions sanctioned readers (rendering, logging, validation)",
     re.search(r"rendering.*Precedent.*question|logging.*validation.*receipt|caller.*validation.*receipt", claude_md_text, re.I) or
     ("rendering" in claude_md_text and "logging" in claude_md_text and "validation" in claude_md_text),
     "Sanctioned readers not properly documented")

# ============================================================================
# INVARIANT 3: ADR-0024 documents Precedent Pat design
# ============================================================================
print("\n[Invariant 3] ADR-0024 documents Precedent Pat design")

test("ADR-0024 exists with correct title",
     re.search(r"# ADR-0024.*Precedent Pat", adr_text),
     "Title not found or mis-formatted")

test("ADR-0024 describes Pat as a pipeline role",
     re.search(r"pipeline role", adr_text, re.I),
     "Pipeline role description missing")

test("ADR-0024 documents display-only invariant",
     re.search(r"display-only", adr_text, re.I),
     "display-only invariant not documented")

test("ADR-0024 says triage never reads output",
     re.search(r"triage.*never.*read", adr_text, re.I),
     "Triage/never-read invariant not documented")

test("ADR-0024 documents citation logic with ceiling and confidence ordering",
     re.search(r"five.*citation|citation.*ceiling|descending.*confidence.*order", adr_text, re.I),
     "Citation ceiling and confidence-order logic not documented")

# ============================================================================
# INVARIANT 4: ADR-0007 amendment documents Precedent Pat escape hatch
# ============================================================================
print("\n[Invariant 4] ADR-0007 amendment references Precedent Pat as escape hatch")

test("ADR-0007 mentions ADR-0024 and Precedent Pat",
     "ADR-0024" in adr_0007_text and ("Precedent Pat" in adr_0007_text or "precedent.md" in adr_0007_text),
     "ADR-0024 reference or Precedent Pat not mentioned")

test("ADR-0007 describes Pat as prompt-the-human design",
     re.search(r"prompt.the.human|display-only", adr_0007_text, re.I),
     "Prompt-the-human or display-only design not documented")

# ============================================================================
# INVARIANT 5: agents/expert-reviewer.md documents Precedent Pat wiring
# ============================================================================
print("\n[Invariant 5] agents/expert-reviewer.md documents Precedent Pat wiring")

test("agents/expert-reviewer.md mentions Precedent Pat",
     "Precedent Pat" in agent_text,
     "Precedent Pat not mentioned")

test("agents/expert-reviewer.md says Pat reads digest and pending questions",
     re.search(r"Precedent Pat.*read.*digest|pending.*question", agent_text, re.I) or
     re.search(r"reads.*two input files.*digest.*pending", agent_text, re.I),
     "Input files specification not found")

test("agents/expert-reviewer.md says Pat writes precedent.md only",
     re.search(r"writes.*precedent\.md", agent_text),
     "Output file specification not found")

test("agents/expert-reviewer.md documents output path validation",
     re.search(r"output.*path|path.*validation|precedent\.md.*under", agent_text, re.I),
     "Output path validation not documented")

# ============================================================================
# INVARIANT 6: Prompt describes input schema
# ============================================================================
print("\n[Invariant 6] Precedent Pat prompt documents input schema")

test("Prompt mentions pending questions input",
     re.search(r"pending.*question", prompt_text, re.I),
     "Pending questions input not mentioned")

test("Prompt describes id field",
     re.search(r"\bid\b.*field|field.*\bid\b", prompt_text, re.I),
     "id field not documented")

test("Prompt describes header field",
     re.search(r"\bheader\b.*field|field.*\bheader\b", prompt_text, re.I),
     "header field not documented")

test("Prompt describes question field",
     re.search(r"\bquestion\b.*field|field.*\bquestion\b", prompt_text, re.I),
     "question field not documented")

test("Prompt describes options field",
     re.search(r"\boptions?\b", prompt_text),
     "options field not mentioned")

test("Prompt describes digest input",
     re.search(r"digest", prompt_text, re.I),
     "Digest input not mentioned")

# ============================================================================
# INVARIANT 7: Prompt documents output heading format
# ============================================================================
print("\n[Invariant 7] Prompt documents output heading format: ## Q{n} [{id}] {header}")

test("Prompt specifies Q{n} [id] header format",
     re.search(r"Q\{.*\}.*\[.*id.*\]|##.*Q\d", prompt_text),
     "Output heading format not documented or mis-specified")

# ============================================================================
# INVARIANT 8: Prompt documents citation logic
# ============================================================================
print("\n[Invariant 8] Prompt documents citation logic and ordering")

test("Prompt mentions high confidence threshold",
     re.search(r"high.*confidence|confidence.*high", prompt_text, re.I),
     "High confidence rule not documented")

test("Prompt mentions citation ordering (most recent first)",
     re.search(r"most recent.*first|recent.*order", prompt_text, re.I),
     "Most recent first ordering not documented")

test("Prompt documents citation limits (5 total, max 3 per question)",
     re.search(r"five.*citation|3.*citation|no.*exceed.*3|at most 5", prompt_text, re.I),
     "Citation limits (5 total, max 3 per question) not documented")

test("Prompt mentions user vs external prior ordering",
     re.search(r"user.*external|external.*user", prompt_text, re.I),
     "User/external prior ordering not documented")

# ============================================================================
# INVARIANT 9: Prompt documents failure arms
# ============================================================================
print("\n[Invariant 9] Prompt documents failure modes")

test("Prompt describes bad digest or failure handling",
     re.search(r"bad.*digest|digest.*invalid|digest.*error|failure.*arm|if.*digest.*fail", prompt_text, re.I),
     "Bad digest failure not documented")

test("Prompt describes bad questions file or failure handling",
     re.search(r"bad.*question|question.*invalid|question.*error|question.*file.*fail", prompt_text, re.I),
     "Bad questions failure not documented")

test("Prompt describes unwritable/invalid output path failure",
     re.search(r"output.*path.*invalid|unwritable.*output|path.*validation", prompt_text, re.I),
     "Output path failure not documented")

# ============================================================================
# INVARIANT 10: Prompt documents display-only invariant
# ============================================================================
print("\n[Invariant 10] Prompt enforces display-only invariant")

test("Prompt or ADR says no decision logic reads precedent.md",
     re.search(r"display-only|never.*read", prompt_text, re.I) or
     re.search(r"no.*decision.*logic.*read|decision.*logic.*never.*read", adr_text, re.I),
     "Display-only or decision-never-reads contract not documented")

test("Prompt or ADR says triage/decision never reads precedent.md",
     re.search(r"triage.*never.*read|never.*read.*triage|decision.*never.*read", prompt_text, re.I) or
     re.search(r"triage.*never.*read|never.*read.*triage", adr_text, re.I),
     "Triage / never-read contract not documented")

# ============================================================================
# INVARIANT 11: Prompt documents whitespace normalization
# ============================================================================
print("\n[Invariant 11] Prompt documents text sanitization rules")

test("Prompt mentions whitespace normalization",
     re.search(r"whitespace.*normaliz|normaliz.*whitespace", prompt_text, re.I),
     "Whitespace normalization not documented")

test("Prompt mentions leading character neutralization (#, **, backticks)",
     re.search(r"#|backtick|\*\*|leading.*character", prompt_text, re.I),
     "Leading character neutralization not documented")

test("Prompt mentions reason truncation",
     re.search(r"reason.*truncat|truncat.*reason", prompt_text, re.I),
     "Reason truncation not documented")

test("Prompt mentions untrusted question text handling",
     re.search(r"untrusted|question.*text.*untrust", prompt_text, re.I),
     "Untrusted question text handling not documented")

# ============================================================================
# INVARIANT 12: Output path validation
# ============================================================================
print("\n[Invariant 12] Output path validation rules")

test("Prompt or ADR documents output path must end in /precedent.md",
     re.search(r"precedent\.md", prompt_text) or re.search(r"precedent\.md", adr_text),
     "Output filename /precedent.md not specified")

test("Prompt or ADR restricts output path to ~/.claude/reviews/ or ~/.claude/plan-sessions/",
     re.search(r"~.*reviews|~.*plan-sessions", prompt_text) or re.search(r"~.*reviews|~.*plan-sessions", adr_text),
     "Allowed output paths not specified")

test("Prompt or ADR forbids .. in output path",
     re.search(r"\.\.|no.*\.\.|forbid.*\.\.", prompt_text) or re.search(r"\.\.|no.*\.\.|forbid.*\.\.", adr_text),
     "Path traversal protection not documented")

test("agents/expert-reviewer.md documents output path validation",
     re.search(r"output.*path.*must|path.*validation", agent_text, re.I),
     "Output path validation not in agent file")

# ============================================================================
# INVARIANT 13: Worked examples and receipt reconciliation
# ============================================================================
print("\n[Invariant 13] Worked examples and receipt reconciliation")

test("Prompt includes worked examples with receipt format",
     re.search(r"##.*Example|precedent-pat.*\|.*questions:.*\|.*spoke-on:", prompt_text, re.I),
     "Worked examples with receipt format not documented in prompt")

test("Receipt counts concept documented (all silent, zero-count, one-count outcomes)",
     re.search(r"receipt|count.*silent|silent.*count", prompt_text, re.I) or
     re.search(r"receipt|all.*silent", adr_text, re.I),
     "Receipt count/outcome documentation not found")

# ============================================================================
# INVARIANT 14: Silent question handling
# ============================================================================
print("\n[Invariant 14] Silent question handling (empty options)")

test("Prompt documents empty options means silent",
     re.search(r"empty.*option|option.*empty|silent", prompt_text, re.I),
     "Empty options / silent handling not documented")

# ============================================================================
# INVARIANT 15: Distinguish requirement
# ============================================================================
print("\n[Invariant 15] Distinguish mandatory for each question")

test("Prompt documents Distinguish as mandatory when speaking",
     re.search(r"distinguish.*mandatory|mandatory.*distinguish|distinguish.*whenever.*speak", prompt_text, re.I),
     "Distinguish mandatory requirement not clearly documented")

# ============================================================================
# INVARIANT 16: id field is data not instruction
# ============================================================================
print("\n[Invariant 16] id field treated as data, not instruction")

test("Prompt clarifies id field is data, not instruction",
     re.search(r"id.*data.*never.*instruction|never.*instruction.*id|id.*field.*data", prompt_text, re.I),
     "id-as-data clarification not clearly documented")

# ============================================================================
# INVARIANT 17: Sonnet model specification
# ============================================================================
print("\n[Invariant 17] Pat model pinned to Sonnet (ADR-0004)")

test("CLAUDE.md specifies Pat runs on Sonnet",
     re.search(r"Pat.*sonnet|sonnet.*Pat", claude_md_text, re.I),
     "Model pinning not documented in CLAUDE.md")

# ============================================================================
# INVARIANT 18: No index entry or routing
# ============================================================================
print("\n[Invariant 18] Pat has no reviewers/index.yaml entry")

test("CLAUDE.md says Pat has no index entry",
     re.search(r"Pat.*no.*index|no.*index.*Pat", claude_md_text, re.I),
     "No-index-entry rule not documented")

test("CLAUDE.md says Pat has no routing configuration",
     re.search(r"routing.*configuration|no.*routing", claude_md_text, re.I),
     "No-routing rule not documented")

# ============================================================================
# INVARIANT 19: Answer is digest-only (not used for prediction)
# ============================================================================
print("\n[Invariant 19] Answer field is digest-only")

test("Prompt documents answer field as digest-only",
     re.search(r"answer.*digest|digest.*answer", prompt_text, re.I),
     "Answer / digest-only relationship not documented")

# ============================================================================
# INVARIANT 20: Recommended field is optional
# ============================================================================
print("\n[Invariant 20] Recommended field is optional in input schema")

test("Input schema allows optional recommended field",
     re.search(r"optional|recommend", prompt_text, re.I),
     "Optional recommended field not documented")

# ============================================================================
# INVARIANT 21: No suppression of questions
# ============================================================================
print("\n[Invariant 21] No suppression of questions or prior choices")

test("CLAUDE.md/ADR guarantees non-suppression",
     re.search(r"nothing.*suppressed|suppressed.*nothing|not.*suppress|suppress.*not", claude_md_text, re.I) or
     re.search(r"nothing.*suppressed|not.*suppress|never.*suppress", adr_text, re.I),
     "Non-suppression guarantee not documented")

test("CLAUDE.md/ADR says every question and prior choice are visible",
     re.search(r"every.*question|prior.*choice|visible", claude_md_text) or
     re.search(r"visible", adr_text),
     "Question/choice visibility guarantee not documented")

# ============================================================================
# INVARIANT 22: Agent file output path validation note
# ============================================================================
print("\n[Invariant 22] Agent file documents output path validation failure note")

test("agents/expert-reviewer.md specifies note on output path validation failure",
     re.search(r"File not written.*output path", agent_text),
     "Output path validation failure note not documented")

# ============================================================================
# INVARIANT 23: CLAUDE.md distinguishes Distinguish lines for spoken questions
# ============================================================================
print("\n[Invariant 23] CLAUDE.md specifies Distinguish lines for spoken questions")

test("CLAUDE.md says Distinguish lines are for each spoken pending question",
     re.search(r"Distinguish.*spoken|spoken.*question", claude_md_text, re.I),
     "Distinguish scope (spoken questions) not clearly documented")

# ============================================================================
# INVARIANT 24: ADR-0024 documents external prior and provenance rules
# ============================================================================
print("\n[Invariant 24] ADR-0024 documents external prior and unknown provenance rules")

test("ADR-0024 documents external priors and confidence ceiling",
     re.search(r"External.*citation.*ceiling|count.*toward.*ceiling", adr_text, re.I),
     "External prior ceiling rule not documented in ADR-0024")

test("ADR-0024 documents unknown provenance fail-closed rule",
     re.search(r"unknown.*provenance|provenance.*unknown|fail.*closed.*medium", adr_text, re.I),
     "Unknown provenance fail-closed rule not documented in ADR-0024")

test("Prompt documents external priors do not count toward high confidence",
     re.search(r"External.*do not.*independent|not.*count.*independent.*user.*prior", prompt_text, re.I),
     "External prior independence rule not documented in prompt")

test("Prompt documents unknown provenance treatment",
     re.search(r"unknown.*provenance|provenance.*unknown|fail.*closed", prompt_text, re.I),
     "Unknown provenance treatment not documented in prompt")

# ============================================================================
# Summarize and exit
# ============================================================================
print()
h.summarize_and_exit()
