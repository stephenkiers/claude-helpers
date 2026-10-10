#!/usr/bin/env python3
"""
Doc-consistency tests for the /expert-spike rollout: the spikes/ write-prefix, the spike-researcher
agent, the ADR-0018 amendment pointing at ADR-0022, and the cross-references to /expert-spike.

Every assertion is section-scoped where the spec names a section, so a mention elsewhere in the
file cannot satisfy it. Files are read at runtime; a missing file yields an empty string and fails.

Run with: python3 tests/test_expert_spike_docs.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ADR_0018 = REPO_ROOT / "docs" / "adr" / "0018-parallel-planning-checkpoint-architecture.md"
EXPERT_REVIEWER = REPO_ROOT / "agents" / "expert-reviewer.md"
REVIEWERS_README = REPO_ROOT / "reviewers" / "README.md"
WORKTREE_REFERENCE = REPO_ROOT / "prompts" / "worktree-reference.md"
RESEARCH_SWARM = REPO_ROOT / "commands" / "research-swarm.md"


def read(path):
    """Return a file's text, or '' if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


def bold_block(text, header_prefix):
    """
    Return the block starting at the line that begins with header_prefix (a bold header) and
    running up to the next line that starts with '**' (or EOF). The header line is included.
    """
    m = re.search(r"^" + re.escape(header_prefix) + r".*$", text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^\*\*", rest, re.M)
    end = m.end() + (nxt.start() if nxt else len(rest))
    return text[m.start():end]


def paragraph_from(text, prefix):
    """Return the paragraph beginning with prefix, up to the next blank line (or EOF)."""
    m = re.search(r"^" + re.escape(prefix), text, re.M)
    if not m:
        return ""
    rest = text[m.start():]
    end = re.search(r"\n[ \t]*\n", rest)
    return rest[: end.start()] if end else rest


def h2_sections(text):
    """Return a list of (heading_line, body) for every '## ' heading; body runs to the next '## ' or EOF."""
    headings = list(re.finditer(r"^## .*$", text, re.M))
    sections = []
    for idx, m in enumerate(headings):
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(text)
        sections.append((m.group(0), text[m.end():end]))
    return sections


CLAUDE_TEXT = read(CLAUDE_MD)
ADR_0018_TEXT = read(ADR_0018)
EXPERT_REVIEWER_TEXT = read(EXPERT_REVIEWER)
REVIEWERS_README_TEXT = read(REVIEWERS_README)
WORKTREE_REFERENCE_TEXT = read(WORKTREE_REFERENCE)
RESEARCH_SWARM_TEXT = read(RESEARCH_SWARM)

h = Harness("EXPERT-SPIKE DOC TEST SUITE")
t = h.test_result

# ============================================================================
# 1. CLAUDE.md "Research & writing" block lists /expert-spike
# ============================================================================
print("[1] CLAUDE.md '**Research & writing**' block mentions /expert-spike")

research_block = bold_block(CLAUDE_TEXT, "**Research & writing**")
t("CLAUDE.md has a '**Research & writing**' block",
  research_block != "",
  "Could not find a line starting with '**Research & writing**' in CLAUDE.md")
t("'**Research & writing**' block contains /expert-spike",
  "/expert-spike" in research_block,
  "The '**Research & writing**' block in CLAUDE.md must list /expert-spike "
  "(a mention elsewhere in CLAUDE.md does not count)")

print()

# ============================================================================
# 2. CLAUDE.md "Panel agents are capability-restricted" paragraph mentions spikes/
# ============================================================================
print("[2] CLAUDE.md 'Panel agents are capability-restricted' paragraph mentions spikes/")

panel_paragraph = paragraph_from(CLAUDE_TEXT, "**Panel agents are capability-restricted")
t("CLAUDE.md has the '**Panel agents are capability-restricted' paragraph",
  panel_paragraph != "",
  "Could not find a paragraph beginning '**Panel agents are capability-restricted' in CLAUDE.md")
t("'Panel agents are capability-restricted' paragraph contains spikes/",
  "spikes/" in panel_paragraph,
  "The capability-restricted paragraph in CLAUDE.md must mention the spikes/ write prefix")

print()

# ============================================================================
# 3. CLAUDE.md "## Agents" section lists spike-researcher
# ============================================================================
print("[3] CLAUDE.md '## Agents' section mentions spike-researcher")

agents_sections = [body for heading, body in h2_sections(CLAUDE_TEXT)
                   if heading.strip() == "## Agents"]
agents_body = agents_sections[0] if agents_sections else ""
t("CLAUDE.md has a '## Agents' section",
  bool(agents_sections),
  "Could not find a '## Agents' heading in CLAUDE.md")
t("'## Agents' section contains spike-researcher",
  "spike-researcher" in agents_body,
  "The '## Agents' section of CLAUDE.md must list the spike-researcher agent")

print()

# ============================================================================
# 4. ADR-0018 has a spike-related '## Amendment' section citing spikes/ and ADR-0022
# ============================================================================
print("[4] ADR-0018 spike amendment mentions spikes/ and ADR-0022")

spike_amendments = [
    body for heading, body in h2_sections(ADR_0018_TEXT)
    if heading.lower().startswith("## amendment") and "spike" in heading.lower()
]
t("ADR-0018 has a '## Amendment' heading containing 'spike' (case-insensitive)",
  bool(spike_amendments),
  "Could not find a '## Amendment' heading with 'spike' in its title in ADR-0018")
t("ADR-0018 spike amendment section mentions spikes/ and ADR-0022",
  any("spikes/" in body and "ADR-0022" in body for body in spike_amendments),
  "The spike '## Amendment' section of ADR-0018 must mention both spikes/ and ADR-0022")

print()

# ============================================================================
# 5. agents/expert-reviewer.md has no 'still pending' phrase
# ============================================================================
print("[5] agents/expert-reviewer.md does not contain 'still pending'")

t("agents/expert-reviewer.md exists and is non-empty",
  EXPERT_REVIEWER_TEXT != "",
  "File not found or empty")
t("agents/expert-reviewer.md does not contain 'still pending'",
  "still pending" not in EXPERT_REVIEWER_TEXT,
  "Phrase 'still pending' must not appear in agents/expert-reviewer.md")

print()

# ============================================================================
# 6. Cross-references: reviewers/README.md, prompts/worktree-reference.md, commands/research-swarm.md
# ============================================================================
print("[6] Cross-references to /expert-spike and spikes/")

t("reviewers/README.md mentions /expert-spike",
  "/expert-spike" in REVIEWERS_README_TEXT,
  "reviewers/README.md must mention /expert-spike")
t("reviewers/README.md mentions the word 'plan'",
  re.search(r"\bplan\b", REVIEWERS_README_TEXT) is not None,
  "reviewers/README.md must mention the word 'plan'")

t("prompts/worktree-reference.md mentions spikes/",
  "spikes/" in WORKTREE_REFERENCE_TEXT,
  "prompts/worktree-reference.md must mention the spikes/ directory")

t("commands/research-swarm.md mentions /expert-spike",
  "/expert-spike" in RESEARCH_SWARM_TEXT,
  "commands/research-swarm.md must mention /expert-spike")

print()

h.summarize_and_exit()
