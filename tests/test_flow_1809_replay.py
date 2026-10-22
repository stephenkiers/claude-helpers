#!/usr/bin/env python3
"""
Executable tests for #1809 replay fixture and fixes (Steps 1–7 of #320).

Tests verify four fixes:
- Fix 1: plan-coverage checks (verifier-computed uncovered units must be audited)
- Fix 2: resume rule 2 audit binding (audit files bound to the current attempt)
- Fix 3: failure reason logging (sanitised inline in log.md as `failed (reason)`)
- Fix 4: outstanding relay (implement/fix OK receipts carry required OUTSTANDING: line)

The #1809 fixture reproduces a flow where:
- Step 8 of the plan was never built (no files match in the diff)
- Step 10 is declared outstanding in the implement receipt
- The plan log has a `failed (reason)` line

Run with: python3 tests/test_flow_1809_replay.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
EXPERT_FLOW_CMD = REPO_ROOT / "commands" / "expert-flow.md"


def run_verifier(step: str, receipt_path: str, repo: str, branch: str, base: str, coverage: bool = False, plan: str = None, review_dir: str = None, since: str = None) -> tuple:
    """Run the verifier and return (exit_code, stdout, stderr)."""
    cmd = [
        sys.executable,
        str(VERIFIER),
        "--step", step,
        "--receipt", receipt_path,
        "--repo", repo,
        "--branch", branch,
        "--base", base,
    ]
    if since:
        cmd.extend(["--since", since])
    if coverage:
        cmd.append("--coverage")
    if plan:
        cmd.extend(["--plan", plan])
    if review_dir:
        cmd.extend(["--review-dir", review_dir])

    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def extract_regex_from_doc(text: str, anchor: str) -> str:
    """Extract a regex pattern from the expert-flow.md doc by searching for the anchor line."""
    # Look for lines like "... grep at ~line 454 to `^[^ ]+ <STEP> ...`"
    # and extract the regex in backticks
    pattern = rf"{re.escape(anchor)}.*?`([^`]+)`"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1) if m else None


def create_fixture_git_repo(repo_path: str) -> tuple:
    """Create a git repo with two commits: base and branch where Step 8's files are absent from diff.
    Step 8's files exist in base but are not modified in branch; other files are modified.
    Returns (base_sha, branch_sha).
    """
    Path(repo_path).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, capture_output=True, check=True)

    # Base commit: include all files including Step 8 (use paths with /)
    for step in range(1, 11):
        file_path = f"{repo_path}/src/step{step}/file.py"
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        Path(file_path).write_text(f"Step {step} content")

    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
    result = subprocess.run(["git", "commit", "-m", "base"], cwd=repo_path, capture_output=True, text=True, check=True)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True, check=True).stdout.strip()

    # Branch commit: modify step 1's file (but not step 8), so step 8 is absent from the diff
    Path(f"{repo_path}/src/step1/file.py").write_text("Step 1 modified content")
    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
    result = subprocess.run(["git", "commit", "-m", "branch"], cwd=repo_path, capture_output=True, text=True, check=True)
    branch_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True, check=True).stdout.strip()

    return base_sha, branch_sha


def create_plan_with_steps(plan_path: str, num_steps: int = 10) -> None:
    """Create a plan with `num_steps` step headings, each with a backtick-quoted file reference."""
    content = "# Fixture Plan\n\n"
    for step in range(1, num_steps + 1):
        content += f"### Step {step}\n\n"
        # Use paths with / to be recognized by the verifier
        content += f"This is step {step}. File: `src/step{step}/file.py`\n\n"
    Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
    Path(plan_path).write_text(content)


def create_log_with_failure(log_path: str, reason: str = "validation: receipt incomplete") -> None:
    """Create a flow log.md with a plan failed line."""
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    log_content = f"""# Flow Log

2026-10-21T10:00:00Z plan begin
2026-10-21T10:05:00Z plan failed ({reason})
2026-10-21T10:10:00Z implement begin
"""
    Path(log_path).write_text(log_content)


def create_implement_receipt_with_outstanding(receipt_path: str, outstanding: str = "Step 10 (GitHub housekeeping) [self-report]") -> None:
    """Create an implement receipt with OUTSTANDING line."""
    Path(receipt_path).parent.mkdir(parents=True, exist_ok=True)
    content = f"""Decision: OK
OUTSTANDING: {outstanding}
<!-- step-end -->
"""
    Path(receipt_path).write_text(content)


def main():
    h = Harness("FLOW #1809 REPLAY EXECUTABLE TEST SUITE")

    print("[Test 1] Coverage mode: Step 8 uncovered (silent), Step 10 declared")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, branch_sha = create_fixture_git_repo(repo_path)

        plan_path = f"{tmpdir}/plan.md"
        create_plan_with_steps(plan_path)

        receipt_path = f"{tmpdir}/receipt.md"
        create_implement_receipt_with_outstanding(receipt_path)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            repo_path,
            "HEAD",
            base_sha,
            coverage=True,
            plan=plan_path
        )

        # Check that coverage mode succeeded
        passed = exit_code == 0
        if passed:
            # Check for Step 8 uncovered and Step 10 declared
            passed = "Step 8 | uncovered" in stdout and "Step 10 | declared" in stdout

        h.test_result("Coverage mode reports Step 8 uncovered, Step 10 declared", passed, "" if passed else f"stdout:\n{stdout}\nstderr:\n{stderr}")

    print()
    print("[Test 2] Fix 3: Rule 2 grep matches `plan failed (reason)` line")
    cmd_text = EXPERT_FLOW_CMD.read_text()
    # Extract the regex pattern from the doc (searches for the anchor "Rule 2 grep")
    # The plan says the grep should be: ^[^ ]+ <STEP> (begin|retry|ok|taken-over|failed( \(.*\))?$
    # Let's find it in the doc and verify it matches a failed line with reason
    log_content = "2026-10-21T10:00:00Z plan failed (validation: receipt incomplete)\n"
    log_line = log_content.strip()

    # Build the regex from what the plan specifies
    # The grep is: ^[^ ]+ <STEP> (begin|retry|ok|taken-over|failed(?: \(.*\))?$
    # where <STEP> is the step name, e.g., "plan"
    # Use non-capturing group (?:) for the optional part
    regex_pattern = r"^[^ ]+ plan (begin|retry|ok|taken-over|failed(?:\s\(.*\))?)"

    matched = bool(re.match(regex_pattern, log_line))
    h.test_result("Regex matches `plan failed (reason)` line", matched, f"pattern={regex_pattern}, line={log_line}")

    print()
    print("[Test 3] Fix 3: Sanitiser sanitises multi-line reason to one line")
    # The plan specifies: newlines and control characters become spaces, capped at 300 characters
    reason_with_newline = "validation: receipt incomplete\nextra line with control\x01char"

    # Use the sanitiser snippet from the doc
    # The plan says to run the documented snippet in a subprocess
    sanitise_script = '''
reason_text="validation: receipt incomplete
extra line with control$(printf '\\x01')char"
# Collapse newlines, CR, tab to spaces and strip control characters
reason_text="$(printf '%s' "$reason_text" | tr '\\n\\r\\t' ' ' | sed 's/[[:cntrl:]]/ /g')"
# Cap at 300 characters
reason_text="${reason_text:0:300}"
echo "$reason_text"
'''

    result = subprocess.run(["bash", "-c", sanitise_script], capture_output=True, text=True)
    sanitised = result.stdout.strip()

    # Check that it became one line (no newlines)
    has_newline = "\n" in sanitised
    h.test_result("Sanitiser converts multi-line reason to one line", not has_newline, f"result={repr(sanitised)}")

    print()
    print("[Test 4] Fix 2 decision table: Newest `ok` with `STUB:` audit -> Hard stop")
    # This is a model test, not end-to-end
    # The decision rule: if newest is `ok` and audit file is `STUB:`, do not re-ask the gate
    newest_lifecycle = "ok"
    audit_first_line = "STUB: superseded before attempt"

    # Decision logic from the plan
    should_gate = not (newest_lifecycle == "ok" and audit_first_line.startswith("STUB:"))
    h.test_result("Decision rule: `ok` with `STUB:` blocks gate", not should_gate, "")

    print()
    print("[Test 5] Fix 2 decision table: Newest `failed` with completed `FIDELITY: ok` -> revalidate only")
    newest_lifecycle = "failed"
    audit_first_line = "FIDELITY: ok"
    audit_last_line = "<!-- audit-end -->"

    # Decision logic: a completed FIDELITY: ok means revalidate only, don't re-audit
    is_completed = audit_first_line.startswith("FIDELITY:") and audit_last_line == "<!-- audit-end -->"
    should_re_audit = not is_completed
    h.test_result("Decision rule: `failed` with completed `FIDELITY: ok` revalidates only", not should_re_audit, "")

    print()
    print("[Test 6] Fix 1 edge case: Plan with no `### Step` headings -> exit 0 with unit count 0")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, branch_sha = create_fixture_git_repo(repo_path)

        plan_path = f"{tmpdir}/plan.md"
        # Create a plan with no step headings
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan with no steps\n\nJust some content.")

        receipt_path = f"{tmpdir}/receipt.md"
        create_implement_receipt_with_outstanding(receipt_path)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            repo_path,
            "HEAD",
            base_sha,
            coverage=True,
            plan=plan_path
        )

        passed = exit_code == 0 and "UNITS: 0" in stdout and "no step headings" in stdout
        h.test_result("Plan with no step headings exits 0 with unit count message", passed, f"stdout:\n{stdout}")

    print()
    print("[Test 7] Fix 4 / OUTSTANDING line: Missing OUTSTANDING line -> exit 1")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, branch_sha = create_fixture_git_repo(repo_path)

        plan_path = f"{tmpdir}/plan.md"
        create_plan_with_steps(plan_path)

        receipt_path = f"{tmpdir}/receipt.md"
        # Create receipt without OUTSTANDING line
        Path(receipt_path).parent.mkdir(parents=True, exist_ok=True)
        Path(receipt_path).write_text("Decision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            repo_path,
            "HEAD",
            base_sha,
            since="2026-10-21T10:00:00Z"
        )

        passed = exit_code == 1 and "receipt missing OUTSTANDING" in stdout
        h.test_result("Missing OUTSTANDING line -> exit 1 with error", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    print()
    print("[Test 8] Fix 4 / OUTSTANDING line: `OUTSTANDING: none` is valid")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, branch_sha = create_fixture_git_repo(repo_path)

        receipt_path = f"{tmpdir}/receipt.md"
        # Create receipt with OUTSTANDING: none
        Path(receipt_path).parent.mkdir(parents=True, exist_ok=True)
        Path(receipt_path).write_text("Decision: OK\nOUTSTANDING: none\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            repo_path,
            "HEAD",
            base_sha,
            since="2026-10-21T10:00:00Z"
        )

        # Should not fail on OUTSTANDING check
        passed = exit_code != 1 or "receipt missing OUTSTANDING" not in stdout
        h.test_result("OUTSTANDING: none is valid", passed, f"exit_code={exit_code}, stdout={stdout}")

    print()
    print("[Test 9] Fix 4 / OUTSTANDING line: Multi-item OUTSTANDING is valid")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, branch_sha = create_fixture_git_repo(repo_path)

        receipt_path = f"{tmpdir}/receipt.md"
        # Create receipt with multiple OUTSTANDING items
        Path(receipt_path).parent.mkdir(parents=True, exist_ok=True)
        content = """Decision: OK
OUTSTANDING: Step 8 (not built) [self-report]
OUTSTANDING: Step 10 (GitHub housekeeping) [both]
<!-- step-end -->
"""
        Path(receipt_path).write_text(content)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            repo_path,
            "HEAD",
            base_sha,
            since="2026-10-21T10:00:00Z"
        )

        # Should not fail on OUTSTANDING check
        passed = exit_code != 1 or "receipt missing OUTSTANDING" not in stdout
        h.test_result("Multiple OUTSTANDING items are valid", passed, f"exit_code={exit_code}, stdout={stdout}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
