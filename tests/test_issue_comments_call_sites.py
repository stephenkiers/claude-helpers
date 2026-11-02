#!/usr/bin/env python3
"""
Doc-level tests for issue_comments helper call sites.

Verifies:
- No bare `gh issue view ... comments` or `--comments` in command/prompt docs
- Every command doc that calls `issue-comments fetch` is a rostered consumer
- Consumers call the helper in the explicit-cwd form and pass the flags their role needs
- Consumer frontmatter `allowed-tools` authorises the helper call (parsed, not grepped)
- Deprecated planners have NO issue-comments wiring
- Mechanical readers do NOT call the helper
- Marker literals in track docs match the helper constants (unconditional)
- The "new comment since plan" Ask is wired in every post-plan consumer
- Acknowledgement is resolved from questions/answers artifacts (no answers.jsonl, no ack file)
- The relevance scout prompt fails closed

Run with: python3 tests/test_issue_comments_call_sites.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import REPO_ROOT, Harness
from workflow import issue_comments

h = Harness("ISSUE COMMENTS CALL SITES TEST")

COMMANDS_DIR = REPO_ROOT / "commands"
PROMPTS_DIR = REPO_ROOT / "prompts"
DOCS_DIR = REPO_ROOT / "docs"

# Rostered helper consumers: command doc -> the flags its role requires.
HELPER_CONSUMER_DOCS = {
    "commands/expert-plan.md": {"trusted_only": False, "flow_dir": True},
    "commands/implement-with-haiku.md": {"trusted_only": True, "flow_dir": True},
    "commands/expert-review.md": {"trusted_only": True, "flow_dir": True},
    "commands/shipit.md": {"trusted_only": True, "flow_dir": True},
    "commands/expert-is-it-done.md": {"trusted_only": True, "flow_dir": False},
    "commands/expert-flow.md": {"trusted_only": True, "flow_dir": True},  # pre-merge check
}

# Post-plan consumers that raise the "new comment since plan" Ask.
ASK_CONSUMER_DOCS = [
    "commands/implement-with-haiku.md",
    "commands/expert-review.md",
    "commands/shipit.md",
    "commands/expert-flow.md",
]

# Deprecated planners: no issue-comments wiring at all (ADR-0008 2026-10-30 amendment).
DEPRECATED_PLANNER_DOCS = [
    "commands/expert-plan-deprecated.md",
    "commands/expert-plan-deprecated-v2.md",
]

# Mechanical readers (should NOT call helper)
MECHANICAL_READER_DOCS = {
    "commands/track-and-start.md": ["body/metadata only", "branch name", "duplicate detection", "cache"],
    "commands/track.md": ["cache", "duplicate detection"],
    "commands/cleanup.md": ["retro summary input"],
}

FETCH_CALL = "issue-comments fetch"


def read(rel):
    return (REPO_ROOT / rel).read_text()


def frontmatter_block(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    return m.group(1) if m else ""


def allowed_tools_line(text):
    """Return the raw allowed-tools value from frontmatter, or None if undeclared."""
    for line in frontmatter_block(text).splitlines():
        if line.startswith("allowed-tools:"):
            return line[len("allowed-tools:"):]
    return None


# ==============================================================================
# TEST: No bare gh issue view ... comments
# ==============================================================================
print("\n[No Bare gh issue view ... comments]")

NO_COMMENTS_PATTERNS = [
    r"--json\s+[^\n]*comments",
    r"--comments",
]

all_command_docs = sorted(COMMANDS_DIR.glob("*.md"))
all_prompt_docs = sorted(PROMPTS_DIR.glob("*.md"))

for doc in all_command_docs + all_prompt_docs:
    rel = doc.relative_to(REPO_ROOT)
    text = doc.read_text()
    has_bare_comments = any(re.search(p, text) for p in NO_COMMENTS_PATTERNS)
    h.test_result(
        f"{rel}: does not use bare gh issue view ... comments",
        not has_bare_comments,
        "found --json with comments or --comments flag" if has_bare_comments else "",
    )

# ==============================================================================
# TEST: Only rostered consumers call the helper
# ==============================================================================
print("\n[Only Rostered Consumers Call issue-comments fetch]")

for doc in all_command_docs:
    rel = str(doc.relative_to(REPO_ROOT))
    calls_fetch = FETCH_CALL in doc.read_text()
    is_rostered = rel in HELPER_CONSUMER_DOCS
    h.test_result(
        f"{rel}: issue-comments fetch only in rostered consumers",
        (not calls_fetch) or is_rostered,
        "calls issue-comments fetch but is not in the consumer roster" if calls_fetch and not is_rostered else "",
    )

# ==============================================================================
# TEST: Helper consumer docs call the helper correctly
# ==============================================================================
print("\n[Helper Consumer Docs Call issue-comments fetch]")

for doc_path, config in HELPER_CONSUMER_DOCS.items():
    text = read(doc_path)

    h.test_result(
        f"{doc_path}: calls issue-comments fetch",
        FETCH_CALL in text,
        "no issue-comments fetch call found",
    )

    has_explicit_cwd = "--cwd" in text or "explicit-cwd form" in text
    h.test_result(
        f"{doc_path}: uses the explicit-cwd form",
        has_explicit_cwd,
        "missing --cwd and no reference to the explicit-cwd form" if not has_explicit_cwd else "",
    )

    uses_issue_number = '--issue "$ISSUE_NUMBER"' in text
    h.test_result(
        f"{doc_path}: uses $ISSUE_REF, not $ISSUE_NUMBER, for the helper",
        not uses_issue_number,
        'helper still called with --issue "$ISSUE_NUMBER"' if uses_issue_number else "",
    )

    if config["trusted_only"]:
        h.test_result(
            f"{doc_path}: passes --trusted-only",
            "--trusted-only" in text,
            "missing --trusted-only flag",
        )

    if config["flow_dir"]:
        h.test_result(
            f"{doc_path}: passes --flow-dir",
            "--flow-dir" in text,
            "missing --flow-dir flag",
        )

# ==============================================================================
# TEST: allowed-tools authorises the helper call (parsed frontmatter)
# ==============================================================================
print("\n[allowed-tools Authorises the Helper Call]")

for doc_path in HELPER_CONSUMER_DOCS:
    tools = allowed_tools_line(read(doc_path))
    if tools is None:
        # No allowed-tools declared: the command is not tool-gated, so nothing to authorise.
        h.test_result(f"{doc_path}: no allowed-tools restriction declared", True)
        continue
    explicit_grant = "issue-comments fetch" in tools
    # expert-plan grants the helper through the generic cd and python3 prefixes.
    prefix_grant = "Bash(cd:*)" in tools and "Bash(python3:*)" in tools
    h.test_result(
        f"{doc_path}: allowed-tools authorises issue-comments fetch",
        explicit_grant or prefix_grant,
        "allowed-tools does not grant issue-comments fetch (explicitly or via cd and python3 prefixes)",
    )

# ==============================================================================
# TEST: Deprecated planners have NO issue-comments wiring
# ==============================================================================
print("\n[Deprecated Planners Have No Issue-Comments Wiring]")

for doc_path in DEPRECATED_PLANNER_DOCS:
    text = read(doc_path)
    tools = allowed_tools_line(text) or ""
    h.test_result(
        f"{doc_path}: no issue-comments helper call",
        "issue-comments" not in text,
        "found issue-comments wiring" if "issue-comments" in text else "",
    )
    h.test_result(
        f"{doc_path}: no --allow-other-repo flag",
        "--allow-other-repo" not in text,
        "found --allow-other-repo" if "--allow-other-repo" in text else "",
    )
    h.test_result(
        f"{doc_path}: no ## Issue comments section",
        "## Issue comments" not in text,
        "found ## Issue comments section" if "## Issue comments" in text else "",
    )
    h.test_result(
        f"{doc_path}: allowed-tools grants no issue-comments helper",
        "issue-comments" not in tools,
        "allowed-tools grants issue-comments" if "issue-comments" in tools else "",
    )

# ==============================================================================
# TEST: Mechanical readers do NOT call helper
# ==============================================================================
print("\n[Mechanical Readers Do NOT Call Helper]")

for doc_path, reason_list in MECHANICAL_READER_DOCS.items():
    text = read(doc_path)
    has_helper_call = FETCH_CALL in text
    h.test_result(
        f"{doc_path}: does NOT call issue-comments fetch ({', '.join(reason_list[:2])})",
        not has_helper_call,
        "found issue-comments fetch call" if has_helper_call else "",
    )

# ==============================================================================
# TEST: Marker literals in commands match constants (unconditional)
# ==============================================================================
print("\n[Marker Literals Match Constants]")

SUPERSEDED_MARKER = issue_comments.SUPERSEDED_PLAN_MARKER
IMPLEMENTATION_MARKER = issue_comments.IMPLEMENTATION_PLAN_MARKER

h.test_result(
    "track-and-start.md Step 4d uses exact SUPERSEDED_PLAN_MARKER literal",
    SUPERSEDED_MARKER in read("commands/track-and-start.md"),
    f"literal '{SUPERSEDED_MARKER}' not found",
)
h.test_result(
    "track.md uses exact IMPLEMENTATION_PLAN_MARKER literal",
    IMPLEMENTATION_MARKER in read("commands/track.md"),
    f"literal '{IMPLEMENTATION_MARKER}' not found",
)

# ==============================================================================
# TEST: CONTINUE_LABEL and the Ask procedure in issue-comments-reference.md
# ==============================================================================
print("\n[Reference Doc Carries CONTINUE_LABEL and the Ask Procedure]")

ref_text = read("prompts/issue-comments-reference.md")
continue_label = issue_comments.CONTINUE_LABEL
h.test_result(
    "issue-comments-reference.md contains exact CONTINUE_LABEL",
    continue_label in ref_text,
    f"literal '{continue_label}' not found",
)
h.test_result(
    "issue-comments-reference.md names the New comment since plan Ask procedure",
    "\"New comment since plan\" Ask procedure" in ref_text or "New Comment Since Plan" in ref_text,
    "Ask procedure heading not found",
)
h.test_result(
    "issue-comments-reference.md names the Stop for re-plan option",
    "Stop for re-plan" in ref_text,
    "Stop for re-plan option not found",
)

# ==============================================================================
# TEST: Every post-plan consumer wires the "new comment since plan" Ask
# ==============================================================================
print("\n[New Comment Since Plan Ask Wired In Post-Plan Consumers]")

for doc_path in ASK_CONSUMER_DOCS:
    text = read(doc_path)
    h.test_result(
        f"{doc_path}: raises the New comment since plan Ask",
        "New comment since plan" in text and "issue-comments-reference.md" in text,
        "Ask procedure reference missing" if "New comment since plan" not in text else "reference to issue-comments-reference.md missing",
    )

# ==============================================================================
# TEST: Acknowledgement is resolved from artifacts (no answers.jsonl, no ack file)
# ==============================================================================
print("\n[Acknowledgement From Existing Artifacts]")

h.test_result(
    "issue-comments-reference.md resolves acknowledgement from questions/ and answers/",
    "questions/" in ref_text and "answers/" in ref_text,
    "questions/ or answers/ artifact reference missing",
)

ADR_0023 = DOCS_DIR / "adr" / "0023-expert-flow.md"
ADR_0008 = DOCS_DIR / "adr" / "0008-machine-enforced-agent-guardrails.md"
for label, path in [
    ("issue-comments-reference.md", REPO_ROOT / "prompts" / "issue-comments-reference.md"),
    ("ADR-0023", ADR_0023),
    ("ADR-0008", ADR_0008),
]:
    # answers.jsonl may appear only in a sentence that denies it exists as a store.
    asserting_lines = [
        line for line in path.read_text().splitlines()
        if "answers.jsonl" in line and not re.search(r"\b(no|not)\b", line, re.IGNORECASE)
    ]
    h.test_result(
        f"{label}: answers.jsonl appears only in negated form (no such store)",
        not asserting_lines,
        "answers.jsonl asserted as a store" if asserting_lines else "",
    )

# ==============================================================================
# TEST: Relevance scout prompt fails closed
# ==============================================================================
print("\n[Relevance Scout Fails Closed]")

scout_text = read("prompts/comment-relevance-scout.md")
h.test_result(
    "comment-relevance-scout.md does not say 'when in doubt, include'",
    "when in doubt, include" not in scout_text.lower(),
    "scout prompt still says 'when in doubt, include'",
)
h.test_result(
    "comment-relevance-scout.md fails closed on uncertainty",
    "when in doubt, drop" in scout_text.lower(),
    "scout prompt does not state the fail-closed rule",
)

# ==============================================================================
# TEST: flow-reference.md Ask block schema carries comment_id
# ==============================================================================
print("\n[flow-reference.md Ask Block Carries comment_id]")

flow_ref_text = read("prompts/flow-reference.md")
h.test_result(
    "flow-reference.md schema mentions comment_id for questions",
    "comment_id" in flow_ref_text,
    "comment_id not mentioned in question schema",
)

# ==============================================================================
# TEST: expert-flow.md relay strips comment_id from AskUserQuestion
# ==============================================================================
print("\n[expert-flow.md Relay Mentions comment_id]")

expert_flow_text = read("commands/expert-flow.md")
h.test_result(
    "expert-flow.md mentions comment_id",
    "comment_id" in expert_flow_text,
    "comment_id not found in expert-flow.md",
)

# ==============================================================================
# TEST: implement-with-haiku.md body from helper (no separate gh call)
# ==============================================================================
print("\n[implement-with-haiku.md Body From Helper]")

impl_text = read("commands/implement-with-haiku.md")
has_separate_body_call = re.search(r'gh\s+issue\s+view.*--json.*body', impl_text) is not None
h.test_result(
    "implement-with-haiku.md gets body from helper output",
    FETCH_CALL in impl_text and not has_separate_body_call,
    "either missing helper call or still has separate gh call" if (FETCH_CALL not in impl_text or has_separate_body_call) else "",
)

h.summarize_and_exit()
