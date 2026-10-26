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
    - Exit 0 if snapshot succeeds, 2 on error (git fails, path not a repo, unreadable output)

  compare --baseline <file> --ticket-wt <path> --start-sha <sha> --planned <file>
    - Re-probe each non-skipped checkout from baseline
    - Compute planned set: paths from --planned file + git diff --name-only <start-sha> + uncommitted/untracked in ticket-wt
    - Classify each new/changed path: FAIL (planned) or WARN (unplanned)
    - Print lines: FAIL|WARN|INFO|SKIP <path> [ticket-match|ticket-differs|ticket-absent]
    - Exit 0 clean, 1 drift (FAIL including head-moved), 2 error
    - ERROR lines to stdout: baseline-unavailable, probe-failed, planned-set-unavailable

Exit codes: 0 clean, 1 drift, 2 error (missing evidence, probe failures, etc.)
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Exit code constants
EXIT_CLEAN = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

# Timeout in seconds for git operations
GIT_TIMEOUT = 10


def resolve_path(path_str: str) -> str:
    """Resolve a path to its realpath, return as string."""
    try:
        return str(Path(path_str).resolve())
    except Exception as e:
        print(f"Failed to resolve path {path_str}: {e}", file=sys.stderr)
        return path_str


def git_rev_parse_head(checkout_path: str) -> Optional[str]:
    """Get the HEAD commit SHA, return None on error."""
    try:
        result = subprocess.run(
            ["git", "-C", checkout_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print(f"Timeout reading HEAD in {checkout_path}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Failed to read HEAD in {checkout_path}: {e}", file=sys.stderr)
        return None
    return None


def escape_control_chars(s: str) -> str:
    """Escape control characters (newline, ESC, etc.) in a string for safe printing."""
    result = []
    for c in s:
        if ord(c) < 32 and c not in ('\t',):
            # Escape as \xHH
            result.append(f"\\x{ord(c):02x}")
        elif ord(c) == 27:  # ESC
            result.append("\\x1b")
        elif ord(c) >= 127 and ord(c) < 160:
            # Other control characters
            result.append(f"\\x{ord(c):02x}")
        else:
            # Keep normal characters and non-ASCII printable text unchanged
            result.append(c)
    return "".join(result)


def git_status_porcelain(checkout_path: str) -> Optional[Dict[str, str]]:
    """Probe git status with -z format, return dict of path -> XY status, or None on error."""
    try:
        result = subprocess.run(
            ["git", "-C", checkout_path, "--no-optional-locks", "status",
             "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True,
            timeout=GIT_TIMEOUT,
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
            if xy.startswith("R") or xy.startswith("C"):
                # Record the original path too (F18)
                path_end2 = output.find(b"\0", i)
                if path_end2 == -1:
                    break
                origin_path = output[i:path_end2].decode("utf-8", errors="replace")
                # Store origin path with special key to not lose rename/copy origin
                status_map[origin_path] = xy
                i = path_end2 + 1

        return status_map
    except subprocess.TimeoutExpired:
        print(f"Timeout reading git status in {checkout_path}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Failed to read git status in {checkout_path}: {e}", file=sys.stderr)
        return None


def cmd_snapshot(checkout_paths: List[str], orchestrator_cwd: str, out_file: str) -> int:
    """Take a snapshot of git status for each checkout. Return EXIT_CLEAN on success, EXIT_ERROR on error."""
    orchestrator_cwd_real = resolve_path(orchestrator_cwd)
    checkouts: Dict[str, Any] = {}

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
            print("ERROR probe-failed", file=sys.stdout)
            return EXIT_ERROR

        # Get status
        status_map = git_status_porcelain(checkout_path)
        if status_map is None:
            print("ERROR probe-failed", file=sys.stdout)
            return EXIT_ERROR

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
        return EXIT_CLEAN
    except Exception as e:
        print(f"ERROR baseline-unavailable {e}", file=sys.stdout)
        return EXIT_ERROR


def load_baseline(baseline_file: str) -> Optional[Dict[str, Any]]:
    """Load and validate the baseline JSON file. Return the baseline dict, or None on error."""
    try:
        with open(baseline_file) as f:
            baseline = json.load(f)
    except Exception:
        print("ERROR baseline-unavailable", file=sys.stdout)
        return None

    if baseline.get("version") != 1:
        print("ERROR baseline-unavailable", file=sys.stdout)
        return None

    return baseline


def build_planned_set(planned_file: str, ticket_wt: str, start_sha: str) -> Optional[Set[str]]:
    """Build the set of planned paths from file, git diff, and ticket worktree status.
    Return the set, or None on error."""
    planned_set: Set[str] = set()

    # Load paths from planned file
    try:
        with open(planned_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    planned_set.add(line)
    except Exception:
        print("ERROR planned-set-unavailable", file=sys.stdout)
        return None

    # Add paths from git diff --name-only -z <start-sha>
    try:
        result = subprocess.run(
            ["git", "-C", ticket_wt, "diff", "--name-only", "-z", start_sha],
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
        if result.returncode != 0:
            print("ERROR planned-set-unavailable", file=sys.stdout)
            return None
        # Parse NUL-separated paths
        if result.stdout:
            for path_bytes in result.stdout.split(b"\0"):
                if path_bytes:
                    path = path_bytes.decode("utf-8", errors="replace")
                    if path:
                        planned_set.add(path)
    except subprocess.TimeoutExpired:
        print("ERROR planned-set-unavailable", file=sys.stdout)
        return None
    except Exception:
        print("ERROR planned-set-unavailable", file=sys.stdout)
        return None

    # Add uncommitted and untracked paths from ticket worktree
    status_map = git_status_porcelain(ticket_wt)
    if status_map is None:
        print("ERROR planned-set-unavailable", file=sys.stdout)
        return None
    for path in status_map.keys():
        planned_set.add(path)

    return planned_set


def cmd_compare(
    baseline_file: str, ticket_wt: str, start_sha: str, planned_file: str
) -> int:
    """Compare current status against baseline. Return EXIT_CLEAN, EXIT_DRIFT, or EXIT_ERROR."""

    # Load baseline
    baseline = load_baseline(baseline_file)
    if baseline is None:
        return EXIT_ERROR

    baseline_checkouts = baseline.get("checkouts", {})

    # Load planned paths
    planned_set = build_planned_set(planned_file, ticket_wt, start_sha)
    if planned_set is None:
        return EXIT_ERROR

    # Re-probe each checkout from baseline
    fail_paths: Set[str] = set()  # Track FAIL paths for REASON line

    for checkout_real, baseline_entry in baseline_checkouts.items():
        # Skip if this entry was skipped at baseline
        if baseline_entry.get("skipped"):
            print(f"SKIP: {checkout_real} {baseline_entry.get('skipped')}")
            continue

        # Probe current status
        current_status = git_status_porcelain(checkout_real)
        if current_status is None:
            print("ERROR probe-failed", file=sys.stdout)
            return EXIT_ERROR

        # Get current HEAD
        current_head = git_rev_parse_head(checkout_real)
        if current_head is None:
            print("ERROR probe-failed", file=sys.stdout)
            return EXIT_ERROR

        baseline_head = baseline_entry.get("head")

        # Check for HEAD movement (F10: this is now a FAIL)
        if current_head != baseline_head:
            escaped_checkout = escape_control_chars(checkout_real)
            print(f"FAIL {escaped_checkout} head-moved")
            fail_paths.add(escaped_checkout)

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

                    escaped_path = escape_control_chars(path)

                    if not primary_exists:
                        print(f"FAIL {escaped_path} ticket-absent")
                        fail_paths.add(escaped_path)
                    elif not ticket_exists:
                        print(f"FAIL {escaped_path} ticket-absent")
                        fail_paths.add(escaped_path)
                    else:
                        # Compare hashes
                        try:
                            result_ticket = subprocess.run(
                                ["git", "-C", ticket_wt, "hash-object", "--", path],
                                capture_output=True,
                                text=True,
                                timeout=GIT_TIMEOUT,
                            )
                            result_primary = subprocess.run(
                                ["git", "-C", checkout_real, "hash-object", "--", path],
                                capture_output=True,
                                text=True,
                                timeout=GIT_TIMEOUT,
                            )

                            if result_ticket.returncode == 0 and result_primary.returncode == 0:
                                ticket_hash = result_ticket.stdout.strip()
                                primary_hash = result_primary.stdout.strip()

                                if ticket_hash == primary_hash:
                                    print(f"FAIL {escaped_path} ticket-match")
                                else:
                                    print(f"FAIL {escaped_path} ticket-differs")
                            else:
                                print(f"FAIL {escaped_path} ticket-absent")
                            fail_paths.add(escaped_path)
                        except (subprocess.TimeoutExpired, Exception):
                            print(f"FAIL {escaped_path} ticket-absent")
                            fail_paths.add(escaped_path)
                else:
                    # Unplanned path
                    escaped_path = escape_control_chars(path)
                    print(f"WARN {escaped_path}")
            elif baseline_xy is not None and baseline_xy == xy:
                # Pre-dirty planned path (already dirty at baseline, still dirty with same XY)
                if path in planned_set:
                    escaped_path = escape_control_chars(path)
                    print(f"WARN pre-dirty-planned {escaped_path}")

    if fail_paths:
        # Print REASON line with collected FAIL paths (F17: reuse first-pass FAIL set)
        reason_paths = ",".join(sorted(fail_paths))
        print(f"ERROR primary-checkout-drift {reason_paths}", file=sys.stdout)
        return EXIT_DRIFT

    return EXIT_CLEAN


def main() -> int:
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
        return EXIT_ERROR


if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except Exception as e:
        print(f"ERROR probe-failed {e}", file=sys.stdout)
        sys.exit(EXIT_ERROR)
