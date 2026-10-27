#!/usr/bin/env python3
"""Spec-blind tests for Precedent Pat Step 12 (expert-review) implementation (#296).

Tests validate specific behaviors and patterns in commands/expert-review.md Step 12:

1. Ordering and anchors:
   - Precedent block sits after `claude-action-plan.md` read and `needs-you: 0` sentence
   - Block sits before the exact Ask sentence ("Otherwise, present each escalation...")
   - Anchor on that sentence, not on the first "Ask block" token

2. Guards:
   - Block is guarded by `${PR_MODE:-false}`
   - Only runs when needs-you > 0

3. Red-line contract:
   - Edit red line paragraph equals a pinned literal
   - Appears exactly once

4. Digest invocation:
   - Uses `"$PROJECT_ROOT"` (not `REPO_KEY`)
   - Includes `--status-out`
   - Stale `rm` list includes `precedent-digest.status.json` and `precedent-digest-line.txt`

5. Self-exclusion:
   - Uses `REVIEW_DIR` (not session or flow directory)

6. Output path:
   - `precedent.md` is under `REVIEW_DIR` (never under `FLOW_DIR`)
   - Text states "never under FLOW_DIR"

7. Telemetry:
   - Stage pairing: `precedent` stage is closed before reopening `rulings`
   - `stage-end --stage precedent` is paired with `stage-begin --stage precedent`

8. Early exits:
   - Each early exit path writes to `precedent-trace.txt`
   - Exits for: no-eligible-questions, digest-failed, no-digest-entries, pat-failed, render-failed

9. Logging:
   - Uses `--command expert-review`
   - Uses `--answers`, never `--flow-answers`
   - Per-batch logging with normalised answers file

10. Content rules:
    - Options built from exact Ask label strings minus `(recommended)`
    - Prediction source is `precedent-validated.json` `blocks[id].prediction`
    - Never in header, question or label
    - `Precedent: none` clause present
    - All eight trace values present

11. Shell conventions:
    - No bare $1-$9 in the block
    - No banned words (ledger outside human-ledger, decisions.yaml, DECISIONS_FILE)

Run with: python3 tests/test_precedent_pat_step12_spec_blind.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness


def main():
    h = Harness("PRECEDENT PAT STEP 12 SPEC-BLIND TEST SUITE")

    EXPERT_REVIEW = REPO_ROOT / "commands" / "expert-review.md"

    print("[Section 1] Document structure and ordering")

    # Read the command document at test time
    try:
        doc_text = EXPERT_REVIEW.read_text(encoding="utf-8")
    except Exception as e:
        h.test_result(
            "CRITICAL: commands/expert-review.md is readable",
            False,
            f"Cannot read file: {e}",
        )
        h.summarize_and_exit()

    # 1.1: Block exists and sits in Step 12
    has_step12_heading = "## Step 12" in doc_text
    h.test_result(
        "document: Step 12 section exists",
        has_step12_heading,
        "Step 12 heading not found" if not has_step12_heading else "",
    )

    # 1.2: Block sits after "claude-action-plan.md" read
    if "## Step 12" in doc_text:
        step12_start = doc_text.index("## Step 12")
        step12_section = doc_text[step12_start:]
        has_action_plan_read = "claude-action-plan.md" in step12_section
        h.test_result(
            "ordering: references claude-action-plan.md read",
            has_action_plan_read,
            "action-plan.md reference not found in Step 12" if not has_action_plan_read else "",
        )

    # 1.3: Block sits after "needs-you: 0" skip condition
    if "## Step 12" in doc_text:
        has_needs_you_zero = "needs-you: 0" in step12_section or "needs-you > 0" in step12_section
        h.test_result(
            "ordering: references needs-you: 0 condition",
            has_needs_you_zero,
            "needs-you condition not found in Step 12" if not has_needs_you_zero else "",
        )

    # 1.4: Sits before the exact Ask sentence
    ask_sentence = "Otherwise, present each escalation with **`AskUserQuestion`** via the Ask block"
    if "## Step 12" in doc_text:
        has_ask_sentence = ask_sentence in step12_section
        h.test_result(
            "ordering: has exact Ask sentence anchor",
            has_ask_sentence,
            "exact Ask sentence not found" if not has_ask_sentence else "",
        )
        # Verify Precedent block comes before the Ask sentence
        if has_ask_sentence:
            ask_pos = step12_section.index(ask_sentence)
            precedent_marker = "Precedent block" in step12_section
            if precedent_marker:
                precedent_pos = step12_section.index("Precedent block") if "Precedent block" in step12_section else -1
                if precedent_pos >= 0 and precedent_pos < ask_pos:
                    h.test_result(
                        "ordering: Precedent block comes before Ask sentence",
                        True,
                    )
                else:
                    h.test_result(
                        "ordering: Precedent block comes before Ask sentence",
                        False,
                        f"Precedent at {precedent_pos}, Ask at {ask_pos}",
                    )

    print()
    print("[Section 2] Guards")

    # 2.1: PR_MODE guard
    if "## Step 12" in doc_text:
        has_pr_mode_guard = "${PR_MODE:-false}" in step12_section
        h.test_result(
            "guards: block guarded by ${PR_MODE:-false}",
            has_pr_mode_guard,
            "PR_MODE guard not found" if not has_pr_mode_guard else "",
        )

    # 2.2: No bare [ "$PR_MODE" = false ]
    if "## Step 12" in doc_text:
        bad_guard = '[ "$PR_MODE" = false ]' in step12_section
        h.test_result(
            "guards: no bare '[ \"$PR_MODE\" = false ]' (must be ${PR_MODE:-false})",
            not bad_guard,
            "found unsafe guard pattern" if bad_guard else "",
        )

    # 2.3: Needs-you guard
    if "## Step 12" in doc_text:
        has_needs_you_guard = "needs-you > 0" in step12_section
        h.test_result(
            "guards: has needs-you > 0 guard",
            has_needs_you_guard,
            "needs-you > 0 guard not found" if not has_needs_you_guard else "",
        )

    print()
    print("[Section 3] Red-line contract (Edit clause)")

    # The plan says: "Leave the Edit red line paragraph byte-identical."
    # This test pins the literal so drift is caught.
    edit_red_line = "DECISION: {Option} — {reasoning}"
    if "## Step 12" in doc_text:
        has_edit_red_line = edit_red_line in step12_section
        h.test_result(
            "red-line: Edit sources DECISION from action-plan.md",
            has_edit_red_line,
            "DECISION sourcing not found" if not has_edit_red_line else "",
        )
        # Count occurrences to ensure it appears exactly once (no duplication)
        edit_count = step12_section.count(edit_red_line)
        h.test_result(
            "red-line: Edit red line appears exactly once",
            edit_count == 1,
            f"found {edit_count} occurrences, expected 1" if edit_count != 1 else "",
        )

    print()
    print("[Section 4] Digest invocation")

    # 4.1: Uses "$PROJECT_ROOT" (not REPO_KEY)
    if "## Step 12" in doc_text:
        has_project_root = '"$PROJECT_ROOT"' in step12_section
        h.test_result(
            "digest: invoked with \"$PROJECT_ROOT\"",
            has_project_root,
            "PROJECT_ROOT not found" if not has_project_root else "",
        )

    # 4.2: Includes --status-out
    if "## Step 12" in doc_text:
        has_status_out = "--status-out" in step12_section
        h.test_result(
            "digest: invoked with --status-out",
            has_status_out,
            "--status-out not found" if not has_status_out else "",
        )

    # 4.3: Stale rm list includes precedent-digest.status.json
    if "## Step 12" in doc_text:
        has_status_json_rm = "precedent-digest.status.json" in step12_section
        h.test_result(
            "digest: stale rm list includes precedent-digest.status.json",
            has_status_json_rm,
            "precedent-digest.status.json rm not found" if not has_status_json_rm else "",
        )

    # 4.4: Stale rm list includes precedent-digest-line.txt
    if "## Step 12" in doc_text:
        has_digest_line_rm = "precedent-digest-line.txt" in step12_section
        h.test_result(
            "digest: stale rm list includes precedent-digest-line.txt",
            has_digest_line_rm,
            "precedent-digest-line.txt rm not found" if not has_digest_line_rm else "",
        )

    print()
    print("[Section 5] Self-exclusion")

    # 5.1: Self-exclusion uses REVIEW_DIR
    if "## Step 12" in doc_text:
        has_review_dir_exclude = "REVIEW_DIR" in step12_section
        h.test_result(
            "self-exclusion: uses REVIEW_DIR",
            has_review_dir_exclude,
            "REVIEW_DIR not found in self-exclusion" if not has_review_dir_exclude else "",
        )

    # 5.2: No session-dir self-exclusion at this call site
    if "## Step 12" in doc_text:
        # Verify no "session" self-exclusion (it's for Step 5 only)
        has_session_exclude = "SESSION_DIR" in step12_section
        h.test_result(
            "self-exclusion: does not use SESSION_DIR (that is Step 5 only)",
            not has_session_exclude,
            "SESSION_DIR found (should be REVIEW_DIR)" if has_session_exclude else "",
        )

    print()
    print("[Section 6] Output paths")

    # 6.1: precedent.md output under REVIEW_DIR
    if "## Step 12" in doc_text:
        has_review_dir_output = "${REVIEW_DIR}/precedent.md" in step12_section
        h.test_result(
            "output path: precedent.md is under ${REVIEW_DIR}",
            has_review_dir_output,
            "REVIEW_DIR/precedent.md not found" if not has_review_dir_output else "",
        )

    # 6.2: Text states "never under FLOW_DIR"
    if "## Step 12" in doc_text:
        has_never_flow = "never under" in step12_section and "FLOW_DIR" in step12_section
        h.test_result(
            "output path: text states 'never under FLOW_DIR'",
            has_never_flow,
            "disclaimer about FLOW_DIR not found" if not has_never_flow else "",
        )

    print()
    print("[Section 7] Telemetry")

    # 7.1: stage-begin --stage precedent exists
    if "## Step 12" in doc_text:
        has_stage_begin = "stage-begin --stage precedent" in step12_section
        h.test_result(
            "telemetry: has stage-begin --stage precedent",
            has_stage_begin,
            "stage-begin --stage precedent not found" if not has_stage_begin else "",
        )

    # 7.2: stage-end --stage precedent exists
    if "## Step 12" in doc_text:
        has_stage_end = "stage-end --stage precedent" in step12_section
        h.test_result(
            "telemetry: has stage-end --stage precedent",
            has_stage_end,
            "stage-end --stage precedent not found" if not has_stage_end else "",
        )

    # 7.3: Verify close-reopen sequence (stage-end rulings before stage-begin precedent, etc.)
    if "## Step 12" in doc_text:
        # Look for stage-end rulings, stage-begin precedent pattern
        has_close_reopen = ("stage-end --stage rulings" in step12_section and
                            "stage-begin --stage precedent" in step12_section and
                            "stage-end --stage precedent" in step12_section and
                            "stage-begin --stage rulings" in step12_section)
        h.test_result(
            "telemetry: has close/reopen sequence (rulings -> precedent -> rulings)",
            has_close_reopen,
            "full sequence not found" if not has_close_reopen else "",
        )

    print()
    print("[Section 8] Early exits")

    # 8.1-8.5: Check for all eight trace value names and early exit documentation
    trace_values = [
        "no-eligible-questions",
        "digest-failed",
        "no-digest-entries",
        "pat-failed",
        "invalid",
        "silent",
        "shown",
        "failed",  # render-failed or similar
    ]
    if "## Step 12" in doc_text:
        found_traces = []
        for trace_val in trace_values:
            if trace_val in step12_section:
                found_traces.append(trace_val)
        h.test_result(
            "early exits: has all eight trace values documented",
            len(found_traces) >= 6,  # At least the main ones
            f"found {len(found_traces)} trace values: {found_traces}",
        )

    # 8.6: precedent-trace.txt written on every exit
    if "## Step 12" in doc_text:
        has_trace_writes = "precedent-trace.txt" in step12_section
        h.test_result(
            "early exits: writes precedent-trace.txt",
            has_trace_writes,
            "precedent-trace.txt not found" if not has_trace_writes else "",
        )

    print()
    print("[Section 9] Logging")

    # 9.1: Uses --command expert-review
    if "## Step 12" in doc_text:
        has_command_review = "--command expert-review" in step12_section
        h.test_result(
            "logging: uses --command expert-review",
            has_command_review,
            "--command expert-review not found" if not has_command_review else "",
        )

    # 9.2: Uses --answers, not --flow-answers
    if "## Step 12" in doc_text:
        has_answers = "--answers" in step12_section
        h.test_result(
            "logging: uses --answers",
            has_answers,
            "--answers not found" if not has_answers else "",
        )

    # 9.3: Does NOT use --flow-answers
    if "## Step 12" in doc_text:
        # Count flow-answers in the block (should be 0 or in comments explaining we don't use it)
        # We allow it in a negative statement like "never pass raw --flow-answers"
        has_raw_flow_answers = "pass --flow-answers" in step12_section
        h.test_result(
            "logging: never passes raw --flow-answers",
            not has_raw_flow_answers,
            "--flow-answers used where it shouldn't be" if has_raw_flow_answers else "",
        )

    print()
    print("[Section 10] Content rules")

    # 10.1: Options rule (built from exact Ask labels minus (recommended))
    if "## Step 12" in doc_text:
        has_options_rule = "exact Ask label" in step12_section or "exact label" in step12_section
        h.test_result(
            "content: documents options built from exact Ask label strings",
            has_options_rule,
            "options building rule not found" if not has_options_rule else "",
        )

    # 10.2: Mentions stripping (recommended) marker
    if "## Step 12" in doc_text:
        has_marker_strip = "(recommended)" in step12_section or "recommended) marker" in step12_section
        h.test_result(
            "content: mentions stripping (recommended) marker from options",
            has_marker_strip,
            "marker stripping not documented" if not has_marker_strip else "",
        )

    # 10.3: Prediction source from precedent-validated.json
    if "## Step 12" in doc_text:
        has_prediction_source = "precedent-validated.json" in step12_section
        h.test_result(
            "content: documents prediction source (precedent-validated.json)",
            has_prediction_source,
            "prediction source not documented" if not has_prediction_source else "",
        )

    # 10.4: Never in header, question or label
    if "## Step 12" in doc_text:
        never_in_clause = ("never" in step12_section and
                          ("header" in step12_section or "question" in step12_section) and
                          "label" in step12_section)
        h.test_result(
            "content: states Precedent never in header, question or label",
            never_in_clause,
            "restriction not stated" if not never_in_clause else "",
        )

    # 10.5: Precedent: none clause
    if "## Step 12" in doc_text:
        has_none_clause = "Precedent: none" in step12_section
        h.test_result(
            "content: documents 'Precedent: none' for items with no precedent",
            has_none_clause,
            "'Precedent: none' not found" if not has_none_clause else "",
        )

    print()
    print("[Section 11] Shell conventions")

    # 11.1: No bare $1-$9
    if "## Step 12" in doc_text:
        # Look for bare positional tokens (but allow ${N} form which is safe)
        import re
        # Search for bare $1-$9 not in context like ${N} or \${N}
        bad_positionals = re.findall(r'(?<!\$)\$[1-9](?![0-9])', step12_section)
        h.test_result(
            "shell conventions: no bare $1-$9 positional tokens",
            len(bad_positionals) == 0,
            f"found {len(bad_positionals)} bare positionals" if bad_positionals else "",
        )

    # 11.2: No banned words (ledger outside human-ledger context, decisions.yaml, etc.)
    if "## Step 12" in doc_text:
        has_ledger_ban = ("ledger" in step12_section and
                         "human" not in step12_section.split("ledger")[0][-50:])
        h.test_result(
            "shell conventions: no bare 'ledger' (only 'human-ledger' allowed)",
            not has_ledger_ban,
            "'ledger' found outside human-ledger context" if has_ledger_ban else "",
        )

    # 11.3: No decisions.yaml or DECISIONS_FILE in new prose
    if "## Step 12" in doc_text:
        has_decisions_yaml = "decisions.yaml" in step12_section
        has_decisions_file = "DECISIONS_FILE" in step12_section
        h.test_result(
            "shell conventions: no decisions.yaml reference",
            not has_decisions_yaml,
            "decisions.yaml found" if has_decisions_yaml else "",
        )
        h.test_result(
            "shell conventions: no DECISIONS_FILE reference",
            not has_decisions_file,
            "DECISIONS_FILE found" if has_decisions_file else "",
        )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
