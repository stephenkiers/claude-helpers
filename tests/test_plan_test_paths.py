#!/usr/bin/env python3
"""
Tests for scripts/plan-test-paths.py: enforcing rule that test files are Round 2 deliverables.

Covers:
1. plan mode: exit codes and stderr messages for hits and clean cases
2. units mode: checking owned_files in units.json
3. Predicate parity: classifications match anti-cheat-scan.py's is_test_path

Run with: python3 tests/test_plan_test_paths.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "plan-test-paths.py"


def run_plan_test_paths(mode: str, path: str) -> tuple:
    """Run the script in plan or units mode. Returns (exit_code, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT), mode, f"--{mode}", path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def main():
    h = Harness("PLAN-TEST-PATHS TEST SUITE")

    print("[Section 1] plan mode: exits and error messages")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 1.1: Hit in Files")
        plan_with_test_file = """# Plan

## Implementation Steps

### Step 1: Setup
- **Files**: `src/app.py`

### Step 2: Pin key
- **Files**: `src/keys.py`, `tests/test_keys.py`

## Testing Strategy

### Tests (written spec-blind in Round 2)
Test that the key endpoint returns valid values.
"""
        plan_file = Path(tmp) / "plan.md"
        plan_file.write_text(plan_with_test_file)

        rc, stdout, stderr = run_plan_test_paths("plan", str(plan_file))
        h.test_result("plan mode with test file in Files: exit 1", rc == 1, f"rc={rc}")
        h.test_result("plan mode with test file: stderr contains 'Step 2'", "Step 2" in stderr, f"stderr={stderr}")
        h.test_result("plan mode with test file: stderr contains test path", "tests/test_keys.py" in stderr, f"stderr={stderr}")
        h.test_result("plan mode with test file: stderr contains 'Round 2 deliverable'", "Round 2 deliverable" in stderr, f"stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 1.2: Annotated hit (Files with (annotation))")
        plan_with_annotation = """# Plan

## Implementation Steps

### Step 2: Pin key
- **Files**: `src/keys.py`, `tests/test_keys.py` (pin)

## Testing Strategy
"""
        plan_file = Path(tmp) / "plan_anno.md"
        plan_file.write_text(plan_with_annotation)

        rc, stdout, stderr = run_plan_test_paths("plan", str(plan_file))
        h.test_result("plan mode with annotated hit: exit 1", rc == 1, f"rc={rc}")
        h.test_result("plan mode with annotated hit: stderr contains test path", "tests/test_keys.py" in stderr, f"stderr={stderr}")
        h.test_result("plan mode with annotated hit: stderr does not contain '(pin)'", "(pin)" not in stderr, f"stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 1.3: Multiple steps with hits")
        plan_multi_hits = """# Plan

## Implementation Steps

### Step 1: Add endpoint
- **Files**: `src/endpoints.py`, `tests/test_endpoints.py`

### Step 2: Add cache
- **Files**: `src/cache.py`, `tests/test_cache.py`

### Step 3: Docs
- **Files**: `README.md`

## Testing Strategy
"""
        plan_file = Path(tmp) / "plan_multi.md"
        plan_file.write_text(plan_multi_hits)

        rc, stdout, stderr = run_plan_test_paths("plan", str(plan_file))
        h.test_result("plan mode with multiple hits: exit 1", rc == 1, f"rc={rc}")
        h.test_result("plan mode with multiple hits: stderr has both step numbers",
                      "Step 1" in stderr and "Step 2" in stderr, f"stderr={stderr}")
        h.test_result("plan mode with multiple hits: stderr has both test paths",
                      "tests/test_endpoints.py" in stderr and "tests/test_cache.py" in stderr, f"stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 1.4: No test files (exit 0)")
        plan_clean = """# Plan

## Implementation Steps

### Step 1: Setup
- **Files**: `src/app.py`, `src/util.py`

### Step 2: Integrate
- **Files**: `src/main.py`

## Testing Strategy
"""
        plan_file = Path(tmp) / "plan_clean.md"
        plan_file.write_text(plan_clean)

        rc, stdout, stderr = run_plan_test_paths("plan", str(plan_file))
        h.test_result("plan mode with non-test files only: exit 0", rc == 0, f"rc={rc} stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 1.5: Test file outside Implementation Steps (exit 0)")
        plan_outside = """# Plan

## Implementation Steps

### Step 1: Key endpoint
- **Files**: `src/keys.py`

## Testing Strategy

### Tests (written spec-blind in Round 2)
Test that tests/test_keys.py validates the endpoint.
"""
        plan_file = Path(tmp) / "plan_outside.md"
        plan_file.write_text(plan_outside)

        rc, stdout, stderr = run_plan_test_paths("plan", str(plan_file))
        h.test_result("plan mode with test file only in Testing Strategy: exit 0", rc == 0, f"rc={rc} stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 1.6: Step with no Files line (exit 0)")
        plan_no_files = """# Plan

## Implementation Steps

### Step 1: Refactor
This step refactors the code without listing files.

### Step 2: Test
- **Files**: `src/main.py`

## Testing Strategy
"""
        plan_file = Path(tmp) / "plan_no_files.md"
        plan_file.write_text(plan_no_files)

        rc, stdout, stderr = run_plan_test_paths("plan", str(plan_file))
        h.test_result("plan mode with step missing Files line: exit 0", rc == 0, f"rc={rc} stderr={stderr}")

    print("\n[Section 2] units mode")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 2.1: owned_files with test path (exit 1)")
        units_with_test = {
            "units": [
                {
                    "id": "u1",
                    "owned_files": ["src/app.py", "src/util.py"]
                },
                {
                    "id": "u2",
                    "owned_files": ["src/a.py", "src/a_test.py"]
                }
            ]
        }
        units_file = Path(tmp) / "units.json"
        units_file.write_text(json.dumps(units_with_test))

        rc, stdout, stderr = run_plan_test_paths("units", str(units_file))
        h.test_result("units mode with test file: exit 1", rc == 1, f"rc={rc}")
        h.test_result("units mode with test file: stderr contains unit id", "u2" in stderr, f"stderr={stderr}")
        h.test_result("units mode with test file: stderr contains test path", "src/a_test.py" in stderr, f"stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 2.2: owned_files with non-test files only (exit 0)")
        units_clean = {
            "units": [
                {
                    "id": "u1",
                    "owned_files": ["src/app.py", "src/util.py"]
                },
                {
                    "id": "u2",
                    "owned_files": ["src/main.py", "src/config.py"]
                }
            ]
        }
        units_file = Path(tmp) / "units_clean.json"
        units_file.write_text(json.dumps(units_clean))

        rc, stdout, stderr = run_plan_test_paths("units", str(units_file))
        h.test_result("units mode with non-test files: exit 0", rc == 0, f"rc={rc} stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 2.3: Missing file (exit 2)")
        rc, stdout, stderr = run_plan_test_paths("units", str(Path(tmp) / "nonexistent.json"))
        h.test_result("units mode with missing file: exit 2", rc == 2, f"rc={rc}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 2.4: Invalid JSON (exit 2)")
        units_file = Path(tmp) / "invalid.json"
        units_file.write_text("{ invalid json }")

        rc, stdout, stderr = run_plan_test_paths("units", str(units_file))
        h.test_result("units mode with invalid JSON: exit 2", rc == 2, f"rc={rc}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 2.5: Missing 'units' key (exit 2)")
        bad_json = {"not_units": []}
        units_file = Path(tmp) / "bad.json"
        units_file.write_text(json.dumps(bad_json))

        rc, stdout, stderr = run_plan_test_paths("units", str(units_file))
        h.test_result("units mode with missing 'units' key: exit 2", rc == 2, f"rc={rc}")

    print("\n[Section 3] Predicate parity: shared is_test_path classification")

    # Test cases from the plan: paths and their expected classifications
    test_cases = [
        ("tests/x.py", True),
        ("src/foo.test.ts", True),
        ("src/foo_test.go", True),
        ("spec/x.rb", True),
        ("test_mod.py", True),
        ("test/foo.py", False),
        ("src/foo_spec.rb", False),
        ("scripts/plan-test-paths.py", False),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 3.1: Predicate classification matches anti-cheat-scan.py")
        # We need to call plan-test-paths and anti-cheat-scan with test files to verify
        # they classify the same way. Create units and a plan for each case.

        for path, should_be_test in test_cases:
            units = {
                "units": [
                    {
                        "id": "u1",
                        "owned_files": [path]
                    }
                ]
            }
            units_file = Path(tmp) / f"units_{path.replace('/', '_')}.json"
            units_file.write_text(json.dumps(units))

            rc, stdout, stderr = run_plan_test_paths("units", str(units_file))

            if should_be_test:
                h.test_result(f"predicate parity: {path} is classified as test (exit 1)",
                              rc == 1, f"rc={rc} for path {path}")
            else:
                h.test_result(f"predicate parity: {path} is not classified as test (exit 0)",
                              rc == 0, f"rc={rc} for path {path}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
