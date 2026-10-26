#!/usr/bin/env python3
"""Assign tiers (D/W/Q) to checkpoint items based on reaction-pass evidence.

Three modes:
1. Default (no flag): outputs brief_id -> tier JSON (legacy default).
2. --fragment: outputs handle-keyed checkpoint ledger fragment with tier, brief_id,
   brief_type, and evidence fields. Handles are numbered per tier in brief_id sort
   order: D1, D2... for decided; W1, W2... for worth-a-look; Q1, Q2... for question.
3. --check-ledger: pure-read mode; validates checkpoint ledger shape and compares it
   against a freshly computed fragment. Exits 1 with a named reason on any mismatch.
   Never writes a file.

Returns 0 on success (or --check-ledger validation pass), 1 on validation error or
missing file, 2 on internal error.
"""

import argparse
import json
import sys
from pathlib import Path

# Add parent directory to path to import plan_checkpoint_schema
sys.path.insert(0, str(Path(__file__).parent))
import plan_checkpoint_schema as schema


def _build_fragment(tiers: dict, briefs_map: dict, ledger: dict, decisions_dir: Path) -> dict:
    """Build the handle-keyed fragment with all four fields.

    Assigns handles using deterministic numbering rule: group by tier, number each
    group from 1 in ascending brief_id order.

    Args:
        tiers: {brief_id: tier_string} from tiering
        briefs_map: ledger.briefs {brief_id: {brief_type, raiser}}
        ledger: the full ledger (for expected_reactions)
        decisions_dir: directory containing the ledger (for path resolution)

    Returns:
        {handle: {tier, brief_id, brief_type, evidence}}
    """
    # Group briefs by tier and sort within each tier by brief_id
    tier_groups = {}
    for brief_id, tier in tiers.items():
        if tier not in tier_groups:
            tier_groups[tier] = []
        tier_groups[tier].append(brief_id)

    # Sort each group by brief_id (numeric sort: B2 before B10)
    for tier in tier_groups:
        tier_groups[tier].sort(key=lambda b: int(b[1:]))

    # Assign handles: D for decided, W for worth-a-look, Q for question
    tier_to_letter = {
        "decided": "D",
        "worth-a-look": "W",
        "question": "Q",
    }
    fragment = {}
    counters = {"decided": 1, "worth-a-look": 1, "question": 1}

    for tier in ["decided", "worth-a-look", "question"]:
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
                # Check if this file contains the brief_id
                if Path(path_str).is_absolute():
                    reaction_path = Path(path_str)
                else:
                    reaction_path = decisions_dir / path_str
                try:
                    reactions = json.loads(reaction_path.read_text(encoding="utf-8"))
                    if brief_id in reactions:
                        evidence_paths.append(path_str)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    pass

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
        help="Pure-read validation mode: verify checkpoint ledger shape and numbering against computed fragment",
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

    # Validate ledger shape
    ledger_error = schema.validate_ledger_shape(ledger)
    if ledger_error:
        print(f"Error: {ledger_error}", file=sys.stderr)
        return 1

    # Handle --check-ledger mode (pure-read validation)
    if args.check_ledger:
        # Validate shape and entry fields
        shape_err = schema.validate_ledger_shape(ledger)
        if shape_err:
            print(shape_err, file=sys.stderr)
            return 1
        nested_err = schema.nested_handle_map_reason(ledger)
        if nested_err:
            print(nested_err, file=sys.stderr)
            return 1
        entry_err = schema.validate_handle_entries(ledger)
        if entry_err:
            print(entry_err, file=sys.stderr)
            return 1

        # Compute the expected fragment and compare with ledger
        # First, we need to run tiering to get the tiers
        # (Copy tiering logic inline to avoid duplication)
        briefs_map = ledger.get("briefs", {})
        if not isinstance(briefs_map, dict):
            print("Error: ledger 'briefs' is not a dict", file=sys.stderr)
            return 1

        # Build evidence and tier as in main path
        evidence = {}
        raiser_map = {}
        raw_reactions = {}

        for brief_id, brief_data in briefs_map.items():
            if isinstance(brief_data, dict):
                raiser = brief_data.get("raiser")
                if raiser:
                    raiser_map[brief_id] = raiser

        # Finding 3: Validate all expected reaction files exist and are readable (before reading)
        for reaction_spec in ledger.get("expected_reactions", []):
            reaction_path_str = reaction_spec.get("path")
            if not reaction_path_str:
                continue
            if Path(reaction_path_str).is_absolute():
                reaction_path = Path(reaction_path_str)
            else:
                reaction_path = decisions_dir / reaction_path_str
            if not reaction_path.exists():
                print(f"Error: expected reaction file missing: {reaction_path_str}", file=sys.stderr)
                return 1
            try:
                # Validate readable
                reaction_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                print(f"Error reading reaction file {reaction_path_str}: {e}", file=sys.stderr)
                return 2

        # Now read reaction files and build evidence
        for reaction_spec in ledger.get("expected_reactions", []):
            reaction_path_str = reaction_spec.get("path")
            if not reaction_path_str:
                continue
            if Path(reaction_path_str).is_absolute():
                reaction_path = Path(reaction_path_str)
            else:
                reaction_path = decisions_dir / reaction_path_str
            try:
                reactions = json.loads(reaction_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not reactions.get("complete"):
                continue

            slug = reaction_spec.get("slug")
            if not slug:
                continue

            for brief_id, reaction in reactions.items():
                if brief_id == "complete" or not isinstance(reaction, dict):
                    continue
                classification = schema.classify_reaction(reaction, slug)
                if brief_id not in evidence:
                    evidence[brief_id] = {}
                evidence[brief_id][slug] = classification
                if brief_id not in raw_reactions:
                    raw_reactions[brief_id] = {}
                raw_reactions[brief_id][slug] = reaction

        # Finding 1: Validate that briefs with reactions have brief_type in ledger
        for brief_id in evidence:
            if brief_id in briefs_map and isinstance(briefs_map[brief_id], dict):
                brief_type = briefs_map[brief_id].get("brief_type")
                if not brief_type:
                    print(
                        f"Error: brief {brief_id} has reactions but no brief_type in ledger briefs",
                        file=sys.stderr,
                    )
                    return 1
            else:
                # Brief has reactions but no entry in briefs map or no brief_type
                print(
                    f"Error: brief {brief_id} has reactions but no brief_type in ledger briefs",
                    file=sys.stderr,
                )
                return 1

        # Assign tiers
        tiers = {}
        for brief_id in evidence:
            reactions_by_slug = evidence[brief_id]
            brief_type = briefs_map.get(brief_id, {}).get("brief_type", "")
            raiser_slug = raiser_map.get(brief_id)
            raw_brief_reactions = raw_reactions.get(brief_id, {})

            protested_by = [s for s, c in reactions_by_slug.items() if c == "protested"]
            needs_human_by = [s for s, c in reactions_by_slug.items() if c == "needs-human"]
            resolved_by = [s for s, c in reactions_by_slug.items() if c == "resolved"]
            agreed_by = [s for s, c in reactions_by_slug.items() if c == "agreed"]
            could_go_other_way = any(
                raw_brief_reactions.get(s, {}).get("could-go-other-way") is True
                for s in raw_brief_reactions
            )

            tier = schema.tier_item(
                brief_type or "",
                protested_by,
                needs_human_by,
                resolved_by,
                agreed_by,
                raiser_slug,
                could_go_other_way,
            )
            tiers[brief_id] = tier

        # Build the expected fragment
        expected_fragment = _build_fragment(tiers, briefs_map, ledger, decisions_dir)

        # Check if briefs is non-empty and ledger has no top-level handle keys
        ledger_handles = {k for k in ledger.keys() if isinstance(k, str) and schema.LEDGER_HANDLE_RE.fullmatch(k)}
        if briefs_map and not ledger_handles:
            print("checkpoint ledger has briefs but no top-level D/W/Q handle keys", file=sys.stderr)
            return 1

        # Compare ledger handles with expected fragment keys
        expected_keys = set(expected_fragment.keys())
        if ledger_handles != expected_keys:
            missing = expected_keys - ledger_handles
            extra = ledger_handles - expected_keys
            if missing:
                # Pick the first missing handle to report
                handle = sorted(missing)[0]
                print(f"checkpoint ledger is missing handle {handle}", file=sys.stderr)
                return 1
            if extra:
                # Pick the first extra handle to report
                handle = sorted(extra)[0]
                print(f"checkpoint ledger has unexpected handle {handle}", file=sys.stderr)
                return 1

        # Check that tier and brief_id match for each handle
        for handle in ledger_handles:
            expected_entry = expected_fragment.get(handle, {})
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

    # Read all expected reaction files and build evidence per brief
    evidence = {}  # brief_id -> {slug -> classification}
    raiser_map = {}  # brief_id -> raiser slug

    # Extract briefs map
    briefs_map = ledger.get("briefs", {})
    if not isinstance(briefs_map, dict):
        print("Error: ledger 'briefs' is not a dict", file=sys.stderr)
        return 1

    # First pass: collect raiser info from briefs
    for brief_id, brief_data in briefs_map.items():
        if isinstance(brief_data, dict):
            raiser = brief_data.get("raiser")
            if raiser:
                raiser_map[brief_id] = raiser

    for reaction_spec in ledger.get("expected_reactions", []):
        reaction_path_str = reaction_spec.get("path")
        if not reaction_path_str:
            continue

        # Resolve path: relative to decisions_dir or absolute
        if Path(reaction_path_str).is_absolute():
            reaction_path = Path(reaction_path_str)
        else:
            reaction_path = decisions_dir / reaction_path_str

        # Confine path to decisions_dir
        path_error = schema.confine_path_to_dir(str(reaction_path), decisions_dir)
        if path_error:
            print(f"Error: {path_error}", file=sys.stderr)
            return 1

        if not reaction_path.exists():
            print(f"Error: expected reaction file missing: {reaction_path_str}", file=sys.stderr)
            return 1

        try:
            reactions = json.loads(reaction_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f"Error reading reaction file {reaction_path_str}: {e}", file=sys.stderr)
            return 2

        # Validate reaction file shape
        reaction_error = schema.validate_reaction_file_shape(reactions)
        if reaction_error:
            print(f"Error in {reaction_path_str}: {reaction_error}", file=sys.stderr)
            return 1

        if not reactions.get("complete"):
            print(f"Error: reaction file missing 'complete': true sentinel: {reaction_path_str}", file=sys.stderr)
            return 1

        slug = reaction_spec.get("slug")
        if not slug:
            continue

        # Extract reactions per brief (all keys except 'complete')
        for brief_id, reaction in reactions.items():
            if brief_id == "complete":
                continue
            if not isinstance(reaction, dict):
                continue

            # Classify this reaction
            classification = schema.classify_reaction(reaction, slug)

            if brief_id not in evidence:
                evidence[brief_id] = {}
            evidence[brief_id][slug] = classification

    # Collect brief types from ledger (must be present before tiering)
    brief_types = {}

    for brief_id in evidence:
        if brief_id in briefs_map and isinstance(briefs_map[brief_id], dict):
            brief_type = briefs_map[brief_id].get("brief_type")
            if not brief_type:
                print(
                    f"Error: brief {brief_id} has reactions but no brief_type in ledger briefs",
                    file=sys.stderr,
                )
                return 1
            brief_types[brief_id] = brief_type
        else:
            # Brief has reactions but no entry in briefs map or no brief_type
            print(
                f"Error: brief {brief_id} has reactions but no brief_type in ledger briefs",
                file=sys.stderr,
            )
            return 1

    # For could-go-other-way check, need access to raw reactions
    # Re-collect raw reactions for flag checking
    raw_reactions = {}  # brief_id -> {slug -> raw_reaction_dict}
    for reaction_spec in ledger.get("expected_reactions", []):
        reaction_path_str = reaction_spec.get("path")
        if not reaction_path_str:
            continue

        # Resolve path
        if Path(reaction_path_str).is_absolute():
            reaction_path = Path(reaction_path_str)
        else:
            reaction_path = decisions_dir / reaction_path_str

        if not reaction_path.exists():
            continue

        try:
            reactions = json.loads(reaction_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue

        slug = reaction_spec.get("slug")
        if not slug:
            continue

        # Extract raw reactions per brief
        for brief_id, reaction in reactions.items():
            if brief_id == "complete":
                continue
            if not isinstance(reaction, dict):
                continue

            if brief_id not in raw_reactions:
                raw_reactions[brief_id] = {}
            raw_reactions[brief_id][slug] = reaction

    # Assign tiers based on rules
    tiers = {}

    for brief_id in evidence:
        reactions_by_slug = evidence[brief_id]  # Classification
        brief_type = brief_types.get(brief_id)
        raiser_slug = raiser_map.get(brief_id)
        raw_brief_reactions = raw_reactions.get(brief_id, {})

        # Collect reactions by type
        protested_by = [s for s, c in reactions_by_slug.items() if c == "protested"]
        needs_human_by = [s for s, c in reactions_by_slug.items() if c == "needs-human"]
        resolved_by = [s for s, c in reactions_by_slug.items() if c == "resolved"]
        agreed_by = [s for s, c in reactions_by_slug.items() if c == "agreed"]

        # Check for could-go-other-way flag in any expert's raw reaction
        could_go_other_way = any(
            raw_brief_reactions.get(s, {}).get("could-go-other-way") is True
            for s in raw_brief_reactions
        )

        # Apply tier rule
        tier = schema.tier_item(
            brief_type or "",
            protested_by,
            needs_human_by,
            resolved_by,
            agreed_by,
            raiser_slug,
            could_go_other_way,
        )

        tiers[brief_id] = tier

    # Output based on mode
    if args.fragment:
        fragment = _build_fragment(tiers, briefs_map, ledger, decisions_dir)
        print(json.dumps(fragment))
    else:
        # Default mode: output brief_id -> tier JSON
        print(json.dumps(tiers))

    return 0


if __name__ == "__main__":
    sys.exit(main())
