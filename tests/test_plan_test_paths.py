#!/usr/bin/env python3
"""
Tests for scripts/plan-test-paths.py: enforcing rule that test files are Round 2 deliverables.

Covers:
1. plan mode: exit codes and stderr messages for hits and clean cases
2. units mode: checking owned_files in units.json
3. Predicate parity: classifications match anti-cheat-scan.py's is_test_path
4. Plan and units edge cases: fail-closed exit 2 on malformed input

Run with: python3 tests/test_plan_test_paths.py
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "plan-test-paths.py"
ANTI_CHEAT_SCAN = REPO_ROOT / "scripts" / "anti-cheat-scan.py"


def run_plan_test_paths(mode: str, path: str) -> tuple:
    """Run the script in plan or units mode. Returns (exit_code, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT), mode, f"--{mode}", path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def load_is_test_path():
    """Load is_test_path from scripts/anti-cheat-scan.py, the predicate the script reuses."""
    spec = importlib.util.spec_from_file_location("anti_cheat_scan", ANTI_CHEAT_SCAN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.is_test_path


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
        h.test_result("plan mode with test file: stderr contains step title 'Pin key'",
                      "Step 2 (Pin key)" in stderr, f"stderr={stderr}")
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
        # The annotation must be stripped from the reported path. A buggy script would
        # report "tests/test_keys.py (pin)", so assert on the exact reported-path form.
        h.test_result("plan mode with annotated hit: reported path has no '(pin)' annotation",
                      "lists test file tests/test_keys.py;" in stderr and "tests/test_keys.py (pin)" not in stderr,
                      f"stderr={stderr}")

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
        print("\nTest 1.4: No test files (exit 0) and scanned count")
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
        h.test_result("plan mode prints scanned step count", "scanned 2 step(s)" in stdout, f"stdout={stdout}")

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

    print("\n[Section 1b] plan mode: fail-closed edge cases (exit 2)")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 1.7: Missing Implementation Steps heading (exit 2)")
        plan_no_heading = """# Plan

## Approach

### Step 1: Setup
- **Files**: `tests/test_keys.py`

## Testing Strategy
"""
        plan_file = Path(tmp) / "plan_no_heading.md"
        plan_file.write_text(plan_no_heading)

        rc, stdout, stderr = run_plan_test_paths("plan", str(plan_file))
        h.test_result("plan mode with missing heading: exit 2", rc == 2, f"rc={rc} stderr={stderr}")
        h.test_result("plan mode with missing heading: stderr names the heading",
                      "Implementation Steps" in stderr and "plan-test-paths:" in stderr, f"stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 1.8: Heading is anchored, so a suffixed heading does not match (exit 2)")
        plan_suffixed = """# Plan

## Implementation Steps (draft)

### Step 1: Setup
- **Files**: `tests/test_keys.py`

## Testing Strategy
"""
        plan_file = Path(tmp) / "plan_suffixed.md"
        plan_file.write_text(plan_suffixed)

        rc, stdout, stderr = run_plan_test_paths("plan", str(plan_file))
        h.test_result("plan mode with suffixed heading: exit 2 (not silently clean)", rc == 2, f"rc={rc} stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 1.9: Implementation Steps with zero steps (exit 2)")
        plan_zero_steps = """# Plan

## Implementation Steps

Steps will be described later.

## Testing Strategy
"""
        plan_file = Path(tmp) / "plan_zero_steps.md"
        plan_file.write_text(plan_zero_steps)

        rc, stdout, stderr = run_plan_test_paths("plan", str(plan_file))
        h.test_result("plan mode with zero steps: exit 2", rc == 2, f"rc={rc} stderr={stderr}")
        h.test_result("plan mode with zero steps: stderr reports scanned 0 steps",
                      "scanned 0 steps" in stderr, f"stderr={stderr}")

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

    print("\n[Section 2b] units mode: malformed records fail closed (exit 2)")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 2.6: Unit without owned_files list (exit 2, not skipped)")
        units_no_owned = {
            "units": [
                {"id": "u1", "owned_files": ["src/app.py"]},
                {"id": "u2", "owned_files": ["tests/test_app.py"]},
                {"id": "u3", "files": ["src/x.py"]},
            ]
        }
        units_file = Path(tmp) / "units_no_owned.json"
        units_file.write_text(json.dumps(units_no_owned))

        rc, stdout, stderr = run_plan_test_paths("units", str(units_file))
        h.test_result("units mode with unit lacking owned_files: exit 2", rc == 2, f"rc={rc} stderr={stderr}")
        h.test_result("units mode with unit lacking owned_files: stderr names unit u3 and owned_files",
                      "u3" in stderr and "owned_files" in stderr, f"stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 2.7: Unit whose owned_files is not a list (exit 2)")
        units_bad_owned = {"units": [{"id": "u1", "owned_files": "tests/test_app.py"}]}
        units_file = Path(tmp) / "units_bad_owned.json"
        units_file.write_text(json.dumps(units_bad_owned))

        rc, stdout, stderr = run_plan_test_paths("units", str(units_file))
        h.test_result("units mode with string owned_files: exit 2", rc == 2, f"rc={rc} stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 2.8: Non-object unit (exit 2)")
        units_non_object = {"units": ["tests/test_app.py"]}
        units_file = Path(tmp) / "units_non_object.json"
        units_file.write_text(json.dumps(units_non_object))

        rc, stdout, stderr = run_plan_test_paths("units", str(units_file))
        h.test_result("units mode with non-object unit: exit 2", rc == 2, f"rc={rc} stderr={stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 2.9: Top-level JSON list instead of object (exit 2)")
        units_top_list = [{"id": "u1", "owned_files": ["src/app.py"]}]
        units_file = Path(tmp) / "units_top_list.json"
        units_file.write_text(json.dumps(units_top_list))

        rc, stdout, stderr = run_plan_test_paths("units", str(units_file))
        h.test_result("units mode with top-level list: exit 2 (no crash)",
                      rc == 2 and "plan-test-paths:" in stderr and "internal error" not in stderr,
                      f"rc={rc} stderr={stderr}")

    print("\n[Section 3] Predicate parity: shared is_test_path classification")

    is_test_path = load_is_test_path()

    # Shared path table. Each entry is checked against both anti-cheat-scan.py's
    # is_test_path (loaded directly) and plan-test-paths.py's units-mode exit code.
    test_cases = [
        ("tests/x.py", True),
        ("src/foo.test.ts", True),
        ("src/foo.spec.ts", True),
        ("src/foo_test.go", True),
        ("spec/x.rb", True),
        ("test_mod.py", True),
        # Surprising negatives, kept on purpose:
        # "test/foo.py": the directory is "test" (singular). Only "tests", "__tests__"
        # and "spec" count as test directories, and "foo.py" matches no test basename
        # glob (it is not test_*.py and has no "_test." or ".test." infix).
        ("test/foo.py", False),
        # "src/foo_spec.rb": the basename uses "_spec", not ".spec.", so "*.spec.*"
        # does not match, and "*_test.*" does not either. The "spec" directory rule
        # only applies to directory components, not to a "_spec" filename suffix.
        ("src/foo_spec.rb", False),
        ("scripts/plan-test-paths.py", False),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        print("\nTest 3.1: Script exit code matches anti-cheat-scan.py is_test_path")
        # Create a units record for each case and check that the script agrees with the
        # predicate loaded from anti-cheat-scan.py, and that the expectation table agrees
        # with both.
        for path, should_be_test in test_cases:
            predicate_says = is_test_path(path)
            h.test_result(f"predicate table: {path} is_test_path == {should_be_test}",
                          predicate_says == should_be_test,
                          f"is_test_path({path})={predicate_says}")

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
            expected_rc = 1 if predicate_says else 0
            h.test_result(f"predicate parity: script exit for {path} matches is_test_path (rc={expected_rc})",
                          rc == expected_rc, f"rc={rc} for path {path} predicate={predicate_says}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
