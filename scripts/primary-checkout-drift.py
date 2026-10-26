#!/usr/bin/env python3
"""
Report-only detector for drift into the primary checkout (the first git worktree entry).

Takes a baseline snapshot of the primary checkout's status, then compares it against
a later probe to detect new files or status changes. Never modifies any checkout.

Subcommands:
  snapshot --checkout <path> [--checkout <path>...] --orchestrator-cwd <path> --out <file>
    - Snapshot the git status of one or more checkouts
    - Skip when a checkout's realpath equals orchestrator cwd's realpath
    - Probe with: git -C <path> --no-optional-locks status --porcelain=v1 -z --untracked-files=all
    - Parse NUL-separated records, including rename/copy form (R/C entry + original path)
    - Write JSON: {"version":1,"checkouts":{"<realpath>":{"skipped":false|"primary-is-orchestrator-cwd","head":"<sha>","status":{"<path>":"XY"}}}}
    - Exit 2 if git fails or path is not a repo

  compare --baseline <file> --ticket-wt <path> --start-sha <sha> --planned <file>
    - Re-probe each non-skipped checkout from baseline
    - Compute planned set: paths from --planned file + git diff --name-only <start-sha> + uncommitted/untracked in ticket-wt
    - Classify each new/changed path: FAIL (planned) or WARN (unplanned)
    - Print lines: FAIL|WARN|INFO|SKIP <path> [ticket-match|ticket-differs|ticket-absent]
    - Exit 0 clean, 1 drift (FAIL), 2 error
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional


def resolve_path(path_str: str) -> str:
    """Resolve a path to its realpath, return as string."""
    try:
        return str(Path(path_str).resolve())
    except Exception:
        return path_str


def git_rev_parse_head(checkout_path: str) -> Optional[str]:
    """Get the HEAD commit SHA, return None on error."""
    try:
        result = subprocess.run(
            ["git", "-C", checkout_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def git_status_porcelain(checkout_path: str) -> Optional[Dict[str, str]]:
    """Probe git status with -z format, return dict of path -> XY status, or None on error."""
    try:
        result = subprocess.run(
            ["git", "-C", checkout_path, "--no-optional-locks", "status",
             "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None

        status_map = {}
        output = result.stdout

        i = 0
        while i < len(output):
            # Read XY + space (first 3 bytes)
            if i + 3 > len(output):
                break
            xy = output[i:i+2].decode("utf-8", errors="replace")
            space = output[i+2:i+3]

            if space != b" "[0:1]:
                # Malformed, skip
                i += 1
                continue

            i += 3

            # Read path until NUL
            path_end = output.find(b"\0", i)
            if path_end == -1:
                # No terminator, read rest
                path = output[i:].decode("utf-8", errors="replace")
                status_map[path] = xy
                break

            path = output[i:path_end].decode("utf-8", errors="replace")
            i = path_end + 1
            status_map[path] = xy

            # For R/C (rename/copy), there's an additional path (the "from" path)
            if xy[0] in ("R", "C"):
                # Skip the second path (original path in a rename/copy)
                path_end2 = output.find(b"\0", i)
                if path_end2 == -1:
                    break
                i = path_end2 + 1

        return status_map
    except Exception:
        return None


def cmd_snapshot(checkout_paths: list, orchestrator_cwd: str, out_file: str) -> int:
    """Take a snapshot of git status for each checkout. Return 0 on success, 2 on error."""
    orchestrator_cwd_real = resolve_path(orchestrator_cwd)
    checkouts = {}

    for checkout_path in checkout_paths:
        checkout_real = resolve_path(checkout_path)

        # Check if this checkout is the same as orchestrator cwd
        if checkout_real == orchestrator_cwd_real:
            checkouts[checkout_real] = {
                "skipped": "primary-is-orchestrator-cwd",
            }
            print(f"SKIP: {checkout_path} primary-is-orchestrator-cwd")
            continue

        # Get HEAD sha
        head_sha = git_rev_parse_head(checkout_path)
        if head_sha is None:
            print(f"ERROR: Cannot read HEAD in {checkout_path}", file=sys.stderr)
            return 2

        # Get status
        status_map = git_status_porcelain(checkout_path)
        if status_map is None:
            print(f"ERROR: Cannot read git status in {checkout_path}", file=sys.stderr)
            return 2

        checkouts[checkout_real] = {
            "skipped": False,
            "head": head_sha,
            "status": status_map,
        }

    # Write JSON
    try:
        output = {
            "version": 1,
            "checkouts": checkouts,
        }
        with open(out_file, "w") as f:
            json.dump(output, f)
        return 0
    except Exception as e:
        print(f"ERROR: Cannot write output file {out_file}: {e}", file=sys.stderr)
        return 2


def cmd_compare(
    baseline_file: str, ticket_wt: str, start_sha: str, planned_file: str
) -> int:
    """Compare current status against baseline. Return 0 clean, 1 drift, 2 error."""

    # Load baseline
    try:
        with open(baseline_file) as f:
            baseline = json.load(f)
    except Exception:
        print("REASON: primary-checkout-drift: baseline-unavailable", file=sys.stderr)
        return 2

    if baseline.get("version") != 1:
        print("REASON: primary-checkout-drift: baseline-unavailable", file=sys.stderr)
        return 2

    baseline_checkouts = baseline.get("checkouts", {})

    # Load planned paths
    planned_set = set()
    try:
        with open(planned_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    planned_set.add(line)
    except Exception:
        print("REASON: primary-checkout-drift: baseline-unavailable", file=sys.stderr)
        return 2

    # Add paths from git diff --name-only <start-sha>
    try:
        result = subprocess.run(
            ["git", "-C", ticket_wt, "diff", "--name-only", start_sha],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line:
                    planned_set.add(line)
    except Exception:
        pass

    # Add uncommitted and untracked paths from ticket worktree
    status_map = git_status_porcelain(ticket_wt)
    if status_map is not None:
        for path in status_map.keys():
            planned_set.add(path)

    # Re-probe each checkout from baseline
    drift_detected = False

    for checkout_real, baseline_entry in baseline_checkouts.items():
        # Skip if this entry was skipped at baseline
        if baseline_entry.get("skipped"):
            print(f"SKIP: {checkout_real} {baseline_entry.get('skipped')}")
            continue

        # Probe current status
        current_status = git_status_porcelain(checkout_real)
        if current_status is None:
            print("REASON: primary-checkout-drift: baseline-unavailable", file=sys.stderr)
            return 2

        # Get current HEAD
        current_head = git_rev_parse_head(checkout_real)
        baseline_head = baseline_entry.get("head")

        if current_head != baseline_head:
            print(f"INFO head-changed {baseline_head} {current_head}")

        # Check for new or changed paths
        baseline_status = baseline_entry.get("status", {})

        for path, xy in current_status.items():
            baseline_xy = baseline_status.get(path)

            if baseline_xy is None or baseline_xy != xy:
                # This is new or changed
                if path in planned_set:
                    # Compare content with ticket worktree
                    ticket_file = Path(ticket_wt) / path
                    primary_file = Path(checkout_real) / path

                    ticket_exists = ticket_file.exists()
                    primary_exists = primary_file.exists()

                    if not primary_exists:
                        print(f"FAIL {path} ticket-absent")
                        drift_detected = True
                    elif not ticket_exists:
                        print(f"FAIL {path} ticket-absent")
                        drift_detected = True
                    else:
                        # Compare hashes
                        try:
                            result_ticket = subprocess.run(
                                ["git", "-C", ticket_wt, "hash-object", path],
                                capture_output=True,
                                text=True,
                                timeout=10,
                            )
                            result_primary = subprocess.run(
                                ["git", "-C", checkout_real, "hash-object", path],
                                capture_output=True,
                                text=True,
                                timeout=10,
                            )

                            if result_ticket.returncode == 0 and result_primary.returncode == 0:
                                ticket_hash = result_ticket.stdout.strip()
                                primary_hash = result_primary.stdout.strip()

                                if ticket_hash == primary_hash:
                                    print(f"FAIL {path} ticket-match")
                                else:
                                    print(f"FAIL {path} ticket-differs")
                            else:
                                print(f"FAIL {path} ticket-absent")
                            drift_detected = True
                        except Exception:
                            print(f"FAIL {path} ticket-absent")
                            drift_detected = True
                else:
                    # Unplanned path
                    if baseline_xy is None:
                        # New file
                        print(f"WARN {path}")
                    else:
                        # Changed but unplanned
                        print(f"WARN {path}")
            elif baseline_xy is not None and baseline_xy == xy:
                # Pre-dirty planned path (already dirty at baseline, still dirty with same XY)
                if path in planned_set:
                    print(f"WARN pre-dirty-planned {path}")

    if drift_detected:
        # Collect FAIL paths for REASON line
        fail_paths = []
        for checkout_real, baseline_entry in baseline_checkouts.items():
            if baseline_entry.get("skipped"):
                continue
            current_status = git_status_porcelain(checkout_real)
            if current_status is None:
                continue
            baseline_status = baseline_entry.get("status", {})
            for path, xy in current_status.items():
                baseline_xy = baseline_status.get(path)
                if (baseline_xy is None or baseline_xy != xy) and path in planned_set:
                    fail_paths.append(path)

        reason_paths = ",".join(fail_paths)
        print(f"REASON: primary-checkout-drift: {reason_paths}", file=sys.stderr)
        return 1

    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # snapshot subcommand
    snapshot_parser = subparsers.add_parser("snapshot", help="Take a snapshot")
    snapshot_parser.add_argument("--checkout", action="append", required=True,
                                help="Checkout path (can be repeated)")
    snapshot_parser.add_argument("--orchestrator-cwd", required=True,
                                help="Orchestrator working directory")
    snapshot_parser.add_argument("--out", required=True, help="Output JSON file")

    # compare subcommand
    compare_parser = subparsers.add_parser("compare", help="Compare against baseline")
    compare_parser.add_argument("--baseline", required=True, help="Baseline JSON file")
    compare_parser.add_argument("--ticket-wt", required=True, help="Ticket worktree path")
    compare_parser.add_argument("--start-sha", required=True, help="Starting commit SHA")
    compare_parser.add_argument("--planned", required=True, help="Planned paths file")

    args = parser.parse_args()

    if args.command == "snapshot":
        return cmd_snapshot(args.checkout, args.orchestrator_cwd, args.out)
    elif args.command == "compare":
        return cmd_compare(args.baseline, args.ticket_wt, args.start_sha, args.planned)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
