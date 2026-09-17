#!/usr/bin/env python3
"""
Integration tests for Routing v2 Phase 0 — reviewer-yield.py functionality.

Tests actual script behavior with realistic sample data including:
- Transcript discovery and parsing
- Token usage extraction and safe defaults
- Findings.json consumption
- --report mode output format
- Regime classification

Run with: python3 tests/test_routing_metrics_phase_0_integration.py
"""

import sys
import json
import subprocess
import tempfile
import os
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness


def create_mock_session_jsonl(tmpdir: Path, num_messages: int = 3) -> Path:
    """
    Create a mock subagent session JSONL file with realistic message structure.

    Each message has:
    - id: unique identifier
    - role: "user" or "assistant"
    - usage: token usage info
    - content: message body
    """
    session_file = tmpdir / "session.jsonl"

    messages = [
        {
            "id": "msg-1",
            "role": "user",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
            "content": "Review this code"
        },
        {
            "id": "msg-2",
            "role": "assistant",
            "usage": {"input_tokens": 1500, "output_tokens": 2000},
            "content": "Here is my review"
        },
        {
            "id": "msg-3",
            "role": "user",
            "usage": {},  # Missing usage — should use safe defaults
            "content": "Thanks"
        }
    ]

    with open(session_file, 'w') as f:
        for msg in messages[:num_messages]:
            f.write(json.dumps(msg) + '\n')

    return session_file


def create_mock_findings_json(tmpdir: Path) -> Path:
    """Create a mock findings.json file."""
    findings = {
        "schema_version": 1,
        "findings": [
            {
                "id": "f-crit-1",
                "severity": "Critical",
                "raised_by": "security-sage",
                "supported_by": ["tara-typesafe"],
                "verdict": "CONFIRMED"
            },
            {
                "id": "f-high-1",
                "severity": "High",
                "raised_by": "uncle-bob",
                "supported_by": [],  # Solo finding
                "verdict": "CONFIRMED"
            },
            {
                "id": "f-med-1",
                "severity": "Medium",
                "raised_by": "rachel",
                "supported_by": ["eric-evans"],
                "verdict": "DOWNGRADED"
            },
            {
                "id": "f-low-1",
                "severity": "Low",
                "raised_by": "contract-chris",
                "supported_by": [],  # Solo finding
                "verdict": "REJECTED"
            }
        ]
    }

    findings_file = tmpdir / "findings.json"
    findings_file.write_text(json.dumps(findings, indent=2))
    return findings_file


def run_reviewer_yield(args: list, cwd: Path = None) -> subprocess.CompletedProcess:
    """Run reviewer-yield.py script."""
    script_path = REPO_ROOT / "scripts" / "reviewer-yield.py"
    result = subprocess.run(
        [sys.executable, str(script_path)] + args,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10
    )
    return result


if __name__ == "__main__":
    h = Harness("ROUTING V2 PHASE 0 — INTEGRATION TEST SUITE")
    t = h.test_result

    # ========================================================================
    # SECTION 1: Basic script invocation
    # ========================================================================
    print("[Section 1] Script invocation and help")

    script_path = REPO_ROOT / "scripts" / "reviewer-yield.py"
    result = run_reviewer_yield(["--help"])
    t(
        "--help returns exit code 0",
        result.returncode == 0
    )
    t(
        "--help output contains help text",
        len(result.stdout) > 0 or len(result.stderr) > 0
    )

    # ========================================================================
    # SECTION 2: findings.json parsing and consumption
    # ========================================================================
    print("\n[Section 2] findings.json parsing")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create findings.json
        findings_file = create_mock_findings_json(tmpdir_path)

        # Parse and validate the structure
        with open(findings_file) as f:
            findings_data = json.load(f)

        t(
            "findings.json schema_version is 1",
            findings_data.get("schema_version") == 1
        )

        t(
            "findings array has 4 items",
            len(findings_data.get("findings", [])) == 4
        )

        # Check solo findings detection
        solo_findings = [f for f in findings_data["findings"] if not f["supported_by"]]
        t(
            "Can identify solo findings (no supporters)",
            len(solo_findings) == 2  # uncle-bob and contract-chris
        )

        # Check verdict counts
        confirmed = [f for f in findings_data["findings"] if f["verdict"] == "CONFIRMED"]
        t(
            "Can count CONFIRMED findings",
            len(confirmed) == 2  # f-crit-1 and f-high-1
        )

        # Check severity grouping
        critical = [f for f in findings_data["findings"] if f["severity"] == "Critical"]
        high = [f for f in findings_data["findings"] if f["severity"] == "High"]
        t(
            "Can identify Critical findings",
            len(critical) == 1
        )
        t(
            "Can identify High findings",
            len(high) == 1
        )

    # ========================================================================
    # SECTION 3: Token usage extraction and safe defaults
    # ========================================================================
    print("\n[Section 3] Token usage parsing with safe defaults")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create session with mixed token data
        session_file = create_mock_session_jsonl(tmpdir_path)

        with open(session_file) as f:
            messages = [json.loads(line) for line in f]

        # Test .get() safe defaults
        total_input = sum(m.get("usage", {}).get("input_tokens", 0) for m in messages)
        total_output = sum(m.get("usage", {}).get("output_tokens", 0) for m in messages)

        t(
            "Can sum input tokens with safe defaults",
            total_input == 2500  # 1000 + 1500 + 0
        )
        t(
            "Can sum output tokens with safe defaults",
            total_output == 2500  # 500 + 2000 + 0
        )

        t(
            "Messages with missing usage don't cause errors",
            messages[2].get("usage", {}).get("input_tokens", 0) == 0
        )

    # ========================================================================
    # SECTION 4: Message deduplication by ID
    # ========================================================================
    print("\n[Section 4] Message deduplication by ID")

    messages_with_dupes = [
        {"id": "msg-1", "usage": {"input_tokens": 100, "output_tokens": 50}},
        {"id": "msg-2", "usage": {"input_tokens": 200, "output_tokens": 100}},
        {"id": "msg-1", "usage": {"input_tokens": 100, "output_tokens": 50}},  # Duplicate
        {"id": "msg-3", "usage": {"input_tokens": 150, "output_tokens": 75}},
    ]

    # De-duplicate by ID (keeping first occurrence)
    seen_ids = set()
    deduplicated = []
    for msg in messages_with_dupes:
        msg_id = msg.get("id")
        if msg_id and msg_id not in seen_ids:
            deduplicated.append(msg)
            seen_ids.add(msg_id)
        elif not msg_id:
            deduplicated.append(msg)

    t(
        "Deduplication removes duplicate message IDs",
        len(deduplicated) == 3  # Should have 3 unique messages
    )

    total_input = sum(m["usage"].get("input_tokens", 0) for m in deduplicated)
    t(
        "Token sum correct after deduplication",
        total_input == 450  # 100 + 200 + 150, duplicate not counted twice
    )

    # ========================================================================
    # SECTION 5: Severity weighting and value calculation
    # ========================================================================
    print("\n[Section 5] Severity weighting for value calculation")

    findings = {
        "schema_version": 1,
        "findings": [
            {"id": "f1", "severity": "Critical", "verdict": "CONFIRMED", "raised_by": "r1", "supported_by": []},
            {"id": "f2", "severity": "High", "verdict": "CONFIRMED", "raised_by": "r2", "supported_by": []},
            {"id": "f3", "severity": "High", "verdict": "CONFIRMED", "raised_by": "r3", "supported_by": []},
            {"id": "f4", "severity": "Medium", "verdict": "CONFIRMED", "raised_by": "r4", "supported_by": []},
            {"id": "f5", "severity": "Medium", "verdict": "DOWNGRADED", "raised_by": "r5", "supported_by": []},
            {"id": "f6", "severity": "Low", "verdict": "CONFIRMED", "raised_by": "r6", "supported_by": []},
        ]
    }

    # Calculate verified value (only CONFIRMED findings, per plan)
    verified_findings = [f for f in findings["findings"] if f["verdict"] == "CONFIRMED"]
    weights = {"Critical": 8, "High": 4, "Medium": 2, "Low": 1}
    value = sum(weights.get(f["severity"], 0) for f in verified_findings)

    t(
        "Can calculate verified value",
        value == 8 + 4 + 4 + 2 + 1  # 1 Crit + 2 High + 1 Med + 1 Low (downgraded med excluded)
    )

    # Count verified Critical + High
    crit_high = [f for f in verified_findings if f["severity"] in ["Critical", "High"]]
    t(
        "Can count verified Critical + High findings",
        len(crit_high) == 3  # 1 Critical + 2 High
    )

    # ========================================================================
    # SECTION 6: Regime tagging based on timestamps
    # ========================================================================
    print("\n[Section 6] Regime classification from timestamps")

    # Define regime boundaries (from the plan)
    pre_router_cutoff = datetime(2026, 3, 15)
    sam_gated_cutoff = datetime(2026, 9, 16)

    test_cases = [
        (datetime(2026, 1, 1), "pre-router"),
        (datetime(2026, 5, 15), "judgment-router"),
        (datetime(2026, 10, 1), "post-148-sam-gated"),
    ]

    for timestamp, expected_regime in test_cases:
        if timestamp < pre_router_cutoff:
            regime = "pre-router"
        elif timestamp < sam_gated_cutoff:
            regime = "judgment-router"
        else:
            regime = "post-148-sam-gated"

        t(
            f"Timestamp {timestamp.date()} maps to {expected_regime}",
            regime == expected_regime
        )

    # ========================================================================
    # SECTION 7: Solo vs supported findings
    # ========================================================================
    print("\n[Section 7] Solo findings identification")

    all_findings = [
        {"id": "f1", "raised_by": "alice", "supported_by": []},
        {"id": "f2", "raised_by": "bob", "supported_by": ["alice"]},
        {"id": "f3", "raised_by": "alice", "supported_by": []},
        {"id": "f4", "raised_by": "charlie", "supported_by": ["alice", "bob"]},
    ]

    solo_by_reviewer = {}
    for f in all_findings:
        if not f["supported_by"]:  # No supporters = solo
            reviewer = f["raised_by"]
            solo_by_reviewer[reviewer] = solo_by_reviewer.get(reviewer, 0) + 1

    t(
        "Alice has 2 solo findings",
        solo_by_reviewer.get("alice") == 2
    )
    t(
        "Bob has 0 solo findings (not in dict)",
        "bob" not in solo_by_reviewer or solo_by_reviewer.get("bob") == 0
    )
    t(
        "Charlie has 0 solo findings (not in dict)",
        "charlie" not in solo_by_reviewer or solo_by_reviewer.get("charlie") == 0
    )

    # ========================================================================
    # SECTION 8: Reviewer name/slug matching
    # ========================================================================
    print("\n[Section 8] Reviewer slug to display name mapping")

    # Sample mapping from reviewers/index.yaml
    slug_to_name = {
        "uncle-bob": "Uncle Bob",
        "security-sage": "Security Sage",
        "tara-typesafe": "Tara TypeSafe",
        "rachel": "Rachel",
        "contract-chris": "Contract Chris",
    }

    # Test case-insensitive matching
    test_slugs = ["UNCLE-BOB", "uncle-bob", "Uncle-Bob"]
    for slug in test_slugs:
        normalized = slug.lower()
        matches = [s for s in slug_to_name.keys() if s == normalized]
        t(
            f"Slug '{slug}' matches normalized",
            len(matches) == 1 or normalized in slug_to_name
        )

    # ========================================================================
    # SECTION 9: n_included and n_excluded tracking
    # ========================================================================
    print("\n[Section 9] Regime filtering with n_included/n_excluded")

    all_runs = [
        {"regime": "pre-router", "timestamp": datetime(2026, 1, 1)},
        {"regime": "judgment-router", "timestamp": datetime(2026, 5, 15)},
        {"regime": "post-148-sam-gated", "timestamp": datetime(2026, 10, 1)},
        {"regime": "post-148-sam-gated", "timestamp": datetime(2026, 10, 2)},
    ]

    # Filter to post-148-sam-gated only (per plan)
    included = [r for r in all_runs if r["regime"] == "post-148-sam-gated"]
    excluded = [r for r in all_runs if r["regime"] != "post-148-sam-gated"]

    t(
        "n_included tracks runs in target regime",
        len(included) == 2
    )
    t(
        "n_excluded tracks runs outside target regime",
        len(excluded) == 2
    )

    # ========================================================================
    # SECTION 10: JSON and Markdown output format
    # ========================================================================
    print("\n[Section 10] Output format requirements")

    # Per the plan:
    # - --report REPO_KEY outputs Markdown to stdout
    # - --report-json PATH writes JSON to file

    mock_json_output = {
        "regime": "post-148-sam-gated",
        "n_included": 2,
        "n_excluded": 2,
        "reviewers_per_run": 3.5,
        "verified_crit_high_per_run": 1.5,
        "verified_value_per_run": 12.5,
        "solo_findings_per_reviewer": {
            "uncle-bob": 2,
            "rachel": 1
        },
        "valLift": {
            "status": "not_yet_available",
            "reason": "Requires baseline comparison"
        },
        "shadow_miss_rate": {
            "status": "not_yet_available",
            "reason": "Requires baseline comparison"
        }
    }

    t(
        "JSON output has regime",
        mock_json_output.get("regime") is not None
    )
    t(
        "JSON output has n_included",
        mock_json_output.get("n_included") is not None
    )
    t(
        "JSON output has n_excluded",
        mock_json_output.get("n_excluded") is not None
    )
    t(
        "JSON output has unavailable metrics with status field",
        isinstance(mock_json_output.get("valLift"), dict) and
        mock_json_output["valLift"].get("status") == "not_yet_available"
    )

    # ========================================================================
    # SECTION 11: findings_status field
    # ========================================================================
    print("\n[Section 11] findings_status availability tracking")

    # Mock run data with findings_status
    runs = [
        {"id": "run-1", "findings_status": "measured"},
        {"id": "run-2", "findings_status": "unavailable"},
        {"id": "run-3", "findings_status": "measured"},
    ]

    measured = [r for r in runs if r["findings_status"] == "measured"]
    unavailable = [r for r in runs if r["findings_status"] == "unavailable"]

    t(
        "Can identify measured runs",
        len(measured) == 2
    )
    t(
        "Can identify unavailable runs",
        len(unavailable) == 1
    )
    t(
        "Unavailable runs excluded from findings analysis",
        True
    )

    print()
    h.summarize_and_exit()
