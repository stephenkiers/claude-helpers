#!/usr/bin/env python3
"""
Report-only detector for drift into the primary checkout.

Takes a baseline snapshot of the primary checkout's status, then compares it against
a later probe to detect new files or status changes. Never modifies any checkout.

Primary checkout (resolved by `primary`, from `git worktree list --porcelain`):
  Rule 1: if the first worktree entry is not bare, it is the primary checkout. Its path
          is printed exactly as git reports it (`primary` reads `-z` output, so no quoting or
          newline splitting), even if it is prunable or on a feature branch. `primary` also
          requires it to be an existing directory; otherwise rule 1 is skipped.
  Rule 2: otherwise (first entry bare, no entries, or rule 1 skipped), the primary checkout is
          the first worktree on refs/heads/<candidate>, trying candidates in order: the
          origin/HEAD short name, then main, then master (de-duplicated).
  Otherwise there is no primary checkout and the resolver fails closed. A rule-2 match
  that is prunable, or not an existing directory, also fails closed.
  Note: for a first-entry path containing a space, the old `head -1 | cut -d' ' -f2`
  pipeline truncated the path at the first space; rule 1 returns the full path.

Subcommands:
  snapshot --checkout <path> [--checkout <path>...] --orchestrator-cwd <path> --out <file>
    - Snapshot the git status of one or more checkouts
    - Skip when a checkout's realpath equals orchestrator cwd's realpath
    - Probe with: git -C <path> --no-optional-locks status --porcelain=v1 -z --untracked-files=all
    - Parse NUL-separated records, including rename/copy form (R/C entry + original path)
    - Write JSON: {"version":1,"checkouts":{"<realpath>":{"skipped":false|"primary-is-orchestrator-cwd","head":"<sha>","branch":"<ref>"|null,"status":{"<path>":"XY"}}}}
    - The optional "branch" field is present on all non-skipped entries: a ref string (e.g., "refs/heads/main") when on a branch, null when detached
    - Exit 0 if snapshot succeeds, 2 on error (git fails, path not a repo, unreadable output)
    - On a probe failure it prints the stable stdout line `ERROR probe-failed`; the doc's site-1
      handler matches it with grep -q '^ERROR probe-failed'. Exit 2 also covers a write failure,
      which prints `ERROR baseline-unavailable` (unchanged)

  compare --baseline <file> --ticket-wt <path> --start-sha <sha> --planned <file>
    - Re-probe each non-skipped checkout from baseline
    - Compute planned set: paths from --planned file + git diff --name-only <start-sha> + uncommitted/untracked in ticket-wt
    - Classify each new/changed path: FAIL (planned) or WARN (unplanned)
    - Print lines: FAIL|WARN|INFO|SKIP <path> [ticket-match|ticket-differs|ticket-absent]
    - HEAD movement rule: branch changed, non-ancestor move, no resolvable upstream, move not reachable from upstream, or move against baseline without "branch" key is FAIL <primary> head-moved
    - A fast-forward reachable from the upstream is INFO <primary> head-advanced <old>..<new>, exit 0
    - Exit 0 clean, 1 drift, 2 error (probe failed including head-probe)
    - ERROR lines to stdout: baseline-unavailable, probe-failed, probe-failed head-probe, planned-set-unavailable
      (exit 1 drift also prints `ERROR primary-checkout-drift <fail paths>`)

  fail --compare-exit <1|2> [--cause <probe-failed|baseline-unavailable|no-usable-primary|resolver-failed>]
       (--cause with --reason is an argparse error, exit 2)
       [--label <text>] [--receipt <path>] [--attempt <k>]
       [--implementer-model <m>] [--units <n>] [--max-turns <n>] [--fail-paths <a,b>]
       [--reason <primary-checkout-drift: ...>] [--detail --warn-paths <a,b>
       --ticket-match <path:status>... --owned-overlap <unit:paths>...]
       [--overlap-files <a,b>] [--patch-path <p>] [--drifted-files <a,b>] [--untracked-files <a,b>]
    - Multi-unit sites: --overlap-files writes OVERLAP_FILES; --patch-path/--drifted-files/
      --untracked-files write PATCH_PATH/DRIFTED_FILES/UNTRACKED_FILES plus the standard NOTE
    - The single shared drift-barrier failure mechanism: prints the stderr
      FAILED line, best-effort emits run-metrics.py command-end (failure-class cwd_drift),
      and writes the FAILED receipt (ending with <!-- step-end -->) only when --receipt is given
    - --compare-exit 1 = drift (Reason: primary-checkout-drift: <fail paths>), 2 = error (baseline unavailable or head probe failed); --reason applies to both
    - --reason values emitted by the barrier docs: `primary-checkout-drift: head-probe-failed`
      (compare-exit 2, head probe failed; prints `FAILED: primary-checkout-drift head probe failed`)
    - --cause (valid with --compare-exit 2 only; default baseline-unavailable) sets the Reason
      to primary-checkout-drift: <cause>. --cause together with --reason is rejected by argparse
      (exit 2, nothing written). With --compare-exit 1 --cause is ignored with the stderr warning
      `fail: --cause is only valid with --compare-exit 2; ignored`. baseline-unavailable
      keeps the original text.
      Causes and their producers (the four values of the Cause type in the code):
        probe-failed       site 1 snapshot, when its output has an ERROR probe-failed line
        baseline-unavailable  default: a snapshot failing for another reason, and every
                           compare barrier's exit 2
        no-usable-primary  site 1 when `primary` exits 3, and a failed <PRIMARY_CHECKOUT>
                           re-check at the later consuming sites (never inside the cwd-drift
                           failure procedure, where a re-check failure keeps the drift reason
                           and is reported only in the label)
        resolver-failed    site 1 when `primary` exits anything other than 0 or 3
      FAILURE_CLASS: cwd-drift for every cause
    - Always exits 1 (the code every barrier uses); telemetry or receipt problems never change it

  primary
    - Print the primary checkout path (rules above) on stdout; no path arguments; runs from cwd
    - Exit 0: exactly one path and a newline on stdout
    - Exit 3 (EXIT_NO_PRIMARY) means only "no candidate": stdout empty, stderr
      `NOTE: no usable primary checkout (<detail>)`
    - Exit 2: stdout empty, stderr `ERROR resolver-failed <detail>` (git failure or non-zero
      exit, timeout, unparseable output, any exception)

Exit codes and streams:
  subcommand  exit 0            exit 1                exit 2                            exit 3
  snapshot    snapshot written  -                     ERROR lines on stdout:            -
                                                      probe-failed, baseline-unavailable
  compare     clean             drift: FAIL lines     ERROR lines on stdout:            -
                                and ERROR lines       baseline-unavailable,
                                                      probe-failed, planned-set-unavailable
  fail        -                 always 1 (barrier)    -                                 -
  primary     one path on stdout  -                   ERROR resolver-failed on STDERR   NOTE: no usable
                                                                                        primary checkout
                                                                                        on STDERR

Note: `primary` deliberately puts its errors on STDERR as `ERROR resolver-failed ...`, while the
other subcommands put ERROR lines on stdout. Exit 3 means only "no candidate", never a failure.
The snapshot's failure line is `ERROR probe-failed` on stdout (the step's grep depends on that exact prefix).
An exception that escapes main prints `ERROR unexpected-error <detail>` on stdout and exits 2.
Exit codes: 0 clean, 1 drift, 2 error (missing evidence, probe failures, etc.), 3 no primary candidate (primary only).
The rules and exit codes are recorded in docs/adr/0008-machine-enforced-agent-guardrails.md (2026-11-05 amendment).

The detector never fetches or refreshes any ref; it is report-only and read-only. The benign fast-forward path
only helps where the primary's branch has a tracked upstream. Commits in the primary are primarily prevented
by scripts/plan-implementer-git-guard.py, with this check as the backstop (a commit that is also pushed upstream
would pass, and pushes are guarded separately).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Literal, NamedTuple, Optional, Set, Tuple, get_args

# Outcome of classify_head_move(): how HEAD moved relative to the baseline.
HeadOutcome = Literal["clean", "advanced", "fail", "error"]

# Values of `fail --cause` (the single source: argparse choices and the cause default derive from it).
Cause = Literal["probe-failed", "baseline-unavailable", "no-usable-primary", "resolver-failed"]
CAUSES: Tuple[str, ...] = get_args(Cause)

# Exit code constants
EXIT_CLEAN = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2
# `primary` only: no candidate worktree (not a git failure)
EXIT_NO_PRIMARY = 3

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


def git_symbolic_ref_head(checkout_path: str) -> str:
    """
    Get the symbolic ref for HEAD. Returns the ref string (e.g., "refs/heads/main") on exit 0,
    "detached" on exit 1, and "probe-failed" on any other exit, timeout, or OS error (the cause is
    printed to stderr).
    """
    try:
        result = subprocess.run(
            ["git", "-C", checkout_path, "symbolic-ref", "-q", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        elif result.returncode == 1:
            return "detached"
        else:
            return "probe-failed"
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Failed to read symbolic-ref HEAD in {checkout_path}: {e}", file=sys.stderr)
        return "probe-failed"


def git_is_ancestor(checkout_path: str, ancestor: str, descendant: str) -> Literal["ancestor", "not-ancestor", "probe-failed"]:
    """
    Check if ancestor is an ancestor of descendant, three-valued:
    Returns "ancestor" (exit 0), "not-ancestor" (exit 1), or "probe-failed" (any other exit, timeout,
    or OS error; the cause is printed to stderr).
    """
    try:
        result = subprocess.run(
            ["git", "-C", checkout_path, "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
        if result.returncode == 0:
            return "ancestor"
        elif result.returncode == 1:
            return "not-ancestor"
        else:
            return "probe-failed"
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Failed to check ancestry in {checkout_path}: {e}", file=sys.stderr)
        return "probe-failed"


def git_resolve_upstream(checkout_path: str) -> Optional[str]:
    """
    Resolve @{upstream} for the current branch. Returns the upstream SHA (exit 0), None for no
    upstream (exit 1), or the string "probe-failed" for any other exit, timeout, or OS error
    (the cause is printed to stderr). The "probe-failed" sentinel is a str, so callers must check
    it before using the value as a SHA.
    """
    try:
        result = subprocess.run(
            ["git", "-C", checkout_path, "rev-parse", "--verify", "--quiet", "@{upstream}"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        elif result.returncode == 1:
            return None
        else:
            return "probe-failed"
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Failed to resolve upstream in {checkout_path}: {e}", file=sys.stderr)
        return "probe-failed"


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
                # Record the original path too
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

        # Get branch (symbolic ref)
        branch = git_symbolic_ref_head(checkout_path)
        if branch == "probe-failed":
            print("ERROR probe-failed", file=sys.stdout)
            return EXIT_ERROR

        # branch is either a ref string or "detached" (probe-failed was handled above).
        # Convert "detached" to null for JSON
        branch_value: Optional[str] = None if branch == "detached" else branch

        checkouts[checkout_real] = {
            "skipped": False,
            "head": head_sha,
            "branch": branch_value,
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


def classify_head_move(
    checkout_real: str, baseline_entry: Dict[str, Any], baseline_head: str, current_head: str
) -> HeadOutcome:
    """Classify how HEAD moved since the baseline. Returns "clean", "advanced", "fail", or "error".

    Decision table (rows are checked top to bottom; the first match wins):

    | baseline entry              | observation                                          | outcome  |
    |-----------------------------|------------------------------------------------------|----------|
    | no "branch" key (legacy)    | HEAD differs from baseline                           | fail     |
    | no "branch" key (legacy)    | HEAD unchanged                                       | clean    |
    | has "branch" key            | HEAD's symbolic ref cannot be probed                 | error    |
    | has "branch" key            | branch differs (ref vs ref, or detached vs ref)      | fail     |
    | has "branch" key            | HEAD unchanged                                       | clean    |
    | has "branch" key            | ancestry probe errors                                | error    |
    | has "branch" key            | baseline HEAD is not an ancestor (reset, rewrite)    | fail     |
    | has "branch" key            | upstream probe errors                                | error    |
    | has "branch" key            | no upstream configured                               | fail     |
    | has "branch" key            | reachability probe from upstream errors              | error    |
    | has "branch" key            | current HEAD reachable from upstream (fast-forward)  | advanced |
    | has "branch" key            | current HEAD not reachable from upstream             | fail     |

    The caller prints the lines: "fail" becomes FAIL <path> head-moved, "advanced" becomes
    INFO <path> head-advanced, and "error" becomes ERROR probe-failed head-probe (exit 2).
    """
    if "branch" not in baseline_entry:
        return "clean" if current_head == baseline_head else "fail"

    current_branch = git_symbolic_ref_head(checkout_real)
    if current_branch == "probe-failed":
        return "error"

    # Convert "detached" to None for comparison
    current_branch_value: Optional[str] = None if current_branch == "detached" else current_branch
    baseline_branch_value: Optional[str] = baseline_entry.get("branch")
    if current_branch_value != baseline_branch_value:
        return "fail"

    if current_head == baseline_head:
        return "clean"

    is_ancestor_result = git_is_ancestor(checkout_real, baseline_head, current_head)
    if is_ancestor_result == "probe-failed":
        return "error"
    if is_ancestor_result == "not-ancestor":
        return "fail"

    # Upstream is read from the checkout's own config. Pushes are guarded by the flow Bash guard,
    # so this reachability check is the backstop; a snapshot-recorded upstream is out of scope.
    upstream_sha = git_resolve_upstream(checkout_real)
    if upstream_sha == "probe-failed":
        return "error"
    if upstream_sha is None:
        return "fail"

    reachable_result = git_is_ancestor(checkout_real, current_head, upstream_sha)
    if reachable_result == "probe-failed":
        return "error"
    if reachable_result == "ancestor":
        return "advanced"
    return "fail"


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
        if not isinstance(baseline_head, str) or not baseline_head:
            print("ERROR baseline-unavailable", file=sys.stdout)
            return EXIT_ERROR

        # P is the escaped checkout path
        P = escape_control_chars(checkout_real)

        head_outcome = classify_head_move(checkout_real, baseline_entry, baseline_head, current_head)
        if head_outcome == "error":
            print("ERROR probe-failed head-probe", file=sys.stdout)
            return EXIT_ERROR
        if head_outcome == "fail":
            print(f"FAIL {P} head-moved")
            fail_paths.add(P)
        elif head_outcome == "advanced":
            # Fast-forward reachable from upstream. Escaping is per character, so escaping each
            # SHA on its own equals escaping the whole INFO line once (P is already escaped).
            print(f"INFO {P} head-advanced "
                  f"{escape_control_chars(baseline_head)}..{escape_control_chars(current_head)}")

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
        # Print REASON line with collected FAIL paths (reuse first-pass FAIL set)
        reason_paths = ",".join(sorted(fail_paths))
        print(f"ERROR primary-checkout-drift {reason_paths}", file=sys.stdout)
        return EXIT_DRIFT

    return EXIT_CLEAN


REASON_PREFIX = "primary-checkout-drift:"
RECOVER_NOTE = (
    "NOTE: nothing in the primary checkout was changed by this run's orchestrator; "
    "recover by hand"
)


def _one_line(value: str) -> str:
    """Collapse a field value onto one line so it cannot forge extra receipt lines."""
    return " ".join(str(value).split())


def emit_cwd_drift_telemetry() -> None:
    """Best-effort run-metrics command-end (failure-class cwd_drift); never raises."""
    script = Path(__file__).parent / "run-metrics.py"
    try:
        subprocess.run(
            [sys.executable, str(script), "command-end", "--command", "implement-with-haiku",
             "--outcome", "failure", "--failure-class", "cwd_drift"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:
        pass


def build_fail_receipt(args: argparse.Namespace, reason: str) -> str:
    """Render the FAILED receipt text (always ends with the step-end marker)."""
    lines = [
        f"ATTEMPT: {_one_line(args.attempt)}",
        "Decision: FAILED",
        f"Reason: {reason}",
        "FAILURE_CLASS: cwd-drift",
        f"SPLIT_BASIS: {_one_line(args.split_basis)}",
        f"MIN_UNITS: {_one_line(args.min_units)}",
        f"PRIOR_SPLIT_BASIS: {_one_line(args.prior_split_basis)}",
    ]
    if args.detail and args.compare_exit == EXIT_DRIFT:
        lines.append(f"FAIL_PATHS: {_one_line(args.fail_paths)}")
        lines.append(f"WARN_PATHS: {_one_line(args.warn_paths)}")
        lines.append("TICKET_MATCH_STATUS:")
        for entry in args.ticket_match:
            path, _, status = entry.rpartition(":")
            lines.append(f"  {_one_line(path)}: {_one_line(status)}")
        lines.append("OWNED_FILE_OVERLAP:")
        for entry in args.owned_overlap:
            unit, _, paths = entry.partition(":")
            lines.append(f"  {_one_line(unit)}: {_one_line(paths)}")
        lines.append(RECOVER_NOTE)
    if args.overlap_files:
        lines.append(f"OVERLAP_FILES: {_one_line(args.overlap_files)}")
    if args.patch_path or args.drifted_files or args.untracked_files:
        lines.append(f"PATCH_PATH: {_one_line(args.patch_path)}")
        lines.append(f"DRIFTED_FILES: {_one_line(args.drifted_files)}")
        lines.append(f"UNTRACKED_FILES: {_one_line(args.untracked_files)}")
        lines.append(RECOVER_NOTE)
    lines += [
        f"IMPLEMENTER_MODEL: {_one_line(args.implementer_model)}",
        f"UNITS: {_one_line(args.units)}",
        "TURNS_USED: unknown",
        f"MAX_TURNS: {_one_line(args.max_turns)}",
        "TURNS_EXHAUSTED: unknown",
        "<!-- step-end -->",
    ]
    return "\n".join(lines) + "\n"


def cmd_fail(args: argparse.Namespace) -> int:
    """Shared drift-barrier failure: stderr line, best-effort telemetry, optional receipt."""
    if args.compare_exit == EXIT_DRIFT:
        suffix = f" {args.label}" if args.label else ""
        print(f"FAILED: primary-checkout-drift detected \u2014 cwd drift found{suffix}", file=sys.stderr)
        reason = args.reason or f"{REASON_PREFIX} {_one_line(args.fail_paths) or 'unknown'}"
        if args.cause is not None:
            print("fail: --cause is only valid with --compare-exit 2; ignored", file=sys.stderr)
    else:
        # Exit 2: --reason (e.g. head-probe-failed) wins (--cause with --reason is rejected in main);
        # otherwise --cause picks the Reason
        cause = args.cause or "baseline-unavailable"
        suffix = f" {args.label}" if args.label else ""
        if args.reason:
            reason = args.reason
            if args.reason.startswith("primary-checkout-drift: head-probe-failed"):
                print(f"FAILED: primary-checkout-drift head probe failed{suffix}", file=sys.stderr)
            else:
                print(f"FAILED: primary-checkout-drift baseline unavailable{suffix}", file=sys.stderr)
        elif args.cause is None:
            # No --cause: keep today's exact stderr text (default path)
            print(f"FAILED: primary-checkout-drift baseline unavailable{suffix}", file=sys.stderr)
            reason = f"{REASON_PREFIX} {cause}"
        else:
            print(f"FAILED: primary-checkout-drift {cause}{suffix}", file=sys.stderr)
            reason = f"{REASON_PREFIX} {cause}"
    if not reason.startswith(REASON_PREFIX):
        print(f"fail: --reason must start with '{REASON_PREFIX}'", file=sys.stderr)
        reason = f"{REASON_PREFIX} {_one_line(reason)}"
    reason = _one_line(reason)

    emit_cwd_drift_telemetry()

    if args.receipt:
        try:
            Path(args.receipt).write_text(build_fail_receipt(args, reason))
        except OSError as e:
            print(f"fail: could not write receipt {args.receipt}: {e}", file=sys.stderr)
    return EXIT_DRIFT


class WorktreeRecord(NamedTuple):
    """One entry of `git worktree list --porcelain`."""

    path: str
    branch: Optional[str]
    bare: bool
    detached: bool  # retained for the parse contract (tests read it); select_primary does not use it
    prunable: bool


class PrimarySelection(NamedTuple):
    """Result of select_primary: the chosen path (None = fail closed) and a human-readable detail."""

    path: Optional[str]
    detail: str


def parse_worktree_porcelain(text: str) -> List[WorktreeRecord]:
    """Split `git worktree list --porcelain` output into one record per blank-line block.

    Accepts the `-z` form (NUL-separated fields, an empty field ending each record; used by
    cmd_primary, so a newline inside a path cannot split a record) and the newline form.
    Raises ValueError when a block does not start with a `worktree ` line or repeats it.
    Other attribute lines (HEAD, locked, ...) are ignored.
    """
    sep = "\0" if "\0" in text else "\n"
    blocks: List[List[str]] = []
    current: List[str] = []
    for line in text.split(sep):
        if line == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)

    records: List[WorktreeRecord] = []
    for block in blocks:
        if not block[0].startswith("worktree "):
            raise ValueError(f"worktree block does not start with 'worktree ': {block[0]!r}")
        if any(line.startswith("worktree ") for line in block[1:]):
            raise ValueError("worktree block repeats the 'worktree ' line")
        path = block[0][len("worktree "):]
        branch: Optional[str] = None
        bare = False
        detached = False
        prunable = False
        for line in block[1:]:
            if line.startswith("branch "):
                branch = line[len("branch "):]
            elif line == "bare":
                bare = True
            elif line == "detached":
                detached = True
            elif line == "prunable" or line.startswith("prunable "):
                prunable = True
        records.append(WorktreeRecord(path, branch, bare, detached, prunable))
    return records


def select_primary(
    records: List[WorktreeRecord], default_branch_candidates: List[str], *, require_dir: bool = False
) -> PrimarySelection:
    """Pick the primary checkout from parsed records. Pure: returns PrimarySelection(path, detail).

    Rule 1: a first record that is not bare is the primary, path as given.
    Rule 2: otherwise the first worktree on refs/heads/<candidate>, candidates in order.
    A rule-2 match that is prunable, or no match at all, returns path None (fail closed).
    With require_dir (used by cmd_primary), rule 1 applies only when the path is an existing
    directory, otherwise rule 2 decides; a rule-2 match that is not a directory also fails closed.
    """
    if records and not records[0].bare and (not require_dir or os.path.isdir(records[0].path)):
        return PrimarySelection(records[0].path, "first worktree entry (not bare)")
    for candidate in default_branch_candidates:
        wanted = f"refs/heads/{candidate}"
        for record in records:
            if record.branch == wanted:
                if record.prunable:
                    return PrimarySelection(None, f"worktree {record.path} on {wanted} is prunable")
                if require_dir and not os.path.isdir(record.path):
                    return PrimarySelection(None, f"worktree {record.path} on {wanted} is not a directory")
                return PrimarySelection(record.path, f"worktree on {wanted}")
    return PrimarySelection(None, "no worktree on refs/heads/" + "|".join(default_branch_candidates))


def resolve_default_branch_candidates() -> List[str]:
    """Candidate default branches in order: origin/HEAD short name, then main, then master.

    A failed `git symbolic-ref` (non-zero exit) only shortens the list. Timeouts and other
    exceptions propagate to the caller.
    """
    candidates: List[str] = []
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    if result.returncode == 0:
        ref = result.stdout.strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix) and len(ref) > len(prefix):
            candidates.append(ref[len(prefix):])
    for name in ("main", "master"):
        if name not in candidates:
            candidates.append(name)
    return candidates


def cmd_primary() -> int:
    """Print the primary checkout path on stdout. Exit 0 path, 3 no candidate, 2 resolver failure.

    Errors go to stderr. The whole body is guarded so the __main__ catch-all (which prints
    to stdout) is never reached from this subcommand.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        if result.returncode != 0:
            print(f"ERROR resolver-failed git worktree list exited {result.returncode}", file=sys.stderr)
            return EXIT_ERROR
        records = parse_worktree_porcelain(result.stdout)
        path, detail = select_primary(records, resolve_default_branch_candidates(), require_dir=True)
        if path is None:
            print(f"NOTE: no usable primary checkout ({detail})", file=sys.stderr)
            return EXIT_NO_PRIMARY
        print(path)
        return EXIT_CLEAN
    except Exception as e:
        print(f"ERROR resolver-failed {e}", file=sys.stderr)
        return EXIT_ERROR


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

    fail_parser = subparsers.add_parser("fail", help="Shared drift-barrier failure (telemetry + receipt)")
    fail_parser.add_argument("--compare-exit", type=int, choices=[EXIT_DRIFT, EXIT_ERROR], required=True,
                             help="Exit code the compare (or snapshot) returned: 1 drift, 2 baseline unavailable or head probe failed")
    fail_parser.add_argument("--label", default="", help="Barrier label appended to the stderr line")
    fail_parser.add_argument("--receipt", default="", help="Receipt path; omit to write no receipt")
    fail_parser.add_argument("--attempt", default="1")
    fail_parser.add_argument("--implementer-model", default="haiku")
    fail_parser.add_argument("--units", default="1")
    fail_parser.add_argument("--max-turns", default="unknown")
    fail_parser.add_argument("--fail-paths", default="", help="Comma-separated FAIL paths")
    fail_parser.add_argument("--cause", default=None,
                             choices=list(CAUSES),
                             help="Cause for --compare-exit 2 (default baseline-unavailable)")
    fail_parser.add_argument("--reason", default="", help="Override Reason (must start with primary-checkout-drift:)")
    fail_parser.add_argument("--detail", action="store_true",
                             help="Write the richer drift receipt sections (drift only)")
    fail_parser.add_argument("--warn-paths", default="", help="Comma-separated WARN paths (with --detail)")
    fail_parser.add_argument("--ticket-match", action="append", default=[],
                             help="<path>:<ticket-match|ticket-differs|ticket-absent>, repeatable (with --detail)")
    fail_parser.add_argument("--owned-overlap", action="append", default=[],
                             help="<unit-id>:<comma-separated owned paths>, repeatable (with --detail)")

    fail_parser.add_argument("--overlap-files", default="",
                             help="Comma-separated files overlapping other units' owned files")
    fail_parser.add_argument("--patch-path", default="", help="Path of the saved drifted patch")
    fail_parser.add_argument("--drifted-files", default="", help="Comma-separated drifted files")
    fail_parser.add_argument("--untracked-files", default="", help="Comma-separated untracked drifted files")

    fail_parser.add_argument("--split-basis", default="none",
                             help="Unit split basis (parallel, sequential, single(...), or none)")
    fail_parser.add_argument("--min-units", default="unknown",
                             help="Minimum number of units (integer or 'unknown')")
    fail_parser.add_argument("--prior-split-basis", default="none",
                             help="Prior attempt's split basis (parallel, sequential, single(...), or none)")

    # primary subcommand
    subparsers.add_parser("primary", help="Print the primary checkout path (rules in the module docstring)")

    args = parser.parse_args()

    if args.command == "fail" and args.cause is not None and args.reason:
        fail_parser.error("--cause cannot be combined with --reason (--reason wins); pass only one")

    if args.command == "snapshot":
        return cmd_snapshot(args.checkout, args.orchestrator_cwd, args.out)
    elif args.command == "compare":
        return cmd_compare(args.baseline, args.ticket_wt, args.start_sha, args.planned)
    elif args.command == "fail":
        return cmd_fail(args)
    elif args.command == "primary":
        return cmd_primary()
    else:
        parser.print_help()
        return EXIT_ERROR


if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except Exception as e:
        print(f"ERROR unexpected-error {e}", file=sys.stdout)
        sys.exit(EXIT_ERROR)
