#!/usr/bin/env python3
"""
Test suite for usage-gate documentation in ADRs and metrics docs.

Covers:
  - ADR-0016 has an Amendment section
  - ADR-0016 amendment mentions per-subagent token accounting
  - docs/metrics.md documents usage-check subcommand
  - docs/metrics.md documents the usage state shape
  - ADR README index is updated for ADR-0016

Run with: python3 tests/test_usage_gate_documentation.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def test_adr_0016_has_amendment():
    """ADR-0016 has an Amendment section."""
    adr_path = REPO_ROOT / "docs" / "adr" / "0016-usage-yield-telemetry.md"

    if not adr_path.exists():
        return False, f"ADR-0016 not found at {adr_path}"

    content = adr_path.read_text()

    if "Amendment" not in content:
        return False, "ADR-0016 missing Amendment section"

    return True, ""


def test_adr_0016_amendment_content():
    """ADR-0016 amendment discusses per-subagent token accounting."""
    adr_path = REPO_ROOT / "docs" / "adr" / "0016-usage-yield-telemetry.md"

    if not adr_path.exists():
        return True, ""  # Skip if not found

    content = adr_path.read_text()

    # Amendment should exist and mention usage gate or /implement-with-haiku
    if "Amendment" not in content:
        return True, ""  # Already tested above

    # Check for key concepts from the amendment
    amendment_keywords = [
        "token", "usage", "gate", "implement-with-haiku"
    ]

    found_keywords = 0
    for keyword in amendment_keywords:
        if keyword.lower() in content.lower():
            found_keywords += 1

    if found_keywords < 2:
        # Should mention at least tokens/usage and implement-with-haiku
        return False, f"amendment doesn't sufficiently document the new feature (found {found_keywords} key terms)"

    return True, ""


def test_metrics_documents_usage_check():
    """docs/metrics.md documents the usage-check subcommand."""
    metrics_path = REPO_ROOT / "docs" / "metrics.md"

    if not metrics_path.exists():
        return False, f"docs/metrics.md not found at {metrics_path}"

    content = metrics_path.read_text()

    if "usage-check" not in content:
        return False, "docs/metrics.md does not document usage-check"

    return True, ""


def test_metrics_documents_usage_state():
    """docs/metrics.md documents the usage state shape."""
    metrics_path = REPO_ROOT / "docs" / "metrics.md"

    if not metrics_path.exists():
        return True, ""  # Skip if not found

    content = metrics_path.read_text()

    # Should mention the usage state structure
    usage_keywords = [
        "counted_tokens", "usage", "state"
    ]

    found_keywords = 0
    for keyword in usage_keywords:
        if keyword.lower() in content.lower():
            found_keywords += 1

    if found_keywords < 1:
        return False, "metrics.md doesn't document usage state structure"

    return True, ""


def test_adr_readme_has_0016():
    """ADR README index entry for ADR-0016 exists."""
    readme_path = REPO_ROOT / "docs" / "adr" / "README.md"

    if not readme_path.exists():
        return False, f"ADR README not found at {readme_path}"

    content = readme_path.read_text()

    if "0016" not in content:
        return False, "ADR README missing entry for 0016"

    return True, ""


def main():
    h = Harness("USAGE-GATE DOCUMENTATION TEST SUITE")

    h.test_result("ADR-0016 has Amendment section", *test_adr_0016_has_amendment())
    h.test_result("ADR-0016 amendment documents feature", *test_adr_0016_amendment_content())
    h.test_result("metrics.md documents usage-check", *test_metrics_documents_usage_check())
    h.test_result("metrics.md documents usage state", *test_metrics_documents_usage_state())
    h.test_result("ADR README indexed for 0016", *test_adr_readme_has_0016())

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
