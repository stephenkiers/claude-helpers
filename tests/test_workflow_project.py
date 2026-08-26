#!/usr/bin/env python3
"""
Test suite for project detection (project.py).

Covers: repo identity, local-plan-mode detection, current gh user.

Run with: python3 tests/test_workflow_project.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import workflow.project as project_module
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW PROJECT TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Project module provides core functions")

    test_result(
        "project module imports successfully",
        project_module is not None
    )

    test_result(
        "project module has is_local_plan_mode",
        hasattr(project_module, "is_local_plan_mode")
    )

    print()
    print("[Section 2] Project functions callable")

    if hasattr(project_module, "is_local_plan_mode"):
        func = getattr(project_module, "is_local_plan_mode")
        test_result(
            "is_local_plan_mode is callable",
            callable(func)
        )

    print()
    print("[Section 3] Project functions return expected types")

    if hasattr(project_module, "is_local_plan_mode"):
        result = project_module.is_local_plan_mode()
        test_result(
            "is_local_plan_mode returns bool or Unknown",
            isinstance(result, (bool, Unknown))
        )

    print()
    h.summarize_and_exit()
