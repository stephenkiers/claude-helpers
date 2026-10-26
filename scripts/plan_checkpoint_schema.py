#!/usr/bin/env python3
"""Shared schema, validators, and classifiers for checkpoint ledger and reactions.

Implements: ledger extraction (F6), reaction classification (F2), tier rule including
needs-human (F22), path confinement (F25), shape validation (F17), handle entry
validation, and nested handle map detection.

Both scripts/plan-checkpoint-tiers.py and scripts/flow-verify-receipt.py import from
this module to ensure they agree on all checkpoint structures and rules. The tier
script's --fragment mode prints handle-keyed checkpoint entries with all four
fields; numbering rule documented in prompts/flow-reference.md and enforced by
--check-ledger self-check.
"""

import json
import re
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional

# Post-tier handle set vocabulary and pattern
HANDLE_TIERS = ("decided", "worth-a-look", "question")
ACCEPTABLE_TIERS = ("decided", "worth-a-look")
LEDGER_HANDLE_RE = re.compile(r"[DWQ]\d+")
NON_HANDLE_MAP_KEYS: FrozenSet[str] = frozenset({"briefs"})


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
        # F11: Check for legacy 'answers' key FIRST (most specific error).
        # This precedence ensures we report the specific legacy shape issue rather than
        # generic missing-key errors when the file uses the old 'answers' structure.
        # Only one check needed (not both here and in the "items not in data" branch).
        if "answers" in data and ("kind" not in data or "attempt" not in data or "items" not in data):
            return "report answers file uses 'answers' key; expected 'items'"

        # Check required keys
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


def normalize_whitespace(text: str) -> str:
    """Normalize whitespace for substring matching.

    Converts multiple consecutive whitespace characters to single spaces
    and strips leading/trailing whitespace.

    Args:
        text: The text to normalize

    Returns:
        Normalized text
    """
    import re
    return re.sub(r'\s+', ' ', text).strip()


def answer_file_sort_key(filename: str) -> tuple:
    """Extract numeric sort key from answer file name.

    Extracts the sequence number from filenames like "plan-1.json" or "plan-42.json"
    and returns a tuple suitable for sorted().

    Args:
        filename: The basename of the answer file

    Returns:
        Tuple (seq_num, filename) for use as sort key
    """
    import re
    match = re.match(r'plan-(\d+)', filename)
    if match:
        seq_num = int(match.group(1))
        return (seq_num, filename)
    # Fallback for unexpected format
    return (float('inf'), filename)


def nested_handle_map_reason(ledger: Dict) -> Optional[str]:
    r"""Detect if handle keys are nested under a top-level dict and return the reason.

    Checks each top-level key whose value is a dict, skipping NON_HANDLE_MAP_KEYS
    (e.g., "briefs" which has B\d+ keys that don't match LEDGER_HANDLE_RE). If that
    dict has one or more keys matching LEDGER_HANDLE_RE, returns a named reason.
    Returns None for a pre-tier ledger (no handles anywhere) or a correctly shaped
    top-level ledger. Note: returns only the first nested key found (in dict order)
    and does not look deeper than one level.

    Args:
        ledger: The parsed ledger dict

    Returns:
        Reason string if handles are nested, None otherwise.
    """
    for key, value in ledger.items():
        if not isinstance(value, dict):
            continue
        if key in NON_HANDLE_MAP_KEYS:
            continue
        # Check if this dict contains handle-shaped keys
        handles_in_value = [k for k in value.keys() if isinstance(k, str) and LEDGER_HANDLE_RE.fullmatch(k)]
        if handles_in_value:
            sorted_handles = sorted(handles_in_value)
            return f"checkpoint ledger nests handle keys under '{key}' ({', '.join(sorted_handles)}); handle keys must be top-level ledger keys"
    return None


def validate_handle_entries(ledger: Dict) -> Optional[str]:
    """Validate that each top-level handle entry is a dict with required fields.

    For each top-level key matching LEDGER_HANDLE_RE (in sorted order): if its value
    is not a dict, returns an error; otherwise checks for required fields `tier`,
    `brief_id`, and `brief_type` in order and returns the first missing field error.
    Missing means the key is absent, None, or empty string. Note: checks presence only —
    it does not check that `tier` is a known tier, that `brief_id` exists in `briefs`,
    or `brief_type`'s value, nor the `evidence` field.

    Returns None when there are no handles (pre-tier ledger).

    Args:
        ledger: The parsed ledger dict

    Returns:
        Reason string if entries are invalid, None otherwise.
    """
    handles = sorted([k for k in ledger.keys() if isinstance(k, str) and LEDGER_HANDLE_RE.fullmatch(k)])
    for handle in handles:
        value = ledger[handle]
        if not isinstance(value, dict):
            return f"checkpoint ledger entry {handle} is not an object"
        # Check required fields in order: tier, brief_id, brief_type
        # Primary early check: fail closed if any required field is missing before proceeding to tier-specific logic
        for field in ["tier", "brief_id", "brief_type"]:
            field_value = value.get(field)
            if field_value is None or field_value == "":
                return f"checkpoint ledger entry {handle} has no {field}"
    return None


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
