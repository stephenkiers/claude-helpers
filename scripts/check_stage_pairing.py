#!/usr/bin/env python3
"""
Telemetry stage/command pairing linter for command-doc markdown files.

This linter performs two pragmatic checks on hand-authored markdown+bash:

Check A (Existence pairing per stage name):
  Extract every --stage <name> value from stage-begin and stage-end calls.
  For each doc, report:
  - Stage names with stage-begin but no stage-end anywhere in doc (ORPHANED_BEGIN)
  - Stage names with stage-end but no stage-begin anywhere in doc (ORPHANED_END)

  Known limitation: This is an existence check, not a count check. A stage name
  may have one stage-begin reached by multiple branches and multiple stage-end
  call sites (one per branch), which is safe and expected. We do NOT flag count
  mismatches as errors. Also assumes `--stage` is always followed by a literal
  name, not a shell variable reference (`${VAR}`/`$VAR`) — a doc that
  parameterizes a stage name could produce spurious findings.

Check B (Abort paths that leak an open stage):
  Within each fenced ```bash code block, if the block contains:
  - A run-metrics.py stage-begin call NOT followed later in the SAME block by
    a run-metrics.py stage-end call, AND
  - A bare exit 1 (or exit $<variable>) that is not preceded by a stage-end call
    with --outcome failure/interrupted in that same block
  Then flag it as POSSIBLE_LEAK.

  Known limitation: This is a block-scoped heuristic, not whole-doc-scoped
  control-flow analysis. False negatives (real leaks spanning multiple blocks)
  are acceptable; we keep false positives low by only checking exits textually
  after an unclosed stage-begin within the same fenced block.
"""

import re
import sys
from pathlib import Path


def extract_stage_names(content, call_type):
    """Extract all --stage <name> values from stage-begin or stage-end calls."""
    # Look for lines containing run-metrics.py followed by the call type
    pattern = rf'run-metrics\.py.*{call_type}.*--stage\s+(\S+)'
    matches = re.findall(pattern, content)
    return set(matches)


def check_existence_pairing(filepath, content):
    """Check A: Find orphaned stage begins and ends."""
    findings = []

    # Extract all stage names from stage-begin and stage-end calls
    stage_begins = extract_stage_names(content, r'stage-begin')
    stage_ends = extract_stage_names(content, r'stage-end')

    # Find orphaned begins (stage-begin with no matching stage-end)
    for stage_name in stage_begins - stage_ends:
        findings.append(
            f'ORPHANED_BEGIN: stage "{stage_name}" has stage-begin but no stage-end in {filepath}'
        )

    # Find orphaned ends (stage-end with no matching stage-begin)
    for stage_name in stage_ends - stage_begins:
        findings.append(
            f'ORPHANED_END: stage "{stage_name}" has stage-end but no stage-begin in {filepath}'
        )

    return findings


def check_abort_leaks(filepath, content):
    """Check B: Find abort paths that leak an open stage."""
    findings = []

    # Split by bash code fences
    bash_blocks = re.split(r'```bash\n', content)

    # Process each bash block (skip first split, which is before any fence)
    for block_idx, block in enumerate(bash_blocks[1:], 1):
        # Extract the code up to the closing fence
        if '```' in block:
            code, _ = block.split('```', 1)
        else:
            code = block

        # Check if this block has a stage-begin without a matching stage-end
        has_stage_begin = 'run-metrics.py' in code and 'stage-begin' in code
        has_stage_end = 'run-metrics.py' in code and 'stage-end' in code

        if not has_stage_begin:
            continue  # No stage to leak

        # If there's a stage-begin but no stage-end in this block, check for exit 1
        if has_stage_begin and not has_stage_end:
            # Look for exit 1 or exit $variable
            exit_pattern = r'exit\s+(?:1|"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")'
            has_exit = re.search(exit_pattern, code)

            if has_exit:
                # Find the line number (approximate - count newlines up to exit)
                lines_before = code[:has_exit.start()].count('\n')
                # Account for lines before the bash block starts
                approx_line = lines_before + 1

                findings.append(
                    f'POSSIBLE_LEAK: bash block in {filepath} around line {approx_line} calls exit after stage-begin without a preceding stage-end (outcome failure/interrupted)'
                )

    return findings


def lint_file(filepath):
    """Lint a single file and return list of findings."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        return [f'ERROR: File not found: {filepath}']
    except Exception as e:
        return [f'ERROR: Could not read {filepath}: {e}']

    findings = []
    findings.extend(check_existence_pairing(filepath, content))
    findings.extend(check_abort_leaks(filepath, content))

    return findings


def main():
    # Determine which files to scan
    if len(sys.argv) > 1:
        # Use provided file paths
        files = sys.argv[1:]
    else:
        # Default: scan all commands/*.md files relative to repo root
        repo_root = Path(__file__).parent.parent
        commands_dir = repo_root / 'commands'
        if not commands_dir.exists():
            print(f'ERROR: commands directory not found at {commands_dir}', file=sys.stderr)
            return 1
        files = sorted(str(f) for f in commands_dir.glob('*.md'))

    # Collect all findings
    all_findings = []
    for filepath in files:
        findings = lint_file(filepath)
        all_findings.extend(findings)

    # Print findings
    for finding in sorted(all_findings):
        print(finding)

    # Exit code: 0 if no findings, 1 if any findings exist
    return 1 if all_findings else 0


def self_test():
    """Run basic self-tests with inline markdown fixtures."""
    import tempfile
    import os

    tests = [
        # Test 1: Clean case - balanced stage-begin/end
        {
            'name': 'clean case',
            'content': '''
# Test Command

```bash
TELEMETRY_STAGE_ID=$(python3 run-metrics.py stage-begin --stage test 2>/dev/null || echo unknown)
# Do work
python3 run-metrics.py stage-end --stage-id "$TELEMETRY_STAGE_ID" --stage test --outcome success 2>/dev/null || true
```
''',
            'expected_findings': 0
        },
        # Test 2: Orphaned begin
        {
            'name': 'orphaned begin',
            'content': '''
# Test Command

```bash
TELEMETRY_STAGE_ID=$(python3 run-metrics.py stage-begin --stage missing-end 2>/dev/null || echo unknown)
# Do work
```
''',
            'expected_findings': 1,
            'expected_type': 'ORPHANED_BEGIN'
        },
        # Test 3: Orphaned end
        {
            'name': 'orphaned end',
            'content': '''
# Test Command

```bash
# Some work
python3 run-metrics.py stage-end --stage-id "$TELEMETRY_STAGE_ID" --stage missing-begin --outcome success 2>/dev/null || true
```
''',
            'expected_findings': 1,
            'expected_type': 'ORPHANED_END'
        },
        # Test 4: Abort leak
        {
            'name': 'abort leak',
            'content': '''
# Test Command

```bash
TELEMETRY_STAGE_ID=$(python3 run-metrics.py stage-begin --stage risky 2>/dev/null || echo unknown)
# Do work
if [ "$error" = "yes" ]; then
  exit 1
fi
```

Close stage in another block:

```bash
python3 run-metrics.py stage-end --stage-id "$TELEMETRY_STAGE_ID" --stage risky --outcome success 2>/dev/null || true
```
''',
            'expected_findings': 1,
            'expected_type': 'POSSIBLE_LEAK'
        },
        # Test 5: Abort with proper close
        {
            'name': 'abort with proper close',
            'content': '''
# Test Command

```bash
TELEMETRY_STAGE_ID=$(python3 run-metrics.py stage-begin --stage safe 2>/dev/null || echo unknown)
# Do work
if [ "$error" = "yes" ]; then
  python3 run-metrics.py stage-end --stage-id "$TELEMETRY_STAGE_ID" --stage safe --outcome failure 2>/dev/null || true
  exit 1
fi
```
''',
            'expected_findings': 0
        },
    ]

    print('Running self-tests...')
    passed = 0
    failed = 0

    for test in tests:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(test['content'])
            f.flush()
            temp_path = f.name

        try:
            findings = lint_file(temp_path)

            if len(findings) == test['expected_findings']:
                if test['expected_findings'] > 0 and 'expected_type' in test:
                    if test['expected_type'] in findings[0]:
                        print(f"✓ {test['name']}: PASS")
                        passed += 1
                    else:
                        print(f"✗ {test['name']}: FAIL (wrong finding type)")
                        print(f"  Expected: {test['expected_type']}")
                        print(f"  Got: {findings[0]}")
                        failed += 1
                else:
                    print(f"✓ {test['name']}: PASS")
                    passed += 1
            else:
                print(f"✗ {test['name']}: FAIL (expected {test['expected_findings']} findings, got {len(findings)})")
                for finding in findings:
                    print(f"  {finding}")
                failed += 1
        finally:
            os.unlink(temp_path)

    print(f'\nSelf-test results: {passed} passed, {failed} failed')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    # Check if this is a self-test invocation
    if len(sys.argv) == 2 and sys.argv[1] == '--self-test':
        sys.exit(self_test())
    else:
        sys.exit(main())
