#!/usr/bin/env python3
"""
Spec-blind tests for scripts/plan-checkpoint-tiers.py

These tests are written from the plan specification alone (Step 4 Tier script section),
without reading the implementation. They verify the tier-assignment rule:

- Q: the brief's `Type` is `disagreement`, or any `protested` (or, after #294, a Pat veto).
- D: resolved, at least one other expert `agreed`, and no `could-go-other-way` flag.
- W: every other resolved item.

Run with: python3 tests/test_plan_checkpoint_tiers_spec_blind.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

TIER_SCRIPT = REPO_ROOT / "scripts" / "plan-checkpoint-tiers.py"


def run_tier_script(decisions_path: str) -> tuple:
    """Run the tier script and return (exit_code, stdout, stderr)."""
    cmd = [
        sys.executable,
        str(TIER_SCRIPT),
        "--decisions", decisions_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def create_reaction_file(path: str, reactions: dict) -> None:
    """Create a reaction file with the given reactions dict and 'complete': true sentinel."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    payload = {**reactions, "complete": True}
    Path(path).write_text(json.dumps(payload))


def create_decisions_file(path: str, seated: list, expected_reactions: list) -> None:
    """Create a decisions.md with a Checkpoint ledger block."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    ledger = {
        "seated": seated,
        "expected_reactions": expected_reactions,
    }
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
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "alice-pre-show-r1.json")},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "bob-pre-show-r1.json")},
        ]

        # Create decisions with ledger that includes brief_type
        Path(decisions_file).parent.mkdir(parents=True, exist_ok=True)
        ledger = {
            "seated": seated,
            "expected_reactions": expected_reactions,
            "Q1": {"brief_type": "disagreement"},  # Set brief_type to disagreement
        }
        lines = ["# Decisions", "", "## Checkpoint ledger", "```", json.dumps(ledger), "```", "",
                 "## Decided", "", "## Worth a look", "", "## Questions for you"]
        Path(decisions_file).write_text("\n".join(lines))
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "Q1": {"resolved": True, "agreed": False}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "Q1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if not stdout:
                h.test_result("Disagreement brief -> Q", False, f"exit={exit_code}, stderr={stderr[:200]}")
            else:
                tiers = json.loads(stdout)
                h.test_result("Disagreement brief -> Q",
                             exit_code == 0 and tiers.get("Q1") == "question",
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
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "alice-pre-show-r1.json")},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "bob-pre-show-r1.json")},
        ]

        create_decisions_file(str(decisions_file), seated, expected_reactions)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "W1": {"resolved": True, "agreed": True, "protested": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "W1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if not stdout:
                h.test_result("Protested brief -> Q", False, f"exit={exit_code}, stderr={stderr[:200]}")
            else:
                tiers = json.loads(stdout)
                h.test_result("Protested brief -> Q",
                             exit_code == 0 and tiers.get("W1") == "question",
                             f"exit={exit_code}, W1 tier={tiers.get('W1')}")
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
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "alice-pre-show-r1.json")},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "bob-pre-show-r1.json")},
        ]

        create_decisions_file(str(decisions_file), seated, expected_reactions)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "D1": {"resolved": True, "agreed": False}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "D1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if not stdout:
                h.test_result("Resolved + agreed + no could-go-other-way -> D", False, f"stderr={stderr[:200]}")
            else:
                tiers = json.loads(stdout)
                h.test_result("Resolved + agreed + no could-go-other-way -> D",
                             exit_code == 0 and tiers.get("D1") == "decided",
                             f"exit={exit_code}, D1 tier={tiers.get('D1')}")
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
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "alice-pre-show-r1.json")},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "bob-pre-show-r1.json")},
        ]

        create_decisions_file(str(decisions_file), seated, expected_reactions)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "W1": {"resolved": True, "agreed": False, "could-go-other-way": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "W1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if not stdout:
                h.test_result("Resolved + agreed + could-go-other-way -> W", False, f"stderr={stderr[:200]}")
            else:
                tiers = json.loads(stdout)
                h.test_result("Resolved + agreed + could-go-other-way -> W",
                             exit_code == 0 and tiers.get("W1") == "worth-a-look",
                             f"exit={exit_code}, W1 tier={tiers.get('W1')}")
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
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "alice-pre-show-r1.json")},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "bob-pre-show-r1.json")},
        ]

        create_decisions_file(str(decisions_file), seated, expected_reactions)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "W1": {"resolved": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "W1": {"no-view": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        try:
            if not stdout:
                h.test_result("Resolved with no other agreement -> W", False, f"stderr={stderr[:200]}")
            else:
                tiers = json.loads(stdout)
                h.test_result("Resolved with no other agreement -> W",
                             exit_code == 0 and tiers.get("W1") == "worth-a-look",
                             f"exit={exit_code}, W1 tier={tiers.get('W1')}")
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
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "alice-pre-show-r1.json")},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "bob-pre-show-r1.json")},
        ]

        create_decisions_file(str(decisions_file), seated, expected_reactions)
        # Create only alice's file, not bob's
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {"Q1": {"resolved": True}})

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
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "alice-pre-show-r1.json")},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": str(reactions_dir / "bob-pre-show-r1.json")},
        ]

        create_decisions_file(str(decisions_file), seated, expected_reactions)
        # Create alice's file without the sentinel
        Path(reactions_dir).mkdir(parents=True, exist_ok=True)
        Path(reactions_dir / "alice-pre-show-r1.json").write_text(json.dumps({"Q1": {"resolved": True}}))
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {"Q1": {"agreed": True}})

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        h.test_result("Reaction file without 'complete' sentinel -> exit non-zero",
                     exit_code != 0,
                     f"exit={exit_code}")

    # Test 8: Missing decisions.md -> non-zero exit
    print("[Test 8] Missing decisions.md -> exit non-zero")
    with tempfile.TemporaryDirectory() as tmp:
        nonexistent_file = str(Path(tmp) / "missing.md")
        exit_code, stdout, stderr = run_tier_script(nonexistent_file)
        h.test_result("Missing decisions.md -> exit non-zero", exit_code != 0, f"exit={exit_code}")

    # Test 9: No --decisions argument -> exit non-zero
    print("[Test 9] No --decisions argument -> exit non-zero")
    cmd = [sys.executable, str(TIER_SCRIPT)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    h.test_result("No --decisions argument -> exit non-zero",
                 result.returncode != 0,
                 f"exit={result.returncode}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
