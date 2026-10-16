#!/usr/bin/env python3
"""Shared schema, validators, and classifiers for checkpoint ledger and reactions.

Implements: ledger extraction (F6), reaction classification (F2), brief_id->handle
mapping (F5), tier rule including needs-human (F22), path confinement (F25), and
shape validation (F17).

Both scripts/plan-checkpoint-tiers.py and scripts/flow-verify-receipt.py import from
this module to ensure they agree on all checkpoint structures and rules.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional


def extract_ledger_json(text: str) -> Optional[Dict]:
    """Extract and parse the Checkpoint ledger JSON block from decisions.md.

    Line-based fence extractor: heading line starts with "## Checkpoint ledger",
    the first line starting with ``` after it opens the fence (``` or ```json),
    the next line starting with ``` closes it. Returns None unless json parses to
    a dict.

    Args:
        text: Full content of decisions.md

    Returns:
        Parsed ledger dict, or None if not found or invalid JSON or not a dict.
    """
    lines = text.split('\n')
    in_fence = False
    fence_lines = []
    found_heading = False

    for i, line in enumerate(lines):
        # Look for heading
        if line.startswith('## Checkpoint ledger'):
            found_heading = True
            continue

        if not found_heading:
            continue

        # Look for opening fence
        if not in_fence and line.startswith('```'):
            in_fence = True
            continue

        # Collect fence lines
        if in_fence:
            if line.startswith('```'):
                # Closing fence found
                break
            fence_lines.append(line)

    if not fence_lines:
        return None

    json_text = '\n'.join(fence_lines).strip()
    try:
        result = json.loads(json_text)
        # Only return if it's a dict
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def validate_ledger_shape(ledger: Dict) -> Optional[str]:
    """Validate ledger structure. Return error reason or None.

    Args:
        ledger: The parsed ledger dict

    Returns:
        Specific error reason, or None if valid.
    """
    if not isinstance(ledger, dict):
        return "ledger is not a dict"

    # Check required top-level keys
    if "seated" not in ledger:
        return "ledger missing 'seated' key"
    if "expected_reactions" not in ledger:
        return "ledger missing 'expected_reactions' key"

    seated = ledger.get("seated")
    if not isinstance(seated, list):
        return "'seated' is not a list"

    expected_reactions = ledger.get("expected_reactions")
    if not isinstance(expected_reactions, list):
        return "'expected_reactions' is not a list"

    # Validate expected_reactions entries
    for i, entry in enumerate(expected_reactions):
        if not isinstance(entry, dict):
            return f"expected_reactions[{i}] is not a dict"
        if "path" not in entry:
            return f"expected_reactions[{i}] missing 'path' key"

    return None


def validate_reaction_file_shape(data: Dict) -> Optional[str]:
    """Validate a reaction file structure. Return error reason or None.

    Args:
        data: The parsed reaction file

    Returns:
        Specific error reason, or None if valid.
    """
    if not isinstance(data, dict):
        return "reaction file is not a dict"

    if "complete" not in data:
        return "reaction file missing 'complete' key"

    # Each non-'complete' key should be a handle with a reaction object
    for handle, reaction_data in data.items():
        if handle == "complete":
            continue
        if not isinstance(reaction_data, dict):
            return f"reaction file['{handle}'] is not a dict"

    return None


def validate_answers_file_shape(data: Dict, kind: str) -> Optional[str]:
    """Validate an answers file structure. Return error reason or None.

    Args:
        data: The parsed answers file
        kind: The kind of question (e.g., "report")

    Returns:
        Specific error reason, or None if valid.
    """
    if not isinstance(data, dict):
        return "answers file is not a dict"

    if kind == "report":
        # Report variant: {kind, round, attempt, items, loop_end}
        if "kind" not in data:
            return "answers file missing 'kind' key"
        if "round" not in data:
            return "answers file missing 'round' key"
        if "attempt" not in data:
            return "answers file missing 'attempt' key"
        if "items" not in data:
            return "answers file missing 'items' key"

        items = data.get("items")
        if not isinstance(items, dict):
            return "'items' is not a dict"

        for handle, item in items.items():
            if not isinstance(item, dict):
                return f"items['{handle}'] is not a dict"
            if "disposition" not in item:
                return f"items['{handle}'] missing 'disposition' key"

    else:
        # Standard form variant: {answers, ...}
        if "answers" not in data:
            return "answers file missing 'answers' key"
        answers = data.get("answers")
        if not isinstance(answers, dict):
            return "'answers' is not a dict"

    return None


def classify_reaction(entry: Dict, slug: str) -> str:
    """Classify a single reaction entry by truthiness precedence.

    Precedence (by truthiness check): needs-human > protested > resolved > agreed > no-view

    Args:
        entry: The reaction entry (boolean keys: needs-human, protested, resolved, agreed, no-view)
        slug: The expert slug (for error messages)

    Returns:
        One of: "needs-human", "protested", "resolved", "agreed", "no-view"
    """
    if entry.get("needs-human") is True:
        return "needs-human"
    if entry.get("protested") is True:
        return "protested"
    if entry.get("resolved") is True:
        return "resolved"
    if entry.get("agreed") is True:
        return "agreed"
    return "no-view"


def confine_path_to_dir(path_str: str, base_dir: Path) -> Optional[str]:
    """Resolve a path and ensure it is under base_dir. Return error or None.

    Args:
        path_str: Path to check (may be relative or absolute)
        base_dir: The directory it must be under

    Returns:
        Error reason if outside base_dir, None if valid.
    """
    try:
        # Resolve the path
        if Path(path_str).is_absolute():
            real_path = Path(path_str).resolve()
        else:
            real_path = (base_dir / path_str).resolve()

        # Check it's under base_dir
        real_path.relative_to(base_dir.resolve())
        return None
    except (ValueError, RuntimeError):
        return f"path {path_str} is outside {base_dir}"


def tier_item(
    brief_type: str,
    protested_by: List[str],
    needs_human_by: List[str],
    resolved_by: List[str],
    agreed_by: List[str],
    raiser_slug: Optional[str],
    could_go_other_way: bool,
) -> str:
    """Apply tier rule: Q, D, or W.

    Args:
        brief_type: The brief type (e.g., "disagreement", "question", etc.)
        protested_by: List of slugs who protested (may be empty)
        needs_human_by: List of slugs who marked needs-human (may be empty)
        resolved_by: List of slugs who resolved (may be empty)
        agreed_by: List of slugs who agreed (may be empty)
        raiser_slug: The slug of who raised this brief (for self-agreement check)
        could_go_other_way: Whether any expert flagged this as could-go-other-way

    Returns:
        "question", "decided", or "worth-a-look"
    """
    # Q: brief_type == "disagreement", OR any expert protested, OR any expert needs-human
    if brief_type == "disagreement" or protested_by or needs_human_by:
        return "question"

    # D: no Q condition, AND at least one expert resolved, AND at least one expert
    # OTHER THAN the one(s) who resolved agreed, AND no could-go-other-way
    if resolved_by and not could_go_other_way:
        # Check for another expert who agreed (not the resolver)
        other_agreed = [s for s in agreed_by if s not in resolved_by]
        # Also exclude self-agreement (if raiser agreed, and raiser is the only resolver)
        if other_agreed or (agreed_by and raiser_slug and raiser_slug not in resolved_by):
            return "decided"

    # W: everything else
    return "worth-a-look"
