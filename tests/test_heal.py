#!/usr/bin/env python3
"""Tests for scripts/workflow/heal.py (expert-fleet bounded self-heal harness, #425).

Covers the core heal logic: nomination, probing, recommendations, override schema,
install tool status mapping, and error handling.

Run with: python3 tests/test_heal.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

sys.path.insert(0, str(REPO_ROOT))
from scripts.workflow import heal  # noqa: E402


def make_evidence(**kwargs) -> heal.StopEvidence:
    """Build a StopEvidence with sensible defaults."""
    defaults = {
        "kind": "flow-stop",
        "header": "Flow stop",
        "step": "implement",
        "failures": {},
        "stop_source": "deps",
        "stop_reason_class": None,
        "last_deps_result": None,
        "offers_retry": False,
        "stop_key": "abc123",
    }
    defaults.update(kwargs)
    return heal.StopEvidence(**defaults)


def main():
    h = Harness("heal module tests")

    # ────────────────────────────────────────────────────────────────────────────
    # Section 1: nominate() — Tool nomination and refusals
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 1] nominate() — Tool nomination and refusals")

    # W1 A: failures >= 2 refuses (failure cap)
    evidence_w1_pass = make_evidence(
        failures={"implement": 1},
        last_deps_result="stale",
        stop_source="deps",
    )
    tool, diag = heal.nominate(evidence_w1_pass, heal.TOOLS, {})
    h.test_result(
        "W1 A: failures=1 allows nomination",
        tool is not None,
        f"Got diagnosis: {diag}",
    )

    evidence_w1_fail = make_evidence(failures={"implement": 2})
    tool, diag = heal.nominate(evidence_w1_fail, heal.TOOLS, {})
    h.test_result(
        "W1 A: failures >= 2 refuses with 'Failure limit reached'",
        tool is None and "Failure limit reached" in (diag or ""),
        f"tool={tool}, diag={diag}",
    )

    evidence_w1_fail_3 = make_evidence(failures={"implement": 5})
    tool, diag = heal.nominate(evidence_w1_fail_3, heal.TOOLS, {})
    h.test_result(
        "W1 A: failures = 5 also refuses",
        tool is None and "Failure limit reached" in (diag or ""),
        f"diag={diag}",
    )

    # manifest-changed refusal
    evidence_manifest = make_evidence(
        last_deps_result="manifest-changed",
        stop_source="deps",
    )
    tool, diag = heal.nominate(evidence_manifest, heal.TOOLS, {})
    h.test_result(
        "manifest-changed refusal includes 'approval'",
        tool is None and "approval" in (diag or "").lower(),
        f"diag={diag}",
    )

    # permission-refusal marker never nominates
    evidence_perm = make_evidence(
        stop_reason_class="permission-refusal",
        stop_source="stop_reason",
    )
    tool, diag = heal.nominate(evidence_perm, heal.TOOLS, {})
    h.test_result(
        "permission-refusal marker never nominates",
        tool is None and "Permission refusal" in (diag or ""),
        f"diag={diag}",
    )

    # Install tool nomination (missing-deps / stale)
    evidence_install = make_evidence(
        last_deps_result="stale",
        stop_source="deps",
    )
    tool, diag = heal.nominate(evidence_install, heal.TOOLS, {})
    h.test_result(
        "Nominates install tool when last_deps_result='stale'",
        tool is not None and tool.name == "install",
        f"tool={tool}, diag={diag}",
    )

    # Tool disabled in override
    evidence_install2 = make_evidence(
        last_deps_result="stale",
        stop_source="deps",
    )
    override_disabled = {"install": {"enabled": False}}
    tool, diag = heal.nominate(evidence_install2, heal.TOOLS, override_disabled)
    h.test_result(
        "Disabled tool in override is skipped",
        tool is None,
        f"tool={tool}",
    )

    # No evidence
    tool, diag = heal.nominate(None, heal.TOOLS, {})
    h.test_result(
        "No evidence returns (None, None)",
        tool is None and diag is None,
    )

    # Unknown stop kind
    evidence_bad_kind = make_evidence(kind="unknown-stop")
    tool, diag = heal.nominate(evidence_bad_kind, heal.TOOLS, {})
    h.test_result(
        "Unknown stop kind returns diagnosis",
        tool is None and diag is not None,
        f"diag={diag}",
    )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 2: after_heal() — Recommendation logic
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 2] after_heal() — Recommendation logic")

    evidence_with_retry = make_evidence(
        offers_retry=True,
        last_deps_result=None,
    )
    result_changed = heal.HealResult(
        tool="install",
        status="healed",
        reason="currency-installed",
        changed=True,
        recommendation="take-over",
        stop_key="abc",
    )
    rec = heal.after_heal(result_changed, evidence_with_retry)
    h.test_result(
        "after_heal: retry-recommended when offers_retry, changed, no manifest-changed",
        rec == "retry-recommended",
        f"Got {rec}",
    )

    evidence_no_retry = make_evidence(offers_retry=False)
    result_changed2 = heal.HealResult(
        tool="install",
        status="healed",
        reason="currency-installed",
        changed=True,
        recommendation="take-over",
        stop_key="abc",
    )
    rec = heal.after_heal(result_changed2, evidence_no_retry)
    h.test_result(
        "after_heal: take-over when offers_retry=False",
        rec == "take-over",
        f"Got {rec}",
    )

    evidence_manifest_changed = make_evidence(
        offers_retry=True,
        last_deps_result="manifest-changed",
    )
    result_changed3 = heal.HealResult(
        tool="install",
        status="healed",
        reason="currency-installed",
        changed=True,
        recommendation="take-over",
        stop_key="abc",
    )
    rec = heal.after_heal(result_changed3, evidence_manifest_changed)
    h.test_result(
        "after_heal: take-over when last_deps_result='manifest-changed'",
        rec == "take-over",
        f"Got {rec}",
    )

    result_no_change = heal.HealResult(
        tool="install",
        status="no-op",
        reason="currency-current",
        changed=False,
        recommendation="take-over",
        stop_key="abc",
    )
    rec = heal.after_heal(result_no_change, evidence_with_retry)
    h.test_result(
        "after_heal: take-over when tool didn't change anything",
        rec == "take-over",
        f"Got {rec}",
    )

    result_refused = heal.HealResult(
        tool="install",
        status="refused",
        reason="awaiting-approval",
        changed=False,
        recommendation="take-over",
        stop_key="abc",
    )
    rec = heal.after_heal(result_refused, evidence_with_retry)
    h.test_result(
        "after_heal: take-over for refused tools",
        rec == "take-over",
        f"Got {rec}",
    )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 3: Override schema validation and fail-closed behavior
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 3] Override schema validation (fail-closed)")

    with tempfile.TemporaryDirectory() as tmp:
        main_wt = Path(tmp) / "main"
        main_wt.mkdir()

        # Valid override
        project_yaml = main_wt / ".claude" / "project.yaml"
        project_yaml.parent.mkdir(parents=True)
        project_yaml.write_text(
            """
fleet:
  heal:
    tools:
      install:
        enabled: true
        timeout_secs: 600
"""
        )
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "Valid override reads successfully",
            override.get("install", {}).get("timeout_secs") == 600,
            f"Got {override}",
        )

        # Unknown key (fail-closed)
        project_yaml.write_text(
            """
fleet:
  heal:
    tools:
      install:
        enabled: true
        unknown_param: 999
"""
        )
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "Unknown param fails closed with a diagnosis",
            odiag is not None,
            f"Got {override}",
        )

        # Invalid enabled type (fail-closed)
        project_yaml.write_text(
            """
fleet:
  heal:
    tools:
      install:
        enabled: "yes"
"""
        )
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "Invalid enabled type returns empty override (fail-closed)",
            override == {} and odiag is not None,
            f"Got {override}",
        )

        # Out-of-range timeout (fail-closed)
        project_yaml.write_text(
            """
fleet:
  heal:
    tools:
      install:
        timeout_secs: 0
"""
        )
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "timeout_secs <= 0 returns empty override (fail-closed)",
            override == {} and odiag is not None,
            f"Got {override}",
        )

        # Negative timeout (fail-closed)
        project_yaml.write_text(
            """
fleet:
  heal:
    tools:
      install:
        timeout_secs: -100
"""
        )
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "Negative timeout_secs returns empty override (fail-closed)",
            override == {} and odiag is not None,
            f"Got {override}",
        )

        # Missing project.yaml
        project_yaml.unlink()
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "Missing project.yaml returns empty override, no diagnosis",
            override == {} and odiag is None,
            f"Got {override}",
        )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 4: Install tool status mapping
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 4] Install tool status mapping (Steps 3-4)")

    # The install tool is already tested above; here we document the mapping:
    # installed -> healed
    # current -> no-op
    # manifest-changed -> refused
    # ambiguous -> refused
    # skipped -> refused
    # failed -> failed
    # unknown -> failed

    # These are tested implicitly through the mock DepsResult in the integration,
    # but we can at least verify the tool exists and has the right properties.
    h.test_result(
        "Install tool has correct cause",
        heal.INSTALL_TOOL.cause == "missing-deps",
    )
    h.test_result(
        "Install tool has gate='ensure-deps-exception'",
        heal.INSTALL_TOOL.gate == "ensure-deps-exception",
    )
    h.test_result(
        "Install tool has approval='run-first'",
        heal.INSTALL_TOOL.approval == "run-first",
    )
    h.test_result(
        "Install tool has permission='runs-repo-code'",
        heal.INSTALL_TOOL.permission == "runs-repo-code",
    )
    h.test_result(
        "Install tool has no inputs",
        heal.INSTALL_TOOL.inputs == (),
    )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 5: Diagnoses (causes 2-6)
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 5] Diagnoses for non-tool causes (Step 3)")

    h.test_result(
        "DIAGNOSES maps missing-check-config",
        "missing-check-config" in heal.DIAGNOSES,
    )
    h.test_result(
        "DIAGNOSES maps check-source-flip",
        "check-source-flip" in heal.DIAGNOSES,
    )
    h.test_result(
        "DIAGNOSES maps missing-test-db",
        "missing-test-db" in heal.DIAGNOSES,
    )
    h.test_result(
        "DIAGNOSES maps unregistered-agent",
        "unregistered-agent" in heal.DIAGNOSES,
    )
    h.test_result(
        "DIAGNOSES maps malformed-receipt",
        "malformed-receipt" in heal.DIAGNOSES,
    )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 6: Tool registry (frozen tuple, one entry at end of Step 4)
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 6] Tool registry")

    h.test_result(
        "TOOLS is a tuple",
        isinstance(heal.TOOLS, tuple),
    )
    h.test_result(
        "TOOLS contains exactly one tool (install)",
        len(heal.TOOLS) == 1 and heal.TOOLS[0].name == "install",
    )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 7: Edge cases and error handling
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 7] Edge cases and error handling")

    # Missing failures field (should default to 0)
    evidence_no_failures = make_evidence(
        failures={},
        last_deps_result="stale",
        stop_source="deps",
    )
    tool, diag = heal.nominate(evidence_no_failures, heal.TOOLS, {})
    h.test_result(
        "Missing failures[step] counts as 0",
        tool is not None,  # Should be able to nominate
        f"diag={diag}",
    )

    # No stop_source and no deps entry
    evidence_no_source = make_evidence(
        stop_source=None,
        last_deps_result=None,
    )
    tool, diag = heal.nominate(evidence_no_source, heal.TOOLS, {})
    h.test_result(
        "No stop_source and no deps entry returns no evidence",
        tool is None and "No evidence" in (diag or ""),
        f"diag={diag}",
    )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
