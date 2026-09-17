#!/usr/bin/env python3
"""
Offline reviewer-selection audit harness for /expert-review.

Analyzes corpus of expert-review runs to audit reviewer attendance patterns,
yield efficiency, and simulate impact of candidate index changes.

Never invoked by review-time paths; used only for offline tuning and analysis.
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple, Optional


# Reuse from reviewer-yield.py for consistency
def get_repo_key(review_dir: Path) -> str:
    """
    Extract repo key from the reviews directory structure.

    Review dirs are typically ~/.claude/reviews/{owner-repo}/{review-dir-name}/.
    Validates that repo_key is non-empty and looks reasonable (not ".." or special chars).
    Returns "unknown" only if extraction fails validation.
    """
    parent = review_dir.parent
    repo_key = parent.name if parent else ""

    if not repo_key or repo_key in (".", "..", "reviews") or repo_key.startswith("-"):
        return "unknown"

    return repo_key


def get_review_run_id(review_dir: Path) -> str:
    """Get a stable run ID from review directory name."""
    return review_dir.name


def print_caveat_header() -> None:
    """Print fixed caveat header for all reports."""
    print(
        "Corpus caveats: repo-concentrated (lotl-co-lotl 32.9%, stephenkiers-InsuranceTracking 12.7%, "
        "claude-helpers 9.1%; three repos > 54%); branch repetition moderate (78-80% unique branches); "
        "per-run --effort level is NOT reconstructible (effort-scout.json exists in 1 of 772 review dirs)."
    )
    print()


def parse_panel_decision_table(tagged_sections_path: Path) -> Dict[str, str]:
    """
    Parse Panel Decision table from tagged-sections.md.

    Returns a dict mapping reviewer slug to selection status ("Yes" or "No").
    Returns empty dict if file not found or table not found.
    """
    if not tagged_sections_path.exists():
        return {}

    try:
        content = tagged_sections_path.read_text()
    except OSError:
        return {}

    # Look for the Panel Decision section and its table
    panel_decision_start = content.find("## Panel Decision")
    if panel_decision_start == -1:
        return {}

    # Extract the table section (between Panel Decision and next section or EOF)
    table_end = content.find("\n## ", panel_decision_start + 1)
    if table_end == -1:
        table_section = content[panel_decision_start:]
    else:
        table_section = content[panel_decision_start:table_end]

    # Parse markdown table rows
    result = {}
    lines = table_section.split("\n")
    in_table = False
    for line in lines:
        if line.startswith("|") and "---" in line:
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break

        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue

        # Format: | Reviewer | Selected | Reason |
        reviewer = parts[1]
        selected = parts[2]

        if reviewer and selected and reviewer not in ("Reviewer", ""):
            result[reviewer] = selected

    return result


def parse_structural_gates(tagged_sections_path: Path) -> Dict[str, str]:
    """
    Parse structural-gate markers from tagged-sections.md.

    Returns a dict mapping reviewer slug to gate status ("excluded" or "skipped").
    Gate status "router-no" is inferred (absence of marker or gate that is not excluded/skipped).

    Format:
    <!-- structural-gate: reviewer-slug | excluded | {reason} -->
    <!-- structural-gate: reviewer-slug | skipped | {reason} -->
    """
    if not tagged_sections_path.exists():
        return {}

    try:
        content = tagged_sections_path.read_text()
    except OSError:
        return {}

    gates = {}
    # Match: <!-- structural-gate: reviewer-slug | status | reason -->
    pattern = r"<!--\s*structural-gate:\s*([a-z\-]+)\s*\|\s*(excluded|skipped)\s*\|\s*(.+?)\s*-->"
    for match in re.finditer(pattern, content):
        reviewer = match.group(1)
        status = match.group(2)
        gates[reviewer] = status

    return gates


def parse_findings_by_severity(final_report_path: Path) -> Dict[str, List[Tuple[str, str, bool]]]:
    """
    Parse findings from final-report.md by severity.

    Returns a dict mapping severity level ("Critical", "High", "Medium", "Low")
    to a list of (finding_id, reviewer_string, is_confirmed) tuples.

    A finding is CONFIRMED if it contains "CONFIRMED" marker in its context line.
    Returns empty dict if file not found.
    """
    if not final_report_path.exists():
        return {}

    try:
        content = final_report_path.read_text()
    except OSError:
        return {}

    result: Dict[str, List[Tuple[str, str, bool]]] = {
        "Critical": [],
        "High": [],
        "Medium": [],
        "Low": [],
    }

    # Split by severity section
    severity_pattern = r"###\s+(Critical|High|Medium|Low)\s*\n"
    severity_matches = list(re.finditer(severity_pattern, content))

    for i, match in enumerate(severity_matches):
        severity = match.group(1)
        section_start = match.end()
        section_end = severity_matches[i + 1].start() if i + 1 < len(severity_matches) else len(content)
        section = content[section_start:section_end]

        # Extract findings (e.g., "#### C1 —", "#### H2 —")
        finding_pattern = r"####\s+([A-Z]\d+[a-z]?)\s*—\s*(.+?)(?=####|$)"
        for finding_match in re.finditer(finding_pattern, section, re.DOTALL):
            finding_id = finding_match.group(1)
            finding_body = finding_match.group(2)

            # Check for CONFIRMED marker
            is_confirmed = "CONFIRMED" in finding_body.upper()

            # Extract reviewer info (look for "**Reviewer**:" or "**Reviewers**:")
            reviewer_match = re.search(
                r"\*\*Reviewers?:\*\*\s*([^\n]+)", finding_body
            )
            reviewer_str = reviewer_match.group(1) if reviewer_match else ""

            if finding_id and severity in result:
                result[severity].append((finding_id, reviewer_str, is_confirmed))

    return result


def load_corpus(corpus_root: str) -> List[Path]:
    """
    Load all review directories from corpus root.

    Corpus root defaults to ~/.claude/reviews/*/* format.
    Returns sorted list of review directory paths.
    """
    root = Path(corpus_root).expanduser()
    if not root.exists():
        return []

    # Find all directories matching ~/.claude/reviews/{repo}/{run}/
    review_dirs = []
    for repo_dir in root.glob("*"):
        if repo_dir.is_dir():
            for run_dir in repo_dir.glob("*"):
                if run_dir.is_dir() and (run_dir / "final-report.md").exists():
                    review_dirs.append(run_dir)

    return sorted(review_dirs)


def cmd_attendance(corpus_root: str) -> None:
    """
    Report per-reviewer attendance: fraction of runs where Selected = Yes in Panel Decision.

    Also report three gate states: router-yes, router-no, structural-gate-no.
    """
    print_caveat_header()
    print("=== Reviewer Attendance Analysis ===")
    print()

    corpus = load_corpus(corpus_root)
    if not corpus:
        print(f"No review corpus found at {corpus_root}")
        return

    # Track attendance per reviewer
    reviewer_attendance: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"selected_yes": 0, "router_no": 0, "structural_gate_no": 0, "total": 0}
    )

    # Track by repo
    repo_attendance: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"selected_yes": 0, "router_no": 0, "structural_gate_no": 0, "total": 0})
    )

    # Track runs without any structural-gate markers
    runs_without_markers = 0

    for review_dir in corpus:
        repo_key = get_repo_key(review_dir)
        tagged_sections = review_dir / "tagged-sections.md"

        # Parse panel decision and structural gates
        panel_decisions = parse_panel_decision_table(tagged_sections)
        structural_gates = parse_structural_gates(tagged_sections)

        # Check if this run has any markers
        has_markers = len(structural_gates) > 0

        # Get all reviewers mentioned in panel decision
        reviewers_in_run = set()
        for reviewer in panel_decisions.keys():
            reviewers_in_run.add(reviewer)

        # Add any reviewers in structural gates
        for reviewer in structural_gates.keys():
            reviewers_in_run.add(reviewer)

        if not has_markers:
            runs_without_markers += 1

        # Process each reviewer
        for reviewer in sorted(reviewers_in_run):
            selection = panel_decisions.get(reviewer, "")
            gate_status = structural_gates.get(reviewer, "")

            # Determine gate state
            if gate_status == "excluded":
                state = "structural_gate_no"
            elif gate_status == "skipped":
                state = "structural_gate_no"
            elif selection == "Yes":
                state = "selected_yes"
            else:
                state = "router_no"

            reviewer_attendance[reviewer]["total"] += 1
            reviewer_attendance[reviewer][state] += 1

            repo_attendance[repo_key][reviewer]["total"] += 1
            repo_attendance[repo_key][reviewer][state] += 1

    # Print panel-wide summary
    print("Panel-Wide Attendance Summary")
    print("=" * 100)
    print(f"{'Reviewer':<30} {'Selected (Yes)':<18} {'Router (No)':<18} {'Gate (Excluded)':<18} {'Attendance %':<15}")
    print("-" * 100)

    for reviewer in sorted(reviewer_attendance.keys()):
        stats = reviewer_attendance[reviewer]
        total = stats["total"]
        selected_yes = stats["selected_yes"]
        router_no = stats["router_no"]
        gate_no = stats["structural_gate_no"]
        attendance_pct = 100.0 * selected_yes / total if total > 0 else 0.0

        print(
            f"{reviewer:<30} {selected_yes:<18}/{total:<1} {router_no:<18}/{total:<1} "
            f"{gate_no:<18}/{total:<1} {attendance_pct:<14.1f}%"
        )

    print()
    print(f"Total runs in corpus: {len(corpus)}")
    print(f"Runs without structural-gate markers: {runs_without_markers}")
    print()

    # Print per-repo breakdown
    print("Per-Repo Attendance Breakdown")
    print("=" * 100)
    for repo_key in sorted(repo_attendance.keys()):
        print(f"\n{repo_key}:")
        repo_stats = repo_attendance[repo_key]
        total_runs_in_repo = len([d for d in corpus if get_repo_key(d) == repo_key])
        print(f"  Runs: {total_runs_in_repo}")

        for reviewer in sorted(repo_stats.keys()):
            stats = repo_stats[reviewer]
            total = stats["total"]
            selected_yes = stats["selected_yes"]
            attendance_pct = 100.0 * selected_yes / total if total > 0 else 0.0

            print(f"  {reviewer:<28} {selected_yes:>3}/{total:<3} ({attendance_pct:>5.1f}%)")


def cmd_yield(corpus_root: str) -> None:
    """
    Report severity-weighted yield per attended run.

    Critical=8, High=4, Medium=2, Low=1, counting only CONFIRMED findings.
    """
    print_caveat_header()
    print("=== Reviewer Yield Analysis ===")
    print()

    corpus = load_corpus(corpus_root)
    if not corpus:
        print(f"No review corpus found at {corpus_root}")
        return

    # Severity weights
    weights = {"Critical": 8, "High": 4, "Medium": 2, "Low": 1}

    # Track yield per reviewer per run
    reviewer_yields: Dict[str, List[float]] = defaultdict(list)
    repo_reviewer_yields: Dict[str, Dict[str, List[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for review_dir in corpus:
        repo_key = get_repo_key(review_dir)
        final_report = review_dir / "final-report.md"

        findings_by_severity = parse_findings_by_severity(final_report)

        # Calculate yield per reviewer
        reviewer_scores: Dict[str, float] = defaultdict(float)

        for severity in findings_by_severity:
            weight = weights.get(severity, 0)
            for finding_id, reviewer_str, is_confirmed in findings_by_severity[severity]:
                if not is_confirmed:
                    continue

                # Parse reviewer string (could be comma-separated or with other text)
                # Extract reviewer slugs (words with hyphens)
                reviewers = re.findall(r"[a-z]+(?:-[a-z]+)*", reviewer_str.lower())

                for reviewer in reviewers:
                    reviewer_scores[reviewer] += weight

        # Store yields
        for reviewer, score in reviewer_scores.items():
            if score > 0:  # Only track attended runs with findings
                reviewer_yields[reviewer].append(score)
                repo_reviewer_yields[repo_key][reviewer].append(score)

    # Print panel-wide summary
    print("Panel-Wide Yield Summary (CONFIRMED findings only)")
    print("=" * 90)
    print(f"{'Reviewer':<30} {'Avg Yield':<15} {'Total Yield':<15} {'Run Count':<12}")
    print("-" * 90)

    for reviewer in sorted(reviewer_yields.keys()):
        yields = reviewer_yields[reviewer]
        total_yield = sum(yields)
        avg_yield = total_yield / len(yields) if yields else 0.0
        run_count = len(yields)

        print(
            f"{reviewer:<30} {avg_yield:<15.2f} {total_yield:<15.2f} {run_count:<12}"
        )

    print()

    # Print per-repo breakdown
    print("Per-Repo Yield Breakdown")
    print("=" * 90)
    for repo_key in sorted(repo_reviewer_yields.keys()):
        print(f"\n{repo_key}:")
        repo_stats = repo_reviewer_yields[repo_key]

        for reviewer in sorted(repo_stats.keys()):
            yields = repo_stats[reviewer]
            total_yield = sum(yields)
            avg_yield = total_yield / len(yields) if yields else 0.0
            run_count = len(yields)

            print(
                f"  {reviewer:<28} avg={avg_yield:>6.2f} total={total_yield:>7.2f} runs={run_count:>3}"
            )


def load_candidate_index(candidate_index_path: str) -> Dict:
    """Load candidate reviewer index YAML (simplified JSON parsing)."""
    candidate_path = Path(candidate_index_path).expanduser()
    if not candidate_path.exists():
        print(f"Error: candidate index not found: {candidate_path}", file=sys.stderr)
        return {}

    try:
        with open(candidate_path, "r") as f:
            # Simple YAML parsing for reviewer index structure
            # In practice, we'd parse YAML, but for now we read as text and extract
            # the minimal info needed for comparison
            content = f.read()
            return _parse_reviewer_index_yaml(content)
    except OSError as e:
        print(f"Error reading candidate index: {e}", file=sys.stderr)
        return {}


def _parse_reviewer_index_yaml(yaml_content: str) -> Dict:
    """
    Minimal YAML parser for reviewer index format.

    Returns a dict keyed by reviewer slug with 'useWhen' and 'triggers' fields.
    This is a simplified implementation; a full YAML parser would be more robust.
    """
    # This is a placeholder; actual implementation would use a YAML library
    # For now, return empty dict to indicate this needs implementation
    return {}


def cmd_simulate(corpus_root: str, candidate_index: str, reviewer_filter: Optional[str] = None) -> None:
    """
    Simulate impact of candidate index changes.

    Computes trigger-match deltas between current and candidate index.
    Emits runs flipping include→exclude, exclude→include, and affected findings.
    """
    print_caveat_header()
    print("=== Reviewer Selection Simulation ===")
    print()

    # Load current and candidate indexes
    # For now, this is a placeholder
    print(f"Candidate index: {candidate_index}")
    if reviewer_filter:
        print(f"Reviewer filter: {reviewer_filter}")
    print()
    print("NOTE: simulate subcommand is not yet implemented.")
    print("This requires full trigger-matching logic and diff-index.md parsing.")


def main():
    parser = argparse.ArgumentParser(
        description="Offline reviewer-selection audit for /expert-review.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 reviewer-selection-audit.py attendance
  python3 reviewer-selection-audit.py yield
  python3 reviewer-selection-audit.py simulate --candidate-index /path/to/index.yaml
  python3 reviewer-selection-audit.py simulate --candidate-index /path/to/index.yaml --reviewer uncle-bob
        """,
    )

    parser.add_argument(
        "--corpus-root",
        default="~/.claude/reviews/*/*/",
        help="Corpus root directory (default: ~/.claude/reviews/*/*/)",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to run")

    # Attendance subcommand
    subparsers.add_parser(
        "attendance",
        help="Report per-reviewer attendance in corpus runs",
    )

    # Yield subcommand
    subparsers.add_parser(
        "yield",
        help="Report severity-weighted yield per attended run",
    )

    # Simulate subcommand
    simulate_parser = subparsers.add_parser(
        "simulate",
        help="Simulate impact of candidate index changes",
    )
    simulate_parser.add_argument(
        "--candidate-index",
        required=True,
        help="Path to candidate reviewer index YAML",
    )
    simulate_parser.add_argument(
        "--reviewer",
        help="Filter simulation to single reviewer slug",
    )

    args = parser.parse_args()

    if not args.subcommand:
        parser.print_help()
        sys.exit(1)

    # Normalize corpus root (remove glob pattern for actual filesystem search)
    corpus_root = args.corpus_root.rstrip("/").rstrip("*").rstrip("/")

    if args.subcommand == "attendance":
        cmd_attendance(corpus_root)
    elif args.subcommand == "yield":
        cmd_yield(corpus_root)
    elif args.subcommand == "simulate":
        cmd_simulate(corpus_root, args.candidate_index, args.reviewer)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
