#!/usr/bin/env python3
"""
Doc-shape tests for commands/shipit.md's cache-trust rewiring (issue #213, spec-blind).

Written from the plan's Testing item 17, NOT from the implementation. The doc's bash
must take its PR, issue, and stack verdicts from the CLI plan (an early verdict plan
and a late applied plan), never from a raw read of github-cache.json.

Tests verify:
1. The Create PR block has no `.pr.number` cache read and no `gh pr view --json number` fallback
2. An early `shipit plan` call (with /dev/null as message path) sits in "Stack Detection"
   before the Is-stacked reference, and its output goes to EARLY_PLAN_FILE
3. EARLY_PLAN_FILE is never piped to `shipit apply`
4. The stale_stack_claimed check clears STACK_IS_STACKED / STACK_PARENT_BRANCH / STACK_PARENT_PR
   before the `git push -u` block, and is keyed on stale_stack_claimed, not cache_stale
5. ISSUE_NUM is read from the early plan's issue_number, PR_NUM from pr_exists / pr_number
6. The late `shipit plan "$TMP_MSG" --body-file "$TMP_BODY" --title "$TITLE"` call sits after
   the push block, is followed by the early/late agreement check, and is the plan piped via
   printf '%s' to `shipit apply`
7. The agreement check compares pr_exists, pr_number, cache_stale, and issue_number
8. EARLY_PLAN_FILE is removed by the final rm -f

Run with: python3 tests/test_shipit_cache_trust_doc.py
"""

import re
from _test_harness import REPO_ROOT, Harness

SHIPIT_FILE = REPO_ROOT / "commands" / "shipit.md"


def read(path):
    """Return a file's text, or empty string if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


DOC = read(SHIPIT_FILE)


def first_index(pattern, start=0):
    """Index of the first regex match at or after start, or None."""
    m = re.search(pattern, DOC[start:])
    return None if m is None else start + m.start()


h = Harness("SHIPIT CACHE-TRUST DOC SHAPE (ISSUE #213, SPEC-BLIND)")
t = h.test_result

# ============================================================================
print("[Check 0] shipit.md is readable")
t("commands/shipit.md is readable and non-empty", bool(DOC))

# ============================================================================
print("[Check 1] Create PR block has no raw PR-number cache read or gh fallback")
t(
    "no jq read of .pr.number from the cache remains",
    re.search(r"jq[^\n]*\.pr\.number", DOC) is None,
)
t(
    "no `gh pr view --json number` fallback remains",
    re.search(r"gh pr view[^\n]*--json number", DOC) is None,
)

# ============================================================================
print("[Check 2] Early shipit plan lives in Stack Detection, before the Is-stacked reference")
stack_idx = DOC.find("### Stack Detection")
early_idx = first_index(r"shipit plan /dev/null")
is_stacked_idx = first_index(r"run the \*\*Stack Detection")
t(
    "Stack Detection heading exists",
    stack_idx >= 0,
)
t(
    "early `shipit plan /dev/null` call exists",
    early_idx is not None,
)
t(
    "early plan writes its output to $EARLY_PLAN_FILE",
    re.search(r'shipit plan /dev/null\s*>\s*"\$EARLY_PLAN_FILE"', DOC) is not None,
)
t(
    "early plan appears after the Stack Detection heading",
    stack_idx >= 0 and early_idx is not None and early_idx > stack_idx,
    f"stack_idx={stack_idx} early_idx={early_idx}",
)
t(
    "early plan appears before the first Is-stacked reference",
    early_idx is not None and is_stacked_idx is not None and early_idx < is_stacked_idx,
    f"early_idx={early_idx} is_stacked_idx={is_stacked_idx}",
)

# ============================================================================
print("[Check 3] EARLY_PLAN_FILE is never piped to shipit apply")
t(
    "no `shipit apply` command takes $EARLY_PLAN_FILE",
    re.search(r"shipit apply[^\n]*EARLY_PLAN_FILE", DOC) is None
    and re.search(r"EARLY_PLAN_FILE[^\n]*\|[^\n]*shipit apply", DOC) is None,
)

# ============================================================================
print("[Check 4] Stale cached stack is discarded before the push block, keyed on stale_stack_claimed")
claim_idx = first_index(r"jq[^\n]*stale_stack_claimed")
clear_idx = first_index(r"STACK_IS_STACKED=false")
push_idx = first_index(r"git push -u origin")
t(
    "stale_stack_claimed is read from $EARLY_PLAN_FILE",
    claim_idx is not None and re.search(r'stale_stack_claimed[^\n]*EARLY_PLAN_FILE|EARLY_PLAN_FILE[^\n]*stale_stack_claimed', DOC[claim_idx:claim_idx + 400]) is not None,
    f"claim_idx={claim_idx}",
)
t(
    "STACK_IS_STACKED is set to false when the stale stack is claimed",
    clear_idx is not None,
)
t(
    "STACK_PARENT_BRANCH is cleared",
    re.search(r'STACK_PARENT_BRANCH=""', DOC) is not None,
)
t(
    "STACK_PARENT_PR is cleared",
    re.search(r'STACK_PARENT_PR=""', DOC) is not None,
)
t(
    "stale-stack clearing appears before the `git push -u` block",
    claim_idx is not None and clear_idx is not None and push_idx is not None
    and claim_idx < push_idx and clear_idx < push_idx,
    f"claim_idx={claim_idx} clear_idx={clear_idx} push_idx={push_idx}",
)
t(
    "stale-stack clearing is not keyed on cache_stale (missing cache must not clear)",
    re.search(r"cache_stale[^\n]*STACK_IS_STACKED|STACK_IS_STACKED[^\n]*cache_stale", DOC) is None,
)

# ============================================================================
print("[Check 5] ISSUE_NUM and PR_NUM come from the early plan")
t(
    "ISSUE_NUM is read from the plan's issue_number",
    re.search(r"ISSUE_NUM=[^\n]*issue_number[^\n]*EARLY_PLAN_FILE|ISSUE_NUM=[^\n]*EARLY_PLAN_FILE[^\n]*issue_number", DOC) is not None,
)
t(
    "no jq read of .issue.number from the cache remains",
    re.search(r"jq[^\n]*\.issue\.number", DOC) is None,
)
t(
    "PR_NUM is read from the plan's pr_exists and pr_number",
    re.search(r"PR_NUM=[^\n]*pr_exists[^\n]*pr_number[^\n]*EARLY_PLAN_FILE|PR_NUM=[^\n]*EARLY_PLAN_FILE[^\n]*pr_exists[^\n]*pr_number", DOC) is not None,
)
t(
    "body merge still reads the PR body via gh pr view \"$PR_NUM\" --json body",
    re.search(r'gh pr view "\$PR_NUM" --json body', DOC) is not None,
)

# ============================================================================
print("[Check 6] Late plan sits after the push block, is checked, and is the plan applied")
late_idx = first_index(r'shipit plan "\$TMP_MSG"[^\n]*--body-file "\$TMP_BODY"[^\n]*--title "\$TITLE"')
apply_idx = first_index(r"printf '%s' \"\$SHIPIT_PLAN\"\s*\|[^\n]*shipit apply")
agree_idx = first_index(r"github-cache\.json changed during /shipit")
t(
    "late `shipit plan \"$TMP_MSG\" --body-file \"$TMP_BODY\" --title \"$TITLE\"` call exists",
    late_idx is not None,
)
t(
    "late plan appears after the first `git push -u`",
    late_idx is not None and push_idx is not None and late_idx > push_idx,
    f"late_idx={late_idx} push_idx={push_idx}",
)
t(
    "agreement check ('github-cache.json changed during /shipit') appears after the late plan",
    late_idx is not None and agree_idx is not None and agree_idx > late_idx,
    f"late_idx={late_idx} agree_idx={agree_idx}",
)
t(
    "`printf '%s' \"$SHIPIT_PLAN\" | ... shipit apply` is the apply pipe",
    apply_idx is not None,
)
t(
    "apply pipe appears after the agreement check",
    apply_idx is not None and agree_idx is not None and apply_idx > agree_idx,
    f"apply_idx={apply_idx} agree_idx={agree_idx}",
)
t(
    "no `echo \"$SHIPIT_PLAN\"` pipe remains",
    re.search(r'echo "\$SHIPIT_PLAN"', DOC) is None,
)

# ============================================================================
print("[Check 7] Agreement check compares the four verdict fields between the two plans")
agree_region = DOC[late_idx:apply_idx] if late_idx is not None and apply_idx is not None and late_idx < apply_idx else ""
for field in ("pr_exists", "pr_number", "cache_stale", "issue_number"):
    t(
        f"agreement check compares .{field}",
        re.search(r"\b" + field + r"\b", agree_region) is not None,
        f"region length={len(agree_region)}",
    )

# ============================================================================
print("[Check 8] The early plan file is removed at the end")
t(
    "rm -f removes EARLY_PLAN_FILE",
    re.search(r"rm -f[^\n]*EARLY_PLAN_FILE", DOC) is not None,
)

print()
h.summarize_and_exit()
