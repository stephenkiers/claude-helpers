#!/usr/bin/env python3
"""
Spec-blind tests for scripts/plan-checkpoint-tiers.py

These tests are written from the plan specification alone (Step 4 Tier script section),
without reading the implementation. They verify the tier-assignment rule:

- Q: the brief's `Type` is `disagreement`, or any `protested` (or, after #294, a Pat veto).
- D: resolved, at least one other expert `agreed`, and no `could-go-other-way` flag.
- W: every other resolved item.

Run with: python3 tests/test_plan_checkpoint_tiers.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

TIER_SCRIPT = REPO_ROOT / "scripts" / "plan-checkpoint-tiers.py"


def run_tier_script(decisions_path: str, fragment=False, check_ledger=False) -> tuple:
    """Run the tier script and return (exit_code, stdout, stderr).

    Args:
        decisions_path: path to decisions.md file
        fragment: if True, use --fragment flag instead of default output
        check_ledger: if True, use --check-ledger flag instead of default output
    """
    cmd = [
        sys.executable,
        str(TIER_SCRIPT),
        "--decisions", decisions_path,
    ]
    if fragment:
        cmd.append("--fragment")
    if check_ledger:
        cmd.append("--check-ledger")
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def create_reaction_file(path: str, reactions: dict) -> None:
    """Create a reaction file with the given reactions dict and 'complete': true sentinel."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    payload = {**reactions, "complete": True}
    Path(path).write_text(json.dumps(payload))


def create_decisions_file(path: str, seated: list, expected_reactions: list, briefs: dict = None) -> None:
    """Create a decisions.md with a Checkpoint ledger block."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    ledger = {
        "seated": seated,
        "expected_reactions": expected_reactions,
    }
    if briefs:
        ledger["briefs"] = briefs
    # Build the decisions file with the fenced JSON block
    # The fence parser in the script expects ``` on its own line
    lines = ["# Decisions", "", "## Checkpoint ledger", "```", json.dumps(ledger), "```", "",
             "## Decided", "", "## Worth a look", "", "## Questions for you"]
    content = "\n".join(lines)
    Path(path).write_text(content)


def main():
    h = Harness("PLAN-CHECKPOINT-TIERS SPEC-BLIND TEST SUITE")

    # Test 1: Disagreement brief always becomes Q (even with no protests)
    print("[Test 1] Disagreement brief with no protests -> Q")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "disagreement", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "agreed": False}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if not stdout:
                h.test_result("Disagreement brief -> Q", False, f"exit={exit_code}, stderr={stderr[:200]}")
            else:
                tiers = json.loads(stdout)
                h.test_result("Disagreement brief -> Q",
                             exit_code == 0 and tiers.get("B1") == "question",
                             f"exit={exit_code}, tiers={tiers}")
        except json.JSONDecodeError as e:
            h.test_result("Disagreement brief -> Q", False, f"JSON error: {e}, stdout={stdout[:200]}")

    # Test 2: Protested brief becomes Q
    print("[Test 2] Protested brief -> Q")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "agreed": True, "protested": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if not stdout:
                h.test_result("Protested brief -> Q", False, f"exit={exit_code}, stderr={stderr[:200]}")
            else:
                tiers = json.loads(stdout)
                h.test_result("Protested brief -> Q",
                             exit_code == 0 and tiers.get("B1") == "question",
                             f"exit={exit_code}, B1 tier={tiers.get('B1')}")
        except json.JSONDecodeError as e:
            h.test_result("Protested brief -> Q", False, f"JSON error: {e}")

    # Test 3: Resolved + agreed + no could-go-other-way -> D
    print("[Test 3] Resolved + agreed + no could-go-other-way -> D")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "agreed": False}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if not stdout:
                h.test_result("Resolved + agreed + no could-go-other-way -> D", False, f"stderr={stderr[:200]}")
            else:
                tiers = json.loads(stdout)
                h.test_result("Resolved + agreed + no could-go-other-way -> D",
                             exit_code == 0 and tiers.get("B1") == "decided",
                             f"exit={exit_code}, B1 tier={tiers.get('B1')}")
        except json.JSONDecodeError as e:
            h.test_result("Resolved + agreed + no could-go-other-way -> D", False, f"JSON error: {e}")

    # Test 4: Resolved with could-go-other-way flag -> W
    print("[Test 4] Resolved + agreed + could-go-other-way -> W")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "agreed": False, "could-go-other-way": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if not stdout:
                h.test_result("Resolved + agreed + could-go-other-way -> W", False, f"stderr={stderr[:200]}")
            else:
                tiers = json.loads(stdout)
                h.test_result("Resolved + agreed + could-go-other-way -> W",
                             exit_code == 0 and tiers.get("B1") == "worth-a-look",
                             f"exit={exit_code}, B1 tier={tiers.get('B1')}")
        except json.JSONDecodeError as e:
            h.test_result("Resolved + agreed + could-go-other-way -> W", False, f"JSON error: {e}")

    # Test 5: Resolved with no other expert agreement -> W
    print("[Test 5] Resolved with no other agreement -> W")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"no-view": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if not stdout:
                h.test_result("Resolved with no other agreement -> W", False, f"stderr={stderr[:200]}")
            else:
                tiers = json.loads(stdout)
                h.test_result("Resolved with no other agreement -> W",
                             exit_code == 0 and tiers.get("B1") == "worth-a-look",
                             f"exit={exit_code}, B1 tier={tiers.get('B1')}")
        except json.JSONDecodeError as e:
            h.test_result("Resolved with no other agreement -> W", False, f"JSON error: {e}")

    # Test 6: Missing expected reaction file -> non-zero exit
    print("[Test 6] Missing expected reaction file -> exit non-zero")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        # Create only alice's file, not bob's
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {"B1": {"resolved": True}})

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        h.test_result("Missing expected reaction file -> exit non-zero",
                     exit_code != 0,
                     f"exit={exit_code}")

    # Test 7: Reaction file without 'complete' sentinel -> non-zero exit
    print("[Test 7] Reaction file without 'complete' sentinel -> exit non-zero")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        # Create alice's file without the sentinel
        Path(reactions_dir).mkdir(parents=True, exist_ok=True)
        Path(reactions_dir / "alice-pre-show-r1.json").write_text(json.dumps({"B1": {"resolved": True}}), encoding="utf-8")
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {"B1": {"agreed": True}})

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        h.test_result("Reaction file without 'complete' sentinel -> exit non-zero",
                     exit_code != 0,
                     f"exit={exit_code}")

    # Test 8: Missing brief_type in ledger when brief has reactions -> non-zero exit
    print("[Test 8] Missing brief_type in ledger -> exit non-zero")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {}  # Missing brief_type for B1

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        # Create reaction files that reference B1
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {"B1": {"resolved": True}})
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {"B1": {"agreed": True}})

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        h.test_result("Missing brief_type in ledger -> exit non-zero",
                     exit_code != 0 and "brief_type" in stderr.lower(),
                     f"exit={exit_code}, stderr={stderr[:200]}")

    # Test 9: Missing decisions.md -> non-zero exit
    print("[Test 9] Missing decisions.md -> exit non-zero")
    with tempfile.TemporaryDirectory() as tmp:
        nonexistent_file = str(Path(tmp) / "missing.md")
        exit_code, stdout, stderr = run_tier_script(nonexistent_file)
        h.test_result("Missing decisions.md -> exit non-zero", exit_code != 0, f"exit={exit_code}")

    # Test 10: No --decisions argument -> exit non-zero
    print("[Test 10] No --decisions argument -> exit non-zero")
    cmd = [sys.executable, str(TIER_SCRIPT)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    h.test_result("No --decisions argument -> exit non-zero",
                 result.returncode != 0,
                 f"exit={result.returncode}")

    # Test 11: Edge case - empty seated list (should still work if expected_reactions is also empty)
    print("[Test 11] Empty seated list -> exit 0")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"

        seated = []
        expected_reactions = []
        briefs = {}

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if stdout:
                tiers = json.loads(stdout)
                h.test_result("Empty seated list -> exit 0",
                             exit_code == 0 and tiers == {},
                             f"exit={exit_code}, tiers={tiers}")
            else:
                h.test_result("Empty seated list -> exit 0", exit_code == 0, f"exit={exit_code}, stderr={stderr[:100]}")
        except json.JSONDecodeError as e:
            h.test_result("Empty seated list -> exit 0", False, f"JSON error: {e}")

    # Test 12: Edge case - relative paths in expected_reactions
    print("[Test 12] Relative paths in expected_reactions -> works")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        # Use relative path (not absolute)
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {"B1": {"agreed": True}})

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if stdout:
                tiers = json.loads(stdout)
                h.test_result("Relative paths -> works",
                             exit_code == 0 and tiers.get("B1") == "worth-a-look",
                             f"exit={exit_code}, tiers={tiers}")
            else:
                h.test_result("Relative paths -> works", False, f"exit={exit_code}, stderr={stderr[:100]}")
        except json.JSONDecodeError as e:
            h.test_result("Relative paths -> works", False, f"JSON error: {e}")

    # Test 13: Edge case - mixed protest and resolve (protest wins -> Q)
    print("[Test 13] Mixed protest and resolve -> Q")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        # Alice protested even though she resolved
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "protested": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if stdout:
                tiers = json.loads(stdout)
                h.test_result("Mixed protest and resolve -> Q",
                             exit_code == 0 and tiers.get("B1") == "question",
                             f"exit={exit_code}, tiers={tiers}")
            else:
                h.test_result("Mixed protest and resolve -> Q", False, f"exit={exit_code}, stderr={stderr[:100]}")
        except json.JSONDecodeError as e:
            h.test_result("Mixed protest and resolve -> Q", False, f"JSON error: {e}")

    # ========== --fragment tests ==========

    # Test 14: --fragment outputs handle-keyed JSON with all four fields
    print("[Test 14] --fragment outputs handle-keyed JSON with all four fields")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B4": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B4": {"resolved": True, "agreed": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B4": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), fragment=True)
        try:
            if not stdout:
                h.test_result("--fragment outputs with all fields", False, f"stderr={stderr[:200]}")
            else:
                fragment = json.loads(stdout)
                # Should be keyed by handle (D1 for a decided brief)
                has_handle_key = any(k[0] in "DWQ" and k[1:].isdigit() for k in fragment.keys())
                has_all_fields = all(
                    all(f in v for f in ["tier", "brief_id", "brief_type", "evidence"])
                    for v in (fragment.values() if has_handle_key else [])
                )
                h.test_result("--fragment outputs with all fields",
                             exit_code == 0 and has_handle_key and has_all_fields,
                             f"exit={exit_code}, fragment={stdout[:200]}")
        except json.JSONDecodeError as e:
            h.test_result("--fragment outputs with all fields", False, f"JSON error: {e}")

    # Test 15: --fragment numbering follows tier order (D, W, Q) with brief_id ordering within tier
    print("[Test 15] --fragment numbering follows tier and brief_id order")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        # Create briefs: B4 decided, B1 worth-a-look, B2/B3 questions
        briefs = {
            "B4": {"brief_type": "question", "raiser": "alice"},
            "B1": {"brief_type": "question", "raiser": "alice"},
            "B2": {"brief_type": "question", "raiser": "alice"},
            "B3": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        # B4 -> decided (D1), B1 -> worth-a-look (W1), B2/B3 -> questions (Q1/Q2)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B4": {"resolved": True, "agreed": True},
            "B1": {"resolved": True},
            "B2": {"protested": True},
            "B3": {"protested": True},
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B4": {"agreed": True},
            "B1": {"no-view": True},
            "B2": {"no-view": True},
            "B3": {"no-view": True},
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), fragment=True)
        try:
            if not stdout:
                h.test_result("--fragment numbering order", False, f"stderr={stderr[:200]}")
            else:
                fragment = json.loads(stdout)
                # Expected: D1->B4, W1->B1, Q1->B2, Q2->B3
                expected_handles = {"D1", "W1", "Q1", "Q2"}
                actual_handles = set(fragment.keys())
                h.test_result("--fragment numbering order",
                             exit_code == 0 and actual_handles == expected_handles,
                             f"exit={exit_code}, expected={expected_handles}, actual={actual_handles}")
        except json.JSONDecodeError as e:
            h.test_result("--fragment numbering order", False, f"JSON error: {e}")

    # Test 16: --fragment with B10 sorts after B2 (numeric ordering)
    print("[Test 16] --fragment numeric ordering (B2 before B10)")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
        ]
        briefs = {
            "B2": {"brief_type": "question", "raiser": "alice"},
            "B10": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B2": {"resolved": True},
            "B10": {"resolved": True},
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), fragment=True)
        try:
            if not stdout:
                h.test_result("Numeric ordering B2 before B10", False, f"stderr={stderr[:200]}")
            else:
                fragment = json.loads(stdout)
                # B2 should map to W1, B10 to W2 (since they're both worth-a-look)
                expected = {"W1": {"brief_id": "B2"}, "W2": {"brief_id": "B10"}}
                actual_brief_ids = {k: fragment[k].get("brief_id") for k in fragment.keys()}
                h.test_result("Numeric ordering B2 before B10",
                             exit_code == 0 and actual_brief_ids.get("W1") == "B2" and actual_brief_ids.get("W2") == "B10",
                             f"exit={exit_code}, fragment={actual_brief_ids}")
        except (json.JSONDecodeError, TypeError) as e:
            h.test_result("Numeric ordering B2 before B10", False, f"Error: {e}")

    # ========== --check-ledger tests ==========

    # Test 17: --check-ledger exits 0 on correct ledger
    print("[Test 17] --check-ledger exits 0 on correct ledger")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "agreed": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        # Add the correct handle entry to the decisions file
        decisions_content = decisions_file.read_text()
        # Insert D1 entry into the ledger
        decisions_content = decisions_content.replace(
            '"briefs": {"B1": {"brief_type": "question", "raiser": "alice"}}',
            '"briefs": {"B1": {"brief_type": "question", "raiser": "alice"}}, "D1": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": []}'
        )
        decisions_file.write_text(decisions_content)

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), check_ledger=True)
        h.test_result("--check-ledger exits 0 on correct ledger",
                     exit_code == 0,
                     f"exit={exit_code}, stderr={stderr[:200]}")

    # Test 18: --check-ledger exits 1 on flat entry shape
    print("[Test 18] --check-ledger exits 1 on flat entry shape")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "agreed": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        # Add a flat entry (string instead of dict)
        decisions_content = decisions_file.read_text()
        decisions_content = decisions_content.replace(
            '"briefs": {"B1": {"brief_type": "question", "raiser": "alice"}}',
            '"briefs": {"B1": {"brief_type": "question", "raiser": "alice"}}, "D1": "decided"'
        )
        decisions_file.write_text(decisions_content)

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), check_ledger=True)
        h.test_result("--check-ledger exits 1 on flat entry",
                     exit_code != 0,
                     f"exit={exit_code}, stderr={stderr[:200]}")

    # Test 19: --check-ledger exits 1 on nested handles
    print("[Test 19] --check-ledger exits 1 on nested handles")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "agreed": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        # Add nested handles
        decisions_content = decisions_file.read_text()
        decisions_content = decisions_content.replace(
            '"briefs": {"B1": {"brief_type": "question", "raiser": "alice"}}',
            '"briefs": {"B1": {"brief_type": "question", "raiser": "alice"}}, "handles": {"D1": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": []}}'
        )
        decisions_file.write_text(decisions_content)

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), check_ledger=True)
        h.test_result("--check-ledger exits 1 on nested handles",
                     exit_code != 0,
                     f"exit={exit_code}, stderr={stderr[:200]}")

    # Test 20: --check-ledger is a pure read (does not modify file)
    print("[Test 20] --check-ledger does not modify the decisions file")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice", "bob"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "agreed": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        # Add a flat entry (which will fail the check)
        decisions_content = decisions_file.read_text()
        decisions_content = decisions_content.replace(
            '"briefs": {"B1": {"brief_type": "question", "raiser": "alice"}}',
            '"briefs": {"B1": {"brief_type": "question", "raiser": "alice"}}, "D1": "decided"'
        )
        decisions_file.write_text(decisions_content)

        # Capture the file content before running --check-ledger
        before_content = decisions_file.read_bytes()

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), check_ledger=True)

        # Capture the file content after running --check-ledger
        after_content = decisions_file.read_bytes()

        h.test_result("--check-ledger does not modify the file",
                     before_content == after_content,
                     f"exit={exit_code}, changed={before_content != after_content}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
