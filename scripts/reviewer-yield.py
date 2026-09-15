#!/usr/bin/env python3
"""
Per-reviewer token yield and finding escalation tracker.

Parses subagent transcripts for expert-review runs, cross-references findings in
final-report.md and claude-action-plan.md, and logs reviewer-level yield metrics
(token cost, mention count, escalation count) to a per-repo leaderboard file.

Designed to be run manually after expert-review to track reviewer ROI (cost vs. output).
Never wired into expert-review's own steps — running it is opt-in and human-initiated.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple


def find_subagent_files_by_reviewer(review_dir: str, reviewer_slugs: List[str]) -> Dict[str, List[Path]]:
    """
    Find, for each reviewer, the subagent .jsonl file(s) that wrote that reviewer's own
    checkpoint file into the given review_dir.

    Searches ~/.claude/projects/*/subagents/*.jsonl for a Write tool_use whose
    input.file_path both contains review_dir as a substring AND matches one specific
    reviewer's own filename pattern (e.g. "{reviewer}-pass1.md") — this is what actually
    anchors a transcript to a reviewer, since a review directory holds many subagents'
    files and sort-order pairing between subagent files and reviewer slugs is not
    guaranteed to line up (subagents launch and finish in nondeterministic order).
    """
    review_dir_name = Path(review_dir).name
    projects_dir = Path.home() / ".claude" / "projects"

    result: Dict[str, List[Path]] = {slug: [] for slug in reviewer_slugs}

    if not projects_dir.exists():
        return result

    for subagent_file in projects_dir.glob("**/subagents/*.jsonl"):
        matched_reviewer = _subagent_reviewer_for_review_dir(subagent_file, review_dir_name, reviewer_slugs)
        if matched_reviewer:
            result[matched_reviewer].append(subagent_file)

    return result


def _subagent_reviewer_for_review_dir(
    jsonl_file: Path, review_dir_name: str, reviewer_slugs: List[str]
) -> Optional[str]:
    """
    Return the reviewer slug this subagent transcript belongs to, if it wrote that
    reviewer's own checkpoint file into the given review directory; None otherwise.
    """
    try:
        with open(jsonl_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "assistant":
                        message = entry.get("message", {})
                        content = message.get("content", [])
                        if isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict):
                                    if item.get("type") == "tool_use" and item.get("name") == "Write":
                                        file_path = item.get("input", {}).get("file_path", "")
                                        if review_dir_name not in file_path:
                                            continue
                                        written_name = Path(file_path).name
                                        for slug in reviewer_slugs:
                                            if written_name.startswith(f"{slug}-"):
                                                return slug
                except (json.JSONDecodeError, ValueError):
                    continue
    except (OSError, IOError):
        pass

    return None


def parse_tokens_from_subagent(jsonl_file: Path) -> Dict[str, int]:
    """
    Parse token usage from a subagent transcript.

    Sums input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens
    from all assistant messages that carry an iterations key (complete turns).

    Returns a dict with keys: input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens
    """
    tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    try:
        with open(jsonl_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    # Only count assistant messages with iterations (complete turns)
                    if entry.get("type") == "assistant" and "iterations" in entry:
                        usage = entry.get("usage", {})
                        if usage:
                            tokens["input_tokens"] += usage.get("input_tokens", 0)
                            tokens["output_tokens"] += usage.get("output_tokens", 0)
                            tokens["cache_read_input_tokens"] += usage.get("cache_read_input_tokens", 0)
                            tokens["cache_creation_input_tokens"] += usage.get("cache_creation_input_tokens", 0)
                except (json.JSONDecodeError, ValueError):
                    continue
    except (OSError, IOError):
        pass

    return tokens


def extract_reviewer_name(filename: str) -> Optional[str]:
    """Extract reviewer slug from pass file name (e.g., 'uncle-bob-pass1.md' -> 'uncle-bob')."""
    match = re.match(r"^([a-z\-]+)-pass\d+\.md$", filename)
    if match:
        return match.group(1)
    return None


def count_reviewer_mentions(final_report_path: Path, reviewer: str) -> int:
    """Count mentions of a reviewer by name or slug in final-report.md."""
    if not final_report_path.exists():
        return 0

    try:
        content = final_report_path.read_text()
        # Look for the reviewer name in various contexts (headers, attribution lines, etc.)
        # Case-insensitive to be safe
        pattern = re.escape(reviewer)
        # Match the reviewer name as a whole word (preceded/followed by word boundary or special chars)
        matches = re.findall(rf"\b{pattern}\b", content, re.IGNORECASE)
        return len(matches)
    except (OSError, IOError):
        return 0


def count_reviewer_escalations(action_plan_path: Path, reviewer: str) -> int:
    """
    Count findings escalated by a reviewer in claude-action-plan.md.

    Looks for lines with '**Raised by**: <reviewer>' format.
    """
    if not action_plan_path.exists():
        return 0

    try:
        content = action_plan_path.read_text()
        # Match "**Raised by**: <reviewer>" or similar patterns
        # The reviewer might be listed as a full name or slug
        pattern = rf"\*\*Raised by\*\*:.*\b{re.escape(reviewer)}\b"
        matches = re.findall(pattern, content, re.IGNORECASE)
        return len(matches)
    except (OSError, IOError):
        return 0


def get_review_run_id(review_dir: Path) -> str:
    """Get a stable run ID from review directory name."""
    return review_dir.name


def get_repo_key(review_dir: Path) -> str:
    """Extract repo key from the reviews directory structure."""
    # Review dirs are typically ~/.claude/reviews/{owner-repo}/{review-dir-name}/
    parent = review_dir.parent
    return parent.name if parent else "unknown"


def load_existing_yield_data(yield_file: Path) -> dict:
    """Load existing yield data, keyed by run_id."""
    data = {}
    if yield_file.exists():
        try:
            with open(yield_file, "r") as f:
                for line in f:
                    if line.strip():
                        try:
                            entry = json.loads(line)
                            run_id = entry.get("run_id")
                            if run_id:
                                data[run_id] = entry
                        except json.JSONDecodeError:
                            continue
        except (OSError, IOError):
            pass
    return data


def process_review_dir(review_dir_path: str) -> Tuple[Optional[str], List[dict]]:
    """
    Process a review directory and extract per-reviewer yield data.

    Returns (repo_key, list of per-reviewer entries) or (None, []) on error.
    """
    review_dir = Path(review_dir_path).expanduser().resolve()

    if not review_dir.exists():
        print(f"Error: review directory not found: {review_dir}", file=sys.stderr)
        return None, []

    # Verify this looks like a review directory
    final_report = review_dir / "final-report.md"
    if not final_report.exists():
        print(f"Error: final-report.md not found in {review_dir}", file=sys.stderr)
        return None, []

    repo_key = get_repo_key(review_dir)
    run_id = get_review_run_id(review_dir)

    # Find all reviewer pass files to identify reviewers
    reviewer_slugs = set()
    for file in review_dir.glob("*-pass1.md"):
        slug = extract_reviewer_name(file.name)
        if slug:
            reviewer_slugs.add(slug)

    # Find, per reviewer, the subagent transcript(s) that actually wrote that
    # reviewer's own checkpoint file into this review dir (not sort-order pairing —
    # subagents finish in nondeterministic order, so positional pairing with
    # sorted(reviewer_slugs) would silently misattribute token costs).
    subagent_files_by_reviewer = find_subagent_files_by_reviewer(review_dir_path, sorted(reviewer_slugs))

    reviewer_tokens = {}
    for reviewer in sorted(reviewer_slugs):
        reviewer_tokens[reviewer] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        for subagent_file in subagent_files_by_reviewer.get(reviewer, []):
            tokens = parse_tokens_from_subagent(subagent_file)
            for key in reviewer_tokens[reviewer]:
                reviewer_tokens[reviewer][key] += tokens[key]

    # Count mentions and escalations per reviewer
    action_plan = review_dir / "claude-action-plan.md"

    # Build output rows
    timestamp = datetime.now(timezone.utc).isoformat()
    rows = []

    for reviewer in sorted(reviewer_slugs):
        tokens = reviewer_tokens[reviewer]
        mention_count = count_reviewer_mentions(final_report, reviewer)
        escalation_count = count_reviewer_escalations(action_plan, reviewer)

        row = {
            "run_id": run_id,
            "reviewer": reviewer,
            "timestamp": timestamp,
            "input_tokens": tokens["input_tokens"],
            "output_tokens": tokens["output_tokens"],
            "cache_read_input_tokens": tokens["cache_read_input_tokens"],
            "cache_creation_input_tokens": tokens["cache_creation_input_tokens"],
            "mention_count": mention_count,
            "escalation_count": escalation_count,
        }
        rows.append(row)

    return repo_key, rows


def append_yield_data(repo_key: str, rows: List[dict]) -> Path:
    """
    Append per-reviewer yield data to the leaderboard file, idempotently.

    Checks if run_id already exists and skips if found.
    Returns the path to the yield file.
    """
    yield_dir = Path.home() / ".claude" / "reviews" / repo_key
    yield_file = yield_dir / "reviewer-yield.jsonl"

    # Create directory if needed
    yield_dir.mkdir(parents=True, exist_ok=True)

    # Check for existing run_id
    existing_runs = load_existing_yield_data(yield_file)
    run_ids = {row["run_id"] for row in rows}

    if any(run_id in existing_runs for run_id in run_ids):
        print(f"Already logged (idempotent skip): {run_ids}", file=sys.stderr)
        return yield_file

    # Append new rows
    try:
        with open(yield_file, "a") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
    except (OSError, IOError) as e:
        print(f"Error writing yield file: {e}", file=sys.stderr)
        return yield_file

    return yield_file


def print_review_table(rows: List[dict]) -> None:
    """Print a per-reviewer table to stdout for a single review run."""
    if not rows:
        print("No reviewer data found.")
        return

    print()
    print("Per-Reviewer Yield Metrics")
    print("=" * 120)
    print(f"{'Reviewer':<30} {'Input':<12} {'Output':<12} {'Cache R':<12} {'Cache W':<12} {'Mentions':<10} {'Escalated':<10}")
    print("-" * 120)

    for row in rows:
        reviewer = row["reviewer"]
        input_t = row["input_tokens"]
        output_t = row["output_tokens"]
        cache_r = row["cache_read_input_tokens"]
        cache_w = row["cache_creation_input_tokens"]
        mentions = row["mention_count"]
        escalated = row["escalation_count"]

        print(
            f"{reviewer:<30} {input_t:<12,} {output_t:<12,} {cache_r:<12,} {cache_w:<12,} {mentions:<10} {escalated:<10}"
        )

    print()


def print_aggregate_leaderboard(repo_key: str) -> None:
    """Print aggregated leaderboard across all runs for a repo."""
    yield_file = Path.home() / ".claude" / "reviews" / repo_key / "reviewer-yield.jsonl"

    if not yield_file.exists():
        print(f"No yield data found for {repo_key}")
        return

    # Aggregate across all runs
    reviewer_stats = {}
    total_runs = 0

    try:
        with open(yield_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    reviewer = row["reviewer"]
                    if reviewer not in reviewer_stats:
                        reviewer_stats[reviewer] = {
                            "total_input_tokens": 0,
                            "total_output_tokens": 0,
                            "total_cache_read": 0,
                            "total_cache_creation": 0,
                            "total_mentions": 0,
                            "total_escalations": 0,
                            "run_count": 0,
                        }
                    reviewer_stats[reviewer]["total_input_tokens"] += row["input_tokens"]
                    reviewer_stats[reviewer]["total_output_tokens"] += row["output_tokens"]
                    reviewer_stats[reviewer]["total_cache_read"] += row["cache_read_input_tokens"]
                    reviewer_stats[reviewer]["total_cache_creation"] += row["cache_creation_input_tokens"]
                    reviewer_stats[reviewer]["total_mentions"] += row["mention_count"]
                    reviewer_stats[reviewer]["total_escalations"] += row["escalation_count"]
                    reviewer_stats[reviewer]["run_count"] += 1
                    total_runs = max(total_runs, row.get("run_count", 0))
                except json.JSONDecodeError:
                    continue
    except (OSError, IOError) as e:
        print(f"Error reading yield file: {e}", file=sys.stderr)
        return

    if not reviewer_stats:
        print(f"No yield data found for {repo_key}")
        return

    # Calculate aggregates and averages
    print()
    print(f"Reviewer Leaderboard — {repo_key} (across all runs)")
    print("=" * 140)
    print(
        f"{'Reviewer':<30} {'Avg Input':<14} {'Avg Output':<14} {'Runs':<6} {'Total Mentions':<16} {'Total Escalations':<16} {'Escalation %':<12}"
    )
    print("-" * 140)

    for reviewer in sorted(reviewer_stats.keys()):
        stats = reviewer_stats[reviewer]
        run_count = stats["run_count"]
        avg_input = stats["total_input_tokens"] // run_count if run_count > 0 else 0
        avg_output = stats["total_output_tokens"] // run_count if run_count > 0 else 0
        total_mentions = stats["total_mentions"]
        total_escalations = stats["total_escalations"]
        escalation_pct = (
            100.0 * total_escalations / total_mentions if total_mentions > 0 else 0.0
        )

        print(
            f"{reviewer:<30} {avg_input:<14,} {avg_output:<14,} {run_count:<6} {total_mentions:<16} {total_escalations:<16} {escalation_pct:<12.1f}%"
        )

    print()


def main():
    parser = argparse.ArgumentParser(
        description="Track per-reviewer token yield and finding escalation metrics."
    )
    parser.add_argument(
        "review_dir",
        nargs="?",
        default=None,
        help="Review directory path or name (e.g., ~/.claude/reviews/{repo}/feature-x-123/)",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="Print leaderboard across all runs for the current repo",
    )

    args = parser.parse_args()

    if args.aggregate or not args.review_dir:
        # Aggregate mode: read from ~/.claude/reviews/{current-repo}/reviewer-yield.jsonl
        # For now, we can't auto-detect the repo, so require explicit repo key
        if not args.review_dir:
            print("Error: --aggregate requires running from a git repository or providing a repo key", file=sys.stderr)
            sys.exit(1)

        # Treat review_dir as repo key in aggregate mode
        print_aggregate_leaderboard(args.review_dir)
        return

    # Single review run mode
    repo_key, rows = process_review_dir(args.review_dir)

    if repo_key is None:
        sys.exit(1)

    # Append to leaderboard (idempotent)
    append_yield_data(repo_key, rows)

    # Print table
    print_review_table(rows)
    print(f"Logged to: ~/.claude/reviews/{repo_key}/reviewer-yield.jsonl")


if __name__ == "__main__":
    main()
