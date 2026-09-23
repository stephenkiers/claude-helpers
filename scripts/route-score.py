#!/usr/bin/env python3
"""
Deterministic router scorer for expert-review reviewer seating.

Scores diffs against weighted `route:` blocks in reviewers/index.yaml.
Observe-only shadow mode (#195): never affects seating. All thresholds are PROVISIONAL and untuned (Phase 3 #196).

This script is pure and deterministic — same inputs always produce identical JSON output.
Exception handling is fail-open: errors exit 0 and write status: "error" to the output file.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

SCORER_VERSION = "1"
ALWAYS_RUN_SLUGS = frozenset(["contrarian-carl", "code-rot-cody", "consistency-checker"])

Tier = Literal["Must", "Candidate", "Exclude", "Always"]
ReasonKind = Literal["strong", "weak", "path", "shape", "hard_requires", "always"]

SHAPE_PREDICATES = {
    "cross_file_symbol",
    "file_count_ge",
    "top_dirs_ge",
    "new_files",
    "adr_touched",
    "dep_manifest",
    "test_files",
    "exports_changed",
    "ui_paths",
}

# Paths to exclude from word scanning (still counted for shape predicates).
EXCLUDED_PATTERNS = [
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
    "go.sum",
    "*.snap",
    "*.min.js",
    "vendor/",
    "node_modules/",
    "dist/",
]


class RouteConfigError(ValueError):
    """Validation error in route config; names reviewer slug and field."""
    pass


@dataclass(frozen=True)
class Reason:
    """One contribution to a reviewer score."""
    kind: str  # ReasonKind
    detail: str
    points: int


@dataclass(frozen=True)
class ReviewerScore:
    """Score and tier for one reviewer."""
    slug: str
    score: int
    tier: str  # Tier
    reasons: Tuple[Reason, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Enforce invariants."""
        computed_score = sum(r.points for r in self.reasons)
        if self.score != computed_score:
            raise ValueError(
                f"{self.slug}: score {self.score} != sum(points) {computed_score}"
            )
        if self.tier not in ("Must", "Candidate", "Exclude", "Always"):
            raise ValueError(f"{self.slug}: tier {self.tier} not in Tier literal")


@dataclass(frozen=True)
class RouteConfig:
    """Parsed route: block for one reviewer."""
    paths: Tuple[str, ...] = field(default_factory=tuple)
    strong: Tuple[str, ...] = field(default_factory=tuple)
    weak: Tuple[str, ...] = field(default_factory=tuple)
    shape: Dict[str, Any] = field(default_factory=dict)
    include_at: int = 6  # PROVISIONAL
    candidate_at: int = 3  # PROVISIONAL
    hard_requires: Tuple[str, ...] = field(default_factory=tuple)
    always: bool = False


@dataclass(frozen=True)
class Limits:
    """Bounds for cross_file_symbol scanning."""
    max_symbols: int = 200
    max_scan_lines: int = 5000


@dataclass(frozen=True)
class ScoreResult:
    """Output of score_diff()."""
    scorer_version: str
    degraded: bool
    thresholds_provisional: bool
    reviewers: Dict[str, ReviewerScore]


def _is_excluded_path(path: str) -> bool:
    """Check if a path matches any excluded pattern (lockfiles, generated, vendored)."""
    import fnmatch

    for pattern in EXCLUDED_PATTERNS:
        if fnmatch.fnmatch(path, pattern):
            return True
        if "/" in pattern and fnmatch.fnmatch(path, f"*/{pattern}"):
            return True
    return False


def _match_glob_patterns(path: str, patterns: Tuple[str, ...]) -> bool:
    """Check if path matches any glob pattern."""
    import fnmatch

    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def parse_route_configs(index_data: Any) -> Dict[str, RouteConfig]:
    """
    Parse route: blocks from index.yaml data structure.

    Validates:
    - Every `review`-context reviewer has a `route:` block.
    - `route:` has only allowed keys.
    - No string where list is expected.
    - All thresholds and points are ints (not bool).
    - candidate_at <= include_at.
    - All shape/hard_requires names are in the registry.
    - always: true is the only key if present.

    Returns {slug: RouteConfig}, slug = file: stem (e.g. "north-star-nick").
    Raises RouteConfigError.
    """
    configs = {}

    reviewers = index_data.get("reviewers", [])
    for entry in reviewers:
        file_path = entry.get("file", "")
        if not file_path:
            continue

        slug = Path(file_path).stem
        contexts = entry.get("contexts", {})

        # Check if this reviewer has a review context.
        has_review_context = "review" in contexts

        route_block = entry.get("route")

        if has_review_context and route_block is None:
            raise RouteConfigError(f"{slug}: review-context reviewer missing route: block")

        if route_block is None:
            continue

        # Parse the route block.
        if not isinstance(route_block, dict):
            raise RouteConfigError(f"{slug}: route: must be a dict")

        # If always: true, must be the only key.
        always = route_block.get("always", False)
        if always and len(route_block) > 1:
            raise RouteConfigError(
                f"{slug}: always: true must be the only key in route:"
            )

        if always:
            configs[slug] = RouteConfig(always=True)
            continue

        # Validate allowed keys.
        allowed_keys = {"paths", "strong", "weak", "shape", "include_at", "candidate_at", "hard_requires", "always"}
        for key in route_block.keys():
            if key not in allowed_keys:
                raise RouteConfigError(f"{slug}: unknown key in route:: {key}")

        # Parse fields.
        try:
            paths = route_block.get("paths", [])
            if isinstance(paths, str):
                raise RouteConfigError(f"{slug}: paths must be a list, not a string")
            if not isinstance(paths, list):
                raise RouteConfigError(f"{slug}: paths must be a list")
            paths_tuple = tuple(str(p) for p in paths)

            strong = route_block.get("strong", [])
            if isinstance(strong, str):
                raise RouteConfigError(f"{slug}: strong must be a list, not a string")
            if not isinstance(strong, list):
                raise RouteConfigError(f"{slug}: strong must be a list")
            strong_tuple = tuple(str(w) for w in strong)

            weak = route_block.get("weak", [])
            if isinstance(weak, str):
                raise RouteConfigError(f"{slug}: weak must be a list, not a string")
            if not isinstance(weak, list):
                raise RouteConfigError(f"{slug}: weak must be a list")
            weak_tuple = tuple(str(w) for w in weak)

            include_at = route_block.get("include_at")
            if include_at is not None:
                if isinstance(include_at, bool) or not isinstance(include_at, int):
                    raise RouteConfigError(f"{slug}: include_at must be an int")

            candidate_at = route_block.get("candidate_at")
            if candidate_at is not None:
                if isinstance(candidate_at, bool) or not isinstance(candidate_at, int):
                    raise RouteConfigError(f"{slug}: candidate_at must be an int")

            if include_at is not None and candidate_at is not None:
                if candidate_at > include_at:
                    raise RouteConfigError(
                        f"{slug}: candidate_at ({candidate_at}) must be <= include_at ({include_at})"
                    )

            shape_dict = route_block.get("shape", {})
            if not isinstance(shape_dict, dict):
                raise RouteConfigError(f"{slug}: shape must be a dict")

            # Validate shape predicate names.
            for pred_name in shape_dict.keys():
                if pred_name not in SHAPE_PREDICATES:
                    raise RouteConfigError(f"{slug}: unknown shape predicate: {pred_name}")

            hard_requires = route_block.get("hard_requires", [])
            if isinstance(hard_requires, str):
                raise RouteConfigError(f"{slug}: hard_requires must be a list, not a string")
            if not isinstance(hard_requires, list):
                raise RouteConfigError(f"{slug}: hard_requires must be a list")

            # Validate hard_requires names.
            for pred_name in hard_requires:
                if pred_name not in SHAPE_PREDICATES:
                    raise RouteConfigError(f"{slug}: unknown hard_requires predicate: {pred_name}")
            hard_requires_tuple = tuple(str(p) for p in hard_requires)

            config = RouteConfig(
                paths=paths_tuple,
                strong=strong_tuple,
                weak=weak_tuple,
                shape=shape_dict,
                include_at=include_at if include_at is not None else 6,
                candidate_at=candidate_at if candidate_at is not None else 3,
                hard_requires=hard_requires_tuple,
                always=False,
            )
            configs[slug] = config

        except (KeyError, TypeError, ValueError) as e:
            if isinstance(e, RouteConfigError):
                raise
            raise RouteConfigError(f"{slug}: {e}")

    return configs


def load_route_configs(path: Path) -> Dict[str, RouteConfig]:
    """Load and parse route configs from a YAML file."""
    try:
        import yaml
    except ImportError as e:
        raise ImportError(f"yaml module not available: {e}")

    with open(path) as f:
        data = yaml.safe_load(f)

    return parse_route_configs(data)


def count_shape(diff_text: str) -> Tuple[int, int]:
    """
    Count file_count and top_dirs per the Sam System gate definition.

    file_count = count of post-image `+++ b/` lines (deletions not counted).
    top_dirs = count of distinct first path segments from those same paths.
    """
    file_count = 0
    top_dirs_set = set()

    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            file_count += 1
            path = line[6:]  # Remove "+++ b/"
            if path and path != "/dev/null":
                first_seg = path.split("/")[0]
                top_dirs_set.add(first_seg)

    return file_count, len(top_dirs_set)


def _extract_definitions(line: str) -> List[str]:
    """
    Extract definition names from a code line (added or removed).

    Matches patterns like:
    - def name, class name, function name
    - fn name, struct|enum|trait|interface|type name
    - const|let name =
    - export ... name
    """
    names = []

    patterns = [
        r"^\s*(?:def|class|function)\s+(\w+)",
        r"^\s*(?:fn|struct|enum|trait|interface|type)\s+(\w+)",
        r"^\s*(?:const|let)\s+(\w+)\s*=",
        r"^\s*export\s+(?:default\s+)?(?:class|function|const|interface|type)\s+(\w+)",
        r"^\s*pub\s+(?:fn|struct|enum|trait)\s+(\w+)",
        r"^\s*__all__\s*=.*",  # __all__ export
        r"^\s*module\.exports\s*=",
    ]

    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            if match.lastindex and match.lastindex >= 1:
                names.append(match.group(1))

    return names


def _word_to_regex(word: str) -> str:
    """Convert word to word-boundary regex (escaped)."""
    return r"\b" + re.escape(word) + r"\b"


def score_diff(
    diff_text: str, configs: Dict[str, RouteConfig], limits: Limits = Limits()
) -> ScoreResult:
    """
    Score a diff against all reviewer configs.

    Deterministic and pure (no I/O).
    """
    reviewers = {}
    degraded = False

    # Parse diff into file records: {path, is_new, is_deleted, added_lines, removed_lines}
    files = []
    current_file = None

    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            # Save previous file.
            if current_file is not None:
                files.append(current_file)

            # Start new file.
            # Extract post-image path from `diff --git a/... b/...` or from `+++ b/...`
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                current_file = {
                    "path": parts[1],
                    "is_new": False,
                    "is_deleted": False,
                    "added_lines": [],
                    "removed_lines": [],
                }
            else:
                current_file = {
                    "path": "",
                    "is_new": False,
                    "is_deleted": False,
                    "added_lines": [],
                    "removed_lines": [],
                }

        elif current_file is not None:
            if line.startswith("new file mode"):
                current_file["is_new"] = True
            elif line.startswith("deleted file mode"):
                current_file["is_deleted"] = True
            elif line.startswith("+++ b/"):
                current_file["path"] = line[6:]
                if current_file["path"] == "/dev/null":
                    current_file["is_deleted"] = True
            elif line.startswith("--- a/"):
                pass  # Ignore --- lines
            elif line.startswith("@@"):
                pass  # Ignore hunk headers
            elif line.startswith("+") and not line.startswith("+++"):
                current_file["added_lines"].append(line[1:])
            elif line.startswith("-") and not line.startswith("---"):
                current_file["removed_lines"].append(line[1:])

    if current_file is not None:
        files.append(current_file)

    # Score each reviewer against all files.
    for slug, config in configs.items():
        reasons = []
        score = 0

        # Handle always: true case.
        if config.always:
            reason = Reason(kind="always", detail="always: true", points=0)
            reasons.append(reason)
            tier = "Always"
            reviewers[slug] = ReviewerScore(slug=slug, score=0, tier=tier, reasons=tuple(reasons))
            continue

        # Collect strong/weak word hits per file (per-file cap).
        strong_hits_per_file = {}
        weak_hits_per_file = {}

        for file_record in files:
            path = file_record["path"]
            if _is_excluded_path(path):
                continue

            strong_words_in_file = set()
            weak_words_in_file = set()

            # Scan added and removed lines.
            for line in file_record["added_lines"] + file_record["removed_lines"]:
                for word in config.strong:
                    word_re = _word_to_regex(word)
                    if re.search(word_re, line, re.IGNORECASE):
                        strong_words_in_file.add(word)

                for word in config.weak:
                    word_re = _word_to_regex(word)
                    if re.search(word_re, line, re.IGNORECASE):
                        weak_words_in_file.add(word)

            strong_hits_per_file[path] = strong_words_in_file
            weak_hits_per_file[path] = weak_words_in_file

        # Add strong word hits (3 points each).
        strong_hits_all = set()
        for words_in_file in strong_hits_per_file.values():
            strong_hits_all.update(words_in_file)

        for word in strong_hits_all:
            reason = Reason(kind="strong", detail=word, points=3)
            reasons.append(reason)
            score += 3

        # Add weak word hits (1 point each, capped at 3 total).
        weak_hits_all = set()
        for words_in_file in weak_hits_per_file.values():
            weak_hits_all.update(words_in_file)

        weak_points = 0
        for word in weak_hits_all:
            if weak_points < 3:
                reason = Reason(kind="weak", detail=word, points=1)
                reasons.append(reason)
                weak_points += 1
                score += 1

        # Path matches (3 points per file).
        for file_record in files:
            path = file_record["path"]
            if _match_glob_patterns(path, config.paths):
                reason = Reason(kind="path", detail=path, points=3)
                reasons.append(reason)
                score += 3

        # Shape predicates.
        file_count, top_dirs = count_shape(diff_text)
        new_file_count = sum(1 for f in files if f["is_new"])

        for pred_name, pred_config in config.shape.items():
            # Extract points and optional n parameter.
            if isinstance(pred_config, dict):
                pred_points = pred_config.get("points", 0)
                pred_n = pred_config.get("n")
            else:
                pred_points = pred_config
                pred_n = None

            pred_true = False

            if pred_name == "new_files":
                pred_true = new_file_count > 0
            elif pred_name == "adr_touched":
                pred_true = any("docs/adr/" in f["path"] for f in files)
            elif pred_name == "dep_manifest":
                dep_files = {
                    "package.json",
                    "Cargo.toml",
                    "pyproject.toml",
                    "go.mod",
                    "Gemfile",
                    "setup.py",
                }
                pred_true = any(
                    Path(f["path"]).name in dep_files or
                    re.match(r"^requirements.*\.txt$", Path(f["path"]).name)
                    for f in files
                )
            elif pred_name == "test_files":
                pred_true = any(
                    "tests/" in f["path"] or
                    re.search(r"(test_.*|.*_test|.*\.test|.*\.spec)\.(py|js|ts|jsx|tsx)$", f["path"])
                    for f in files
                )
            elif pred_name == "exports_changed":
                pred_true = False
                for f in files:
                    for line in f["added_lines"] + f["removed_lines"]:
                        if re.search(r"^[+-]\s*export\b", line) or \
                           re.search(r"\bpub (fn|struct|enum|trait)\b", line) or \
                           re.search(r"__all__", line) or \
                           re.search(r"module\.exports", line):
                            pred_true = True
                            break
                    if pred_true:
                        break
            elif pred_name == "ui_paths":
                ui_exts = {".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss"}
                pred_true = any(
                    Path(f["path"]).suffix in ui_exts or "components/" in f["path"]
                    for f in files
                )
            elif pred_name == "file_count_ge":
                n = pred_n if pred_n is not None else 3
                pred_true = file_count >= n
            elif pred_name == "top_dirs_ge":
                n = pred_n if pred_n is not None else 2
                pred_true = top_dirs >= n
            elif pred_name == "cross_file_symbol":
                # Collect definitions from all files (within limit).
                definitions_per_file = {}
                all_definitions = set()
                scan_line_count = 0

                for f in files:
                    if scan_line_count > limits.max_scan_lines:
                        degraded = True
                        break

                    defs_in_file = []
                    for line in f["added_lines"] + f["removed_lines"]:
                        scan_line_count += 1
                        defs_in_file.extend(_extract_definitions(line))
                        if scan_line_count > limits.max_scan_lines:
                            degraded = True
                            break

                    if len(all_definitions) + len(defs_in_file) > limits.max_symbols:
                        degraded = True
                        break

                    definitions_per_file[f["path"]] = defs_in_file
                    all_definitions.update(defs_in_file)

                # Check if any definition appears in another file.
                pred_true = False
                for path, defs in definitions_per_file.items():
                    for definition in defs:
                        word_re = _word_to_regex(definition)
                        for other_path, other_file in zip(
                            [f["path"] for f in files],
                            [f for f in files],
                        ):
                            if other_path != path:
                                for line in other_file["added_lines"] + other_file["removed_lines"]:
                                    if re.search(word_re, line):
                                        pred_true = True
                                        break
                            if pred_true:
                                break
                        if pred_true:
                            break
                    if pred_true:
                        break

            if pred_true:
                reason = Reason(kind="shape", detail=pred_name, points=pred_points)
                reasons.append(reason)
                score += pred_points

        # Check hard_requires.
        hard_requires_failed = None
        for pred_name in config.hard_requires:
            # Re-evaluate the predicate (duplicate logic above; could refactor).
            pred_true = False

            if pred_name == "new_files":
                pred_true = new_file_count > 0
            elif pred_name == "adr_touched":
                pred_true = any("docs/adr/" in f["path"] for f in files)
            elif pred_name == "dep_manifest":
                dep_files = {
                    "package.json",
                    "Cargo.toml",
                    "pyproject.toml",
                    "go.mod",
                    "Gemfile",
                    "setup.py",
                }
                pred_true = any(
                    Path(f["path"]).name in dep_files or
                    re.match(r"^requirements.*\.txt$", Path(f["path"]).name)
                    for f in files
                )
            elif pred_name == "test_files":
                pred_true = any(
                    "tests/" in f["path"] or
                    re.search(r"(test_.*|.*_test|.*\.test|.*\.spec)\.(py|js|ts|jsx|tsx)$", f["path"])
                    for f in files
                )
            elif pred_name == "exports_changed":
                pred_true = False
                for f in files:
                    for line in f["added_lines"] + f["removed_lines"]:
                        if re.search(r"^[+-]\s*export\b", line) or \
                           re.search(r"\bpub (fn|struct|enum|trait)\b", line) or \
                           re.search(r"__all__", line) or \
                           re.search(r"module\.exports", line):
                            pred_true = True
                            break
                    if pred_true:
                        break
            elif pred_name == "ui_paths":
                ui_exts = {".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss"}
                pred_true = any(
                    Path(f["path"]).suffix in ui_exts or "components/" in f["path"]
                    for f in files
                )
            elif pred_name == "file_count_ge":
                # Re-get the n value from shape config if present.
                shape_config = config.shape.get(pred_name, {})
                if isinstance(shape_config, dict):
                    n = shape_config.get("n", 3)
                else:
                    n = 3
                pred_true = file_count >= n
            elif pred_name == "top_dirs_ge":
                shape_config = config.shape.get(pred_name, {})
                if isinstance(shape_config, dict):
                    n = shape_config.get("n", 2)
                else:
                    n = 2
                pred_true = top_dirs >= n
            elif pred_name == "cross_file_symbol":
                # Re-evaluate cross_file_symbol.
                definitions_per_file = {}
                all_definitions = set()
                scan_line_count = 0

                for f in files:
                    if scan_line_count > limits.max_scan_lines:
                        break

                    defs_in_file = []
                    for line in f["added_lines"] + f["removed_lines"]:
                        scan_line_count += 1
                        defs_in_file.extend(_extract_definitions(line))
                        if scan_line_count > limits.max_scan_lines:
                            break

                    if len(all_definitions) + len(defs_in_file) > limits.max_symbols:
                        break

                    definitions_per_file[f["path"]] = defs_in_file
                    all_definitions.update(defs_in_file)

                pred_true = False
                for path, defs in definitions_per_file.items():
                    for definition in defs:
                        word_re = _word_to_regex(definition)
                        for other_path, other_file in zip(
                            [f["path"] for f in files],
                            [f for f in files],
                        ):
                            if other_path != path:
                                for line in other_file["added_lines"] + other_file["removed_lines"]:
                                    if re.search(word_re, line):
                                        pred_true = True
                                        break
                            if pred_true:
                                break
                        if pred_true:
                            break
                    if pred_true:
                        break

            if not pred_true:
                hard_requires_failed = pred_name
                break

        # Determine tier.
        if hard_requires_failed is not None:
            tier = "Exclude"
            reason = Reason(kind="hard_requires", detail=hard_requires_failed, points=0)
            reasons.append(reason)
        elif score >= config.include_at:
            tier = "Must"
        elif score >= config.candidate_at:
            tier = "Candidate"
        else:
            tier = "Exclude"

        reviewers[slug] = ReviewerScore(
            slug=slug, score=score, tier=tier, reasons=tuple(reasons)
        )

    return ScoreResult(
        scorer_version=SCORER_VERSION,
        degraded=degraded,
        thresholds_provisional=True,
        reviewers=reviewers,
    )


def result_to_dict(result: ScoreResult) -> dict:
    """Convert ScoreResult to JSON-serializable dict."""
    return {
        "scorer_version": result.scorer_version,
        "degraded": result.degraded,
        "thresholds_provisional": result.thresholds_provisional,
        "reviewers": {
            slug: {
                "score": rev_score.score,
                "tier": rev_score.tier,
                "reasons": [
                    {
                        "kind": r.kind,
                        "detail": r.detail,
                        "points": r.points,
                    }
                    for r in rev_score.reasons
                ],
            }
            for slug, rev_score in sorted(result.reviewers.items())
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Always returns 0."""
    try:
        parser = argparse.ArgumentParser(description="Route diff scorer")
        parser.add_argument("--diff", required=True, help="Path to diff file")
        parser.add_argument("--index", help="Path to reviewers/index.yaml")
        parser.add_argument("--mode", choices=["routed", "named"], default="routed", help="Seating source")
        parser.add_argument("--pr", action="store_true", help="PR mode flag")
        parser.add_argument("--effort", type=int, help="Effort level")
        parser.add_argument("--out", required=True, help="Output JSON path")

        args = parser.parse_args(argv)

        # Determine index path.
        if args.index:
            index_path = Path(args.index)
        else:
            # Try repo path first, then fallback to ~/.claude/
            script_dir = Path(__file__).resolve().parent
            repo_index = script_dir.parent / "reviewers" / "index.yaml"
            if repo_index.exists():
                index_path = repo_index
            else:
                index_path = Path.home() / ".claude" / "reviewers" / "index.yaml"

        # Load configs.
        configs = load_route_configs(index_path)

        # Read diff.
        diff_path = Path(args.diff)
        if not diff_path.exists():
            raise FileNotFoundError(f"Diff file not found: {args.diff}")
        diff_text = diff_path.read_text()

        # Score.
        result = score_diff(diff_text, configs)

        # Build output JSON.
        output = result_to_dict(result)
        output["status"] = "ok"
        output["mode"] = args.mode
        output["pr"] = args.pr
        output["effort"] = args.effort

        # Write output.
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, sort_keys=True, indent=2))

        return 0

    except Exception as e:
        # Fail open: catch all exceptions and write error JSON.
        exc_type = type(e).__name__
        exc_msg = str(e)
        error_output = {
            "scorer_version": SCORER_VERSION,
            "status": "error",
            "error": f"{exc_type}: {exc_msg}",
            "mode": args.mode if "args" in locals() else None,
            "pr": args.pr if "args" in locals() else False,
            "effort": args.effort if "args" in locals() else None,
        }

        # Try to write error JSON.
        if "args" in locals() and args.out:
            try:
                out_path = Path(args.out)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(error_output, sort_keys=True, indent=2))
            except Exception:
                pass  # If write fails, just continue silently.

        # Log to stderr.
        print(f"route-score.py: {exc_type}: {exc_msg}", file=sys.stderr)

        return 0


if __name__ == "__main__":
    sys.exit(main())
