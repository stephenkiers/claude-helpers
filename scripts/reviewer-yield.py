#!/usr/bin/env python3
"""
Per-reviewer token yield and finding escalation tracker.

Parses subagent transcripts for expert-review runs, cross-references findings in
final-report.md and claude-action-plan.md, and logs reviewer-level yield metrics
(token cost, mention count, escalation count) to a per-repo leaderboard file.

Designed to be run manually after expert-review to track reviewer ROI (cost vs. output).
Never wired into expert-review's own steps — running it is opt-in and human-initiated.

Observation-only in Phase 0 — not wired into `prompts/router.md`, `reviewers/index.yaml`
triggers, or model/effort selection; wiring it into any of those needs an ADR amendment first.

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
from typing import Optional, Dict, List, Set, Tuple, TypedDict, Any

OBSERVATION_ONLY_NOTE = (
    "Observation-only in Phase 0 — not wired into `prompts/router.md`, "
    "`reviewers/index.yaml` triggers, or model/effort selection; wiring it into "
    "any of those needs an ADR amendment first."
)


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Start date of the newest regime classify_regime knows about. If the corpus contains
# runs far past this date, the regime table is probably stale (see _warn_if_regime_stale).
LATEST_KNOWN_REGIME_START = datetime(2026, 9, 16)
REGIME_STALENESS_DAYS = 90


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
    tokens_status: str


class ReviewerStats(TypedDict):
    """Aggregated stats for a reviewer across all runs."""
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read: int
    total_cache_creation: int
    total_mentions: int
    total_escalations: int
    run_count: int


def find_subagent_files_by_reviewer(review_dir: str, reviewer_slugs: List[str]) -> Dict[str, List[Path]]:
    """
    Find, for each reviewer, the subagent .jsonl file(s) that wrote that reviewer's own
    checkpoint file into the given review_dir.

    Reads {review_dir}/transcript-origin.json to determine the session directory. If missing,
    unparsable, unavailable, or the named session dir does not exist, warns to stderr and
    returns an empty mapping. Otherwise, scopes the search to
    ~/.claude/projects/{project_dir}/{session_id}/subagents/*.jsonl.

    Searches for a Write tool_use whose input.file_path both contains review_dir as a substring
    AND matches one specific reviewer's own filename pattern (e.g. "{reviewer}-pass1.md") —
    this anchors a transcript to a reviewer, since a review directory holds many subagents'
    files and sort-order pairing between subagent files and reviewer slugs is not guaranteed
    to line up (subagents launch and finish in nondeterministic order).
    """
    review_dir_path = Path(review_dir).expanduser().resolve()
    review_dir_name = review_dir_path.name
    origin_file = review_dir_path / "transcript-origin.json"

    result: Dict[str, List[Path]] = {slug: [] for slug in reviewer_slugs}

    # Read transcript-origin.json
    if not origin_file.exists():
        print(f"Warning: transcript-origin.json not found in {review_dir}", file=sys.stderr)
        return result

    try:
        origin_data = json.loads(origin_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: failed to read/parse transcript-origin.json: {e}", file=sys.stderr)
        return result

    if not isinstance(origin_data, dict):
        print("Warning: transcript-origin.json is not a JSON object", file=sys.stderr)
        return result

    # Check resolution and session_id
    resolution = origin_data.get("resolution")
    session_id = origin_data.get("session_id")
    project_dir_name = origin_data.get("project_dir")

    if resolution == "unavailable" or not session_id or not project_dir_name:
        print("Warning: transcript-origin.json resolution unavailable or missing session_id", file=sys.stderr)
        return result

    if (
        not isinstance(session_id, str)
        or not isinstance(project_dir_name, str)
        or not _SAFE_ID_RE.match(session_id)
        or not _SAFE_ID_RE.match(project_dir_name)
    ):
        print("Warning: transcript-origin.json session_id/project_dir failed validation; treating as unavailable", file=sys.stderr)
        return result

    # Build path to session directory, and confirm it stays under ~/.claude/projects
    projects_root = Path.home() / ".claude" / "projects"
    session_dir = projects_root / project_dir_name / session_id / "subagents"
    if projects_root.resolve() not in session_dir.resolve().parents:
        print(f"Warning: session directory escapes {projects_root}; treating as unavailable", file=sys.stderr)
        return result
    if not session_dir.exists():
        print(f"Warning: session directory not found: {session_dir}", file=sys.stderr)
        return result

    for subagent_file in session_dir.glob("*.jsonl"):
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

    Reads token usage from message.usage fields (input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens). De-duplicates by
    message["id"] when present; entries without an id are counted once each.

    Skips entries without a message key or where message/usage is not a dict.

    Returns zero-filled TokenRecord on read or parse error (warns to stderr per file-wide policy).
    """
    tokens: TokenRecord = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    seen_message_ids: Set[str] = set()

    try:
        with open(jsonl_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "assistant":
                        message = entry.get("message")
                        if not isinstance(message, dict):
                            continue

                        usage = message.get("usage")
                        if not isinstance(usage, dict):
                            continue

                        # De-duplicate by message id
                        message_id = message.get("id")
                        if message_id:
                            if message_id in seen_message_ids:
                                continue
                            seen_message_ids.add(message_id)

                        # Sum all four fields with defaults
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


def load_reviewer_display_names() -> Dict[str, str]:
    """
    Load slug → display-name map from reviewers/index.yaml.

    Tries repo path first (via symlink resolution), falls back to ~/.claude/reviewers/index.yaml.
    Returns dict mapping slug to display name; on error warns to stderr and returns empty dict.
    """
    names: Dict[str, str] = {}

    # Try repo path first (following symlink back through installed scripts)
    repo_reviewers_index = Path(__file__).resolve().parent.parent / "reviewers" / "index.yaml"
    fallback_index = Path.home() / ".claude" / "reviewers" / "index.yaml"

    for index_path in [repo_reviewers_index, fallback_index]:
        if not index_path.exists():
            continue

        try:
            content = index_path.read_text()
            # Parse with simple regex over name: and file: lines
            # Format: "- name: Uncle Bob" / "  file: uncle-bob.yaml" (repeating)
            name_pattern = r'^\s*-\s+name:\s+(.+)$'
            file_pattern = r'^\s+file:\s+([a-z\-]+)\.yaml$'

            lines = content.split('\n')
            current_name = None
            for line in lines:
                name_match = re.match(name_pattern, line)
                if name_match:
                    current_name = name_match.group(1).strip()
                    continue

                file_match = re.match(file_pattern, line)
                if file_match and current_name:
                    slug = file_match.group(1)
                    names[slug] = current_name
                    current_name = None

            if names:
                return names
        except OSError as e:
            print(f"Warning: failed to read reviewers index {index_path}: {e}", file=sys.stderr)
            continue

    # No index found, warn once but don't raise
    print("Warning: reviewers/index.yaml not found; using title-cased slugs", file=sys.stderr)
    return names


def count_reviewer_mentions(final_report_path: Path, reviewer: str, display_names: Dict[str, str]) -> int:
    """
    Count mentions of a reviewer by name or slug in final-report.md.

    Matches either the slug or the resolved display name, case-insensitive, whole-word.

    Returns 0 on read error (warns to stderr per file-wide exception policy).
    """
    if not final_report_path.exists():
        return 0

    try:
        content = final_report_path.read_text()
        # Look for the reviewer name in various contexts (headers, attribution lines, etc.)
        # Case-insensitive to be safe
        count = 0

        # Match by slug
        slug_pattern = re.escape(reviewer)
        count += len(re.findall(rf"\b{slug_pattern}\b", content, re.IGNORECASE))

        # Match by display name if available
        display_name = display_names.get(reviewer)
        if display_name:
            name_pattern = re.escape(display_name)
            count += len(re.findall(rf"\b{name_pattern}\b", content, re.IGNORECASE))

        return count
    except OSError as e:
        print(f"Warning: failed to read {final_report_path}: {e}", file=sys.stderr)
        return 0


def count_reviewer_escalations(action_plan_path: Path, reviewer: str, display_names: Dict[str, str]) -> int:
    """
    Count findings escalated by a reviewer in claude-action-plan.md.

    Looks for lines with '**Raised by**: <reviewer>' format, matching either slug or display name,
    case-insensitive, whole-word.

    Returns 0 on read error (warns to stderr per file-wide exception policy).
    """
    if not action_plan_path.exists():
        return 0

    try:
        content = action_plan_path.read_text()
        count = 0

        # Match "**Raised by**: <reviewer>" by slug
        slug_pattern = rf"\*\*Raised by\*\*:.*\b{re.escape(reviewer)}\b"
        count += len(re.findall(slug_pattern, content, re.IGNORECASE))

        # Match by display name if available
        display_name = display_names.get(reviewer)
        if display_name:
            name_pattern = rf"\*\*Raised by\*\*:.*\b{re.escape(display_name)}\b"
            count += len(re.findall(name_pattern, content, re.IGNORECASE))

        return count
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


def process_review_dir(review_dir_path: str) -> Tuple[Optional[str], List[YieldRow], str]:
    """
    Process a review directory and extract per-reviewer yield data.

    Returns (repo_key, list of per-reviewer entries, tokens_status) or (None, [], "unavailable") on error.
    tokens_status is "measured" when transcripts are found, "unavailable" when transcript-origin.json
    is missing/unavailable or session dir cannot be accessed.
    """
    review_dir = Path(review_dir_path).expanduser().resolve()

    if not review_dir.exists():
        print(f"Error: review directory not found: {review_dir}", file=sys.stderr)
        return None, [], "unavailable"

    # Verify this looks like a review directory
    final_report = review_dir / "final-report.md"
    if not final_report.exists():
        print(f"Error: final-report.md not found in {review_dir}", file=sys.stderr)
        return None, [], "unavailable"

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

    # Run-level status (returned to caller); rows carry their own per-reviewer status
    tokens_status = "measured" if any(subagent_files_by_reviewer.values()) else "unavailable"

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

    # Load display names for better matching
    display_names = load_reviewer_display_names()

    # Build output rows
    timestamp = datetime.now(timezone.utc).isoformat()
    rows: List[YieldRow] = []

    for reviewer in sorted(reviewer_slugs):
        tokens = reviewer_tokens[reviewer]
        mention_count = count_reviewer_mentions(final_report, reviewer, display_names)
        escalation_count = count_reviewer_escalations(action_plan, reviewer, display_names)

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
            "tokens_status": "measured" if subagent_files_by_reviewer.get(reviewer) else "unavailable",
        }
        rows.append(row)

    return repo_key, rows, tokens_status


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


def load_bucket_config() -> dict:
    """Load bucket configuration from routing-metrics-buckets.json."""
    config_path = Path(__file__).resolve().parent / "routing-metrics-buckets.json"
    try:
        return json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"Warning: failed to load bucket config: {e}, using defaults", file=sys.stderr)
        return {
            "config_source": "fallback",
            "config_version": 1,
            "buckets": [
                {"name": "xs", "max_changed_lines": 49},
                {"name": "s", "max_changed_lines": 199},
                {"name": "m", "max_changed_lines": 799},
                {"name": "l", "max_changed_lines": None}
            ]
        }


def classify_size_bucket(changed_lines: Optional[int], bucket_config: dict) -> str:
    """Classify a review by changed lines using bucket config."""
    if changed_lines is None:
        return "unknown"

    for bucket in bucket_config.get("buckets", []):
        max_lines = bucket.get("max_changed_lines")
        if max_lines is None or changed_lines <= max_lines:
            return bucket.get("name", "unknown")

    return "unknown"


def count_changed_lines(review_dir: Path) -> Optional[int]:
    """Count changed lines from full-diff.patch (lines starting with +/-, excluding +++/---)."""
    patch_file = review_dir / "full-diff.patch"
    if not patch_file.exists():
        return None

    try:
        content = patch_file.read_text()
        count = 0
        for line in content.split('\n'):
            if line.startswith('+') and not line.startswith('+++'):
                count += 1
            elif line.startswith('-') and not line.startswith('---'):
                count += 1
        return count
    except OSError:
        return None


def read_findings_json(review_dir: Path) -> Optional[dict]:
    """Read findings.json from review directory."""
    findings_file = review_dir / "findings.json"
    if not findings_file.exists():
        return None

    try:
        return json.loads(findings_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def parse_review_timestamp(review_dir_name: str) -> Optional[datetime]:
    """Parse timestamp from review directory name (format: branch-hash-YYYYmmddTHHMMSS-rand)."""
    parts = review_dir_name.rsplit('-', 2)  # Split from right to get YYYYmmddTHHMMSS-rand
    if len(parts) < 2:
        return None

    timestamp_part = parts[-2]  # YYYYmmddTHHMMSS
    try:
        return datetime.strptime(timestamp_part, "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def classify_regime(timestamp: Optional[datetime]) -> str:
    """Classify review into regime based on timestamp."""
    if not timestamp:
        return "unknown"

    pre_router_cutoff = datetime(2026, 7, 12)

    if timestamp < pre_router_cutoff:
        return "pre-router"
    elif timestamp < LATEST_KNOWN_REGIME_START:
        return "judgment-router"
    else:
        return "post-148-sam-gated"


def _warn_if_regime_stale(newest: Optional[datetime]) -> None:
    """Warn when the newest run is far past the latest known regime start."""
    if newest is None:
        return
    days = (newest - LATEST_KNOWN_REGIME_START).days
    if days > REGIME_STALENESS_DAYS:
        print(
            f"Warning: newest run is {days} days past LATEST_KNOWN_REGIME_START "
            f"({LATEST_KNOWN_REGIME_START.date()}); classify_regime may be stale",
            file=sys.stderr,
        )


def print_review_table(rows: List[YieldRow]) -> None:
    """Print a per-reviewer table to stdout for a single review run."""
    if not rows:
        print("No reviewer data found.")
        return

    print()
    print("Per-Reviewer Yield Metrics")
    print("(Caveat: a reviewer tagged review:named-only or secondary will show few or zero runs because they are not auto-routed; zero row is not evidence of no value.)")
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
    print("(Caveat: a reviewer tagged review:named-only or secondary will show few or zero runs because they are not auto-routed; zero row is not evidence of no value.)")
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


SEVERITY_VALUES = {"Critical": 8, "High": 4, "Medium": 2, "Low": 1}


def _verified_value_formula() -> str:
    """Generate the value formula string from SEVERITY_VALUES."""
    abbrev = {"Critical": "crit", "High": "high", "Medium": "med", "Low": "low"}
    terms = " + ".join(f"{abbrev[k]}*{v}" for k, v in SEVERITY_VALUES.items())
    return f"{terms} (over CONFIRMED findings)"


class CorpusBoundary(TypedDict):
    repo_keys: List[str]
    regimes: List[str]
    n_included: int
    n_excluded: int
    runs_with_unavailable_findings: int


class Methodology(TypedDict, total=False):
    formula: str
    bucket_config: Dict[str, Any]
    config_source: str
    corpus_boundary: CorpusBoundary
    token_status_note: str
    observation_only: bool
    observation_only_sentence: str


class TokensReport(TypedDict, total=False):
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_creation_tokens: int
    avg_input_per_run: int
    avg_output_per_run: int
    status: str
    reason: str


class ReportData(TypedDict, total=False):
    """Aggregate report for one repo (or an error stub with only error/methodology)."""
    error: str
    repo_key: str
    reviewers_per_run: Dict[str, List[int]]
    verified_crit_high_per_run: Dict[str, List[int]]
    verified_value_per_run: Dict[str, List[int]]
    solo_findings_per_reviewer: Dict[str, int]
    regime_counts: Dict[str, int]
    n_included_runs: int
    n_excluded_runs: int
    tokens: TokensReport
    valLift: Dict[str, str]
    shadow_miss_rate: Dict[str, str]
    methodology: Methodology


def _classify_runs(reports_dir: Path, bucket_config: dict) -> Tuple[List[Tuple[Path, str, str]], Dict[str, int], Optional[datetime]]:
    """Return ([(run_dir, regime, bucket)], regime_counts, newest_timestamp) for all run dirs."""
    runs: List[Tuple[Path, str, str]] = []
    regime_counts: Dict[str, int] = {}
    newest: Optional[datetime] = None
    for review_subdir in sorted(reports_dir.iterdir()):
        if not review_subdir.is_dir():
            continue
        timestamp = parse_review_timestamp(review_subdir.name)
        if timestamp and (newest is None or timestamp > newest):
            newest = timestamp
        regime = classify_regime(timestamp)
        regime_counts[regime] = regime_counts.get(regime, 0) + 1
        bucket = classify_size_bucket(count_changed_lines(review_subdir), bucket_config)
        runs.append((review_subdir, regime, bucket))
    return runs, regime_counts, newest


def _score_findings(runs: List[Tuple[Path, str, str]]) -> Tuple[Dict[str, List[int]], Dict[str, List[int]], Dict[str, List[int]], Dict[str, int], int]:
    """Compute per-bucket reviewer counts, crit/high counts, value, solo findings, and unavailable-findings count."""
    reviewers_per_run: Dict[str, List[int]] = {}
    verified_crit_high_per_run: Dict[str, List[int]] = {}
    verified_value_per_run: Dict[str, List[int]] = {}
    solo_findings_per_reviewer: Dict[str, int] = {}
    runs_with_unavailable_findings = 0

    for review_subdir, regime, bucket in runs:
        include_in_findings = regime == "post-148-sam-gated"

        if bucket not in reviewers_per_run:
            reviewers_per_run[bucket] = []
            verified_crit_high_per_run[bucket] = []
            verified_value_per_run[bucket] = []

        reviewers_per_run[bucket].append(len(list(review_subdir.glob("*-pass1.md"))))

        findings_data = read_findings_json(review_subdir)
        if not findings_data and include_in_findings:
            runs_with_unavailable_findings += 1
            continue

        if findings_data and include_in_findings:
            crit_high_count = 0
            value_total = 0

            for finding in findings_data.get("findings", []):
                if finding.get("verdict") != "CONFIRMED":
                    continue

                severity = finding.get("severity", "")
                supported_by = finding.get("supported_by", [])
                raised_by = finding.get("raised_by", "")

                if severity.lower() in ["critical", "high"]:
                    crit_high_count += 1

                value_total += SEVERITY_VALUES.get(severity, 0)

                if not supported_by and raised_by:
                    solo_findings_per_reviewer[raised_by] = solo_findings_per_reviewer.get(raised_by, 0) + 1

            verified_crit_high_per_run[bucket].append(crit_high_count)
            verified_value_per_run[bucket].append(value_total)

    return (
        reviewers_per_run,
        verified_crit_high_per_run,
        verified_value_per_run,
        solo_findings_per_reviewer,
        runs_with_unavailable_findings,
    )


def _aggregate_tokens(reports_dir: Path, runs: List[Tuple[Path, str, str]]) -> TokensReport:
    """Aggregate measured token rows from reviewer-yield.jsonl for the given runs."""
    all_token_rows = []
    yield_file = reports_dir / "reviewer-yield.jsonl"
    run_names = {r[0].name for r in runs}
    if yield_file.exists():
        try:
            with open(yield_file, "r") as f:
                for line in f:
                    if line.strip():
                        try:
                            row = json.loads(line)
                            if row.get("run_id") in run_names:
                                all_token_rows.append(row)
                        except json.JSONDecodeError:
                            continue
        except OSError:
            pass

    measured_rows = [r for r in all_token_rows if r.get("tokens_status") == "measured"]
    if measured_rows:
        total_input = sum(r.get("input_tokens", 0) for r in measured_rows)
        total_output = sum(r.get("output_tokens", 0) for r in measured_rows)
        total_cache_read = sum(r.get("cache_read_input_tokens", 0) for r in measured_rows)
        total_cache_creation = sum(r.get("cache_creation_input_tokens", 0) for r in measured_rows)
        run_count = len(set(r.get("run_id") for r in measured_rows))

        return {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read_tokens": total_cache_read,
            "total_cache_creation_tokens": total_cache_creation,
            "avg_input_per_run": total_input // run_count if run_count > 0 else 0,
            "avg_output_per_run": total_output // run_count if run_count > 0 else 0,
        }
    return {
        "status": "unavailable",
        "reason": "historical tokens unrecoverable by construction; measured prospectively from the transcript-origin.json fix onward"
    }


def compute_report_data(repo_key: str, bucket_config: dict) -> ReportData:
    """
    Compute report metrics across all runs for a repo.

    Returns dict with:
    - reviewers_per_run: grouped by size bucket
    - verified_crit_high_per_run: grouped by size bucket
    - verified_value_per_run: crit*8 + high*4 + med*2 + low*1
    - solo_findings_per_reviewer: empty supported_by findings
    - regime_counts: {regime: count} with n_included/n_excluded
    - tokens: real numbers or {status, reason}
    - methodology: bucket config, formula, observation note, etc.
    """
    observation_only_note = OBSERVATION_ONLY_NOTE

    reports_dir = Path.home() / ".claude" / "reviews" / repo_key
    if not reports_dir.exists():
        return {
            "error": f"No reviews found for {repo_key}",
            "methodology": {
                "observation_only": True,
                "observation_only_sentence": observation_only_note,
            }
        }

    bucket_config = dict(bucket_config)
    config_source = "fallback" if bucket_config.pop("config_source", None) == "fallback" else "file"

    runs, regime_counts, newest = _classify_runs(reports_dir, bucket_config)
    _warn_if_regime_stale(newest)
    (
        reviewers_per_run,
        verified_crit_high_per_run,
        verified_value_per_run,
        solo_findings_per_reviewer,
        runs_with_unavailable_findings,
    ) = _score_findings(runs)
    tokens_result = _aggregate_tokens(reports_dir, runs)

    # Build regime summary
    n_included = regime_counts.get("post-148-sam-gated", 0)
    n_excluded = sum(regime_counts.get(r, 0) for r in ["pre-router", "judgment-router", "unknown"])

    return {
        "repo_key": repo_key,
        "reviewers_per_run": reviewers_per_run,
        "verified_crit_high_per_run": verified_crit_high_per_run,
        "verified_value_per_run": verified_value_per_run,
        "solo_findings_per_reviewer": solo_findings_per_reviewer,
        "regime_counts": regime_counts,
        "n_included_runs": n_included,
        "n_excluded_runs": n_excluded,
        "tokens": tokens_result,
        "valLift": {
            "status": "not_yet_available",
            "reason": "requires the deterministic scorer (#195/#196)"
        },
        "shadow_miss_rate": {
            "status": "not_yet_available",
            "reason": "requires the deterministic scorer (#195/#196)"
        },
        "methodology": {
            "formula": _verified_value_formula(),
            "bucket_config": bucket_config,
            "config_source": config_source,
            "corpus_boundary": {
                "repo_keys": [repo_key],
                "regimes": ["post-148-sam-gated"],
                "n_included": n_included,
                "n_excluded": n_excluded,
                "runs_with_unavailable_findings": runs_with_unavailable_findings,
            },
            "token_status_note": "unavailable for retrospective runs; measured prospectively from transcript-origin.json fix onward",
            "observation_only": True,
            "observation_only_sentence": observation_only_note,
        }
    }


def _avg(values: list) -> int:
    """Integer average, 0 for an empty list."""
    return sum(values) // len(values) if values else 0


def render_report_json(report_data: Any) -> str:
    """Render report data as JSON."""
    return json.dumps(report_data, indent=2)


def render_report_markdown(report_data: ReportData) -> str:
    """Render report data as Markdown."""
    output = []
    output.append("# Reviewer Routing Metrics Report\n")

    repo_key = report_data.get("repo_key", "unknown")
    output.append(f"**Repository:** {repo_key}\n")

    # Methodology block
    methodology = report_data.get("methodology", {})
    if methodology:
        output.append("## Methodology\n")
        output.append(f"- **Formula:** {methodology.get('formula', 'N/A')}\n")
        output.append(f"- **Bucket Config Version:** {methodology.get('bucket_config', {}).get('config_version', 'N/A')}\n")
        output.append(f"- **Corpus:** {methodology.get('corpus_boundary', {}).get('n_included', 0)} included, {methodology.get('corpus_boundary', {}).get('n_excluded', 0)} excluded\n")
        output.append(f"- **Bucket Config Source:** {methodology.get('config_source', 'N/A')}\n")
        output.append(f"- **Runs With Unavailable Findings:** {methodology.get('corpus_boundary', {}).get('runs_with_unavailable_findings', 0)}\n")
        output.append(f"- **Observation Only:** Yes — {methodology.get('observation_only_sentence', 'Phase 0 only')}\n\n")

    # Regime counts
    regime_counts = report_data.get("regime_counts", {})
    if regime_counts:
        output.append("## Regime Counts\n")
        for regime, count in sorted(regime_counts.items()):
            output.append(f"- {regime}: {count} runs\n")
        output.append("\n")

    # Reviewers per run
    output.append("## Average Reviewers per Run (by Size Bucket)\n")
    reviewers_per_run = report_data.get("reviewers_per_run", {})
    for bucket in sorted(reviewers_per_run.keys()):
        counts = reviewers_per_run[bucket]
        avg = _avg(counts)
        output.append(f"- **{bucket}**: {avg} avg reviewers ({len(counts)} runs)\n")
    output.append("\n")

    # Verified findings
    output.append("## Verified Critical/High Findings per Run (by Size Bucket)\n")
    verified_crit_high = report_data.get("verified_crit_high_per_run", {})
    for bucket in sorted(verified_crit_high.keys()):
        counts = verified_crit_high[bucket]
        avg = _avg(counts)
        output.append(f"- **{bucket}**: {avg} avg critical/high findings ({len(counts)} runs)\n")
    output.append("\n")

    # Value calculation
    output.append("## Verified Value per Run (by Size Bucket)\n")
    output.append("*(crit×8 + high×4 + med×2 + low×1, CONFIRMED findings only)*\n\n")
    verified_value = report_data.get("verified_value_per_run", {})
    for bucket in sorted(verified_value.keys()):
        values = verified_value[bucket]
        avg = _avg(values)
        output.append(f"- **{bucket}**: {avg} avg value points ({len(values)} runs)\n")
    output.append("\n")

    # Solo findings
    output.append("## Solo Findings by Reviewer\n")
    solo_findings = report_data.get("solo_findings_per_reviewer", {})
    if solo_findings:
        for reviewer in sorted(solo_findings.keys()):
            count = solo_findings[reviewer]
            output.append(f"- {reviewer}: {count} solo findings\n")
    else:
        output.append("*(no solo findings in post-148-sam-gated runs)*\n")
    output.append("\n")

    # Tokens
    output.append("## Token Usage\n")
    tokens = report_data.get("tokens", {})
    if isinstance(tokens, dict) and "status" in tokens:
        output.append(f"- **Status:** {tokens['status']}\n")
        output.append(f"- **Reason:** {tokens['reason']}\n")
    elif isinstance(tokens, dict) and "total_input_tokens" in tokens:
        output.append(f"- **Total Input Tokens:** {tokens.get('total_input_tokens', 0):,}\n")
        output.append(f"- **Total Output Tokens:** {tokens.get('total_output_tokens', 0):,}\n")
        output.append(f"- **Avg Input per Run:** {tokens.get('avg_input_per_run', 0):,}\n")
        output.append(f"- **Avg Output per Run:** {tokens.get('avg_output_per_run', 0):,}\n")
    output.append("\n")

    return "".join(output)


def main():
    parser = argparse.ArgumentParser(
        description="Track per-reviewer token yield and finding escalation metrics. "
                    + OBSERVATION_ONLY_NOTE
    )
    parser.add_argument(
        "review_dir",
        nargs="?",
        default=None,
        help="Review directory path for single-run mode (e.g., ~/.claude/reviews/{repo}/feature-x-123/). "
             "In --aggregate or --report mode, pass the repo key instead (e.g., owner-repo).",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="Print leaderboard across all runs. Requires repo key as positional argument.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate aggregate metrics report (Phase 0: observation-only). Requires repo key as positional argument.",
    )
    parser.add_argument(
        "--report-all-repos",
        action="store_true",
        help="Generate report across all repos under ~/.claude/reviews/.",
    )
    parser.add_argument(
        "--report-json",
        metavar="PATH",
        help="Write report JSON source-of-truth to PATH; stdout always prints Markdown rendering.",
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

    if args.report or args.report_all_repos:
        # Report mode
        bucket_config = load_bucket_config()

        if args.report_all_repos:
            # Walk all repos
            reports_root = Path.home() / ".claude" / "reviews"
            if not reports_root.exists():
                print("No reviews found", file=sys.stderr)
                sys.exit(1)

            all_reports = {}
            for repo_subdir in sorted(reports_root.iterdir()):
                if not repo_subdir.is_dir():
                    continue
                repo_key = repo_subdir.name
                all_reports[repo_key] = compute_report_data(repo_key, bucket_config)

            report_data = {
                "scope": "all-repos",
                "reports": all_reports,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            # Single repo report
            if not args.review_dir:
                print("Error: --report requires repo key as positional argument (e.g., owner-repo)", file=sys.stderr)
                sys.exit(1)
            report_data = compute_report_data(args.review_dir, bucket_config)

        # Write JSON if requested
        if args.report_json:
            try:
                json_path = Path(args.report_json).expanduser()
                json_path.parent.mkdir(parents=True, exist_ok=True)
                json_path.write_text(render_report_json(report_data) + "\n")
                print(f"Report written to: {json_path}", file=sys.stderr)
            except OSError as e:
                print(f"Error writing report JSON: {e}", file=sys.stderr)
                sys.exit(1)

        # Print Markdown to stdout
        if args.report_all_repos:
            print("# All Repository Reports\n")
            for repo_key, repo_report in report_data.get("reports", {}).items():
                print(f"## {repo_key}\n")
                print(render_report_markdown(repo_report))
                print("\n---\n")
        else:
            print(render_report_markdown(report_data))

        return

    if not args.review_dir:
        # No positional and no --aggregate: this is for direct/manual invocation only,
        # such as testing or querying a specific repo directly (not part of normal flow)
        print("Error: provide review directory path or use --aggregate/--report with repo key", file=sys.stderr)
        sys.exit(1)

    # Single review run mode
    repo_key, rows, tokens_status = process_review_dir(args.review_dir)

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
