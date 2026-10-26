#!/usr/bin/env python3
r"""Assign tiers (D/W/Q) to checkpoint items based on reaction-pass evidence.

Three modes:
1. Default (no flag): outputs brief_id -> tier JSON (legacy default).
2. --fragment: outputs handle-keyed checkpoint ledger fragment with tier, brief_id,
   brief_type, and evidence fields. Handles are numbered per tier in numeric brief_id
   order (B2 before B10): D1, D2... for decided; W1, W2... for worth-a-look; Q1, Q2... for question.
3. --check-ledger: pure-read mode; validates checkpoint ledger shape and compares its
   handle set, and each handle's tier and brief_id, against a freshly computed fragment
   (brief_type and evidence are not compared). Exits 1 with a named reason on any mismatch.
   Never writes a file.

All three modes route reaction files through a shared reading and validation pipeline.
Returns 0 on success (or --check-ledger validation pass), 1 on validation error or
missing file, 2 on internal error.

Precondition: brief IDs must match B\d+ format (enforced at tiering time).
Handle letters and order are derived from schema.HANDLE_TIERS.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

# Add parent directory to path to import plan_checkpoint_schema
sys.path.insert(0, str(Path(__file__).parent))
import plan_checkpoint_schema as schema


class TierError(Exception):
    """Carrier for tiering errors: the complete stderr line and the exit code."""

    def __init__(self, message: str, exit_code: int = 1):
        self.message = message
        self.exit_code = exit_code
        super().__init__(self.message)


def _resolve_reaction_path(path_str: str, decisions_dir: Path) -> Tuple[Path, Optional[str]]:
    """Resolve a reaction path and confine it to decisions_dir.

    Args:
        path_str: path as written in ledger (relative or absolute)
        decisions_dir: the decisions directory for relative path resolution

    Returns:
        (resolved_path, error_message) where error_message is None on success,
        or the confine_path_to_dir message on failure (the caller prefixes this with "Error: ").
        Possible errors: path outside decisions_dir (from confine_path_to_dir).
    """
    # Resolve path: relative to decisions_dir or absolute
    if Path(path_str).is_absolute():
        reaction_path = Path(path_str)
    else:
        reaction_path = decisions_dir / path_str

    # Confine path to decisions_dir
    confinement_error = schema.confine_path_to_dir(str(reaction_path), decisions_dir)
    if confinement_error:
        return reaction_path, confinement_error

    return reaction_path, None


def _read_reaction_file(
    reaction_path: Path, path_str: str
) -> Tuple[Optional[Dict], Optional[TierError]]:
    """Read and validate a single reaction file.

    Args:
        reaction_path: resolved Path to the file
        path_str: path as written in ledger (for error messages)

    Returns:
        (parsed_reactions_dict, error) where error is None on success.
        Errors:
        - File missing: error with exit_code=1
        - Unreadable (OSError/UnicodeDecodeError): error with exit_code=2
        - Invalid JSON: error with exit_code=1
        - Not a dict: error with exit_code=1
        - Missing 'complete': true sentinel: error with exit_code=1
        - validate_reaction_file_shape fails: error with exit_code=1
    """
    # Check existence
    if not reaction_path.exists():
        return None, TierError(
            f"Error: expected reaction file missing: {path_str}",
            exit_code=1,
        )

    # Try to read
    try:
        content = reaction_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return None, TierError(f"Error reading reaction file {path_str}: {e}", exit_code=2)

    # Try to parse JSON
    try:
        reactions = json.loads(content)
    except json.JSONDecodeError as e:
        return None, TierError(
            f"Error: reaction file is not valid JSON: {path_str}: {e}",
            exit_code=1,
        )

    # Validate shape (rejects non-dict)
    if not isinstance(reactions, dict):
        return None, TierError(
            f"Error in {path_str}: reaction file is not a dict",
            exit_code=1,
        )

    # Sentinel before shape, so a file without "complete" gets the sentinel message
    if reactions.get("complete") is not True:
        return None, TierError(
            f"Error: reaction file missing 'complete': true sentinel: {path_str}",
            exit_code=1,
        )

    reaction_error = schema.validate_reaction_file_shape(reactions)
    if reaction_error:
        return None, TierError(f"Error in {path_str}: {reaction_error}", exit_code=1)

    return reactions, None


def _validate_brief_id(brief_id: str) -> Optional[TierError]:
    r"""Validate that brief_id matches B\d+ format.

    Args:
        brief_id: the brief ID to validate

    Returns:
        None if valid, TierError with exit_code=1 if invalid.
    """
    if not re.match(r"^B\d+$", brief_id):
        return TierError(f"Error: brief id {brief_id} is not of the form B<digits>", exit_code=1)
    return None


def _compute_tiers(
    ledger: Dict, decisions_dir: Path
) -> Tuple[Optional[Dict], Optional[Dict], Optional[TierError]]:
    """Compute tiers and parse reactions in one shared pipeline.

    Reads all reaction files, validates shapes, collects evidence, and assigns tiers.
    Validates brief IDs, handles path confinement, and enforces all contract rules.

    Args:
        ledger: the parsed ledger from extract_ledger_json
        decisions_dir: the decisions directory for relative path resolution

    Returns:
        (tiers_dict, parsed_reactions_dict, error)
        - tiers_dict: {brief_id: tier_string}
        - parsed_reactions_dict: {path_str: parsed_reaction_dict} keyed by ledger path strings
        - error: None on success, TierError on failure
    """
    briefs_map = ledger.get("briefs", {})
    for brief_id in briefs_map:
        brief_id_error = _validate_brief_id(brief_id)
        if brief_id_error:
            return None, None, brief_id_error

    # Build raiser map
    raiser_map: Dict[str, str] = {}
    for brief_id, brief_data in briefs_map.items():
        if isinstance(brief_data, dict):
            raiser = brief_data.get("raiser")
            if raiser:
                raiser_map[brief_id] = raiser

    # Evidence per brief: {brief_id: {slug: classification}}
    evidence: Dict[str, Dict[str, str]] = {}
    # Raw reactions per brief (for could-go-other-way): {brief_id: {slug: raw_dict}}
    raw_reactions: Dict[str, Dict[str, Dict]] = {}
    # Parse reactions keyed by ledger path string
    parsed_reactions: Dict[str, Dict] = {}

    # Read and validate all reaction files in one pass
    for reaction_spec in ledger.get("expected_reactions", []):
        reaction_path_str = reaction_spec.get("path")
        if not reaction_path_str:
            continue

        # Resolve and confine path
        reaction_path, confinement_error = _resolve_reaction_path(
            reaction_path_str, decisions_dir
        )
        if confinement_error:
            return None, None, TierError(f"Error: {confinement_error}", exit_code=1)

        # Read and validate reaction file
        reactions, read_error = _read_reaction_file(reaction_path, reaction_path_str)
        if read_error:
            return None, None, read_error

        # Store parsed reactions by ledger path string
        parsed_reactions[reaction_path_str] = reactions

        slug = reaction_spec.get("slug")
        if not slug:
            continue

        # Extract reactions per brief
        for brief_id, reaction in reactions.items():
            if brief_id == "complete":
                continue
            if not isinstance(reaction, dict):
                continue

            # Validate brief_id format
            brief_id_error = _validate_brief_id(brief_id)
            if brief_id_error:
                return None, None, brief_id_error

            # Classify this reaction
            classification = schema.classify_reaction(reaction, slug)

            if brief_id not in evidence:
                evidence[brief_id] = {}
            evidence[brief_id][slug] = classification

            if brief_id not in raw_reactions:
                raw_reactions[brief_id] = {}
            raw_reactions[brief_id][slug] = reaction

    # Validate brief_types and assign tiers
    tiers: Dict[str, str] = {}

    for brief_id in evidence:
        # Every brief with reactions needs a brief_type in ledger briefs
        brief_data = briefs_map.get(brief_id)
        brief_type = brief_data.get("brief_type") if isinstance(brief_data, dict) else None
        if not brief_type:
            return None, None, TierError(
                f"Error: brief {brief_id} has reactions but no brief_type in ledger briefs",
                exit_code=1,
            )

        reactions_by_slug = evidence[brief_id]
        raiser_slug = raiser_map.get(brief_id)
        raw_brief_reactions = raw_reactions.get(brief_id, {})

        # Collect reactions by type
        protested_by = [s for s, c in reactions_by_slug.items() if c == "protested"]
        needs_human_by = [s for s, c in reactions_by_slug.items() if c == "needs-human"]
        resolved_by = [s for s, c in reactions_by_slug.items() if c == "resolved"]
        agreed_by = [s for s, c in reactions_by_slug.items() if c == "agreed"]

        # Check for could-go-other-way flag
        could_go_other_way = any(
            raw_brief_reactions.get(s, {}).get("could-go-other-way") is True
            for s in raw_brief_reactions
        )

        # Apply tier rule
        tier = schema.tier_item(
            brief_type,
            protested_by,
            needs_human_by,
            resolved_by,
            agreed_by,
            raiser_slug,
            could_go_other_way,
        )

        # Validate tier is known
        if tier not in schema.HANDLE_TIERS:
            return None, None, TierError(
                f"Error: unknown tier {tier} for brief {brief_id}",
                exit_code=2,
            )

        tiers[brief_id] = tier

    return tiers, parsed_reactions, None


def _build_fragment(
    tiers: Dict[str, str],
    briefs_map: Dict,
    ledger: Dict,
    parsed_reactions: Dict[str, Dict],
) -> Dict:
    """Build the handle-keyed fragment with all four fields.

    Assigns handles using deterministic numbering rule: group by tier, number each
    group from 1 in ascending brief_id order.

    Args:
        tiers: {brief_id: tier_string} from tiering
        briefs_map: ledger.briefs {brief_id: {brief_type, raiser}}
        ledger: the full ledger (for expected_reactions list)
        parsed_reactions: {path_str: parsed_dict} already read and validated

    Returns:
        {handle: {tier, brief_id, brief_type, evidence}}
    """
    # Handle letter is each tier's initial: decided -> D, worth-a-look -> W, question -> Q
    tier_to_letter = {tier_name: tier_name[0].upper() for tier_name in schema.HANDLE_TIERS}

    # Group briefs by tier and sort within each tier by brief_id
    tier_groups: Dict[str, list] = {}
    for brief_id, tier in tiers.items():
        if tier not in tier_groups:
            tier_groups[tier] = []
        tier_groups[tier].append(brief_id)

    # Sort each group by brief_id (numeric sort: B2 before B10)
    for tier in tier_groups:
        tier_groups[tier].sort(key=lambda b: int(b[1:]))

    fragment: Dict = {}
    counters = {tier: 1 for tier in schema.HANDLE_TIERS}

    for tier in schema.HANDLE_TIERS:
        if tier not in tier_groups:
            continue
        letter = tier_to_letter[tier]
        for brief_id in tier_groups[tier]:
            handle = f"{letter}{counters[tier]}"
            counters[tier] += 1

            brief_type = briefs_map.get(brief_id, {}).get("brief_type", "")

            # Collect evidence paths: pre-show expected_reactions with this brief_id
            evidence_paths = []
            for entry in ledger.get("expected_reactions", []):
                if not isinstance(entry, dict):
                    continue
                if entry.get("mode") != "pre-show":
                    continue
                path_str = entry.get("path")
                if not path_str:
                    continue

                # Check if this path's parsed reactions contain the brief_id
                reactions = parsed_reactions.get(path_str)
                if reactions and brief_id in reactions:
                    evidence_paths.append(path_str)

            fragment[handle] = {
                "tier": tier,
                "brief_id": brief_id,
                "brief_type": brief_type,
                "evidence": evidence_paths,
            }

    return fragment


def main() -> int:
    """Read ledger and reaction files, output handle->tier JSON or fragment.

    Returns:
        0 on success, 1 on validation error or missing file, 2 on internal error.
    """
    parser = argparse.ArgumentParser(
        description="Assign tiers (D/W/Q) to checkpoint items based on reaction-pass evidence."
    )
    parser.add_argument("--decisions", required=True, help="Path to decisions.md")

    # Create mutually exclusive group for --fragment and --check-ledger
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--fragment",
        action="store_true",
        help="Output handle-keyed checkpoint ledger fragment with all four fields (tier, brief_id, brief_type, evidence)",
    )
    mode_group.add_argument(
        "--check-ledger",
        action="store_true",
        help="Pure-read validation mode: verify checkpoint ledger shape and numbering against computed fragment. Compares handle set, tier, and brief_id only (not brief_type or evidence).",
    )

    args = parser.parse_args()

    decisions_path = Path(args.decisions)
    decisions_dir = decisions_path.parent

    # Read decisions.md and extract the ledger block
    try:
        content = decisions_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"Error: {decisions_path} not found", file=sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError) as e:
        print(f"Error reading {decisions_path}: {e}", file=sys.stderr)
        return 2

    # Extract ledger
    ledger = schema.extract_ledger_json(content)
    if not ledger:
        print("Error: no ## Checkpoint ledger block found or invalid JSON", file=sys.stderr)
        return 1

    # Validate ledger shape (once, for all modes)
    ledger_error = schema.validate_ledger_shape(ledger)
    if ledger_error:
        print(f"Error: {ledger_error}", file=sys.stderr)
        return 1

    # Validate briefs is a dict
    briefs_map = ledger.get("briefs", {})
    if not isinstance(briefs_map, dict):
        print("Error: ledger 'briefs' is not a dict", file=sys.stderr)
        return 1

    # Compute tiers using shared pipeline
    tiers, parsed_reactions, compute_error = _compute_tiers(ledger, decisions_dir)
    if compute_error:
        print(compute_error.message, file=sys.stderr)
        return compute_error.exit_code

    # Handle --check-ledger mode (pure-read validation)
    if args.check_ledger:
        # Validate nested and entry fields
        nested_err = schema.nested_handle_map_reason(ledger)
        if nested_err:
            print(f"Error: {nested_err}", file=sys.stderr)
            return 1
        entry_err = schema.validate_handle_entries(ledger)
        if entry_err:
            print(f"Error: {entry_err}", file=sys.stderr)
            return 1

        # Build the expected fragment
        expected_fragment = _build_fragment(tiers, briefs_map, ledger, parsed_reactions)

        # Extract ledger handles
        ledger_handles = {
            k for k in ledger.keys() if isinstance(k, str) and schema.LEDGER_HANDLE_RE.fullmatch(k)
        }

        # Check if briefs is non-empty and ledger has no top-level handle keys
        if briefs_map and not ledger_handles:
            print("checkpoint ledger has briefs but no top-level D/W/Q handle keys", file=sys.stderr)
            return 1

        # Compare ledger handles with expected fragment keys
        expected_keys = set(expected_fragment.keys())
        if ledger_handles != expected_keys:
            missing = sorted(expected_keys - ledger_handles)
            extra = sorted(ledger_handles - expected_keys)
            if missing:
                print(
                    f"checkpoint ledger is missing handles: {', '.join(missing)}",
                    file=sys.stderr,
                )
            if extra:
                print(
                    f"checkpoint ledger has unexpected handles: {', '.join(extra)}",
                    file=sys.stderr,
                )
            return 1

        # Compare only tier and brief_id per handle, not brief_type or evidence.
        # The handle sets are equal here, so walk the fragment in its handle order.
        for handle, expected_entry in expected_fragment.items():
            actual_entry = ledger.get(handle, {})

            expected_tier = expected_entry.get("tier")
            actual_tier = actual_entry.get("tier")
            if actual_tier != expected_tier:
                print(
                    f"checkpoint ledger handle {handle} expected tier {expected_tier}, found {actual_tier}",
                    file=sys.stderr,
                )
                return 1

            expected_brief_id = expected_entry.get("brief_id")
            actual_brief_id = actual_entry.get("brief_id")
            if actual_brief_id != expected_brief_id:
                print(
                    f"checkpoint ledger handle {handle} expected brief_id {expected_brief_id}, found {actual_brief_id}",
                    file=sys.stderr,
                )
                return 1

        # If we get here, validation passed
        return 0

    # Default and --fragment modes
    if args.fragment:
        fragment = _build_fragment(tiers, briefs_map, ledger, parsed_reactions)
        print(json.dumps(fragment))
    else:
        # Default mode: output brief_id -> tier JSON
        print(json.dumps(tiers))

    return 0


if __name__ == "__main__":
    sys.exit(main())
