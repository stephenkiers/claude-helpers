#!/usr/bin/env python3
"""
Per-reviewer token yield and finding escalation tracker.

Parses subagent transcripts for expert-review runs, cross-references findings in
final-report.md and claude-action-plan.md, and logs reviewer-level yield metrics
(token cost, mention count, escalation count) to a per-repo leaderboard file.

Designed to be run manually after expert-review to track reviewer ROI (cost vs. output).
Never wired into expert-review's own steps — running it is opt-in and human-initiated.

Exception handling policy: Read and parse failures in I/O or JSON operations warn to stderr
and continue with safe defaults (empty results, zero counts), never raising. Write failures
in append_yield_data are surfaced to the caller for explicit error handling. This preserves
idempotency on read-after-read failures while ensuring the caller can distinguish write errors.
JSON parse failures (ValueError/JSONDecodeError) trigger warnings; JSON structure errors
(missing/unexpected keys) are silently skipped, allowing partial results from valid syntax.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple, TypedDict


class TokenRecord(TypedDict):
    """Token usage breakdown from a single subagent."""
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int


class YieldRow(TypedDict):
    """Per-reviewer yield metrics for a single review run."""
    run_id: str
    reviewer: str
    timestamp: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    mention_count: int
    escalation_count: int


class ReviewerStats(TypedDict):
    """Aggregated stats for a reviewer across all runs."""
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read: int
    total_cache_creation: int
    total_mentions: int
    total_escalations: int
    run_count: int


def find_subagent_files_by_reviewer(review_dir: str, reviewer_slugs: List[str], repo_key: str) -> Dict[str, List[Path]]:
    """
    Find, for each reviewer, the subagent .jsonl file(s) that wrote that reviewer's own
    checkpoint file into the given review_dir.

    Scopes the search to ~/.claude/projects/{repo_key}/subagents/*.jsonl to avoid unbounded
    read-scope on a shared machine. Searches for a Write tool_use whose input.file_path
    both contains review_dir as a substring AND matches one specific reviewer's own filename
    pattern (e.g. "{reviewer}-pass1.md") — this anchors a transcript to a reviewer, since
    a review directory holds many subagents' files and sort-order pairing between subagent
    files and reviewer slugs is not guaranteed to line up (subagents launch and finish in
    nondeterministic order).

    Assumes review directories use $RANDOM-suffixed naming per commands/expert-review.md,
    which allows substring matching on review_dir_name to disambiguate among subagent
    transcripts (one $RANDOM suffix is unlikely to collide with another run's suffix).
    """
    review_dir_name = Path(review_dir).name
    project_dir = Path.home() / ".claude" / "projects" / repo_key

    result: Dict[str, List[Path]] = {slug: [] for slug in reviewer_slugs}

    if not project_dir.exists():
        return result

    subagents_dir = project_dir / "subagents"
    if not subagents_dir.exists():
        print(f"Warning: subagent directory not found: {subagents_dir}", file=sys.stderr)
        return result

    for subagent_file in subagents_dir.glob("*.jsonl"):
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

    Returns None on read or parse error (warns to stderr per file-wide exception policy).
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
                except ValueError:
                    continue
    except OSError as e:
        print(f"Warning: failed to read subagent transcript {jsonl_file}: {e}", file=sys.stderr)

    return None


def parse_tokens_from_subagent(jsonl_file: Path) -> TokenRecord:
    """
    Parse token usage from a subagent transcript.

    Sums input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens
    from all assistant messages that carry an iterations key (complete turns).

    Returns zero-filled TokenRecord on read or parse error (warns to stderr per file-wide policy).
    """
    tokens: TokenRecord = {
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
                except ValueError:
                    continue
    except OSError as e:
        print(f"Warning: failed to read subagent transcript {jsonl_file}: {e}", file=sys.stderr)

    return tokens


def extract_reviewer_name(filename: str) -> Optional[str]:
    """Extract reviewer slug from pass file name (e.g., 'uncle-bob-pass1.md' -> 'uncle-bob')."""
    match = re.match(r"^([a-z\-]+)-pass\d+\.md$", filename)
    if match:
        return match.group(1)
    return None


def count_reviewer_mentions(final_report_path: Path, reviewer: str) -> int:
    """
    Count mentions of a reviewer by name or slug in final-report.md.

    Returns 0 on read error (warns to stderr per file-wide exception policy).
    """
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
    except OSError as e:
        print(f"Warning: failed to read {final_report_path}: {e}", file=sys.stderr)
        return 0


def count_reviewer_escalations(action_plan_path: Path, reviewer: str) -> int:
    """
    Count findings escalated by a reviewer in claude-action-plan.md.

    Looks for lines with '**Raised by**: <reviewer>' format.

    Returns 0 on read error (warns to stderr per file-wide exception policy).
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
    except OSError as e:
        print(f"Warning: failed to read {action_plan_path}: {e}", file=sys.stderr)
        return 0


def get_review_run_id(review_dir: Path) -> str:
    """Get a stable run ID from review directory name."""
    return review_dir.name


def get_repo_key(review_dir: Path) -> str:
    """
    Extract repo key from the reviews directory structure.

    Review dirs are typically ~/.claude/reviews/{owner-repo}/{review-dir-name}/.
    Validates that repo_key is non-empty and looks reasonable (not ".." or special chars).
    Returns "unknown" only if extraction fails validation.
    """
    parent = review_dir.parent
    repo_key = parent.name if parent else ""

    # Validate repo_key format: must not be empty and should look like a slug
    if not repo_key or repo_key in (".", "..", "reviews") or repo_key.startswith("-"):
        return "unknown"

    return repo_key


def load_existing_yield_data(yield_file: Path) -> dict:
    """
    Load existing yield data, keyed by run_id.

    Returns empty dict on read error (warns to stderr per file-wide exception policy).
    Warns allow idempotency tracking to distinguish between "file doesn't exist" (normal)
    and "file exists but read failed" (potential race, advisory to retry).
    """
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
                        except ValueError:
                            continue
        except OSError as e:
            print(f"Warning: failed to read yield file {yield_file}: {e}", file=sys.stderr)
    return data


def process_review_dir(review_dir_path: str) -> Tuple[Optional[str], List[YieldRow]]:
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
    subagent_files_by_reviewer = find_subagent_files_by_reviewer(review_dir_path, sorted(reviewer_slugs), repo_key)

    reviewer_tokens: Dict[str, TokenRecord] = {}
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
    rows: List[YieldRow] = []

    for reviewer in sorted(reviewer_slugs):
        tokens = reviewer_tokens[reviewer]
        mention_count = count_reviewer_mentions(final_report, reviewer)
        escalation_count = count_reviewer_escalations(action_plan, reviewer)

        row: YieldRow = {
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


def append_yield_data(repo_key: str, rows: List[YieldRow]) -> Optional[Path]:
    """
    Append per-reviewer yield data to the leaderboard file, idempotently.

    Checks if run_id already exists and skips if found.
    Returns the path to the yield file on success, None on write failure (caller must handle).
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
    except OSError as e:
        print(f"Error writing yield file: {e}", file=sys.stderr)
        return None

    return yield_file


def print_review_table(rows: List[YieldRow]) -> None:
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
    reviewer_stats: Dict[str, ReviewerStats] = {}

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
                except ValueError:
                    continue
    except OSError as e:
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
        help="Review directory path for single-run mode (e.g., ~/.claude/reviews/{repo}/feature-x-123/). "
             "In --aggregate mode, pass the repo key instead (e.g., owner-repo).",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="Print leaderboard across all runs. Requires repo key as positional argument.",
    )

    args = parser.parse_args()

    if args.aggregate:
        # Aggregate mode: read from ~/.claude/reviews/{repo_key}/reviewer-yield.jsonl
        if not args.review_dir:
            print("Error: --aggregate requires repo key as positional argument (e.g., owner-repo)", file=sys.stderr)
            sys.exit(1)
        # In aggregate mode, review_dir is actually the repo key
        print_aggregate_leaderboard(args.review_dir)
        return

    if not args.review_dir:
        # No positional and no --aggregate: this is for direct/manual invocation only,
        # such as testing or querying a specific repo directly (not part of normal flow)
        print("Error: provide review directory path or use --aggregate with repo key", file=sys.stderr)
        sys.exit(1)

    # Single review run mode
    repo_key, rows = process_review_dir(args.review_dir)

    if repo_key is None:
        sys.exit(1)

    # Append to leaderboard (idempotent)
    yield_file = append_yield_data(repo_key, rows)
    if yield_file is None:
        # Write failure
        sys.exit(1)

    # Print table
    print_review_table(rows)
    print(f"Logged to: ~/.claude/reviews/{repo_key}/reviewer-yield.jsonl")


if __name__ == "__main__":
    main()
