#!/usr/bin/env python3
"""
Additional spec-blind tests for scripts/plan-checkpoint-tiers.py

This suite covers gaps not explicitly tested in test_plan_checkpoint_tiers.py:
- Item 3: Unreadable reaction file (exit 2)
- Item 1: Path confinement for --fragment mode
- Item 12: --check-ledger ignores brief_type and evidence differences
- Item 9: Unknown tier in HANDLE_TIERS (optional/hard to provoke)

Run with: python3 tests/test_plan_checkpoint_tiers_spec.py
"""

import json
import os
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


def create_decisions_file(path: str, seated: list, expected_reactions: list, briefs: dict = None,
                          handles: dict = None) -> None:
    """Create a decisions.md with a Checkpoint ledger block.

    `handles` entries are merged in as top-level ledger keys, the shape the tier
    script's --fragment output is pasted into.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    ledger = {
        "seated": seated,
        "expected_reactions": expected_reactions,
    }
    if briefs is not None:
        ledger["briefs"] = briefs
    if handles:
        ledger.update(handles)
    # Build the decisions file with the fenced JSON block
    # The fence parser in the script expects ``` on its own line
    lines = ["# Decisions", "", "## Checkpoint ledger", "```", json.dumps(ledger), "```", "",
             "## Decided", "", "## Worth a look", "", "## Questions for you"]
    content = "\n".join(lines)
    Path(path).write_text(content)


def main():
    h = Harness("PLAN-CHECKPOINT-TIERS ADDITIONAL SPEC-BLIND TESTS")

    # ========== Item 3: Unreadable file -> exit 2 ==========
    # Test 34: Unreadable reaction file (permission denied) -> exit 2, specific error message
    print("[Test 34] Unreadable reaction file (permission denied) -> exit 2")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {"B1": {"resolved": True}})

        # Remove read permissions from the file
        reaction_file = reactions_dir / "alice-pre-show-r1.json"
        os.chmod(str(reaction_file), 0o000)

        try:
            exit_code, stdout, stderr = run_tier_script(str(decisions_file))
            h.test_result("Unreadable reaction file -> exit 2",
                         exit_code == 2 and "Error reading reaction file" in stderr and
                         "reactions/alice-pre-show-r1.json" in stderr,
                         f"exit={exit_code}, stderr={stderr[:300]}")
        finally:
            # Restore permissions for cleanup
            os.chmod(str(reaction_file), 0o644)

    # Test 35: Unreadable reaction file in --check-ledger mode -> exit 2
    print("[Test 35] Unreadable reaction file in --check-ledger mode -> exit 2")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs,
                             handles={"D1": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": []}})
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {"B1": {"resolved": True}})

        # Remove read permissions from the file
        reaction_file = reactions_dir / "alice-pre-show-r1.json"
        os.chmod(str(reaction_file), 0o000)

        try:
            exit_code, stdout, stderr = run_tier_script(str(decisions_file), check_ledger=True)
            h.test_result("Unreadable reaction file in --check-ledger mode -> exit 2",
                         exit_code == 2 and "Error reading reaction file" in stderr,
                         f"exit={exit_code}, stderr={stderr[:300]}")
        finally:
            # Restore permissions for cleanup
            os.chmod(str(reaction_file), 0o644)

    # Test 36: Invalid UTF-8 bytes in reaction file -> exit 2
    print("[Test 36] Invalid UTF-8 bytes in reaction file -> exit 2")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)

        # Write invalid UTF-8 bytes directly
        reaction_file = reactions_dir / "alice-pre-show-r1.json"
        reaction_file.parent.mkdir(parents=True, exist_ok=True)
        with open(str(reaction_file), "wb") as f:
            f.write(b"\x80\x81\x82")  # Invalid UTF-8 sequence

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        h.test_result("Invalid UTF-8 bytes in reaction file -> exit 2",
                     exit_code == 2 and "Error reading reaction file" in stderr,
                     f"exit={exit_code}, stderr={stderr[:300]}")

    # ========== Item 1: Path confinement for --fragment mode ==========
    # Test 37: --fragment mode enforces path confinement
    print("[Test 37] --fragment mode: reaction path outside dir -> exit 1, 'is outside' message")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        session_dir = tmp_path / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "../outside.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        # Create the outside file
        (tmp_path / "outside.json").write_text(json.dumps({"B1": {"resolved": True}, "complete": True}))

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), fragment=True)
        h.test_result("--fragment mode: path outside dir -> exit 1, 'is outside' message",
                     exit_code == 1 and "is outside" in stderr,
                     f"exit={exit_code}, stderr={stderr[:300]}")

    # ========== Item 12: --check-ledger ignores brief_type and evidence differences ==========
    # Test 38: --check-ledger exits 0 when brief_type differs (brief_type ignored)
    print("[Test 38] --check-ledger: brief_type difference ignored -> exit 0")
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

        # Ledger has different brief_type than briefs
        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs,
                             handles={"D1": {"tier": "decided", "brief_id": "B1", "brief_type": "disagreement", "evidence": []}})
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "agreed": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), check_ledger=True)
        h.test_result("--check-ledger: brief_type difference ignored -> exit 0",
                     exit_code == 0,
                     f"exit={exit_code}, stderr={stderr[:300]}")

    # Test 39: --check-ledger exits 0 when evidence differs (evidence ignored)
    print("[Test 39] --check-ledger: evidence difference ignored -> exit 0")
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

        # Ledger has different evidence than expected
        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs,
                             handles={"D1": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": ["different", "evidence"]}})
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "agreed": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), check_ledger=True)
        h.test_result("--check-ledger: evidence difference ignored -> exit 0",
                     exit_code == 0,
                     f"exit={exit_code}, stderr={stderr[:300]}")

    # Test 40: --check-ledger exits 0 when both brief_type and evidence differ
    print("[Test 40] --check-ledger: both brief_type and evidence differ -> exit 0")
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

        # Ledger has both different brief_type and evidence
        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs,
                             handles={"D1": {"tier": "decided", "brief_id": "B1", "brief_type": "disagreement", "evidence": ["wrong", "evidence"]}})
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True, "agreed": True}
        })
        create_reaction_file(str(reactions_dir / "bob-pre-show-r1.json"), {
            "B1": {"agreed": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), check_ledger=True)
        h.test_result("--check-ledger: both brief_type and evidence differ -> exit 0",
                     exit_code == 0,
                     f"exit={exit_code}, stderr={stderr[:300]}")

    # ========== Additional edge cases ==========
    # Test 41: Brief id in a reaction file key (not ledger briefs) not matching B<digits>
    print("[Test 41] Brief id in reaction file (not ledger briefs) not matching B<digits>")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
        ]
        briefs = {}

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        # Create reaction file with invalid brief id as a key
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "X1": {"resolved": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file))
        h.test_result("Brief id in reaction file not matching B<digits> -> error",
                     exit_code == 1 and "is not of the form B<digits>" in stderr,
                     f"exit={exit_code}, stderr={stderr[:300]}")

    # Test 42: --check-ledger with brief id in reaction file not matching B<digits>
    print("[Test 42] --check-ledger: brief id in reaction file not matching B<digits>")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
        ]
        briefs = {}

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        # Create reaction file with invalid brief id as a key
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "BADID": {"resolved": True}
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), check_ledger=True)
        h.test_result("--check-ledger: brief id in reaction file not matching B<digits>",
                     exit_code == 1 and "is not of the form B<digits>" in stderr,
                     f"exit={exit_code}, stderr={stderr[:300]}")

    # Test 43: Reaction file without complete sentinel in --fragment mode -> exit 1
    print("[Test 43] --fragment mode: reaction file missing 'complete': true -> exit 1")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        # Create a reaction file WITHOUT the complete sentinel
        reactions_dir.mkdir(parents=True, exist_ok=True)
        (reactions_dir / "alice-pre-show-r1.json").write_text(json.dumps({"B1": {"resolved": True}}))

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), fragment=True)
        h.test_result("--fragment mode: missing 'complete': true -> exit 1",
                     exit_code == 1 and "missing 'complete': true sentinel" in stderr,
                     f"exit={exit_code}, stderr={stderr[:300]}")

    # Test 44: Invalid JSON in reaction file (malformed JSON) in --fragment mode -> exit 1
    print("[Test 44] --fragment mode: invalid JSON in reaction file -> exit 1")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        # Create a reaction file with malformed JSON
        reactions_dir.mkdir(parents=True, exist_ok=True)
        (reactions_dir / "alice-pre-show-r1.json").write_text("{not valid json}", encoding="utf-8")

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), fragment=True)
        h.test_result("--fragment mode: invalid JSON -> exit 1",
                     exit_code == 1 and "not valid JSON" in stderr,
                     f"exit={exit_code}, stderr={stderr[:300]}")

    # Test 45: Reaction file is not a dict (list) in --fragment mode -> exit 1, no Traceback
    print("[Test 45] --fragment mode: reaction file is list (not dict) -> exit 1")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        # Create a reaction file as a JSON list
        reactions_dir.mkdir(parents=True, exist_ok=True)
        (reactions_dir / "alice-pre-show-r1.json").write_text(json.dumps([]))

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), fragment=True)
        h.test_result("--fragment mode: list instead of dict -> exit 1, no Traceback",
                     exit_code == 1 and "Error in" in stderr and "not a dict" in stderr and "Traceback" not in stderr,
                     f"exit={exit_code}, stderr={stderr[:300]}")

    # Test 46: Numeric ordering in --fragment with B1, B20, B2 (ensure numeric sorting)
    print("[Test 46] --fragment numeric ordering: B1, B20, B2 -> sorted as B1, B2, B20")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        decisions_file = session_dir / "decisions.md"
        reactions_dir = session_dir / "reactions"

        seated = ["alice"]
        expected_reactions = [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
        ]
        briefs = {
            "B1": {"brief_type": "question", "raiser": "alice"},
            "B20": {"brief_type": "question", "raiser": "alice"},
            "B2": {"brief_type": "question", "raiser": "alice"},
        }

        create_decisions_file(str(decisions_file), seated, expected_reactions, briefs)
        create_reaction_file(str(reactions_dir / "alice-pre-show-r1.json"), {
            "B1": {"resolved": True},
            "B20": {"resolved": True},
            "B2": {"resolved": True},
        })

        exit_code, stdout, stderr = run_tier_script(str(decisions_file), fragment=True)
        try:
            if not stdout:
                h.test_result("Numeric ordering B1, B20, B2 -> B1, B2, B20", False, f"stderr={stderr[:200]}")
            else:
                fragment = json.loads(stdout)
                # All should map to W tier (all resolved, no agreement)
                # W1 should be B1, W2 should be B2, W3 should be B20
                brief_ids_by_handle = {k: fragment[k].get("brief_id") for k in fragment.keys()}
                expected_order = {"W1": "B1", "W2": "B2", "W3": "B20"}
                h.test_result("Numeric ordering B1, B20, B2 -> B1, B2, B20",
                             exit_code == 0 and brief_ids_by_handle == expected_order,
                             f"exit={exit_code}, brief_ids_by_handle={brief_ids_by_handle}")
        except (json.JSONDecodeError, TypeError) as e:
            h.test_result("Numeric ordering B1, B20, B2 -> B1, B2, B20", False, f"Error: {e}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
