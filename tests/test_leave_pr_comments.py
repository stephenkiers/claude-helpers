#!/usr/bin/env python3
"""
Test suite for /leave-pr-comments command invariants (issue #77).

Spec-blind structural tests: the EXPECTED behavior below is derived only from
the issue #77 spec, not from reading the implementation. The tests run against
the implementation that already exists in this worktree.

Invariants encoded (one or more test_result calls each):
1. commands/leave-pr-comments.md exists and is non-empty.
2. Frontmatter parses and includes model: sonnet, description, argument-hint,
   and an allowed-tools list.
3. PENDING-ONLY: the command never auto-submits. No "event" field set to
   APPROVE / REQUEST_CHANGES / COMMENT (a pending review is created by OMITTING
   event, not by setting it). Prose mentions of "event" are fine; an actual
   JSON "event": "..." field or --field event=... submit action is the violation.
4. HUMAN-IN-THE-LOOP: the command confirms with the user before posting
   (AskUserQuestion before the POST).
5. STALE-COMMIT REFUSAL: refuses to post if the PR HEAD moved since the review,
   with no --force override for staleness.
6. CONSUMES THE CURATED SET: reads selected-comments.json and errors if absent.
7. WRITE BOUNDARY: artifacts stay under ~/.claude/reviews/ (REVIEW_DIR);
   nothing written to the reviewed repo (no git write, no artifact path under
   the worktree).
8. docs/adr/0013-leave-pr-comments-draft-review.md exists, is non-empty, and has
   the required ADR sections: ## Context, ## Decision, ## Consequences, and a
   **Status:** line. (Renumbered from 0012 to 0013 because ADR-0012 is taken by
   /stack-sync.)
9. ADR-0009 amended: docs/adr/0009-peer-review-and-shared-panel.md contains an
   ## Amendment section referencing ADR-0013, AND uses the hyphen form
   {owner}-{repo}/ (NOT the slash form {owner}/{repo}/).
10. docs/adr/README.md index lists ADR-0013.
11. CLAUDE.md lists /leave-pr-comments under the **Review & planning** section.
12. BOTH coworker walk-throughs emit the data contract: expert-review-coworker.md
    AND expert-review-coworker-beta.md each reference selected-comments.json.

Run with: python3 tests/test_leave_pr_comments.py
"""

import re
from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
ADR_DIR = REPO_ROOT / "docs" / "adr"

LPC_FILE = COMMANDS_DIR / "leave-pr-comments.md"
ADR_0013_FILE = ADR_DIR / "0013-leave-pr-comments-draft-review.md"
ADR_0009_FILE = ADR_DIR / "0009-peer-review-and-shared-panel.md"
ADR_README_FILE = ADR_DIR / "README.md"
CLAUDE_MD_FILE = REPO_ROOT / "CLAUDE.md"
COWORKER_FILE = COMMANDS_DIR / "expert-review-coworker.md"
COWORKER_BETA_FILE = COMMANDS_DIR / "expert-review-coworker-beta.md"


def read(path):
    """Return a file's text, or empty string if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


def extract_frontmatter(text):
    """Return the YAML frontmatter block (between the first two --- fences), or ''."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.DOTALL)
    return m.group(1) if m else ""


def extract_bash_blocks(markdown_text):
    """Extract all bash code block contents from markdown as a list of strings."""
    blocks = []
    pattern = r"```bash\n(.*?)\n```"
    for match in re.finditer(pattern, markdown_text, re.DOTALL):
        blocks.append(match.group(1))
    return blocks


def active_command_lines(block_text):
    """Return lines that execute a command (not comments, blanks, or echo/printf emits)."""
    active = []
    for line in block_text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"(echo|printf)\b", stripped):
            continue
        active.append(stripped)
    return active


LPC = read(LPC_FILE)
ADR_0013 = read(ADR_0013_FILE)
ADR_0009 = read(ADR_0009_FILE)
ADR_README = read(ADR_README_FILE)
CLAUDE_MD = read(CLAUDE_MD_FILE)
COWORKER = read(COWORKER_FILE)
COWORKER_BETA = read(COWORKER_BETA_FILE)

h = Harness("LEAVE-PR-COMMENTS TEST SUITE (ISSUE #77)")
t = h.test_result

# ============================================================================
# INVARIANT 1: commands/leave-pr-comments.md exists and is non-empty
# ============================================================================
print("[Invariant 1] commands/leave-pr-comments.md exists and is non-empty")

t("commands/leave-pr-comments.md exists and is non-empty",
  len(LPC.strip()) > 0,
  "expected commands/leave-pr-comments.md to exist with content")

print()

# ============================================================================
# INVARIANT 2: Frontmatter parses with required fields
# ============================================================================
print("[Invariant 2] Frontmatter includes model: sonnet, description, argument-hint, allowed-tools")

lpc_fm = extract_frontmatter(LPC)
t("leave-pr-comments.md has YAML frontmatter",
  len(lpc_fm) > 0,
  "expected frontmatter delimited by --- fences at the top of leave-pr-comments.md")

t("leave-pr-comments.md frontmatter declares 'model: sonnet'",
  re.search(r"^model:\s*sonnet\s*$", lpc_fm, re.MULTILINE) is not None,
  "expected a 'model: sonnet' field in leave-pr-comments.md frontmatter")

t("leave-pr-comments.md frontmatter declares a description",
  re.search(r"^description:\s*\S", lpc_fm, re.MULTILINE) is not None,
  "expected a 'description' field with a value in leave-pr-comments.md frontmatter")

t("leave-pr-comments.md frontmatter declares an argument-hint",
  "argument-hint" in lpc_fm,
  "expected an 'argument-hint' field in leave-pr-comments.md frontmatter")

t("leave-pr-comments.md frontmatter declares an allowed-tools list",
  "allowed-tools" in lpc_fm,
  "expected an 'allowed-tools' field in leave-pr-comments.md frontmatter")

print()

# ============================================================================
# INVARIANT 3: PENDING-ONLY -- no event field set to a submit action
# ============================================================================
print("[Invariant 3] PENDING-ONLY: no 'event' field set to APPROVE/REQUEST_CHANGES/COMMENT")

# A pending review is created by OMITTING event. The violation is an actual
# JSON "event": "APPROVE"|... field or a gh-api --field event=APPROVE|... submit
# action. A prose mention of the word "event" is permitted.
submit_event_pattern = re.compile(
    r'("event"\s*:\s*"(APPROVE|REQUEST_CHANGES|COMMENT)")'  # JSON payload field
    r'|(\b--field\s+event=(APPROVE|REQUEST_CHANGES|COMMENT))'  # gh --field event=...
    r'|(-f\s+event=(APPROVE|REQUEST_CHANGES|COMMENT))',  # gh -f event=...
    re.IGNORECASE,
)
submit_event_matches = submit_event_pattern.findall(LPC)
t("leave-pr-comments.md does not set event to a submit action (APPROVE/REQUEST_CHANGES/COMMENT)",
  len(submit_event_matches) == 0,
  f"found submit-event field(s): {submit_event_matches}" if submit_event_matches else "")

print()

# ============================================================================
# INVARIANT 4: HUMAN-IN-THE-LOOP -- AskUserQuestion confirm before POST
# ============================================================================
print("[Invariant 4] HUMAN-IN-THE-LOOP: AskUserQuestion confirm-before-posting step")

t("leave-pr-comments.md references AskUserQuestion",
  "AskUserQuestion" in LPC,
  "expected an AskUserQuestion call before posting comments")

# A confirm-before-posting step: confirmation language near a POST action.
has_confirm_before_post = re.search(
    r"(confirm|proceed|approve|go ahead|post\?|submit\?).{0,400}(POST|post|gh api|curl)",
    LPC, re.DOTALL | re.IGNORECASE,
) is not None or re.search(
    r"(POST|post|gh api).{0,400}(confirm|proceed|approve|go ahead)",
    LPC, re.DOTALL | re.IGNORECASE,
) is not None
t("leave-pr-comments.md has a confirm-before-posting step",
  "AskUserQuestion" in LPC and has_confirm_before_post,
  "expected confirmation language tied to the posting/POST step")

print()

# ============================================================================
# INVARIANT 5: STALE-COMMIT REFUSAL -- headRefOid check, no --force override
# ============================================================================
print("[Invariant 5] STALE-COMMIT REFUSAL: staleness check with no --force override")

has_staleness_check = (
    "headRefOid" in LPC
    or re.search(r"headRefOid|HEAD.{0,40}(moved|changed|stale|differ)|reviewed.{0,20}SHA",
                 LPC, re.IGNORECASE) is not None
)
t("leave-pr-comments.md has a staleness check (headRefOid / SHA comparison)",
  has_staleness_check,
  "expected a headRefOid reference or a reviewed-SHA vs current-PR-HEAD comparison")

# No --force override for staleness: --force must not be offered as an accepted
# flag (not in the argument-hint, no --force) case arm in bash).
force_in_hint = "--force" in lpc_fm
force_case_arm = re.search(r"--force\)", LPC) is not None
t("leave-pr-comments.md does not offer a --force override for staleness",
  not (force_in_hint or force_case_arm),
  "expected no --force flag in argument-hint and no --force) case arm")

print()

# ============================================================================
# INVARIANT 6: CONSUMES THE CURATED SET -- reads selected-comments.json
# ============================================================================
print("[Invariant 6] CONSUMES THE CURATED SET: reads selected-comments.json, errors if absent")

t("leave-pr-comments.md references selected-comments.json",
  "selected-comments.json" in LPC,
  "expected a reference to selected-comments.json (the curated comment set)")

# Errors if absent: an existence/error guard around the file.
has_absent_error = re.search(
    r"(not.{0,20}exist|missing|absent|not found|no such|cannot read|\[ -f |test -f|ERROR|error|refuse|stop)",
    LPC, re.IGNORECASE,
) is not None and "selected-comments.json" in LPC
t("leave-pr-comments.md errors when selected-comments.json is absent",
  has_absent_error,
  "expected an existence check / error when selected-comments.json is missing")

print()

# ============================================================================
# INVARIANT 7: WRITE BOUNDARY -- artifacts under REVIEW_DIR, no repo writes
# ============================================================================
print("[Invariant 7] WRITE BOUNDARY: artifacts under REVIEW_DIR, nothing written to reviewed repo")

t("leave-pr-comments.md references REVIEW_DIR for payload/receipt paths",
  "REVIEW_DIR" in LPC,
  "expected payload/receipt artifact paths to live under ${REVIEW_DIR}")

# No git write subcommands as active bash lines (commit/push/checkout/reset/
# rebase/merge/am/cherry-pick/apply). Reads like `git rev-parse` are fine.
GIT_WRITE_VERBS = [
    "git commit", "git push", "git checkout", "git reset", "git rebase",
    "git merge", "git am", "git cherry-pick", "git apply", "git stash",
]
lpc_bash_blocks = extract_bash_blocks(LPC)
git_write_violations = []
for block in lpc_bash_blocks:
    for line in active_command_lines(block):
        for verb in GIT_WRITE_VERBS:
            if verb in line:
                git_write_violations.append(f"executes '{verb}': {line}")
t("leave-pr-comments.md bash blocks perform no git write into the reviewed repo",
  len(git_write_violations) == 0,
  f"found git write(s): {git_write_violations}" if git_write_violations else "")

# No artifact path written under the worktree: REVIEW_DIR must not be assigned
# to a path under WORKTREE_PATH, and no write redirection targets WORKTREE_PATH.
worktree_write_violations = []
for pat in [
    r'REVIEW_DIR\s*=\s*"\$\{?WORKTREE_PATH',
    r'>\s*"\$\{?WORKTREE_PATH',
    r'mkdir\s+-p\s+"\$\{?WORKTREE_PATH',
    r'Write.*\$\{?WORKTREE_PATH',
]:
    if re.search(pat, LPC):
        worktree_write_violations.append(pat)
t("leave-pr-comments.md does not write artifacts under ${WORKTREE_PATH}",
  len(worktree_write_violations) == 0,
  f"found worktree write pattern(s): {worktree_write_violations}" if worktree_write_violations else "")

print()

# ============================================================================
# INVARIANT 8: ADR-0013 exists with required sections + Status line
# ============================================================================
print("[Invariant 8] ADR-0013 exists with Status/Context/Decision/Consequences")

t("docs/adr/0013-leave-pr-comments-draft-review.md exists and is non-empty",
  len(ADR_0013.strip()) > 0,
  "expected docs/adr/0013-leave-pr-comments-draft-review.md to exist with content")

t("ADR-0013 has a **Status:** line",
  re.search(r"\*\*Status:\*\*", ADR_0013) is not None,
  "expected a '**Status:**' line in ADR-0013")

t("ADR-0013 has a Context section",
  re.search(r"^##\s+Context", ADR_0013, re.MULTILINE) is not None,
  "expected a '## Context' section in ADR-0013")

t("ADR-0013 has a Decision section",
  re.search(r"^##\s+Decision", ADR_0013, re.MULTILINE) is not None,
  "expected a '## Decision' section in ADR-0013")

t("ADR-0013 has a Consequences section",
  re.search(r"^##\s+Consequences", ADR_0013, re.MULTILINE) is not None,
  "expected a '## Consequences' section in ADR-0013")

print()

# ============================================================================
# INVARIANT 9: ADR-0009 amended -- Amendment section referencing ADR-0013 + hyphen form
# ============================================================================
print("[Invariant 9] ADR-0009 amended: ## Amendment referencing ADR-0013, {owner}-{repo}/ form")

t("ADR-0009 has an ## Amendment section",
  re.search(r"^##\s+Amendment", ADR_0009, re.MULTILINE) is not None,
  "expected an '## Amendment' section in ADR-0009")

t("ADR-0009 Amendment references ADR-0013",
  "0013" in ADR_0009,
  "expected the ADR-0009 amendment to reference ADR-0013")

t("ADR-0009 uses the hyphen form {owner}-{repo}/",
  "{owner}-{repo}/" in ADR_0009,
  "expected the hyphen form '{owner}-{repo}/' in ADR-0009")

t("ADR-0009 does NOT use the slash form {owner}/{repo}/",
  "{owner}/{repo}/" not in ADR_0009,
  "expected the slash form '{owner}/{repo}/' to be absent from ADR-0009")

print()

# ============================================================================
# INVARIANT 10: ADR README index lists ADR-0013
# ============================================================================
print("[Invariant 10] docs/adr/README.md index lists ADR-0013")

t("docs/adr/README.md exists and is non-empty",
  len(ADR_README.strip()) > 0,
  "expected docs/adr/README.md to exist with content")

t("README.md index references ADR-0013",
  "0013" in ADR_README,
  "expected an ADR-0013 entry in the ADR README index")

t("README.md index links to 0013-leave-pr-comments-draft-review.md",
  "0013-leave-pr-comments-draft-review.md" in ADR_README,
  "expected a link to 0013-leave-pr-comments-draft-review.md in the ADR README index")

print()

# ============================================================================
# INVARIANT 11: CLAUDE.md lists /leave-pr-comments under **Review & planning**
# ============================================================================
print("[Invariant 11] CLAUDE.md lists /leave-pr-comments under **Review & planning**")

# Slice the **Review & planning** section: from that bold header up to the next
# bold-header section (the next line starting with '**' that ends a section).
review_section = ""
lines = CLAUDE_MD.split("\n")
in_section = False
for line in lines:
    if "**Review & planning**" in line:
        in_section = True
        continue
    if in_section and re.match(r"^\*\*[^*]+\*\*", line.strip()):
        # Next bold-header section reached.
        break
    if in_section:
        review_section += line + "\n"

t("CLAUDE.md has a **Review & planning** section",
  len(review_section) > 0 or "**Review & planning**" in CLAUDE_MD,
  "expected a '**Review & planning**' section in CLAUDE.md")

t("CLAUDE.md lists /leave-pr-comments under **Review & planning**",
  "/leave-pr-comments" in review_section,
  "expected '/leave-pr-comments' listed within the **Review & planning** section")

print()

# ============================================================================
# INVARIANT 12: Both coworker walk-throughs reference selected-comments.json
# ============================================================================
print("[Invariant 12] Both coworker walk-throughs emit the selected-comments.json data contract")

t("expert-review-coworker.md references selected-comments.json",
  "selected-comments.json" in COWORKER,
  "expected expert-review-coworker.md to reference selected-comments.json")

t("expert-review-coworker-beta.md references selected-comments.json",
  "selected-comments.json" in COWORKER_BETA,
  "expected expert-review-coworker-beta.md to reference selected-comments.json")

print()
h.summarize_and_exit()
