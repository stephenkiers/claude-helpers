#!/usr/bin/env python3
"""Assign tiers (D/W/Q) to checkpoint items based on reaction-pass evidence."""

import argparse
import json
import sys
from pathlib import Path

# Add parent directory to path to import plan_checkpoint_schema
sys.path.insert(0, str(Path(__file__).parent))
import plan_checkpoint_schema as schema


def main() -> int:
    """Read ledger and reaction files, output handle->tier JSON.

    Returns:
        0 on success, 1 on validation error or missing file, 2 on internal error.
    """
    parser = argparse.ArgumentParser(
        description="Assign tiers (D/W/Q) to checkpoint items based on reaction-pass evidence."
    )
    parser.add_argument("--decisions", required=True, help="Path to decisions.md")
    args = parser.parse_args()

    decisions_path = Path(args.decisions)

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

    # Read all expected reaction files and build evidence per handle
    evidence = {}  # handle -> {slug -> classification}
    decisions_dir = decisions_path.parent
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

    # Output JSON
    print(json.dumps(tiers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
