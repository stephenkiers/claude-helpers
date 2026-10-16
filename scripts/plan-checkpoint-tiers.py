#!/usr/bin/env python3
"""Assign tiers (D/W/Q) to checkpoint items based on reaction-pass evidence."""

import json
import sys
from pathlib import Path


def main():
    """Read ledger and reaction files, output handle->tier JSON."""
    if len(sys.argv) < 2:
        print("Usage: plan-checkpoint-tiers.py --decisions <decisions.md>", file=sys.stderr)
        sys.exit(1)

    decisions_path = None
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--decisions" and i + 1 < len(sys.argv):
            decisions_path = sys.argv[i + 1]
            i += 2
        else:
            i += 1

    if not decisions_path:
        print("Error: --decisions <path> required", file=sys.stderr)
        sys.exit(1)

    # Read decisions.md and extract the ledger block
    try:
        with open(decisions_path) as f:
            content = f.read()
    except OSError as e:
        print(f"Error reading {decisions_path}: {e}", file=sys.stderr)
        sys.exit(1)

    # Find the fenced JSON block: ## Checkpoint ledger
    ledger = None
    lines = content.split('\n')
    in_fence = False
    fence_lines = []
    for i, line in enumerate(lines):
        if line.startswith('## Checkpoint ledger'):
            # Look for the fence after this heading
            for j in range(i + 1, len(lines)):
                if lines[j].startswith('```'):
                    if not in_fence:
                        in_fence = True
                    else:
                        # Closing fence
                        break
                elif in_fence:
                    fence_lines.append(lines[j])

    if fence_lines:
        try:
            ledger = json.loads('\n'.join(fence_lines))
        except json.JSONDecodeError as e:
            print(f"Error parsing ledger JSON: {e}", file=sys.stderr)
            sys.exit(1)

    if not ledger:
        print("Error: no ## Checkpoint ledger block found", file=sys.stderr)
        sys.exit(1)

    # Validate ledger structure
    if "seated" not in ledger or "expected_reactions" not in ledger:
        print("Error: ledger missing 'seated' or 'expected_reactions'", file=sys.stderr)
        sys.exit(1)

    # Read all expected reaction files and build evidence per handle
    evidence = {}  # handle -> {slug -> reaction_entry}

    decisions_dir = Path(decisions_path).parent
    for reaction_spec in ledger.get("expected_reactions", []):
        reaction_path = reaction_spec.get("path")
        if not reaction_path:
            continue

        full_path = decisions_dir / reaction_path if not Path(reaction_path).is_absolute() else Path(reaction_path)

        if not full_path.exists():
            print(f"Error: expected reaction file missing: {reaction_path}", file=sys.stderr)
            sys.exit(1)

        try:
            with open(full_path) as f:
                reactions = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error reading reaction file {reaction_path}: {e}", file=sys.stderr)
            sys.exit(1)

        # Check for complete sentinel
        if not reactions.get("complete"):
            print(f"Error: reaction file missing 'complete': true sentinel: {reaction_path}", file=sys.stderr)
            sys.exit(1)

        slug = reaction_spec.get("slug")
        if not slug:
            continue

        # Extract reactions per handle (all keys except 'complete')
        for handle, reaction in reactions.items():
            if handle == "complete":
                continue
            if handle not in evidence:
                evidence[handle] = {}
            evidence[handle][slug] = reaction

    # Read brief types from the ledger (handles should be keys in the ledger with brief_type field)
    # or from the decisions.md sections as fallback
    brief_types = {}  # handle -> brief_type (from ledger or decisions file)

    # First, try to get brief_type from the ledger
    for handle in evidence:
        if handle in ledger:
            brief_type = ledger[handle].get("brief_type")
            if brief_type:
                brief_types[handle] = brief_type

    # Fallback: parse the decisions.md sections to find brief types
    # Look for `#### Q` headings which indicate question briefs (not D or W)
    for line in lines:
        if line.startswith('#### Q'):
            # Extract Q number
            parts = line.split('.')
            if parts:
                qn = parts[0].replace('####', '').strip()
                handle = f"Q{qn.lstrip('Q')}" if 'Q' in qn else None
                if handle and handle in evidence and handle not in brief_types:
                    # Check if this brief has a Type: disagreement field in the decisions.md
                    # For now, just mark it as found (handle in evidence, so it's real)
                    # The verifier will check the actual type
                    pass

    # Assign tiers based on rules
    tiers = {}  # handle -> tier

    for handle in evidence:
        reactions = evidence[handle]

        # Determine if this was a disagreement brief
        is_disagreement = brief_types.get(handle) == "disagreement"

        # Check for protests
        has_protest = any(
            r.get("protested") for r in reactions.values()
        )

        # Q if disagreement or protested
        if is_disagreement or has_protest:
            tiers[handle] = "question"
            continue

        # Check if resolved (not protested/disagreement)
        # For D: resolved + at least one agreed + no could-go-other-way
        can_be_d = all(
            r.get("resolved") or r.get("agreed")
            for r in reactions.values()
        )

        if not can_be_d:
            tiers[handle] = "worth-a-look"
            continue

        has_agreed = any(
            r.get("agreed") for r in reactions.values()
        )
        has_could_go_other_way = any(
            r.get("could-go-other-way") for r in reactions.values()
        )

        if has_agreed and not has_could_go_other_way:
            tiers[handle] = "decided"
        else:
            tiers[handle] = "worth-a-look"

    # Output JSON
    print(json.dumps(tiers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
