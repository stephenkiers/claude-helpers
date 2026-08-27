#!/usr/bin/env python3
"""
Test suite for project detection (project.py).

Covers: repo identity, local-plan-mode detection, current gh user.

Run with: python3 tests/test_workflow_project.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

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
        with patch("workflow.project.git.repo_view_json") as mock_repo_view:
            mock_repo_view.return_value = {"nameWithOwner": "owner/repo"}
            value, error = project_module.is_local_plan_mode()
            test_result(
                "is_local_plan_mode returns (bool, Optional[Unknown])",
                isinstance(value, bool) and (error is None or isinstance(error, Unknown))
            )
            test_result(
                "is_local_plan_mode returns False when repo view finds a remote",
                value is False and error is None
            )

        with patch("workflow.project.git.repo_view_json") as mock_repo_view:
            # repo_view_json collapses "no remote" and "gh call failed" into
            # the same {} — genuinely ambiguous at this layer, so the
            # fail-closed contract is Unknown, not a guessed True/False.
            mock_repo_view.return_value = {}
            value, error = project_module.is_local_plan_mode()
            test_result(
                "is_local_plan_mode returns Unknown when repo view can't confirm a remote",
                isinstance(error, Unknown)
            )

    print()
    h.summarize_and_exit()
