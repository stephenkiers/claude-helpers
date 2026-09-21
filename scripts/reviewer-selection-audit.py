#!/usr/bin/env python3
"""
Offline reviewer-selection audit harness for /expert-review.

Analyzes corpus of expert-review runs to audit reviewer attendance patterns,
yield efficiency, and simulate impact of candidate index changes.

Never invoked by review-time paths; used only for offline tuning and analysis.
"""

import argparse
import fnmatch
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import yaml

SEVERITY_WEIGHTS = {"Critical": 8, "High": 4, "Medium": 2, "Low": 1}


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
        # The table carries display names ("Sam System"); normalize to the same
        # slug used by structural-gate markers and index.yaml ("sam-system") so
        # callers can key both by the same identifier. "Selected" may read
        # "Yes (pre-seated)" for framework-forced reviewers — still attended.
        reviewer = slugify_reviewer_name(parts[1]) if parts[1] else ""
        selected = "Yes" if parts[2].strip().lower().startswith("yes") else "No"

        if reviewer and reviewer not in ("reviewer", ""):
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

    # Corpus reports vary in severity-heading case ("### High", "### HIGH") and
    # finding-marker shape ("#### M1 — Title", "#### 1. Title", "#### Title").
    # Split on any "### <severity>" heading (any case), bounded by the next
    # heading at level <=3 (another severity section, or a new top-level "## ").
    severity_pattern = r"^###\s+(Critical|High|Medium|Low)\b.*$"
    severity_matches = list(re.finditer(severity_pattern, content, re.IGNORECASE | re.MULTILINE))
    next_heading_pattern = re.compile(r"^#{2,3}\s", re.MULTILINE)

    for match in severity_matches:
        severity = match.group(1).capitalize()
        section_start = match.end()
        next_heading = next_heading_pattern.search(content, section_start)
        section_end = next_heading.start() if next_heading else len(content)
        section = content[section_start:section_end]

        # Findings are "#### ..." headings; body runs until the next #### or EOF.
        finding_pattern = r"^####\s+(.+?)$(.*?)(?=^####\s|\Z)"
        for idx, finding_match in enumerate(
            re.finditer(finding_pattern, section, re.DOTALL | re.MULTILINE), start=1
        ):
            heading_text = finding_match.group(1).strip()
            finding_body = finding_match.group(2)

            # Prefer an explicit short ID ("M1", "C2a") from the heading; else fall
            # back to a positional ID so every finding is still counted.
            id_match = re.match(r"([A-Z]\d+[a-z]?)\b", heading_text)
            finding_id = id_match.group(1) if id_match else f"{severity[0]}{idx}"

            is_confirmed = "CONFIRMED" in finding_body.upper()

            reviewer_match = re.search(
                r"\*\*Reviewers?\*\*:\s*([^\n]+)", finding_body
            )
            reviewer_str = reviewer_match.group(1) if reviewer_match else ""

            if severity in result:
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


def extract_reviewer_slugs(reviewer_str: str, valid_slugs: set) -> List[str]:
    """
    Extract known reviewer slugs from a '**Reviewer(s)**:' field.

    Splits on commas, strips parenthetical asides (e.g. "(orig. HIGH)",
    "(source-verified; ...)"), slugifies each remaining name, and keeps only
    slugs present in valid_slugs — so parenthetical commentary words never
    get mistaken for a reviewer.
    """
    slugs = []
    for segment in reviewer_str.split(","):
        name = re.sub(r"\(.*?\)", "", segment).strip()
        if not name:
            continue
        slug = slugify_reviewer_name(name)
        if slug in valid_slugs:
            slugs.append(slug)
    return slugs


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

    valid_slugs = set(load_reviewer_index(default_current_index_path()).keys())

    # Track yield per reviewer, one entry per ATTENDED run (Selected = Yes in
    # Panel Decision) — including runs with zero confirmed findings, since
    # "yield per attended run" divides by attendance, not by hit rate.
    reviewer_yields: Dict[str, List[float]] = defaultdict(list)
    repo_reviewer_yields: Dict[str, Dict[str, List[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for review_dir in corpus:
        repo_key = get_repo_key(review_dir)
        final_report = review_dir / "final-report.md"
        tagged_sections = review_dir / "tagged-sections.md"

        panel_decisions = parse_panel_decision_table(tagged_sections)
        attended_slugs = {
            slug for slug, selected in panel_decisions.items() if selected == "Yes"
        } & valid_slugs
        if not attended_slugs:
            continue

        findings_by_severity = parse_findings_by_severity(final_report)

        # Calculate confirmed-finding score per reviewer
        reviewer_scores: Dict[str, float] = defaultdict(float)

        for severity in findings_by_severity:
            weight = weights.get(severity, 0)
            for finding_id, reviewer_str, is_confirmed in findings_by_severity[severity]:
                if not is_confirmed:
                    continue

                # Parse reviewer string (comma-separated names, may carry parenthetical asides)
                reviewers = extract_reviewer_slugs(reviewer_str, valid_slugs)

                for reviewer in reviewers:
                    reviewer_scores[reviewer] += weight

        # One entry per attended reviewer, including a 0.0 for a clean attended run
        for reviewer in attended_slugs:
            score = reviewer_scores.get(reviewer, 0.0)
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


def slugify_reviewer_name(name: str) -> str:
    """Convert a reviewer 'name' field to its index slug (e.g. 'Sam System' -> 'sam-system')."""
    return re.sub(r"\s+", "-", name.strip().lower())


def parse_inline_flow_map(value) -> Dict[str, str]:
    """
    Parse an inline YAML flow map into a dict.

    Accepts either:
    - A string like '{review: primary, plan: primary}'
    - A dict already parsed by YAML (e.g., {'review': 'primary', 'plan': 'primary'})

    Validates that keys are in {review, plan, write} and values are in
    {primary, secondary, named-only}.

    Raises ValueError on invalid format or unknown keys/values.
    Returns empty dict if value is None or empty string/dict.
    """
    if not value:
        return {}

    # If already a dict (parsed by YAML), validate and return
    if isinstance(value, dict):
        result: Dict[str, str] = {}
        valid_keys = {"review", "plan", "write"}
        valid_values = {"primary", "secondary", "named-only"}

        for key, val in value.items():
            if key not in valid_keys:
                raise ValueError(f"Unknown context key: {key} (valid: {', '.join(sorted(valid_keys))})")
            if val not in valid_values:
                raise ValueError(f"Unknown context value: {val} (valid: {', '.join(sorted(valid_values))})")
            result[key] = val

        return result

    # Otherwise parse as string
    value = str(value).strip()
    if not value.startswith("{") or not value.endswith("}"):
        raise ValueError(f"Invalid flow map format: {value}")

    # Remove braces and split by comma
    content = value[1:-1].strip()
    if not content:
        return {}

    result: Dict[str, str] = {}
    valid_keys = {"review", "plan", "write"}
    valid_values = {"primary", "secondary", "named-only"}

    for pair in content.split(","):
        pair = pair.strip()
        if not pair:
            continue

        if ":" not in pair:
            raise ValueError(f"Invalid key-value pair: {pair}")

        key, val = pair.split(":", 1)
        key = key.strip()
        val = val.strip()

        if key not in valid_keys:
            raise ValueError(f"Unknown context key: {key} (valid: {', '.join(sorted(valid_keys))})")
        if val not in valid_values:
            raise ValueError(f"Unknown context value: {val} (valid: {', '.join(sorted(valid_values))})")

        result[key] = val

    return result


def load_reviewer_index(index_path: Path) -> Dict[str, Dict]:
    """
    Load a reviewer index.yaml into slug -> {'useWhen': str, 'triggers': List[str], 'contexts': Dict[str, str]}.

    Returns {} if the file is missing or unparsable.
    Raises SystemExit on invalid contexts format (unknown keys/values).
    """
    if not index_path.exists():
        return {}

    try:
        data = yaml.safe_load(index_path.read_text())
    except (OSError, yaml.YAMLError) as e:
        print(f"Error reading index {index_path}: {e}", file=sys.stderr)
        return {}

    result: Dict[str, Dict] = {}
    for entry in (data or {}).get("reviewers", []) or []:
        name = entry.get("name")
        if not name:
            continue
        slug = slugify_reviewer_name(name)

        # Parse contexts field if present
        contexts_raw = entry.get("contexts")
        try:
            contexts = parse_inline_flow_map(contexts_raw) if contexts_raw else {}
        except ValueError as e:
            print(f"Error parsing contexts for reviewer '{name}' ({slug}): {e}", file=sys.stderr)
            sys.exit(1)

        result[slug] = {
            "useWhen": entry.get("useWhen", ""),
            "triggers": [str(t) for t in (entry.get("triggers") or [])],
            "contexts": contexts,
        }
    return result


def default_current_index_path() -> Path:
    """The repo's live reviewers/index.yaml, resolved relative to this script."""
    return Path(__file__).resolve().parent.parent / "reviewers" / "index.yaml"


def trigger_matches(trigger: str, diff_index_content: str) -> bool:
    """
    Does a single trigger match diff-index.md content?

    Any trigger containing "*" is a filename glob (e.g. "*.ts", "*.test.*",
    "*_test.rs") and is matched via fnmatch against touched file paths
    ('+++ b/...' lines). A plain endswith() check only handles single-star
    prefix globs correctly ("*.ts" -> ext ".ts") — it silently never matches
    a second "*" later in the pattern (e.g. "*.test.*" -> ext ".test.*",
    which no real filename ends with literally) or a "*" not immediately
    followed by "." (e.g. "*_test.rs" falls out of the glob branch entirely
    and is searched for as a literal asterisk substring, which never
    appears in diff text). fnmatch handles all of these correctly.
    Everything else is a case-insensitive substring match against the diff
    content, mirroring the Sam System gate's grep -qE approach.
    """
    if "*" in trigger:
        touched_files = re.findall(r"^\+\+\+ b/(.+)$", diff_index_content, re.MULTILINE)
        return any(fnmatch.fnmatch(f.lower(), trigger.lower()) for f in touched_files)
    return trigger.lower() in diff_index_content.lower()


def matched_triggers(triggers: List[str], diff_index_content: str) -> List[str]:
    """Return the subset of triggers that match, preserving input order."""
    return [t for t in triggers if trigger_matches(t, diff_index_content)]


def changed_reviewers(current: Dict[str, Dict], candidate: Dict[str, Dict]) -> List[str]:
    """Reviewer slugs whose triggers, useWhen, or contexts differ between current and candidate."""
    slugs = sorted(set(current.keys()) | set(candidate.keys()))
    changed = []
    for slug in slugs:
        cur = current.get(slug, {})
        cand = candidate.get(slug, {})
        cur_contexts = cur.get("contexts", {})
        cand_contexts = cand.get("contexts", {})
        if (cur.get("triggers") != cand.get("triggers") or
            cur.get("useWhen") != cand.get("useWhen") or
            cur_contexts != cand_contexts):
            changed.append(slug)
    return changed


def has_trigger_delta(slug: str, current: Dict[str, Dict], candidate: Dict[str, Dict]) -> bool:
    return current.get(slug, {}).get("triggers", []) != candidate.get(slug, {}).get("triggers", [])


def has_contexts_delta(slug: str, current: Dict[str, Dict], candidate: Dict[str, Dict]) -> bool:
    """Check if contexts differ between current and candidate for a reviewer."""
    cur_contexts = current.get(slug, {}).get("contexts", {})
    cand_contexts = candidate.get(slug, {}).get("contexts", {})
    return cur_contexts != cand_contexts


def format_contexts_delta(slug: str, current: Dict[str, Dict], candidate: Dict[str, Dict]) -> str:
    """Format context changes as a human-readable string."""
    cur_contexts = current.get(slug, {}).get("contexts", {})
    cand_contexts = candidate.get(slug, {}).get("contexts", {})

    changes = []
    all_keys = sorted(set(cur_contexts.keys()) | set(cand_contexts.keys()))

    for key in all_keys:
        cur_val = cur_contexts.get(key)
        cand_val = cand_contexts.get(key)

        if cur_val == cand_val:
            continue

        if cur_val and cand_val:
            # Both present but different (strength change)
            changes.append(f"contexts: {key}: {cur_val} → {cand_val}")
        elif cur_val and not cand_val:
            # Was present, now removed (exclude)
            changes.append(f"contexts: {key}: {cur_val} → (removed)")
        else:
            # Was not present, now added (include)
            changes.append(f"contexts: {key}: (added) → {cand_val}")

    return "; ".join(changes)


def repo_stratified_sample(corpus: List[Path], sample_size: int = 60) -> List[Path]:
    """
    Sample run directories spread across repos, proportional to each repo's
    share of the corpus, capped at sample_size total.
    """
    by_repo: Dict[str, List[Path]] = defaultdict(list)
    for review_dir in corpus:
        by_repo[get_repo_key(review_dir)].append(review_dir)

    if not corpus:
        return []

    sample: List[Path] = []
    for repo_key in sorted(by_repo.keys()):
        repo_dirs = by_repo[repo_key]
        share = max(1, round(sample_size * len(repo_dirs) / len(corpus)))
        sample.extend(repo_dirs[:share])

    return sample[:sample_size]


REPLAY_PROMPT_TEMPLATE = """You are re-deciding whether reviewer '{slug}' should be selected for this
run, under its NEW useWhen text (below), using only the diff contents on disk for this run directory.
Answer strictly Yes or No plus one sentence of reasoning; do not consult the old useWhen text.

New useWhen: {use_when}

Run directory: {run_dir}
"""


def print_prose_fallback(slug: str, use_when: str, corpus: List[Path]) -> None:
    print(f"No trigger delta for '{slug}' — useWhen text changed but the trigger list did not.")
    print("Emitting a repo-stratified sample of run directories and the replay prompt for Haiku dispatch.")
    print()
    sample = repo_stratified_sample(corpus, sample_size=60)
    print(f"Sample size: {len(sample)} run(s) across {len(set(get_repo_key(d) for d in sample))} repo(s)")
    print()
    print("--- Replay prompt template ---")
    print(REPLAY_PROMPT_TEMPLATE.format(slug=slug, use_when=use_when, run_dir="<run directory, one per sample entry below>"))
    print("--- Sample run directories ---")
    for review_dir in sample:
        print(f"  {review_dir}")


def cmd_simulate(corpus_root: str, candidate_index: str, reviewer_filter: Optional[str] = None) -> None:
    """
    Simulate impact of candidate index changes.

    Computes trigger-match deltas between current and candidate index.
    Emits runs flipping include→exclude, exclude→include, and affected findings.
    """
    print_caveat_header()
    print("=== Reviewer Selection Simulation ===")
    print()

    current = load_reviewer_index(default_current_index_path())
    candidate = load_reviewer_index(Path(candidate_index).expanduser())
    if not candidate:
        print(f"Error: candidate index not found or unparsable: {candidate_index}", file=sys.stderr)
        return

    if reviewer_filter:
        slugs = [reviewer_filter]
    else:
        slugs = changed_reviewers(current, candidate)

    if not slugs:
        print("No changed reviewers between current and candidate index.")
        return

    corpus = load_corpus(corpus_root)
    if not corpus:
        print(f"No review corpus found at {corpus_root}")
        return

    # Separate reviewers by type of change
    trigger_delta_slugs = [s for s in slugs if has_trigger_delta(s, current, candidate)]
    contexts_delta_slugs = [s for s in slugs if has_contexts_delta(s, current, candidate) and s not in trigger_delta_slugs]
    prose_only_slugs = [s for s in slugs if s not in trigger_delta_slugs and s not in contexts_delta_slugs]

    # Report context-only changes
    if contexts_delta_slugs:
        print("=== Context Changes (no trigger delta) ===")
        for slug in contexts_delta_slugs:
            delta_str = format_contexts_delta(slug, current, candidate)
            print(f"{slug}: {delta_str}")
        print()

    # Prose-only fallback: any requested reviewer with no trigger delta at all
    # (useWhen text changed, triggers identical) gets the replay-prompt path
    # instead of a mechanical simulation.
    for slug in prose_only_slugs:
        use_when = candidate.get(slug, current.get(slug, {})).get("useWhen", "")
        print_prose_fallback(slug, use_when, corpus)
        print()

    if not trigger_delta_slugs:
        return

    print(f"Simulating trigger-delta reviewers: {', '.join(trigger_delta_slugs)}")
    print()

    include_to_exclude: Dict[str, List[Path]] = defaultdict(list)
    exclude_to_include: Dict[str, List[Path]] = defaultdict(list)
    compounding_runs: List[Path] = []

    for review_dir in corpus:
        diff_index_path = review_dir / "diff-index.md"
        if not diff_index_path.exists():
            continue
        try:
            diff_content = diff_index_path.read_text()
        except OSError:
            continue

        flipped_out_this_run = []
        for slug in trigger_delta_slugs:
            cur_triggers = current.get(slug, {}).get("triggers", [])
            cand_triggers = candidate.get(slug, {}).get("triggers", [])
            cur_match = bool(matched_triggers(cur_triggers, diff_content))
            cand_match = bool(matched_triggers(cand_triggers, diff_content))

            if cur_match and not cand_match:
                include_to_exclude[slug].append(review_dir)
                flipped_out_this_run.append(slug)
            elif not cur_match and cand_match:
                exclude_to_include[slug].append(review_dir)

        if len(trigger_delta_slugs) > 1 and len(flipped_out_this_run) == len(trigger_delta_slugs):
            compounding_runs.append(review_dir)

    valid_slugs = set(current.keys()) | set(candidate.keys())

    print("=== Runs flipping include -> exclude ===")
    for slug in trigger_delta_slugs:
        runs = include_to_exclude.get(slug, [])
        print(f"\n{slug}: {len(runs)} run(s)")
        for review_dir in runs:
            print(f"  {review_dir}")
            findings = parse_findings_by_severity(review_dir / "final-report.md")
            for severity in ("Critical", "High"):
                for finding_id, reviewer_str, is_confirmed in findings.get(severity, []):
                    if not is_confirmed:
                        continue
                    if slug not in extract_reviewer_slugs(reviewer_str, valid_slugs):
                        continue
                    print(f"    CONFIRMED {severity} {finding_id} (reviewer: {reviewer_str.strip()})")

    print()
    print("=== Runs flipping exclude -> include ===")
    for slug in trigger_delta_slugs:
        runs = exclude_to_include.get(slug, [])
        print(f"\n{slug}: {len(runs)} run(s)")
        for review_dir in runs:
            print(f"  {review_dir}")

    if len(trigger_delta_slugs) > 1:
        print()
        print(f"=== Compounding narrowing: runs where ALL of {', '.join(trigger_delta_slugs)} flip out together ===")
        print(f"{len(compounding_runs)} run(s)")
        for review_dir in compounding_runs:
            print(f"  {review_dir}")


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

    # Normalize corpus root: load_corpus() globs two levels itself (repo/run),
    # so strip all trailing "*"/"/" segments rather than leaving one behind.
    corpus_root = args.corpus_root
    while corpus_root.endswith("/") or corpus_root.endswith("*"):
        corpus_root = corpus_root[:-1]

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
