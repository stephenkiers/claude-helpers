#!/usr/bin/env python3
"""
Test suite for shell conventions in slash-command docs (commands/*.md).

Covers: the positional-parameter hazard. Every .md in commands/ is registered as an
invocable slash command, and the harness substitutes the invocation's arguments into
positional tokens in the doc body *before* the shell runs it.

Bare positional tokens `$0` through `$9` are substituted by the harness before the shell
sees them. A literal `$0` in an embedded shell snippet is rewritten to the user's
arguments. The braced form `${1}` is the form this suite treats as safe (the bare-token
detector does not match it), and the indexed `$ARGUMENTS[N]` / `${ARGUMENTS[N]}` forms are
checked by the synthetic guard section. This applies to both prose and fenced code blocks in
command docs; prompts/ files are exempt because they are lazy-loaded by path, never
argument-substituted.

awk cannot use `${1}` (invalid syntax), so awk code should use python3, sed, or git commands.

This was observed live: commands/cleanup.md's `_cap8k` helper was written as
`awk '{b=length($0)+1; ... substr($0,1,8000-n) ...}'` and reached the model as
`awk '{b=length(/Users/.../some-worktree)+1; ...}'` — a broken awk program built from a
filesystem path. prefer python3, sed, or a git command that answers the question directly.

Run with: python3 tests/test_command_doc_shell_conventions.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

# The literal tokens, assembled so this file does not itself contain them.
POSITIONAL_ZERO = "$" + "0"


def has_literal_zero(line):
    """The per-line literal-$0 detector used by the Section 1 scan (and exercised by Section 1.3)."""
    return POSITIONAL_ZERO in line

# Allowlist of known offenders with their maximum allowed bare-token counts (per Step 11 of #303).
# Tracked in #306: remove the remaining bare `$N` tokens from command docs.
# Counts are recomputed at each run from the repo state to enforce ratcheting (counts can only decrease).
# A file whose count reaches zero, or that no longer exists, fails the ratchet until its entry is
# removed from this allowlist, so stale entries cannot linger.
BARE_POSITIONAL_ALLOWLIST = {
    "cleanup.md": 6,
    "expert-plan-deprecated-v2.md": 5,
    "expert-rebase.md": 1,
    "expert-review.md": 11,
    "handoff-resume.md": 2,
    "save-usage.md": 2,
    "stack-sync.md": 8,
    "verify-queue.md": 15,
}


def main():
    h = Harness("COMMAND DOC SHELL CONVENTIONS TEST SUITE")

    commands_dir = REPO_ROOT / "commands"
    h.test_result("commands/ directory exists", commands_dir.is_dir(), str(commands_dir))

    docs = sorted(commands_dir.rglob("*.md"))
    h.test_result("found command docs to scan", len(docs) > 0, f"found {len(docs)}")

    print()
    print("[Section 1] Positional parameter hazard: bare $0-$9 tokens")

    # Check for bare $0 (strict, no allowlist)
    offenders_zero = []
    for doc in docs:
        for lineno, line in enumerate(doc.read_text().splitlines(), start=1):
            if has_literal_zero(line):
                rel = doc.relative_to(REPO_ROOT)
                offenders_zero.append(f"{rel}:{lineno}: {line.strip()}")

    h.test_result(
        f"no command doc contains a literal {POSITIONAL_ZERO} (clobbered by argument substitution)",
        not offenders_zero,
        "\n      " + "\n      ".join(offenders_zero) if offenders_zero else "",
    )

    # Generalized bare-positional guard: scan all commands/*.md for bare $1-$9
    bare_positional_pattern = re.compile(r"\$[1-9]\b")

    # Recompute counts from the repo
    file_counts = {}
    for doc in docs:
        rel_name = doc.name
        content = doc.read_text()
        count = len(bare_positional_pattern.findall(content))
        if count > 0:
            file_counts[rel_name] = count

    # Check against allowlist
    ratchet_failures = []

    # Files not on allowlist must have zero count
    for filename, count in file_counts.items():
        if filename not in BARE_POSITIONAL_ALLOWLIST and count > 0:
            ratchet_failures.append(f"{filename}: {count} bare tokens (not on allowlist, must be 0)")

    # Files on allowlist must not exceed their count
    for filename, max_count in BARE_POSITIONAL_ALLOWLIST.items():
        actual = file_counts.get(filename, 0)
        if actual == 0:
            # Missing file or zero bare tokens: the entry is stale and must be removed.
            ratchet_failures.append(
                f"{filename}: allowlisted but missing or zero bare tokens (remove the stale allowlist entry)"
            )
        elif actual > max_count:
            ratchet_failures.append(f"{filename}: {actual} tokens (exceeds limit {max_count})")
        elif actual < max_count:
            ratchet_failures.append(f"{filename}: {actual} tokens (lower than baseline {max_count}, lower the baseline)")

    h.test_result(
        "bare positional tokens ($1-$9) in allowlisted files only, within counts (ratchet)",
        not ratchet_failures,
        "\n      " + "\n      ".join(ratchet_failures) if ratchet_failures else ""
    )

    # Explicit check: expert-flow.md and expert-plan.md have zero bare $1-$9
    print()
    print("[Section 1.1] expert-flow.md and expert-plan.md: zero bare positional tokens")

    zero_check_failures = []
    for doc_name in ["expert-flow.md", "expert-plan.md"]:
        doc_path = commands_dir / doc_name
        if doc_path.exists():
            count = len(bare_positional_pattern.findall(doc_path.read_text()))
            if count > 0:
                zero_check_failures.append(f"{doc_name}: {count} bare tokens (must be 0)")

    h.test_result(
        "expert-flow.md and expert-plan.md have zero bare $1-$9 tokens",
        not zero_check_failures,
        "\n      " + "\n      ".join(zero_check_failures) if zero_check_failures else ""
    )

    # Guard the guard: synthetic $1 string should match
    print()
    print("[Section 1.2] Guard the guard: detector still matches")

    synthetic_bare = "case $" + "1 in"  # Assembled to avoid containing it
    synthetic_braced = "case $" + "{1} in"  # Assembled
    synthetic_args = "$" + "ARGUMENTS"  # Assembled

    bare_match = bool(bare_positional_pattern.search(synthetic_bare))
    braced_no_match = not bare_positional_pattern.search(synthetic_braced)
    args_no_match = not bare_positional_pattern.search(synthetic_args)

    h.test_result(
        "detector matches synthetic bare `$1` token",
        bare_match,
        "" if bare_match else "failed to match $1"
    )

    h.test_result(
        "detector does NOT match synthetic `${1}` token",
        braced_no_match,
        "" if braced_no_match else "incorrectly matched ${1}"
    )

    h.test_result(
        "detector does NOT match `$ARGUMENTS`",
        args_no_match,
        "" if args_no_match else "incorrectly matched $ARGUMENTS"
    )

    print()
    print("[Section 1.3] Guard the guard: literal $0 and indexed ARGUMENTS forms")

    # A literal $0 inside an awk program is the original observed hazard. This runs the same
    # per-line detector (has_literal_zero) that the Section 1 scan uses, so removing it fails here.
    synthetic_zero = f"_cap8k() {{ awk '{{print substr({POSITIONAL_ZERO},1,10)}}'; }}"
    h.test_result(
        "detector matches synthetic literal $0 token inside an awk snippet",
        has_literal_zero(synthetic_zero),
        "failed to match literal $0",
    )

    # Indexed ARGUMENTS forms (documentation of the pattern only: these assertions use a local regex
    # and do not guard any scan). The bare form is the hazard, the braced form is not matched.
    bare_args_pattern = re.compile(r"\$ARGUMENTS\[\d+\]")
    synthetic_bare_args = "echo $" + "ARGUMENTS[1]"
    synthetic_braced_args = "echo $" + "{ARGUMENTS[1]}"

    h.test_result(
        "indexed-ARGUMENTS detector matches synthetic bare `$ARGUMENTS[N]`",
        bool(bare_args_pattern.search(synthetic_bare_args)),
        "failed to match bare $ARGUMENTS[N]",
    )
    h.test_result(
        "indexed-ARGUMENTS detector does NOT match synthetic braced `${ARGUMENTS[N]}`",
        not bare_args_pattern.search(synthetic_braced_args),
        "incorrectly matched ${ARGUMENTS[N]}",
    )

    print()
    print("[Section 2] Verdict validation field invariants")

    # Scan commands and prompts for deprecated validation_passed usage
    validation_passed_offenders = []
    all_docs = sorted((REPO_ROOT / "commands").rglob("*.md")) + \
              sorted((REPO_ROOT / "prompts").rglob("*.md"))

    for doc in all_docs:
        for lineno, line in enumerate(doc.read_text().splitlines(), start=1):
            if "validation_passed" in line:
                rel = doc.relative_to(REPO_ROOT)
                validation_passed_offenders.append(f"{rel}:{lineno}: {line.strip()}")

    h.test_result(
        "no doc references deprecated validation_passed",
        not validation_passed_offenders,
        "\n      " + "\n      ".join(validation_passed_offenders) if validation_passed_offenders else "",
    )

    print()
    print("[Section 3] Verdict field must use validation, not validation_passed")

    # Check that validation-related lines don't use // true
    validation_related_offenders = []
    for doc in sorted((REPO_ROOT / "commands").rglob("*.md")):
        content = doc.read_text()
        for lineno, line in enumerate(content.splitlines(), start=1):
            # Check for lines that reference validation and use // true
            if ("validation" in line or "VALIDATION" in line) and "// true" in line:
                rel = doc.relative_to(REPO_ROOT)
                validation_related_offenders.append(f"{rel}:{lineno}: {line.strip()}")

    h.test_result(
        "no validation-related line uses // true (fail-closed)",
        not validation_related_offenders,
        "\n      " + "\n      ".join(validation_related_offenders) if validation_related_offenders else "",
    )

    print()
    print("[Section 4] cleanup.md calls cleanup render-validation")

    cleanup_doc = REPO_ROOT / "commands" / "cleanup.md"
    if cleanup_doc.exists():
        content = cleanup_doc.read_text()
        has_render_validation = "cleanup render-validation" in content
        h.test_result(
            "cleanup.md calls cleanup render-validation",
            has_render_validation,
            "render-validation call not found" if not has_render_validation else "",
        )
    else:
        h.test_result(
            "cleanup.md exists",
            False,
            f"{cleanup_doc} not found",
        )

    print()
    print("[Section 5] merge-and-cleanup.md Phase 4-Q passes --queue-started-at")

    merge_cleanup_doc = REPO_ROOT / "commands" / "merge-and-cleanup.md"
    if merge_cleanup_doc.exists():
        content = merge_cleanup_doc.read_text()
        has_queue_started_at = "--queue-started-at" in content
        h.test_result(
            "merge-and-cleanup.md Phase 4-Q references --queue-started-at",
            has_queue_started_at,
            "--queue-started-at not found" if not has_queue_started_at else "",
        )
    else:
        h.test_result(
            "merge-and-cleanup.md exists",
            False,
            f"{merge_cleanup_doc} not found",
        )

    print()
    print("[Section 6] Command doc fence parity (balanced markdown code blocks)")

    # Test cleanup.md specifically for fence parity (owned file; other files may have
    # pre-existing unbalanced fences not yet fixed).
    cleanup_doc = commands_dir / "cleanup.md"
    if cleanup_doc.exists():
        content = cleanup_doc.read_text()
        fence_count = content.count("```")
        h.test_result(
            "cleanup.md has balanced markdown fences (even count of ``` lines)",
            fence_count % 2 == 0,
            f"found {fence_count} fence lines (unbalanced)" if fence_count % 2 != 0 else "",
        )
    else:
        h.test_result("cleanup.md exists", False, "cleanup.md not found")

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
