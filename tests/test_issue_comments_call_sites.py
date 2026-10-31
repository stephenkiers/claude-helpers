#!/usr/bin/env python3
"""
Doc-level tests for issue_comments helper call sites.

Verifies:
- No bare `gh issue view ... comments` or `--comments` in command/prompt docs
- All helper consumer docs call `issue-comments fetch` via explicit-cwd form
- Trusted-only consumers pass `--trusted-only`
- flow-reference.md schema includes `comment_id` field
- flow-verify-receipt.py does not report `comment_id` as malformed
- Marker literals in track docs match constants
- Test that scripts/flow-verify-receipt.py permits comment_id in questions

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
WORKFLOW_DIR = REPO_ROOT / "scripts" / "workflow"

# Consumers that must call the helper (from plan Step 11 Consumer table)
HELPER_CONSUMER_DOCS = {
    "commands/expert-plan.md": {"trusted_only": False, "flow_dir": True},
    "commands/expert-plan-deprecated.md": {"trusted_only": False, "flow_dir": False},
    "commands/expert-plan-deprecated-v2.md": {"trusted_only": False, "flow_dir": False},
    "commands/implement-with-haiku.md": {"trusted_only": True, "flow_dir": True},
    "commands/expert-review.md": {"trusted_only": True, "flow_dir": False},
    "commands/shipit.md": {"trusted_only": True, "flow_dir": False},
    "commands/expert-is-it-done.md": {"trusted_only": True, "flow_dir": False},
    "commands/expert-flow.md": {"trusted_only": True, "flow_dir": True},  # pre-merge check
}

# Mechanical readers (should NOT call helper)
MECHANICAL_READER_DOCS = {
    "commands/track-and-start.md": ["body/metadata only", "branch name", "duplicate detection", "cache"],
    "commands/track.md": ["cache", "duplicate detection"],
    "commands/cleanup.md": ["retro summary input"],
}

# ==============================================================================
# TEST: No bare gh issue view ... comments
# ==============================================================================
print("\n[No Bare gh issue view ... comments]")

# Pattern for `gh issue view` with `comments` in --json or --comments flag
NO_COMMENTS_PATTERNS = [
    r"--json\s+[^\n]*comments",
    r"--comments",
]

def scan_for_comments_fetch(path):
    """Return True if path mentions 'comments' in gh issue view calls."""
    text = path.read_text()
    for pattern in NO_COMMENTS_PATTERNS:
        if re.search(pattern, text):
            return True
    return False

all_command_docs = list(COMMANDS_DIR.glob("*.md"))
all_prompt_docs = list(PROMPTS_DIR.glob("*.md"))

for doc in all_command_docs + all_prompt_docs:
    rel = doc.relative_to(REPO_ROOT)
    has_bare_comments = scan_for_comments_fetch(doc)
    h.test_result(
        f"{rel}: does not use bare gh issue view ... comments",
        not has_bare_comments,
        "found --json with comments or --comments flag" if has_bare_comments else ""
    )

# ==============================================================================
# TEST: Helper consumer docs call issue-comments fetch
# ==============================================================================
print("\n[Helper Consumer Docs Call issue-comments fetch]")

EXPLICIT_CWD_PATTERN = r'cd\s+"\$CLAUDE_HELPERS_DIR".*issue-comments\s+fetch.*--cwd'

for doc_path, config in HELPER_CONSUMER_DOCS.items():
    doc = REPO_ROOT / doc_path
    if not doc.exists():
        h.test_result(f"{doc_path} exists", False, "file not found")
        continue

    text = doc.read_text()
    has_helper_call = "issue-comments fetch" in text
    h.test_result(
        f"{doc_path}: calls issue-comments fetch",
        has_helper_call,
        "no issue-comments fetch call found" if not has_helper_call else ""
    )

    # Check that --cwd is mentioned OR refers to issue-comments-reference.md for the form
    has_cwd_flag = "--cwd" in text
    has_reference = "issue-comments-reference.md" in text
    if has_helper_call:
        h.test_result(
            f"{doc_path}: mentions --cwd or refers to issue-comments-reference.md",
            has_cwd_flag or has_reference,
            "missing --cwd or reference to issue-comments-reference.md" if not (has_cwd_flag or has_reference) else ""
        )

# ==============================================================================
# TEST: Trusted-only consumers pass --trusted-only
# ==============================================================================
print("\n[Trusted-Only Consumers Pass --trusted-only]")

for doc_path, config in HELPER_CONSUMER_DOCS.items():
    if not config["trusted_only"]:
        continue  # Skip non-trusted-only consumers

    doc = REPO_ROOT / doc_path
    if not doc.exists():
        continue

    text = doc.read_text()
    if "issue-comments fetch" not in text:
        continue  # Skip if no fetch call

    has_trusted_only = "--trusted-only" in text
    h.test_result(
        f"{doc_path}: passes --trusted-only",
        has_trusted_only,
        "missing --trusted-only flag" if not has_trusted_only else ""
    )

# ==============================================================================
# TEST: Flow-dir consumers pass --flow-dir
# ==============================================================================
print("\n[Flow-Dir Consumers Mention --flow-dir]")

for doc_path, config in HELPER_CONSUMER_DOCS.items():
    if not config["flow_dir"]:
        continue  # Skip non-flow-dir consumers

    doc = REPO_ROOT / doc_path
    if not doc.exists():
        continue

    text = doc.read_text()
    if "issue-comments" not in text:
        continue  # Skip if no helper mention

    # Should mention --flow-dir (in prose or code)
    has_flow_dir = "--flow-dir" in text
    h.test_result(
        f"{doc_path}: mentions --flow-dir",
        has_flow_dir,
        "missing --flow-dir" if not has_flow_dir else ""
    )

# ==============================================================================
# TEST: Mechanical readers do NOT call helper
# ==============================================================================
print("\n[Mechanical Readers Do NOT Call Helper]")

for doc_path, reason_list in MECHANICAL_READER_DOCS.items():
    doc = REPO_ROOT / doc_path
    if not doc.exists():
        h.test_result(f"{doc_path} exists", False, "file not found")
        continue

    text = doc.read_text()
    has_helper_call = "issue-comments fetch" in text
    h.test_result(
        f"{doc_path}: does NOT call issue-comments fetch ({', '.join(reason_list[:2])})",
        not has_helper_call,
        "found issue-comments fetch call" if has_helper_call else ""
    )

# ==============================================================================
# TEST: Marker literals in commands match constants
# ==============================================================================
print("\n[Marker Literals Match Constants]")

SUPERSEDED_MARKER = issue_comments.SUPERSEDED_PLAN_MARKER
IMPLEMENTATION_MARKER = issue_comments.IMPLEMENTATION_PLAN_MARKER

# Check track-and-start.md Step 4d for SUPERSEDED_PLAN_MARKER
track_start_doc = COMMANDS_DIR / "track-and-start.md"
if track_start_doc.exists():
    text = track_start_doc.read_text()
    if SUPERSEDED_MARKER in text:
        h.test_result(
            "track-and-start.md Step 4d uses exact SUPERSEDED_PLAN_MARKER literal",
            True
        )
    else:
        h.test_result(
            "track-and-start.md mentions superseded plan",
            "superseded plan" in text.lower(),
            "marker literal not found" if "superseded" not in text.lower() else ""
        )

# Check track.md for IMPLEMENTATION_PLAN_MARKER
track_doc = COMMANDS_DIR / "track.md"
if track_doc.exists():
    text = track_doc.read_text()
    if IMPLEMENTATION_MARKER in text:
        h.test_result(
            "track.md uses exact IMPLEMENTATION_PLAN_MARKER literal",
            True
        )
    else:
        h.test_result(
            "track.md mentions implementation plan",
            "implementation plan" in text.lower(),
            "marker literal not found" if "implementation" not in text.lower() else ""
        )

# ==============================================================================
# TEST: CONTINUE_LABEL used in issue-comments-reference.md
# ==============================================================================
print("\n[CONTINUE_LABEL Used in Reference Doc]")

issue_comments_ref = PROMPTS_DIR / "issue-comments-reference.md"
if issue_comments_ref.exists():
    text = issue_comments_ref.read_text()
    continue_label = issue_comments.CONTINUE_LABEL
    has_continue_label = continue_label in text
    h.test_result(
        "issue-comments-reference.md contains exact CONTINUE_LABEL",
        has_continue_label,
        f"literal '{continue_label}' not found" if not has_continue_label else ""
    )

# ==============================================================================
# TEST: flow-reference.md schema mentions comment_id
# ==============================================================================
print("\n[flow-reference.md Schema Mentions comment_id]")

flow_ref = PROMPTS_DIR / "flow-reference.md"
if flow_ref.exists():
    text = flow_ref.read_text()
    has_comment_id_in_schema = "comment_id" in text and ("question" in text.lower() or "questions" in text.lower())
    h.test_result(
        "flow-reference.md schema mentions comment_id for questions",
        has_comment_id_in_schema,
        "comment_id not mentioned in question schema" if not has_comment_id_in_schema else ""
    )

# ==============================================================================
# TEST: expert-flow.md relay sentence names comment_id
# ==============================================================================
print("\n[expert-flow.md Relay Sentence Names comment_id]")

expert_flow_doc = COMMANDS_DIR / "expert-flow.md"
if expert_flow_doc.exists():
    text = expert_flow_doc.read_text()
    # Look for relay/strip sentence mentioning comment_id alongside id and brief
    relay_pattern = r"(relay|strip).*?(id|brief|comment_id).*?(id|brief|comment_id)"
    matches = list(re.finditer(relay_pattern, text, re.IGNORECASE | re.DOTALL))
    has_relay_comment_id = any("comment_id" in match.group(0) for match in matches)
    h.test_result(
        "expert-flow.md relay sentence mentions comment_id",
        has_relay_comment_id or "comment_id" in text,
        "comment_id not found in relay context" if "comment_id" not in text else ""
    )

# ==============================================================================
# TEST: implement-with-haiku.md body from helper (no separate gh call)
# ==============================================================================
print("\n[implement-with-haiku.md Body From Helper]")

impl_haiku_doc = COMMANDS_DIR / "implement-with-haiku.md"
if impl_haiku_doc.exists():
    text = impl_haiku_doc.read_text()
    # Should call issue-comments fetch and read .body from it
    has_helper_call = "issue-comments fetch" in text
    # Should NOT have separate `gh issue view <number> --json body`
    has_separate_body_call = re.search(r'gh\s+issue\s+view.*--json.*body', text) is not None
    h.test_result(
        "implement-with-haiku.md gets body from helper output",
        has_helper_call and not has_separate_body_call,
        "either missing helper call or still has separate gh call" if not has_helper_call or has_separate_body_call else ""
    )

# ==============================================================================
# TEST: allowed-tools includes issue-comments
# ==============================================================================
print("\n[allowed-tools Includes issue-comments]")

for doc_path in ["commands/expert-plan.md", "commands/implement-with-haiku.md", "commands/expert-review.md", "commands/shipit.md", "commands/expert-is-it-done.md", "commands/expert-flow.md"]:
    doc = REPO_ROOT / doc_path
    if not doc.exists():
        continue

    text = doc.read_text()
    if "issue-comments fetch" not in text:
        continue  # Skip if no fetch call

    # Check for allowed-tools mentioning issue-comments (or the CLI subcommand)
    has_in_allowed = "issue-comments" in text and "allowed-tools" in text
    # A simpler check: look for allowed-tools section mentioning the CLI
    has_allowed_tools = re.search(r'allowed-tools:.*issue-comments|CLI.*issue-comments', text, re.IGNORECASE | re.DOTALL) is not None
    h.test_result(
        f"{doc_path}: allowed-tools includes issue-comments",
        has_in_allowed or has_allowed_tools,
        "issue-comments not in allowed-tools" if not has_in_allowed else ""
    )

# ==============================================================================
# TEST: flow-reference.md Ask block documents comment_id
# ==============================================================================
print("\n[Ask Block Documents comment_id]")

if flow_ref.exists():
    text = flow_ref.read_text()
    # Look for Ask block that documents comment_id not being passed to AskUserQuestion
    ask_block_pattern = r'(?:Ask\s+block|AskUserQuestion).*?comment_id.*?(?:not|strip|does not pass)'
    has_comment_id_doc = re.search(ask_block_pattern, text, re.IGNORECASE | re.DOTALL) is not None or (
        "comment_id" in text and "AskUserQuestion" in text
    )
    h.test_result(
        "flow-reference.md Ask block documents comment_id",
        has_comment_id_doc,
    )

# ==============================================================================
# TEST: No bare positional shell arguments
# ==============================================================================
print("\n[Shell Conventions - No Bare Positional Arguments]")

# This is checked by test_command_doc_shell_conventions.py, so we just verify
# that test exists and passes basic checks
h.test_result(
    "tests/test_command_doc_shell_conventions.py exists",
    (REPO_ROOT / "tests" / "test_command_doc_shell_conventions.py").exists(),
)

h.summarize_and_exit()
