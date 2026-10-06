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

Note (#193 0c deviation): There is no standalone `scripts/routing-report.py` because the
report was folded into `reviewer-yield.py --report` and `--snapshot` modes. Related #193 0c
deviation is already noted; do not duplicate.
"""

import argparse
import fcntl
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Set, Tuple, TypedDict, Any, NamedTuple, Final, Literal

# Lazy-load transcript_discovery at point-of-use (see _load_scorer_module pattern below)
_transcript_discovery = None
resolve_session = None


def _load_transcript_discovery():
    """
    Load transcript_discovery module via importlib.
    Returns (module, error_msg). If error, module is None and error_msg is a string.
    """
    global _transcript_discovery, resolve_session
    if _transcript_discovery is not None:
        return _transcript_discovery, None

    discovery_path = Path(__file__).resolve().parent / "transcript_discovery.py"
    if not discovery_path.exists():
        return None, f"transcript_discovery.py not found at {discovery_path}"
    try:
        spec = importlib.util.spec_from_file_location("transcript_discovery", str(discovery_path))
        if spec is None or spec.loader is None:
            return None, f"Failed to load spec from {discovery_path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _transcript_discovery = module
        resolve_session = module.resolve_session
        return module, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

OBSERVATION_ONLY_NOTE = (
    "Observation-only in Phase 0 — not wired into `prompts/router.md`, "
    "`reviewers/index.yaml` triggers, or model/effort selection; wiring it into "
    "any of those needs an ADR amendment first."
)

ZERO_RUNS_CAVEAT = "(Caveat: a reviewer tagged review:named-only or secondary will show few or zero runs because they are not auto-routed; zero row is not evidence of no value.)"

# Type aliases for structured fields
Severity = Literal["Critical", "High", "Medium", "Low"]
Verdict = Literal["CONFIRMED", "DOWNGRADED", "REJECTED"]

# Findings schema constants (must match prompts/amalgamator.md)
FINDINGS_SCHEMA_VERSION = 1
SEVERITIES: Tuple[Severity, ...] = ("Critical", "High", "Medium", "Low")
VERDICTS: Tuple[Verdict, ...] = ("CONFIRMED", "DOWNGRADED", "REJECTED")
# Schema reference (not all fields are validated; see parse_findings for validation rules)
FINDING_FIELDS = {"id", "severity", "raised_by", "supported_by", "verdict"}
FORBIDDEN_KEYS = {"STATUS", "DECISION", "triage_bucket", "bucket"}


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Start date of the newest regime classify_regime knows about. If the corpus contains
# runs far past this date, the regime table is probably stale (see _warn_if_regime_stale).
LATEST_KNOWN_REGIME_START = datetime(2026, 9, 16)
REGIME_STALENESS_DAYS = 90

# Time slack for backdating backstop scan when parsing from review_dir timestamp
BACKSTOP_TIME_SLACK_SECONDS: Final[int] = 300

# Sentinel key for overhead questions-answered unit
OVERHEAD_QA_KEY: Final[str] = "overhead:questions-answered"

# Exhaustive checkpoint filename patterns (closed set as of 2026-09-23).
# Used by _subagent_reviewer_for_review_dir to attribute transcripts to reviewers/overhead.
# Patterns:
#   - {slug}-pass1.md -> slug (classic Pass 1)
#   - {slug}-pass2.md -> slug (classic Pass 2)
#   - {slug}-questions-answered.md -> OVERHEAD_QA_KEY (Q&A overhead)
# Pod-format runs use {pod-name}-pod.md and are not attributed per-reviewer.
# A tripwire test (to be added) will verify this set remains complete.
CHECKPOINT_FILENAME_PATTERNS: Final[List[str]] = [
    "{slug}-pass1.md",
    "{slug}-pass2.md",
    "{slug}-questions-answered.md",
]


def _get_route_score_module_path() -> Path:
    """Get the path to route-score.py."""
    return Path(__file__).resolve().parent / "route-score.py"


def _load_scorer_module():
    """
    Load route-score.py module via importlib.
    Returns (module, error_msg). If error, module is None and error_msg is a string.
    """
    scorer_path = _get_route_score_module_path()
    if not scorer_path or not scorer_path.exists():
        return None, f"route-score.py not found at {scorer_path}"
    try:
        spec = importlib.util.spec_from_file_location("route_score", scorer_path)
        if spec is None or spec.loader is None:
            return None, f"Failed to load spec from {scorer_path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _load_panel_decision_parser():
    """
    Load parse_panel_decision_table from reviewer-selection-audit.py via importlib.
    Returns (func, error_msg). If error, func is None and error_msg is a string.
    """
    audit_path = Path(__file__).resolve().parent / "reviewer-selection-audit.py"
    if not audit_path.exists():
        return None, f"reviewer-selection-audit.py not found at {audit_path}"
    try:
        spec = importlib.util.spec_from_file_location("reviewer_selection_audit", audit_path)
        if spec is None or spec.loader is None:
            return None, f"Failed to load spec from {audit_path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "parse_panel_decision_table"):
            return None, "parse_panel_decision_table not found in reviewer-selection-audit.py"
        return module.parse_panel_decision_table, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


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
    unit_kind: str  # "reviewer" | "overhead"


class ParsedFindingsOk(TypedDict):
    """Successfully parsed findings from a findings.json file."""
    status: Literal["ok", "legacy-unversioned"]
    findings: List[Dict[str, Any]]  # validated findings
    skipped_findings: int  # count of invalid findings skipped


class ParsedFindingsError(TypedDict):
    """Malformed findings.json file."""
    status: Literal["malformed"]
    reason: str  # explanation of what was wrong
    findings: List[Dict[str, Any]]  # empty list
    skipped_findings: int  # count of invalid findings skipped


# Union type for discriminated-union pattern (similar to SessionRef/Unresolved)
ParsedFindings = ParsedFindingsOk | ParsedFindingsError


class StratumBucketMetrics(TypedDict):
    """Per-(stratum, bucket) metrics breakdown."""
    reviewers: List[int]
    crit_high: List[int]
    value: List[int]


class ReviewerStats(TypedDict):
    """Aggregated stats for a reviewer across all runs."""
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read: int
    total_cache_creation: int
    total_mentions: int
    total_escalations: int
    run_count: int


class TranscriptOrigin(TypedDict, total=False):
    """
    Schema for transcript-origin.json: session discovery metadata.

    Fields:
    - schema_version: int (required)
    - cwd: str (recorded working directory; used as fallback for project_dir when empty)
    - project_dir: str (sanitized project directory id; empty string when unavailable)
    - session_id: str or None (Claude Code session id; None when unavailable)
    - resolution: str ("env", "most-recent-dir", "unavailable"; "unavailable" means session_id is None)
    - recorded_at: str (ISO 8601 UTC timestamp with Z suffix)
    """
    schema_version: int
    cwd: str
    project_dir: str
    session_id: Optional[str]
    resolution: str
    recorded_at: str


class FindingsScoreResult(NamedTuple):
    """Result of _score_findings: per-bucket reviewer counts, crit/high counts, value, and metadata."""
    reviewers_per_run: Dict[str, List[int]]
    verified_crit_high_per_run: Dict[str, List[int]]
    verified_value_per_run: Dict[str, List[int]]
    strata: Dict[str, Dict[str, StratumBucketMetrics]]
    solo_findings_per_reviewer: Dict[str, int]
    runs_with_unavailable_findings: int
    pod_lenses_per_run: Dict[str, List[int]]
    pod_runs_unrecorded: int
    unknown_format_runs: int
    malformed_findings_by_reason: Dict[str, int]
    skipped_findings_total: int
    excluded_by_effort_reason: Dict[str, int]


def sanitize_project_dir_id(cwd: str) -> Optional[str]:
    """
    Sanitize cwd to a project dir id by replacing every char outside [A-Za-z0-9-] with -.
    Returns the sanitized id if it passes _SAFE_ID_RE validation, else None.

    Mirrors the logic in scripts/write-transcript-origin.py to ensure consistent
    project directory naming across the transcript pipeline.
    """
    if not cwd:
        return None
    sanitized = re.sub(r"[^A-Za-z0-9-]", "-", cwd)
    if _SAFE_ID_RE.match(sanitized):
        return sanitized
    return None


def _path_is_under_root(child_path: Path, root_path: Path) -> bool:
    """
    Return True if root_path is an ancestor of child_path after resolving symlinks
    (any depth, not just direct children).
    """
    try:
        child_path.resolve().relative_to(root_path.resolve())
        return True
    except ValueError:
        return False


def classify_review_format(review_dir: Path) -> str:
    """
    Classify review directory format by checkpoint files present.

    Returns "classic" if any *-pass1.md exists,
    "pod" if any *-pod.md exists,
    "unknown" otherwise.
    """
    if list(review_dir.glob("*-pass1.md")):
        return "classic"
    if list(review_dir.glob("*-pod.md")):
        return "pod"
    return "unknown"


def read_pod_manifest(review_dir: Path) -> Tuple[List[str], List[str]]:
    """
    Read pods and lenses from review-metrics.json.

    Returns (pods: List[str], lenses: List[str]) on success.
    Returns ([], []) with a stderr warning if file missing, unparseable, or fields not lists of strings.
    """
    manifest_file = review_dir / "review-metrics.json"
    if not manifest_file.exists():
        print(f"Warning: review-metrics.json not found in {review_dir}", file=sys.stderr)
        return [], []

    try:
        data = json.loads(manifest_file.read_text())
        pods = data.get("pods", [])
        lenses = data.get("lenses", [])

        # Validate that both are lists of strings
        if not (isinstance(pods, list) and isinstance(lenses, list)):
            print(f"Warning: review-metrics.json pods/lenses are not lists in {review_dir}", file=sys.stderr)
            return [], []

        if not all(isinstance(p, str) for p in pods) or not all(isinstance(x, str) for x in lenses):
            print(f"Warning: review-metrics.json pods/lenses contain non-string values in {review_dir}", file=sys.stderr)
            return [], []

        return pods, lenses
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: failed to read/parse review-metrics.json: {e}", file=sys.stderr)
        return [], []


def find_subagent_files_by_reviewer(review_dir: str, reviewer_slugs: List[str]) -> Dict[str, List[Path]]:
    """
    Find, for each reviewer, the subagent .jsonl file(s) that wrote that reviewer's own
    checkpoint file into the given review_dir.

    Two-stage search:
    1. Hint path: reads {review_dir}/transcript-origin.json to determine the session directory.
       Searches within ~/.claude/projects/{project_dir}/{session_id}/subagents/*.jsonl.
       On success, logs matching count; on failure (missing, unparseable, unavailable,
       validation-failed, session dir missing), falls through to stage 2 without early return.

    2. Backstop scan: if stage 1 found zero matches, triggers a glob scan of
       projects/<project_dir>/*/subagents/*.jsonl (one project_dir only).
       Uses recorded_at or parsed review_dir timestamp for time-bound filtering.
       Project dir from origin file if present and valid, then sanitized origin cwd,
       then os.getcwd(). Prints one stderr line on success.

    Returns dict mapping reviewer slug or OVERHEAD_QA_KEY to list of subagent transcript paths.
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
    result[OVERHEAD_QA_KEY] = []

    # Stage 1: Hint path search (fast path via transcript-origin.json)
    origin_data: Optional[TranscriptOrigin] = None
    if origin_file.exists():
        try:
            origin_data = json.loads(origin_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: failed to read/parse transcript-origin.json: {e}", file=sys.stderr)

    stage1_match_count = 0
    if origin_data and isinstance(origin_data, dict):
        resolution = origin_data.get("resolution")
        session_id = origin_data.get("session_id")
        project_dir_name = origin_data.get("project_dir")

        # Allow "env" or legacy "most-recent-dir" resolution; reject "unavailable"
        resolution_ok = resolution in ("env", "most-recent-dir")

        if resolution_ok and session_id and project_dir_name:
            if (
                isinstance(session_id, str)
                and isinstance(project_dir_name, str)
                and _SAFE_ID_RE.match(session_id)
                and _SAFE_ID_RE.match(project_dir_name)
            ):
                # Build path to session directory and confirm it stays under ~/.claude/projects
                projects_root = Path.home() / ".claude" / "projects"
                session_dir = projects_root / project_dir_name / session_id / "subagents"
                if _path_is_under_root(session_dir, projects_root):
                    if session_dir.exists():
                        # Stage 1: scan hint path
                        for subagent_file in session_dir.glob("*.jsonl"):
                            matched_reviewer = _subagent_reviewer_for_review_dir(subagent_file, review_dir_name, reviewer_slugs)
                            if matched_reviewer:
                                result[matched_reviewer].append(subagent_file)
                                stage1_match_count += 1

    # Stage 2: Backstop scan (only if stage 1 found zero matches)
    if not any(result.values()):
        if stage1_match_count == 0 and origin_data and isinstance(origin_data, dict):
            print("Info: stage 1 hint path found no matches; falling back to backstop scan", file=sys.stderr)

        # Determine project_dir for backstop search: try origin project_dir,
        # then sanitized origin cwd, then os.getcwd()
        project_dir_name = None
        if origin_data and isinstance(origin_data, dict):
            project_dir_name = origin_data.get("project_dir")

        if not project_dir_name:
            # Try sanitizing the recorded cwd
            if origin_data and isinstance(origin_data, dict):
                recorded_cwd = origin_data.get("cwd")
                if recorded_cwd:
                    project_dir_name = sanitize_project_dir_id(recorded_cwd)

        if not project_dir_name:
            # Fall back to sanitized os.getcwd()
            project_dir_name = sanitize_project_dir_id(os.getcwd())

        if not project_dir_name:
            print("Warning: cannot determine project_dir for backstop scan", file=sys.stderr)
            return result

        # Validate project_dir_name
        if not _SAFE_ID_RE.match(project_dir_name):
            print(f"Warning: sanitized project_dir {project_dir_name} failed validation", file=sys.stderr)
            return result

        # Get time bounds for filtering
        time_lower_bound = None
        if origin_data and isinstance(origin_data, dict):
            recorded_at_str = origin_data.get("recorded_at")
            if recorded_at_str:
                try:
                    recorded_at = datetime.fromisoformat(recorded_at_str.replace("Z", "+00:00"))
                    time_lower_bound = recorded_at.astimezone().timestamp()
                except (ValueError, TypeError):
                    pass

        if time_lower_bound is None:
            # Try to parse from review_dir_name
            parsed_ts = parse_review_timestamp(review_dir_name)
            if parsed_ts:
                # Convert to local time, then to timestamp
                time_lower_bound = parsed_ts.astimezone().timestamp()

        if time_lower_bound is None:
            print("Warning: cannot determine time bound for backstop scan", file=sys.stderr)
            return result

        # Subtract time slack
        time_lower_bound -= BACKSTOP_TIME_SLACK_SECONDS

        # Perform backstop scan: glob exactly projects/<project_dir>/*/subagents/*.jsonl
        projects_root = Path.home() / ".claude" / "projects"
        project_path = projects_root / project_dir_name

        # Validate path stays under projects_root
        if not _path_is_under_root(project_path, projects_root):
            print(f"Warning: project path escapes {projects_root}", file=sys.stderr)
            return result

        matched_count = 0
        for subagent_file in project_path.glob("*/subagents/*.jsonl"):
            # Time bound check
            try:
                mtime = subagent_file.stat().st_mtime
                if mtime < time_lower_bound:
                    continue
            except OSError:
                continue

            matched_reviewer = _subagent_reviewer_for_review_dir(subagent_file, review_dir_name, reviewer_slugs)
            if matched_reviewer:
                result[matched_reviewer].append(subagent_file)
                matched_count += 1

        if matched_count > 0:
            print(f"Info: attributed {matched_count} transcript(s) via read-time scan of {project_dir_name}", file=sys.stderr)

    return result


def _subagent_reviewer_for_review_dir(
    jsonl_file: Path, review_dir_name: str, reviewer_slugs: List[str]
) -> Optional[str]:
    """
    Return the reviewer slug or overhead unit this subagent transcript belongs to,
    if it wrote that reviewer's own checkpoint file into the given review directory;
    None otherwise.

    Filename-to-unit mapping (exhaustive closed set as of 2026-09-23):
    - {slug}-pass1.md -> slug (classic-format Pass 1 checkpoint)
    - {slug}-pass2.md -> slug (classic-format Pass 2 checkpoint)
    - {slug}-questions-answered.md -> OVERHEAD_QA_KEY (Q&A overhead for that reviewer)

    The mapping is exhaustive: all review checkpoints belong to one of these three patterns.
    Pod-format runs use different filenames (*-pod.md) and are not attributed per-reviewer.

    Returns None on read or parse error (warns to stderr per file-wide exception policy).
    """
    # Build explicit filename-to-unit map for this review run
    filename_to_unit: Dict[str, str] = {}
    for slug in reviewer_slugs:
        filename_to_unit[f"{slug}-pass1.md"] = slug
        filename_to_unit[f"{slug}-pass2.md"] = slug
        # Questions-answered files (Q&A overhead) map to overhead unit, not the reviewer
        filename_to_unit[f"{slug}-questions-answered.md"] = OVERHEAD_QA_KEY

    try:
        with open(jsonl_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                # Prefilter: check if review_dir_name in raw line before parsing
                if review_dir_name not in line:
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
                                        if written_name in filename_to_unit:
                                            return filename_to_unit[written_name]
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


def get_canonical_reviewer_slugs() -> Set[str]:
    """
    Get the set of canonical reviewer slugs from reviewers/index.yaml.

    Returns a set of canonical slugs (lowercase, hyphenated). On error, returns empty set.
    """
    slugs: Set[str] = set()

    # Try repo path first (following symlink back through installed scripts)
    repo_reviewers_index = Path(__file__).resolve().parent.parent / "reviewers" / "index.yaml"
    fallback_index = Path.home() / ".claude" / "reviewers" / "index.yaml"

    for index_path in [repo_reviewers_index, fallback_index]:
        if not index_path.exists():
            continue

        try:
            content = index_path.read_text()
            # Parse with simple regex to extract slugs from "file: slug.yaml" lines
            file_pattern = r'^\s+file:\s+([a-z\-]+)\.yaml$'

            for line in content.split('\n'):
                file_match = re.match(file_pattern, line)
                if file_match:
                    slug = file_match.group(1)
                    slugs.add(slug)

            if slugs:
                return slugs
        except OSError as e:
            print(f"Warning: failed to read reviewers index {index_path}: {e}", file=sys.stderr)
            continue

    # No index found; return empty set (caller should handle gracefully)
    return slugs


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

    Only classic-format review dirs (`*-pass1.md` checkpoints) yield rows; pod/unknown dirs
    return empty results. The caller (main()) must classify via classify_review_format() first
    and print the pod/unknown notice — this function does not.

    Unavailable rows carry a `reason` field: origin-missing, origin-unavailable, session-dir-missing, etc.
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

    # Load transcript_discovery module (lazy load at point-of-use)
    discovery_module, load_error = _load_transcript_discovery()
    if load_error:
        print(f"Warning: failed to load transcript_discovery: {load_error}", file=sys.stderr)
        return repo_key, [], "unavailable"

    # Resolve session (try origin.json, then path-scan)
    session_ref = discovery_module.resolve_session(review_dir_path)
    unavailable_reason = None
    if not session_ref.get("resolved", False):
        unavailable_reason = session_ref.get("reason", "origin-missing")
        # Print warning for missing/unavailable session
        print(f"Warning: Could not resolve session for {review_dir.name}: {unavailable_reason}", file=sys.stderr)

    # Find all reviewer pass files to identify reviewers
    reviewer_slugs = set()
    for file in review_dir.glob("*-pass1.md"):
        slug = extract_reviewer_name(file.name)
        if slug:
            reviewer_slugs.add(slug)

    # If no pass files, return empty rows and exit 2 in main()
    if not reviewer_slugs:
        return repo_key, [], "unavailable"

    # Find, per reviewer, the subagent transcript(s) that actually wrote that
    # reviewer's own checkpoint file into this review dir (not sort-order pairing —
    # subagents finish in nondeterministic order, so positional pairing with
    # sorted(reviewer_slugs) would silently misattribute token costs).
    subagent_files_by_reviewer = find_subagent_files_by_reviewer(review_dir_path, sorted(reviewer_slugs))

    # Run-level status (returned to caller); only check reviewer rows, not overhead
    # (overhead presence alone does not mean reviewers were measured)
    reviewer_files_found = any(
        subagent_files_by_reviewer.get(slug, []) for slug in reviewer_slugs
    )
    tokens_status = "measured" if reviewer_files_found else "unavailable"

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

    # Count overhead:questions-answered if present
    overhead_tokens: TokenRecord = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    for subagent_file in subagent_files_by_reviewer.get(OVERHEAD_QA_KEY, []):
        tokens = parse_tokens_from_subagent(subagent_file)
        for key in overhead_tokens:
            overhead_tokens[key] += tokens[key]

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

        # Determine tokens_status and reason based on actual subagent file presence
        has_subagent_files = bool(subagent_files_by_reviewer.get(reviewer))
        tokens_status_for_row = "measured" if has_subagent_files else "unavailable"

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
            "tokens_status": tokens_status_for_row,
            "unit_kind": "reviewer",
        }

        # Add reason if unavailable
        if tokens_status_for_row == "unavailable":
            # Use session-resolution reason if available, else indicate subagent not found
            if unavailable_reason:
                row["reason"] = unavailable_reason
            else:
                row["reason"] = "subagent-not-found"

        rows.append(row)

    # Add overhead row if questions-answered was found
    if subagent_files_by_reviewer.get(OVERHEAD_QA_KEY):
        overhead_row: YieldRow = {
            "run_id": run_id,
            "reviewer": OVERHEAD_QA_KEY,
            "timestamp": timestamp,
            "input_tokens": overhead_tokens["input_tokens"],
            "output_tokens": overhead_tokens["output_tokens"],
            "cache_read_input_tokens": overhead_tokens["cache_read_input_tokens"],
            "cache_creation_input_tokens": overhead_tokens["cache_creation_input_tokens"],
            "mention_count": 0,
            "escalation_count": 0,
            "tokens_status": "measured",
            "unit_kind": "overhead",
        }
        rows.append(overhead_row)

    return repo_key, rows, tokens_status


def append_yield_data(repo_key: str, rows: List[YieldRow]) -> Optional[Path]:
    """
    Append or upgrade per-reviewer yield data to the leaderboard file.

    Upgrade-on-measured semantics:
    - If a run_id's existing rows are all unavailable and new rows are measured, rewrite with the new rows.
    - If both old and new rows are unavailable, skip (idempotent).
    - If existing rows have any measured rows, skip (do not downgrade).

    Acquires an exclusive lock on {yield_file}.lock throughout all read and write operations
    to prevent row loss under concurrent access.

    Returns the path to the yield file on success, None on write failure (caller must handle).
    Unavailable rows carry a `reason` field for skip-and-count diagnostics.
    """
    yield_dir = Path.home() / ".claude" / "reviews" / repo_key
    yield_file = yield_dir / "reviewer-yield.jsonl"
    lock_file_path = yield_dir / f"{yield_file.name}.lock"

    # Create directory if needed
    yield_dir.mkdir(parents=True, exist_ok=True)

    # Acquire exclusive lock and hold it through all I/O
    lock_f = None
    try:
        lock_f = open(lock_file_path, "a")
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)

        # Check for existing run_ids (under lock)
        existing_runs = load_existing_yield_data(yield_file)
        run_ids_in_new = {row["run_id"] for row in rows}

        # Determine which run_ids need upgrade
        rows_to_upgrade = {}
        rows_to_append = []

        for row in rows:
            run_id = row["run_id"]
            if run_id in existing_runs:
                rows_to_upgrade[run_id] = row
            else:
                rows_to_append.append(row)

        # If any run_id exists, check upgrade conditions
        if rows_to_upgrade:
            # Check if we can upgrade (all existing rows for this run_id are unavailable)
            should_upgrade = False
            for run_id, new_row in rows_to_upgrade.items():
                existing_rows = [r for r in existing_runs.values() if r.get("run_id") == run_id]
                if new_row.get("tokens_status") == "measured":
                    # Check if all existing rows for this run_id are unavailable
                    if all(r.get("tokens_status") == "unavailable" for r in existing_rows):
                        should_upgrade = True
                        break
                else:
                    # New row is unavailable; skip
                    pass

            if should_upgrade:
                # Rewrite with upgraded rows (under lock)
                try:
                    # Read all rows
                    all_rows = []
                    if yield_file.exists():
                        try:
                            with open(yield_file, "r") as f:
                                for line in f:
                                    if line.strip():
                                        try:
                                            all_rows.append(json.loads(line))
                                        except ValueError:
                                            continue
                        except OSError as e:
                            # Abort the upgrade; don't silently destroy accumulated data
                            print(f"Error reading yield file during upgrade: {e}", file=sys.stderr)
                            return None

                    # Filter out the run_ids we're upgrading
                    filtered_rows = [r for r in all_rows if r.get("run_id") not in rows_to_upgrade]

                    # Add new rows (both upgraded and any new ones)
                    all_rows = filtered_rows + list(rows_to_upgrade.values()) + rows_to_append

                    # Write to temp file, then rename
                    with tempfile.NamedTemporaryFile(mode='w', dir=yield_dir, delete=False, suffix='.jsonl') as tmp:
                        for row in all_rows:
                            tmp.write(json.dumps(row) + "\n")
                        tmp.flush()
                        os.fsync(tmp.fileno())
                        tmp_path = tmp.name

                    # Atomically replace
                    os.replace(tmp_path, yield_file)
                    return yield_file
                except OSError as e:
                    print(f"Error upgrading yield file: {e}", file=sys.stderr)
                    # Clean up temp file if it exists
                    try:
                        if 'tmp_path' in locals():
                            os.unlink(tmp_path)
                    except OSError:
                        pass
                    return None
            else:
                # Skip (either unavailable-to-unavailable or measured-to-measured)
                print(f"Already logged (idempotent skip): {set(rows_to_upgrade.keys())}", file=sys.stderr)
                return yield_file

        # No upgrades needed; just append new rows (under lock)
        try:
            with open(yield_file, "a") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
        except OSError as e:
            print(f"Error writing yield file: {e}", file=sys.stderr)
            return None

        return yield_file
    finally:
        # Release the lock
        if lock_f is not None:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                lock_f.close()
            except OSError:
                pass


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


def parse_findings(raw_data: Any) -> ParsedFindings:
    """
    Validate and parse findings from a parsed JSON object.

    Returns ParsedFindings with:
    - status: "ok", "malformed", or "legacy-unversioned"
    - reason: explanation if not "ok"
    - findings: list of validated findings (normalized severity)
    - skipped_findings: count of invalid findings that were skipped

    A file is malformed when:
    - top level is not a dict
    - findings key is missing or not a list
    - schema_version is present but not an int or not 1
    """
    if not isinstance(raw_data, dict):
        return ParsedFindingsError(status="malformed", reason="top-level not an object", findings=[], skipped_findings=0)

    if "findings" not in raw_data:
        return ParsedFindingsError(status="malformed", reason="findings key missing", findings=[], skipped_findings=0)

    findings_list = raw_data.get("findings")
    if not isinstance(findings_list, list):
        return ParsedFindingsError(status="malformed", reason="findings is not a list", findings=[], skipped_findings=0)

    # Check schema version
    schema_version = raw_data.get("schema_version")
    if schema_version is not None:
        if not isinstance(schema_version, int):
            return ParsedFindingsError(status="malformed", reason="schema_version is not an int", findings=[], skipped_findings=0)
        if schema_version != FINDINGS_SCHEMA_VERSION:
            return ParsedFindingsError(status="malformed", reason=f"unsupported schema_version {schema_version}", findings=[], skipped_findings=0)
    else:
        # Missing schema_version is tolerated but marked as legacy
        pass

    # Validate each finding
    validated_findings = []
    skipped_count = 0
    has_forbidden_key_warning = False

    for finding in findings_list:
        if not isinstance(finding, dict):
            skipped_count += 1
            continue

        # Check for forbidden keys and warn once
        for forbidden_key in FORBIDDEN_KEYS:
            if forbidden_key in finding:
                if not has_forbidden_key_warning:
                    print(f"Warning: findings.json contains forbidden key '{forbidden_key}' (should be in claude-action-plan.md, not findings.json)", file=sys.stderr)
                    has_forbidden_key_warning = True

        # Validate required fields
        severity = finding.get("severity", "")
        verdict = finding.get("verdict", "")
        raised_by = finding.get("raised_by", "")
        supported_by = finding.get("supported_by", [])

        # severity: case-insensitive match to SEVERITIES
        severity_matched: Optional[Severity] = None
        for sev in SEVERITIES:
            if isinstance(severity, str) and severity.lower() == sev.lower():
                severity_matched = sev
                break

        if not severity_matched:
            skipped_count += 1
            continue

        # verdict: must be in VERDICTS
        if verdict not in VERDICTS:
            skipped_count += 1
            continue

        # raised_by: must be a string
        if not isinstance(raised_by, str):
            skipped_count += 1
            continue

        # supported_by: must be a list of strings
        if not isinstance(supported_by, list) or not all(isinstance(s, str) for s in supported_by):
            skipped_count += 1
            continue

        # Valid finding; normalize severity to capitalized form
        validated = dict(finding)
        validated["severity"] = severity_matched
        validated_findings.append(validated)

    status = "legacy-unversioned" if schema_version is None else "ok"
    return ParsedFindingsOk(status=status, findings=validated_findings, skipped_findings=skipped_count)  # type: ignore


def read_findings_json(review_dir: Path) -> Optional[ParsedFindings]:
    """
    Read and validate findings.json from review directory.

    Returns ParsedFindings on success (parsed and validated), or ParsedFindings with malformed status
    on JSON syntax error. Returns None if file is absent or unreadable.
    """
    findings_file = review_dir / "findings.json"
    if not findings_file.exists():
        return None

    try:
        raw_data = json.loads(findings_file.read_text())
        return parse_findings(raw_data)
    except json.JSONDecodeError as e:
        return ParsedFindingsError(status="malformed", reason=f"JSON syntax error: {e}", findings=[], skipped_findings=0)
    except OSError:
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


def _print_yield_row(row: YieldRow) -> None:
    """Print a single yield row with consistent formatting."""
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


def print_review_table(rows: List[YieldRow]) -> None:
    """Print a per-reviewer table to stdout for a single review run, with overhead rows under a separator."""
    if not rows:
        print("No reviewer data found.")
        return

    print()
    print("Per-Reviewer Yield Metrics")
    print(ZERO_RUNS_CAVEAT)
    print("=" * 120)
    print(f"{'Reviewer':<30} {'Input':<12} {'Output':<12} {'Cache R':<12} {'Cache W':<12} {'Mentions':<10} {'Escalated':<10}")
    print("-" * 120)

    # Print reviewer rows
    for row in rows:
        unit_kind = row.get("unit_kind", "reviewer")  # Default to "reviewer" for legacy rows
        if unit_kind != "reviewer":
            continue
        _print_yield_row(row)

    # Separate and print overhead rows
    overhead_rows = [r for r in rows if r.get("unit_kind") == "overhead"]
    if overhead_rows:
        print("-" * 120)
        print("Overhead (not attributed to any reviewer)")
        print("-" * 120)
        for row in overhead_rows:
            _print_yield_row(row)

    print()


def print_aggregate_leaderboard(repo_key: str) -> None:
    """Print aggregated leaderboard across all runs for a repo, excluding overhead rows."""
    yield_file = Path.home() / ".claude" / "reviews" / repo_key / "reviewer-yield.jsonl"

    if not yield_file.exists():
        print(f"No yield data found for {repo_key}")
        return

    # Aggregate across all runs, separating reviewers from overhead
    reviewer_stats: Dict[str, ReviewerStats] = {}
    overhead_stats: ReviewerStats = {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "total_mentions": 0,
        "total_escalations": 0,
        "run_count": 0,
    }

    try:
        with open(yield_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    reviewer = row["reviewer"]
                    unit_kind = row.get("unit_kind", "reviewer")  # Default to "reviewer" for legacy rows

                    # Separate overhead rows from per-reviewer stats
                    if unit_kind == "overhead":
                        overhead_stats["total_input_tokens"] += row["input_tokens"]
                        overhead_stats["total_output_tokens"] += row["output_tokens"]
                        overhead_stats["total_cache_read"] += row["cache_read_input_tokens"]
                        overhead_stats["total_cache_creation"] += row["cache_creation_input_tokens"]
                        overhead_stats["total_mentions"] += row["mention_count"]
                        overhead_stats["total_escalations"] += row["escalation_count"]
                        overhead_stats["run_count"] += 1
                        continue

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

    if not reviewer_stats and overhead_stats["run_count"] == 0:
        print(f"No yield data found for {repo_key}")
        return

    # Calculate aggregates and averages
    print()
    print(f"Reviewer Leaderboard — {repo_key} (across all runs)")
    print(ZERO_RUNS_CAVEAT)
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

    # Print overhead totals if any
    if overhead_stats["run_count"] > 0:
        print("-" * 140)
        print("Overhead (not attributed to any reviewer)")
        print("-" * 140)
        overhead_total_input = overhead_stats["total_input_tokens"]
        overhead_total_output = overhead_stats["total_output_tokens"]
        overhead_count = overhead_stats["run_count"]
        print(
            f"{'Total overhead':<30} {overhead_total_input:<14,} {overhead_total_output:<14,} {overhead_count:<6}"
        )

    print()


SEVERITY_VALUES: Dict[Severity, int] = {"Critical": 8, "High": 4, "Medium": 2, "Low": 1}
# Cross-check with SEVERITIES constant
assert set(SEVERITY_VALUES.keys()) == set(SEVERITIES), "SEVERITY_VALUES must match SEVERITIES constant"


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
    """Token usage aggregation for a report.

    When all fields are present (success case):
    - total_*: aggregate token counts including all overhead (Q&A, triage, etc.)
    - avg_*_per_run: totals divided by number of included runs

    When status/reason fields are present (unavailable case):
    - status: "unavailable"
    - reason: explanation (e.g. historical runs before transcript-origin.json)
    """
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_creation_tokens: int
    avg_input_per_run: int
    avg_output_per_run: int
    status: str
    reason: str
    by_stratum: Dict[str, Dict[str, Any]]


class ReportData(TypedDict, total=False):
    """Aggregate report for one repo (or an error stub with only error/methodology)."""
    error: str
    repo_key: str
    reviewers_per_run: Dict[str, List[int]]
    verified_crit_high_per_run: Dict[str, List[int]]
    verified_value_per_run: Dict[str, List[int]]
    strata: Dict[str, Dict[str, StratumBucketMetrics]]
    solo_findings_per_reviewer: Dict[str, int]
    regime_counts: Dict[str, int]
    n_included_runs: int
    n_excluded_runs: int
    tokens: TokensReport
    valLift: Dict[str, str]
    pod_lenses_per_run: Dict[str, List[int]]
    pod_runs_unrecorded: int
    unknown_format_runs: int
    shadow: Dict[str, Any]
    methodology: Methodology


def _classify_shadow_run(
    run_dir: Path,
    scorer,
    configs: Dict[str, Any],
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Classify a single run for shadow scoring and re-score it.
    Returns (status, result_dict) where status is:
    - "pre-shadow": no route-scores.json
    - "unscored:no-diff": no diff found
    - "unscored:scorer-error": exception during scoring
    - "unattributable": no findings.json
    - "scored": success
    And result_dict carries the re-scored tiers and mode/effort/pr info.
    """
    route_scores_file = run_dir / "route-scores.json"

    # Check for stored route-scores.json
    stored_data = None
    if route_scores_file.exists():
        try:
            stored_data = json.loads(route_scores_file.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    if not stored_data:
        return "pre-shadow", None

    # Find diff file
    diff_text = None
    for diff_name in ["full-diff.patch", "full.diff"]:
        diff_file = run_dir / diff_name
        if diff_file.exists():
            try:
                diff_text = diff_file.read_text()
                break
            except OSError:
                pass

    if not diff_text:
        return "unscored:no-diff", stored_data

    # Re-score with the current scorer and config; downstream code works on the
    # JSON-shaped dict form so it never depends on the scorer's dataclasses.
    try:
        result = scorer.score_diff(diff_text, configs, scorer.Limits())
        return "scored", {
            "score_result": scorer.result_to_dict(result),
            "stored_status": stored_data.get("status"),
            "mode": stored_data.get("mode"),
            "pr": stored_data.get("pr", False),
            "effort": stored_data.get("effort"),
        }
    except Exception as e:
        return "unscored:scorer-error", {
            "error": f"{type(e).__name__}: {e}",
            "stored_status": stored_data.get("status"),
            "mode": stored_data.get("mode"),
            "pr": stored_data.get("pr", False),
            "effort": stored_data.get("effort"),
        }


def _finding_miss_status(
    finding: Dict[str, Any],
    re_scored_reviewers: Dict[str, Any],
    always_run_slugs: Set[str],
) -> Tuple[bool, str]:
    """
    Determine if a finding is a miss.
    Returns (is_miss, status_code) where status_code is:
    - "always-run-only": all attributors are always-run
    - "unattributed": raised_by missing or attributor not in scores
    - "not-miss": at least one attributor is not Exclude
    - "miss": all attributors are Exclude
    """
    raised_by = finding.get("raised_by", "")
    supported_by = finding.get("supported_by", []) or []

    if not raised_by:
        return False, "unattributed"

    attributors = {raised_by}
    if supported_by:
        attributors.update(supported_by)

    # Remove always-run slugs
    attributors_after_removal = attributors - always_run_slugs
    if not attributors_after_removal:
        return False, "always-run-only"

    # Check if all remaining attributors are in scores
    for slug in attributors_after_removal:
        if slug not in re_scored_reviewers:
            return False, "unattributed"

    # Check if all attributors are Exclude
    all_excluded = all(
        re_scored_reviewers.get(slug, {}).get("tier") == "Exclude"
        for slug in attributors_after_removal
    )

    if all_excluded:
        return True, "miss"
    else:
        return False, "not-miss"


def _resolve_index_path() -> Path:
    """Repo reviewers/index.yaml (via the script's symlink target), else ~/.claude/reviewers/index.yaml."""
    repo_index = Path(__file__).resolve().parent.parent / "reviewers" / "index.yaml"
    if repo_index.exists():
        return repo_index
    return Path.home() / ".claude" / "reviewers" / "index.yaml"


def _compute_shadow_section(reports_dir: Path) -> Dict[str, Any]:
    """
    Compute the shadow section by re-scoring all post-148 runs.
    Returns a dict with status and data, or {status: "unavailable", reason}.
    """
    # Load scorer and parser
    scorer, scorer_error = _load_scorer_module()
    if not scorer or scorer_error:
        return {"status": "unavailable", "reason": scorer_error or "Unknown error"}

    parse_panel_func, parse_error = _load_panel_decision_parser()
    if not parse_panel_func or parse_error:
        return {"status": "unavailable", "reason": parse_error or "Unknown error"}

    # Re-scoring needs the current route: config; without it every reviewer
    # would be absent from the scores and the section would be silently empty.
    index_path = _resolve_index_path()
    try:
        configs = scorer.load_route_configs(index_path)
    except Exception as e:
        return {"status": "unavailable", "reason": f"route config load failed ({index_path}): {type(e).__name__}: {e}"}

    # Get always-run slugs from scorer
    if not hasattr(scorer, "ALWAYS_RUN_SLUGS"):
        return {"status": "unavailable", "reason": "ALWAYS_RUN_SLUGS not found in scorer"}
    always_run_slugs = set(scorer.ALWAYS_RUN_SLUGS)

    # Collect runs
    runs_data: Dict[str, Any] = {
        "pre_shadow": [],
        "unscored_no_diff": [],
        "unscored_scorer_error": [],
        "hook_errors": 0,
        "degraded": 0,
        "unattributable": [],
        "excluded_non_router_seated": [],
        "effort4_routed": [],
        "effort5_full": [],
        "large_diffs": 0,
    }

    for review_subdir in sorted(reports_dir.iterdir()):
        if not review_subdir.is_dir():
            continue

        timestamp = parse_review_timestamp(review_subdir.name)
        regime = classify_regime(timestamp)

        # Only include post-148 runs
        if regime != "post-148-sam-gated":
            continue

        changed_lines = count_changed_lines(review_subdir)
        if changed_lines and changed_lines > 800:
            runs_data["large_diffs"] += 1

        # Classify and re-score
        status, result_dict = _classify_shadow_run(review_subdir, scorer, configs)

        if status == "pre-shadow":
            runs_data["pre_shadow"].append(review_subdir.name)
            continue
        elif status == "unscored:no-diff":
            runs_data["unscored_no_diff"].append(review_subdir.name)
            if result_dict and result_dict.get("status") == "error":
                runs_data["hook_errors"] += 1
            continue
        elif status.startswith("unscored:scorer-error"):
            runs_data["unscored_scorer_error"].append(review_subdir.name)
            if result_dict and result_dict.get("stored_status") == "error":
                runs_data["hook_errors"] += 1
            continue

        # At this point, status == "scored"
        if result_dict and result_dict.get("stored_status") == "error":
            runs_data["hook_errors"] += 1

        mode = result_dict.get("mode", "unknown")
        pr = result_dict.get("pr", False)
        effort = result_dict.get("effort")
        score_result = result_dict.get("score_result")

        # Track degraded re-scores
        is_degraded = (score_result or {}).get("degraded", False)
        if is_degraded:
            runs_data["degraded"] += 1

        # Read findings
        findings_data = read_findings_json(review_subdir)
        if not findings_data or findings_data.get("status") == "malformed":
            runs_data["unattributable"].append(review_subdir.name)
            continue

        # Extract re-scored reviewers
        re_scored_reviewers = (score_result or {}).get("reviewers", {})

        # Get panel decision
        tagged_sections_path = review_subdir / "tagged-sections.md"
        panel_decision = parse_panel_func(tagged_sections_path) if tagged_sections_path.exists() else {}

        # Determine cohort
        if mode == "routed" and effort == 4:
            cohort = "effort4-routed"
        elif mode == "named" and effort == 5:
            cohort = "effort5-full"
        else:
            cohort = "excluded-non-router-seated"

        # Process findings
        run_info = {
            "run_id": review_subdir.name,
            "cohort": cohort,
            "pr": pr,
            "degraded": is_degraded,
            "panel_decision": panel_decision,
            "re_scored": re_scored_reviewers,
            "findings": findings_data.get("findings", []),
        }

        if cohort == "effort4-routed":
            runs_data["effort4_routed"].append(run_info)
        elif cohort == "effort5-full":
            runs_data["effort5_full"].append(run_info)
        else:
            runs_data["excluded_non_router_seated"].append(run_info)

    # Compute miss rates and details for each cohort
    severities = ("critical", "high", "medium", "low")

    def compute_cohort_stats(cohort_runs):
        confirmed_count = {sev: 0 for sev in severities}
        missed_count = {sev: 0 for sev in severities}
        other_severity = 0
        unattributed = 0
        always_run_only = 0
        missed_findings_list = []
        sole_source_misses: Dict[str, int] = {}
        # Router x scorer contingency: reviewer-run cells, seated Yes/No x tier.
        contingency: Dict[str, Dict[str, int]] = {
            "seated_yes": {"Must": 0, "Candidate": 0, "Exclude": 0},
            "seated_no": {"Must": 0, "Candidate": 0, "Exclude": 0},
        }
        runs_list = []

        for run_info in cohort_runs:
            re_scored = run_info.get("re_scored", {})
            panel_decision = run_info.get("panel_decision", {})
            runs_list.append({"run_id": run_info["run_id"], "pr": run_info.get("pr", False)})

            for slug, reviewer_score in re_scored.items():
                tier = reviewer_score.get("tier")
                if tier not in ("Must", "Candidate", "Exclude"):
                    continue  # always-run reviewers are not scored for routing
                seated = str(panel_decision.get(slug, "No")).strip().lower().startswith("yes")
                contingency["seated_yes" if seated else "seated_no"][tier] += 1

            for finding in run_info.get("findings", []):
                if str(finding.get("verdict", "")).upper() != "CONFIRMED":
                    continue

                severity = str(finding.get("severity", "")).strip().lower()
                if severity in confirmed_count:
                    confirmed_count[severity] += 1
                else:
                    other_severity += 1

                is_miss, miss_status = _finding_miss_status(finding, re_scored, always_run_slugs)
                if miss_status == "unattributed":
                    unattributed += 1
                elif miss_status == "always-run-only":
                    always_run_only += 1
                if not is_miss:
                    continue

                if severity in missed_count:
                    missed_count[severity] += 1

                raised_by = finding.get("raised_by", "")
                supported_by = finding.get("supported_by", []) or []
                attr_reasons = {}
                for slug in [raised_by] + list(supported_by):
                    reasons = re_scored.get(slug, {}).get("reasons") or []
                    if reasons:
                        attr_reasons[slug] = reasons[:3]  # Top 3 reasons

                if not supported_by:
                    sole_source_misses[raised_by] = sole_source_misses.get(raised_by, 0) + 1

                missed_findings_list.append({
                    "run_id": run_info["run_id"],
                    "pr": run_info.get("pr", False),
                    "degraded": run_info.get("degraded", False),
                    "finding_id": finding.get("id", "unknown"),
                    "severity": finding.get("severity", "Unknown"),
                    "title": finding.get("title"),
                    "raised_by": raised_by,
                    "supported_by": supported_by,
                    "attributor_reasons": attr_reasons,
                })

        total_crit_high = confirmed_count["critical"] + confirmed_count["high"]
        missed_crit_high = missed_count["critical"] + missed_count["high"]
        # n=0 yields no rate, never "0% misses".
        crit_high_rate = f"{missed_crit_high}/{total_crit_high}" if total_crit_high > 0 else None

        return {
            "runs": runs_list,
            "confirmed_count": confirmed_count,
            "missed_count": missed_count,
            "other_severity_count": other_severity,
            "unattributed_count": unattributed,
            "always_run_only_count": always_run_only,
            "crit_high_rate": crit_high_rate,
            "contingency_table": contingency,
            "missed_findings": missed_findings_list,
            "sole_source_misses": sole_source_misses,
        }

    effort4_stats = compute_cohort_stats(runs_data["effort4_routed"]) if runs_data["effort4_routed"] else None
    effort5_stats = compute_cohort_stats(runs_data["effort5_full"]) if runs_data["effort5_full"] else None

    scorer_version = getattr(scorer, "SCORER_VERSION", "unknown") if scorer else "unknown"

    return {
        "status": "available",
        "scorer_version": scorer_version,
        "thresholds_provisional": True,
        "effort4_routed": effort4_stats,
        "effort5_full": effort5_stats,
        "counts": {
            "pre_shadow": len(runs_data["pre_shadow"]),
            "unscored_no_diff": len(runs_data["unscored_no_diff"]),
            "unscored_scorer_error": len(runs_data["unscored_scorer_error"]),
            "hook_errors": runs_data["hook_errors"],
            "degraded": runs_data["degraded"],
            "unattributable": len(runs_data["unattributable"]),
            "excluded_non_router_seated": len(runs_data["excluded_non_router_seated"]),
            "large_diffs": runs_data["large_diffs"],
        },
        "methodology": {
            "scoring_note": "re-scored with current config",
            "censoring_sentence": "The effort-4 miss rate only measures whether the scorer's exclusions remove a reviewer the Router seated who then produced a verified finding; it is a lower bound, not proof the scorer is safe. The effort-5 cohort is the uncensored estimate.",
        }
    }


def classify_effort_path(run_dir: Path) -> str:
    """
    Classify a review run's effort path based on available files.

    Returns: "full-panel", "pods", "scouts", or "unknown"

    Decision tree (in order):
    1. review-metrics.json with dict and "effort" key -> "pods"
    2. effort-scout.json valid dict without "error" key:
       - effort==2 -> "pods"
       - effort in (3,4,5) -> "full-panel"
       - effort==1 -> "scouts"
    3. File signature:
       - any *-pod.md -> "pods"
       - any *-pass1.md -> "full-panel"
       - final-report.md exists with neither -> "scouts"
    4. else -> "unknown"

    LIMITATION: Efforts 3/4/5 are not resolvable to distinct strata for ~771 of 772 historical runs
    (only determinable for runs that created effort-scout.json). Most runs classify as "full-panel"
    but cannot be distinguished as effort 3, 4, or 5.
    """
    # Check for review-metrics.json (effort 2)
    review_metrics = run_dir / "review-metrics.json"
    if review_metrics.exists():
        try:
            metrics = json.loads(review_metrics.read_text())
            if isinstance(metrics, dict) and "effort" in metrics:
                return "pods"
        except (json.JSONDecodeError, OSError):
            pass

    # Check for effort-scout.json (effort 1, 3, 4, 5)
    effort_scout = run_dir / "effort-scout.json"
    if effort_scout.exists():
        try:
            scout_data = json.loads(effort_scout.read_text())
            if isinstance(scout_data, dict) and "error" not in scout_data and "effort" in scout_data:
                effort = scout_data.get("effort")
                if effort == 2:
                    # effort 2 = compact-lens pods
                    return "pods"
                elif effort in (3, 4, 5):
                    # efforts 3/4/5 = full-panel with varying selection logic
                    return "full-panel"
                elif effort == 1:
                    # effort 1 = parallel haiku scouts
                    return "scouts"
        except (json.JSONDecodeError, OSError):
            pass

    # Check file signature
    has_pod = any(run_dir.glob("*-pod.md"))
    has_pass1 = any(run_dir.glob("*-pass1.md"))
    has_final = (run_dir / "final-report.md").exists()

    if has_pod:
        return "pods"
    elif has_pass1:
        return "full-panel"
    elif has_final and not has_pass1 and not has_pod:
        # Scouts: final-report but no pass1/pod files
        return "scouts"

    # Unknown
    return "unknown"


def _classify_runs(reports_dir: Path, bucket_config: dict, until: Optional[datetime] = None) -> Tuple[List[Tuple[Path, str, str]], Dict[str, int], Optional[datetime], int, Optional[datetime], Optional[datetime]]:
    """
    Return ([(run_dir, regime, bucket)], regime_counts, newest_timestamp, n_after_window, first_run_timestamp, last_run_timestamp) for all run dirs.

    If until is provided, drops runs whose timestamp is >= until.
    Returns n_after_window count of dropped runs, and first_run_timestamp/last_run_timestamp of included runs.
    """
    runs: List[Tuple[Path, str, str]] = []
    regime_counts: Dict[str, int] = {}
    newest: Optional[datetime] = None
    n_after_window: int = 0
    first_run_timestamp: Optional[datetime] = None
    last_run_timestamp: Optional[datetime] = None

    for review_subdir in sorted(reports_dir.iterdir()):
        if not review_subdir.is_dir():
            continue
        timestamp = parse_review_timestamp(review_subdir.name)

        # Check if this run should be excluded by the until cutoff
        if until is not None and timestamp is not None and timestamp >= until:
            n_after_window += 1
            continue

        if timestamp and (newest is None or timestamp > newest):
            newest = timestamp
        regime = classify_regime(timestamp)
        regime_counts[regime] = regime_counts.get(regime, 0) + 1
        bucket = classify_size_bucket(count_changed_lines(review_subdir), bucket_config)
        runs.append((review_subdir, regime, bucket))

        # Track min and max timestamps of included runs
        if timestamp:
            if first_run_timestamp is None or timestamp < first_run_timestamp:
                first_run_timestamp = timestamp
            if last_run_timestamp is None or timestamp > last_run_timestamp:
                last_run_timestamp = timestamp

    return runs, regime_counts, newest, n_after_window, first_run_timestamp, last_run_timestamp


def _score_findings(runs: List[Tuple[Path, str, str]]) -> FindingsScoreResult:
    """
    Compute metrics across all runs: per-bucket reviewer counts, crit/high counts, value,
    solo findings, unavailable-findings count, pod lens counts, pod-unrecorded count,
    unknown-format count, malformed-findings count, skipped-findings count, per-effort-stratum
    breakdown (reviewers/crit_high/value by stratum+bucket), and effort-exclusion counters.

    Returns FindingsScoreResult NamedTuple.
    """
    reviewers_per_run: Dict[str, List[int]] = {}
    verified_crit_high_per_run: Dict[str, List[int]] = {}
    verified_value_per_run: Dict[str, List[int]] = {}
    strata: Dict[str, Dict[str, StratumBucketMetrics]] = {}
    solo_findings_per_reviewer: Dict[str, int] = {}
    pod_lenses_per_run: Dict[str, List[int]] = {}
    runs_with_unavailable_findings = 0
    pod_runs_unrecorded = 0
    unknown_format_runs = 0
    malformed_findings_by_reason: Dict[str, int] = {}
    skipped_findings_total = 0
    excluded_by_effort_reason: Dict[str, int] = {}

    # Load canonical reviewer slugs for normalization
    canonical_slugs = get_canonical_reviewer_slugs()

    for review_subdir, regime, bucket in runs:
        include_in_findings = regime == "post-148-sam-gated"

        # Classify format (classic/pod/unknown) for pod-lens tracking (backward compat)
        review_format = classify_review_format(review_subdir)

        if review_format == "pod":
            if bucket not in pod_lenses_per_run:
                pod_lenses_per_run[bucket] = []

            pods, lenses = read_pod_manifest(review_subdir)
            if lenses:
                pod_lenses_per_run[bucket].append(len(lenses))
            else:
                pod_runs_unrecorded += 1
        elif review_format == "unknown":
            unknown_format_runs += 1

        # Only process effort strata and exclusions for runs that are included in findings
        if include_in_findings:
            # Initialize bucket dicts if needed
            if bucket not in reviewers_per_run:
                reviewers_per_run[bucket] = []
                verified_crit_high_per_run[bucket] = []
                verified_value_per_run[bucket] = []

            # Classify effort stratum
            effort_stratum = classify_effort_path(review_subdir)
            if effort_stratum not in strata:
                strata[effort_stratum] = {}
            if bucket not in strata[effort_stratum]:
                strata[effort_stratum][bucket] = {"reviewers": [], "crit_high": [], "value": []}

            # Compute reviewer count for this stratum
            if effort_stratum == "full-panel":
                reviewer_count = len(list(review_subdir.glob("*-pass1.md")))
            elif effort_stratum == "pods":
                # Try to get lenses count from review-metrics.json
                review_metrics = review_subdir / "review-metrics.json"
                if review_metrics.exists():
                    try:
                        metrics = json.loads(review_metrics.read_text())
                        lenses = metrics.get("lenses")
                        if isinstance(lenses, list):
                            reviewer_count = len(lenses)
                        else:
                            # Unmeasured lenses
                            excluded_by_effort_reason["pods-unmeasured-lenses"] = excluded_by_effort_reason.get("pods-unmeasured-lenses", 0) + 1
                            reviewer_count = None
                    except (json.JSONDecodeError, OSError):
                        excluded_by_effort_reason["pods-unmeasured-lenses"] = excluded_by_effort_reason.get("pods-unmeasured-lenses", 0) + 1
                        reviewer_count = None
                else:
                    excluded_by_effort_reason["pods-unmeasured-lenses"] = excluded_by_effort_reason.get("pods-unmeasured-lenses", 0) + 1
                    reviewer_count = None
            elif effort_stratum == "scouts":
                reviewer_count = 6
            else:
                # unknown
                excluded_by_effort_reason["unknown-effort"] = excluded_by_effort_reason.get("unknown-effort", 0) + 1
                reviewer_count = None

            # Append to strata and full-panel metrics
            if reviewer_count is not None:
                # For full-panel, also update the main reviewers_per_run (backward compat)
                if effort_stratum == "full-panel":
                    reviewers_per_run[bucket].append(reviewer_count)
                strata[effort_stratum][bucket]["reviewers"].append(reviewer_count)

        parsed = read_findings_json(review_subdir)
        if not parsed:
            if include_in_findings:
                runs_with_unavailable_findings += 1
            continue

        if parsed.get("status") == "malformed":
            reason = parsed.get("reason", "unknown")
            malformed_findings_by_reason[reason] = malformed_findings_by_reason.get(reason, 0) + 1
            if include_in_findings:
                runs_with_unavailable_findings += 1
            continue

        # Valid (or legacy-unversioned) findings
        findings_list = parsed.get("findings", [])
        skipped_findings_total += parsed.get("skipped_findings", 0)

        if not include_in_findings:
            continue

        crit_high_count = 0
        value_total = 0

        for finding in findings_list:
            if finding.get("verdict") != "CONFIRMED":
                continue

            severity = finding.get("severity", "")
            supported_by = finding.get("supported_by", [])
            raised_by = finding.get("raised_by", "")

            # Severity is already normalized (capitalized) by parse_findings
            if severity in ["Critical", "High"]:
                crit_high_count += 1

            value_total += SEVERITY_VALUES.get(severity, 0)

            # Gate solo finding accumulation to full-panel runs only
            # Normalize raised_by against canonical slugs to avoid duplicate entries
            if effort_stratum == "full-panel" and not supported_by and raised_by:
                # Normalize raised_by: if it matches a canonical slug exactly, use it;
                # otherwise use the value as-is (may be from legacy/malformed data)
                normalized_raised_by = raised_by if canonical_slugs and raised_by in canonical_slugs else raised_by
                solo_findings_per_reviewer[normalized_raised_by] = solo_findings_per_reviewer.get(normalized_raised_by, 0) + 1

        # For full-panel, also update main dicts (backward compat)
        if effort_stratum == "full-panel":
            verified_crit_high_per_run[bucket].append(crit_high_count)
            verified_value_per_run[bucket].append(value_total)
        # Only append crit_high/value to strata when reviewer_count is not None (consistent with reviewers)
        if reviewer_count is not None:
            strata[effort_stratum][bucket]["crit_high"].append(crit_high_count)
            strata[effort_stratum][bucket]["value"].append(value_total)

    return FindingsScoreResult(
        reviewers_per_run=reviewers_per_run,
        verified_crit_high_per_run=verified_crit_high_per_run,
        verified_value_per_run=verified_value_per_run,
        strata=strata,
        solo_findings_per_reviewer=solo_findings_per_reviewer,
        runs_with_unavailable_findings=runs_with_unavailable_findings,
        pod_lenses_per_run=pod_lenses_per_run,
        pod_runs_unrecorded=pod_runs_unrecorded,
        unknown_format_runs=unknown_format_runs,
        malformed_findings_by_reason=malformed_findings_by_reason,
        skipped_findings_total=skipped_findings_total,
        excluded_by_effort_reason=excluded_by_effort_reason,
    )


def _aggregate_tokens(reports_dir: Path, runs: List[Tuple[Path, str, str]]) -> TokensReport:
    """Aggregate measured token rows from reviewer-yield.jsonl for the given runs, with per-stratum breakdown."""
    all_token_rows = []
    yield_file = reports_dir / "reviewer-yield.jsonl"
    run_names_to_strata: Dict[str, str] = {}  # Maps run_id to stratum

    # Build map of run_id -> stratum
    for run_dir, _, _ in runs:
        run_names_to_strata[run_dir.name] = classify_effort_path(run_dir)

    run_names = set(run_names_to_strata.keys())
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

    # Build per-stratum breakdown
    by_stratum: Dict[str, Dict[str, Any]] = {}
    for stratum in set(run_names_to_strata.values()):
        stratum_runs = {run_id for run_id, s in run_names_to_strata.items() if s == stratum}
        stratum_measured = [r for r in measured_rows if r.get("run_id") in stratum_runs]

        total_input = sum(r.get("input_tokens", 0) for r in stratum_measured)
        total_output = sum(r.get("output_tokens", 0) for r in stratum_measured)
        measured_run_count = len(set(r.get("run_id") for r in stratum_measured))

        by_stratum[stratum] = {
            "measured_runs": measured_run_count,
            "total_runs": len(stratum_runs),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
        }

    if measured_rows:
        total_input = sum(r.get("input_tokens", 0) for r in measured_rows)
        total_output = sum(r.get("output_tokens", 0) for r in measured_rows)
        total_cache_read = sum(r.get("cache_read_input_tokens", 0) for r in measured_rows)
        total_cache_creation = sum(r.get("cache_creation_input_tokens", 0) for r in measured_rows)
        run_count = len(set(r.get("run_id") for r in measured_rows))

        result: TokensReport = {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read_tokens": total_cache_read,
            "total_cache_creation_tokens": total_cache_creation,
            "avg_input_per_run": total_input // run_count if run_count > 0 else 0,
            "avg_output_per_run": total_output // run_count if run_count > 0 else 0,
            "by_stratum": by_stratum,  # type: ignore
        }
        return result

    result = {
        "status": "unavailable",
        "reason": "historical tokens unrecoverable by construction; measured prospectively from the transcript-origin.json fix onward",
        "by_stratum": by_stratum,  # type: ignore
    }
    return result


def compute_report_data(repo_key: str, bucket_config: dict, until: Optional[datetime] = None) -> ReportData:
    """
    Compute report metrics across all runs for a repo.

    Returns dict with:
    - reviewers_per_run: grouped by size bucket (full-panel only, backward compat)
    - verified_crit_high_per_run: grouped by size bucket (full-panel only)
    - verified_value_per_run: crit*8 + high*4 + med*2 + low*1 (full-panel only)
    - strata: {stratum: {bucket: {metric: [counts]}}} covering all strata
    - solo_findings_per_reviewer: empty supported_by findings
    - regime_counts: {regime: count} with n_included/n_excluded
    - tokens: real numbers or {status, reason}, includes by_stratum breakdown
    - methodology: bucket config, formula, observation note, etc., includes effort exclusions

    If until is provided (as datetime), drops runs with timestamp >= until.
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

    runs, regime_counts, newest, n_after_window, first_run_timestamp, last_run_timestamp = _classify_runs(reports_dir, bucket_config, until)
    _warn_if_regime_stale(newest)
    (
        reviewers_per_run,
        verified_crit_high_per_run,
        verified_value_per_run,
        strata,
        solo_findings_per_reviewer,
        runs_with_unavailable_findings,
        pod_lenses_per_run,
        pod_runs_unrecorded,
        unknown_format_runs,
        malformed_findings_by_reason,
        skipped_findings_total,
        excluded_by_effort_reason,
    ) = _score_findings(runs)
    tokens_result = _aggregate_tokens(reports_dir, runs)

    # Compute shadow section
    shadow_result = _compute_shadow_section(reports_dir)

    # Build regime summary
    n_included = regime_counts.get("post-148-sam-gated", 0)
    n_excluded = sum(regime_counts.get(r, 0) for r in ["pre-router", "judgment-router", "unknown"])

    # Merge effort-reason counters into excluded_by_reason
    excluded_by_reason: Dict[str, Any] = {
        "malformed_findings": malformed_findings_by_reason,
        "skipped_findings_total": skipped_findings_total,
    }
    if excluded_by_effort_reason:
        excluded_by_reason["effort"] = excluded_by_effort_reason

    report: ReportData = {
        "repo_key": repo_key,
        "reviewers_per_run": reviewers_per_run,
        "verified_crit_high_per_run": verified_crit_high_per_run,
        "verified_value_per_run": verified_value_per_run,
        "strata": strata,  # type: ignore
        "solo_findings_per_reviewer": solo_findings_per_reviewer,
        "regime_counts": regime_counts,
        "n_included_runs": n_included,
        "n_excluded_runs": n_excluded,
        "tokens": tokens_result,
        "valLift": {
            "status": "not_yet_available",
            "reason": "requires the deterministic scorer (#195/#196)"
        },
        "pod_lenses_per_run": pod_lenses_per_run,
        "pod_runs_unrecorded": pod_runs_unrecorded,
        "unknown_format_runs": unknown_format_runs,
        "shadow": shadow_result,
        "methodology": {
            "formula": _verified_value_formula(),
            "bucket_config": bucket_config,
            "config_source": config_source,
            "corpus_boundary": {
                "repo_keys": [repo_key],
                "regimes": ["post-148-sam-gated"],
                "n_included": n_included,
                "n_excluded": n_excluded,
                "n_after_window": n_after_window,
                "first_run": first_run_timestamp.isoformat() if first_run_timestamp else None,
                "last_run": last_run_timestamp.isoformat() if last_run_timestamp else None,
                "runs_with_unavailable_findings": runs_with_unavailable_findings,
            },
            "excluded_by_reason": excluded_by_reason,  # type: ignore
            "token_status_note": "unavailable for retrospective runs; measured prospectively from transcript-origin.json fix onward",
            "observation_only": True,
            "observation_only_sentence": observation_only_note,
        }
    }
    return report


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

        excluded_by_reason = methodology.get('excluded_by_reason', {})
        if excluded_by_reason:
            malformed = excluded_by_reason.get('malformed_findings', {})
            if malformed:
                output.append("- **Malformed Findings by Reason:**\n")
                for reason, count in malformed.items():
                    output.append(f"  - {reason}: {count}\n")
            skipped = excluded_by_reason.get('skipped_findings_total', 0)
            if skipped:
                output.append(f"- **Skipped Individual Findings:** {skipped}\n")

        output.append(f"- **Observation Only:** Yes — {methodology.get('observation_only_sentence', 'Phase 0 only')}\n\n")

    # Regime counts
    regime_counts = report_data.get("regime_counts", {})
    if regime_counts:
        output.append("## Regime Counts\n")
        for regime, count in sorted(regime_counts.items()):
            output.append(f"- {regime}: {count} runs\n")
        output.append("\n")

    # Reviewers per run (classic format only)
    output.append("## Average Reviewers per Run (by Size Bucket)\n")
    reviewers_per_run = report_data.get("reviewers_per_run", {})
    for bucket in sorted(reviewers_per_run.keys()):
        counts = reviewers_per_run[bucket]
        avg = _avg(counts)
        output.append(f"- **{bucket}**: {avg} avg reviewers ({len(counts)} runs)\n")
    output.append("\n")

    # Pod runs (if any)
    pod_lenses_per_run = report_data.get("pod_lenses_per_run", {})
    pod_runs_unrecorded = report_data.get("pod_runs_unrecorded", 0)
    if pod_lenses_per_run or pod_runs_unrecorded > 0:
        output.append("## Pod Runs (effort 2)\n")
        for bucket in sorted(pod_lenses_per_run.keys()):
            lenses = pod_lenses_per_run[bucket]
            avg = _avg(lenses)
            output.append(f"- **{bucket}**: avg {avg} lenses ({len(lenses)} runs)\n")
        if pod_runs_unrecorded > 0:
            output.append(f"- Runs with unrecorded lenses: {pod_runs_unrecorded}\n")
        output.append("\n")

    # Unknown format runs (if any)
    unknown_format_runs = report_data.get("unknown_format_runs", 0)
    if unknown_format_runs > 0:
        output.append("## Excluded Runs\n")
        output.append(f"- Unrecognized review format: {unknown_format_runs} runs\n\n")

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

    # Per-stratum token coverage
    by_stratum = tokens.get("by_stratum", {})
    if by_stratum:
        output.append("\n### Token Coverage by Effort Stratum\n")
        for stratum in sorted(by_stratum.keys()):
            stratum_data = by_stratum[stratum]
            measured = stratum_data.get("measured_runs", 0)
            total = stratum_data.get("total_runs", 0)
            input_tokens = stratum_data.get("total_input_tokens", 0)
            output_tokens = stratum_data.get("total_output_tokens", 0)
            coverage = f"{measured}/{total}" if total > 0 else "0/0"
            output.append(f"- **{stratum}**: {coverage} measured ({input_tokens:,} input, {output_tokens:,} output)\n")
    output.append("\n")

    # Shadow scorer section
    shadow = report_data.get("shadow", {})
    output.append("## Shadow scorer (observe-only)\n")

    if isinstance(shadow, dict) and shadow.get("status") == "unavailable":
        output.append("**Status:** Unavailable\n")
        output.append(f"**Reason:** {shadow.get('reason', 'Unknown error')}\n\n")
    elif isinstance(shadow, dict) and shadow.get("status") == "available":
        output.append(f"**Scorer Version:** {shadow.get('scorer_version', 'unknown')}\n")
        output.append(f"**Thresholds Provisional:** {shadow.get('thresholds_provisional', False)}\n")
        output.append(f"**Methodology:** {shadow.get('methodology', {}).get('scoring_note', 'N/A')}\n\n")

        # Counts
        counts = shadow.get("counts", {})
        output.append("**Counts:**\n")
        output.append(f"- Pre-shadow runs: {counts.get('pre_shadow', 0)}\n")
        output.append(f"- Unscored (no diff): {counts.get('unscored_no_diff', 0)}\n")
        output.append(f"- Unscored (scorer error): {counts.get('unscored_scorer_error', 0)}\n")
        output.append(f"- Unattributable: {counts.get('unattributable', 0)}\n")
        output.append(f"- Excluded (non-router-seated): {counts.get('excluded_non_router_seated', 0)}\n")
        output.append(f"- Large diffs (>800 lines): {counts.get('large_diffs', 0)}\n")
        hook_errors = counts.get('hook_errors', 0)
        output.append(f"  - Of the above, {hook_errors} had a stored hook error\n")
        degraded = counts.get('degraded', 0)
        if degraded > 0:
            output.append(f"  - Of the above, {degraded} were degraded re-scores\n")
        output.append("\n")

        cohorts = [
            ("effort4_routed", "Effort-4 Routed Cohort (censored lower bound)", " (3% guardrail as observation)"),
            ("effort5_full", "Effort-5 Full Cohort (uncensored estimate)", ""),
        ]
        for key, heading, rate_note in cohorts:
            cohort = shadow.get(key)
            output.append(f"**{heading}:**\n")
            if not cohort:
                output.append("- n=0 runs; no rate\n\n")
                continue
            confirmed = cohort.get("confirmed_count", {})
            missed = cohort.get("missed_count", {})
            pr_runs = sum(1 for r in cohort.get("runs", []) if r.get("pr"))
            output.append(f"- Runs: {len(cohort.get('runs', []))} ({pr_runs} PR-mode)\n")
            for sev in ("critical", "high", "medium", "low"):
                output.append(f"- {sev.capitalize()}: {missed.get(sev, 0)} missed / {confirmed.get(sev, 0)} confirmed\n")
            rate = cohort.get("crit_high_rate")
            output.append(f"- Miss rate (crit+high): {rate + rate_note if rate else 'n=0; no rate'}\n")
            output.append(f"- Unattributed findings: {cohort.get('unattributed_count', 0)}; always-run-only: {cohort.get('always_run_only_count', 0)}\n\n")

            table = cohort.get("contingency_table", {})
            output.append("| Router seated | Must | Candidate | Exclude |\n|---|---|---|---|\n")
            for row_key, label in (("seated_yes", "Yes"), ("seated_no", "No")):
                row = table.get(row_key, {})
                output.append(f"| {label} | {row.get('Must', 0)} | {row.get('Candidate', 0)} | {row.get('Exclude', 0)} |\n")
            output.append("\n")

            missed_list = cohort.get("missed_findings", [])
            if missed_list:
                output.append("Missed findings:\n")
                for m in missed_list:
                    attrs = ", ".join([m.get("raised_by", "")] + list(m.get("supported_by", [])))
                    title = f" — {m['title']}" if m.get("title") else ""
                    pr_tag = " [pr]" if m.get("pr") else ""
                    degraded_tag = " (degraded)" if m.get("degraded") else ""
                    output.append(f"- {m.get('run_id')}{pr_tag}{degraded_tag} {m.get('finding_id')} ({m.get('severity')}){title}; attributors: {attrs}\n")
                    for slug, reasons in sorted(m.get("attributor_reasons", {}).items()):
                        details = "; ".join(f"{r.get('kind')}:{r.get('detail')} (+{r.get('points')})" for r in reasons)
                        output.append(f"  - {slug}: {details}\n")
                output.append("\n")
            sole = cohort.get("sole_source_misses", {})
            if sole:
                output.append("Sole-source misses: " + ", ".join(f"{k}={v}" for k, v in sorted(sole.items())) + "\n\n")

        # Censoring sentence
        methodology = shadow.get("methodology", {})
        if methodology.get("censoring_sentence"):
            output.append(f"**Censoring caveat:** {methodology.get('censoring_sentence')}\n\n")
    else:
        output.append("*(Shadow section unavailable)*\n\n")

    # Effort Strata
    strata = report_data.get("strata", {})
    if strata:
        output.append("## Effort Strata\n")
        for stratum in sorted(strata.keys()):
            stratum_buckets = strata[stratum]
            output.append(f"\n### {stratum}\n")

            # Aggregate across buckets for this stratum
            total_reviewers = []
            total_crit_high = []
            total_value = []
            total_runs = 0

            for bucket, metrics in stratum_buckets.items():
                total_reviewers.extend(metrics.get("reviewers", []))
                total_crit_high.extend(metrics.get("crit_high", []))
                total_value.extend(metrics.get("value", []))
                total_runs += len(metrics.get("reviewers", []))

            if total_runs > 0:
                output.append(f"- **Runs:** {total_runs}\n")
                output.append(f"- **Avg Reviewers:** {_avg(total_reviewers)}\n")
                output.append(f"- **Avg Crit/High Findings:** {_avg(total_crit_high)}\n")
                output.append(f"- **Avg Value Points:** {_avg(total_value)}\n")
            else:
                output.append("- *(no runs with measured findings)*\n")
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
    parser.add_argument(
        "--snapshot",
        metavar="PATH",
        help="Generate cross-repo aggregate snapshot and write to PATH. Sanitized (no repo names/paths), aggregate-only metrics.",
    )
    parser.add_argument(
        "--until",
        metavar="ISO_DATETIME",
        help="Exclude runs with timestamp >= until (naive local ISO like 2026-09-23T00:51:00). Valid only with --snapshot. On parse failure, error to stderr and exit 1.",
    )

    args = parser.parse_args()

    # Parse --until if provided
    until_dt: Optional[datetime] = None
    if args.until:
        if not args.snapshot:
            print("Error: --until is only valid with --snapshot", file=sys.stderr)
            sys.exit(1)
        try:
            until_dt = datetime.fromisoformat(args.until)
        except ValueError:
            print(f"Error: failed to parse --until: {args.until} (expected naive local ISO like 2026-09-23T00:51:00)", file=sys.stderr)
            sys.exit(1)
        # Reject timezone-aware datetimes; must be naive-local
        if until_dt.tzinfo is not None:
            print(f"Error: --until must be naive-local (no timezone); got {args.until} with tzinfo={until_dt.tzinfo}", file=sys.stderr)
            sys.exit(1)

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

    if args.snapshot:
        # Snapshot mode: cross-repo aggregate, sanitized
        bucket_config = load_bucket_config()
        reports_root = Path.home() / ".claude" / "reviews"
        if not reports_root.exists():
            print("No reviews found", file=sys.stderr)
            sys.exit(1)

        # Compute aggregates across all repos
        all_reports = {}
        total_runs_included = 0
        total_runs_excluded = 0
        total_runs_after_window = 0
        aggregate_regime_counts: Dict[str, int] = {}
        aggregate_strata: Dict[str, Dict[str, Dict[str, Any]]] = {}
        aggregate_excluded_by_reason: Dict[str, Any] = {"malformed_findings": {}, "effort": {}}
        aggregate_skipped_findings_total = 0
        aggregate_solo_findings: Dict[str, int] = {}
        aggregate_tokens_by_stratum: Dict[str, Dict[str, int]] = {}
        first_run_timestamp: Optional[datetime] = None
        last_run_timestamp: Optional[datetime] = None

        for repo_subdir in sorted(reports_root.iterdir()):
            if not repo_subdir.is_dir():
                continue
            repo_key = repo_subdir.name
            report = compute_report_data(repo_key, bucket_config, until_dt)
            all_reports[repo_key] = report

            # Skip repos with errors (no reviews)
            if "error" in report:
                continue

            # Accumulate metrics
            total_runs_included += report.get("n_included_runs", 0)
            total_runs_excluded += report.get("n_excluded_runs", 0)

            # Track corpus window timestamps
            corpus = report.get("methodology", {}).get("corpus_boundary", {})
            first_iso = corpus.get("first_run")
            last_iso = corpus.get("last_run")
            if first_iso:
                first_dt = datetime.fromisoformat(first_iso)
                if first_run_timestamp is None or first_dt < first_run_timestamp:
                    first_run_timestamp = first_dt
            if last_iso:
                last_dt = datetime.fromisoformat(last_iso)
                if last_run_timestamp is None or last_dt > last_run_timestamp:
                    last_run_timestamp = last_dt

            # Accumulate n_after_window
            total_runs_after_window += corpus.get("n_after_window", 0)

            # Accumulate regime counts
            for regime, count in report.get("regime_counts", {}).items():
                aggregate_regime_counts[regime] = aggregate_regime_counts.get(regime, 0) + count

            # Accumulate strata
            for stratum, buckets in report.get("strata", {}).items():
                if stratum not in aggregate_strata:
                    aggregate_strata[stratum] = {}
                for bucket, metrics in buckets.items():
                    if bucket not in aggregate_strata[stratum]:
                        aggregate_strata[stratum][bucket] = {
                            "n_runs": 0,
                            "reviewers": [],
                            "crit_high": [],
                            "value": [],
                        }
                    aggregate_strata[stratum][bucket]["reviewers"].extend(metrics.get("reviewers", []))
                    aggregate_strata[stratum][bucket]["crit_high"].extend(metrics.get("crit_high", []))
                    aggregate_strata[stratum][bucket]["value"].extend(metrics.get("value", []))
                    aggregate_strata[stratum][bucket]["n_runs"] += len(metrics.get("reviewers", []))

            # Accumulate solo findings
            for reviewer, count in report.get("solo_findings_per_reviewer", {}).items():
                aggregate_solo_findings[reviewer] = aggregate_solo_findings.get(reviewer, 0) + count

            # Accumulate excluded_by_reason
            excluded = report.get("methodology", {}).get("excluded_by_reason", {})
            for reason, count in excluded.get("malformed_findings", {}).items():
                aggregate_excluded_by_reason["malformed_findings"][reason] = aggregate_excluded_by_reason["malformed_findings"].get(reason, 0) + count
            aggregate_skipped_findings_total += excluded.get("skipped_findings_total", 0)
            for reason, count in excluded.get("effort", {}).items():
                aggregate_excluded_by_reason["effort"][reason] = aggregate_excluded_by_reason["effort"].get(reason, 0) + count

            # Accumulate tokens by stratum
            tokens = report.get("tokens", {})
            by_stratum = tokens.get("by_stratum", {})
            for stratum, token_data in by_stratum.items():
                if stratum not in aggregate_tokens_by_stratum:
                    aggregate_tokens_by_stratum[stratum] = {
                        "measured_runs": 0,
                        "total_runs": 0,
                    }
                aggregate_tokens_by_stratum[stratum]["measured_runs"] += token_data.get("measured_runs", 0)
                aggregate_tokens_by_stratum[stratum]["total_runs"] += token_data.get("total_runs", 0)

        # Compute aggregated strata with averages (pooled per-repo lists, then averaged)
        sanitized_strata: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for stratum, buckets in aggregate_strata.items():
            sanitized_strata[stratum] = {}
            for bucket, data in buckets.items():
                reviewers_list = data.get("reviewers", [])
                crit_high_list = data.get("crit_high", [])
                value_list = data.get("value", [])
                n_runs = data.get("n_runs", 0)

                sanitized_strata[stratum][bucket] = {
                    "n_runs": n_runs,
                    "avg_reviewers": round(_avg(reviewers_list), 3) if reviewers_list else None,
                    "avg_crit_high": round(_avg(crit_high_list), 3) if crit_high_list else None,
                    "avg_verified_value": round(_avg(value_list), 3) if value_list else None,
                }

        # Build token status strings per stratum
        tokens_status_by_stratum: Dict[str, str] = {}
        for stratum in sanitized_strata.keys():
            stratum_token_data = aggregate_tokens_by_stratum.get(stratum, {})
            measured_runs = stratum_token_data.get("measured_runs", 0)
            if measured_runs == 0:
                tokens_status_by_stratum[stratum] = "not measured pre-#206 (unrecoverable by construction)"
            else:
                tokens_status_by_stratum[stratum] = f"{measured_runs} measured"

        # Build the aggregate with pooled token strata
        aggregate_dict: Dict[str, Any] = {
            "total_runs_included": total_runs_included,
            "total_runs_excluded": total_runs_excluded,
            "total_runs_after_window": total_runs_after_window,
            "regime_counts": aggregate_regime_counts,
            "strata": sanitized_strata,
            "solo_findings_per_reviewer": aggregate_solo_findings,
            "excluded_by_reason": {
                "malformed_findings": aggregate_excluded_by_reason["malformed_findings"],
                "skipped_findings_total": aggregate_skipped_findings_total,
            },
        }
        if aggregate_excluded_by_reason["effort"]:
            aggregate_dict["excluded_by_reason"]["effort"] = aggregate_excluded_by_reason["effort"]

        # Add tokens by stratum with status strings
        aggregate_dict["tokens"] = {}
        for stratum in sorted(sanitized_strata.keys()):
            stratum_token_data = aggregate_tokens_by_stratum.get(stratum, {})
            aggregate_dict["tokens"][stratum] = {
                "measured_runs": stratum_token_data.get("measured_runs", 0),
                "total_runs": stratum_token_data.get("total_runs", 0),
                "status": tokens_status_by_stratum.get(stratum, "unknown"),
            }

        # Build generating_command with exact flags
        generating_command = "reviewer-yield.py --snapshot"
        if args.until:
            generating_command += f" --until {args.until}"

        # Create sanitized cross-repo aggregate
        snapshot_data = {
            "snapshot_schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generating_command": generating_command,
            "regime_filter": "post-148-sam-gated",
            "corpus_window": {
                "from": first_run_timestamp.isoformat() if first_run_timestamp else None,
                "to": last_run_timestamp.isoformat() if last_run_timestamp else None,
                "until_exclusive": args.until if args.until else None,
                "corpus_window_timezone": "local-naive",
            },
            "bucket_config_version": bucket_config.get("config_version", 1),
            "n_repos": len([r for r in all_reports.values() if "error" not in r]),
            "methodology": {
                "formula": _verified_value_formula(),
                "observation_only": True,
                "observation_only_sentence": OBSERVATION_ONLY_NOTE,
                "note": "No standalone scripts/routing-report.py — folded into reviewer-yield.py --report/--snapshot (#193 0c deviation)",
            },
            "aggregate": aggregate_dict,
        }

        # Write snapshot to file
        try:
            snapshot_path = Path(args.snapshot).expanduser()
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_text(json.dumps(snapshot_data, indent=2) + "\n")
            print(f"Snapshot written to: {snapshot_path}", file=sys.stderr)
        except OSError as e:
            print(f"Error writing snapshot: {e}", file=sys.stderr)
            sys.exit(1)

        return

    if not args.review_dir:
        # No positional and no --aggregate: this is for direct/manual invocation only,
        # such as testing or querying a specific repo directly (not part of normal flow)
        print("Error: provide review directory path or use --aggregate/--report with repo key", file=sys.stderr)
        sys.exit(1)

    # Single review run mode
    # Classify format first
    review_dir = Path(args.review_dir).expanduser().resolve()
    if not review_dir.exists():
        print(f"Error: review directory not found: {review_dir}", file=sys.stderr)
        sys.exit(1)

    review_format = classify_review_format(review_dir)

    if review_format == "pod":
        pods, lenses = read_pod_manifest(review_dir)
        pods_str = ", ".join(pods) if pods else "unrecorded"
        lenses_str = ", ".join(lenses) if lenses else "unrecorded"
        print(f"Pod-format (effort 2) run: per-reviewer yield not applicable (pods: {pods_str}; lenses: {lenses_str})")
        sys.exit(0)

    if review_format == "unknown":
        print("No per-reviewer checkpoint files (*-pass1.md) found; unrecognized review format — not measured")
        sys.exit(0)

    repo_key, rows, tokens_status = process_review_dir(args.review_dir)

    if repo_key is None:
        sys.exit(1)

    # No-data case: no pass1 files means no reviewers
    if not rows:
        print("No reviewer data found; nothing logged", file=sys.stderr)
        sys.exit(2)

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
