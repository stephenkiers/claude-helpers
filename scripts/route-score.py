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
from typing import Any, Dict, List, Literal, Optional, Set, Tuple, get_args

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
    kind: ReasonKind
    detail: str
    points: int


@dataclass(frozen=True)
class ReviewerScore:
    """Score and tier for one reviewer."""
    slug: str
    score: int
    tier: Tier
    reasons: Tuple[Reason, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Enforce invariants."""
        computed_score = sum(r.points for r in self.reasons)
        if self.score != computed_score:
            raise ValueError(
                f"{self.slug}: score {self.score} != sum(points) {computed_score}"
            )
        valid_tiers = get_args(Tier)
        if self.tier not in valid_tiers:
            raise ValueError(f"{self.slug}: tier {self.tier} not in Tier literal")


@dataclass(frozen=True)
class ShapeRule:
    """One shape predicate with its points and optional n threshold."""
    points: int
    n: Optional[int] = None


@dataclass(frozen=True)
class RouteConfig:
    """Parsed route: block for one reviewer."""
    paths: Tuple[str, ...] = field(default_factory=tuple)
    strong: Tuple[str, ...] = field(default_factory=tuple)
    weak: Tuple[str, ...] = field(default_factory=tuple)
    shape: Tuple[Tuple[str, ShapeRule], ...] = field(default_factory=tuple)
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
        if pattern.endswith("/"):
            # Directory pattern: exclude everything beneath it, at any depth.
            if path.startswith(pattern) or f"/{pattern}" in path:
                return True
        elif fnmatch.fnmatch(Path(path).name, pattern):
            return True
    return False


def _match_glob_patterns(path: str, patterns: Tuple[str, ...]) -> bool:
    """Check if path matches any glob pattern (checks both full path and basename)."""
    import fnmatch

    basename = Path(path).name
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern):
            return True
    return False


def parse_route_configs(index_data: Any) -> Dict[str, RouteConfig]:
    """
    Parse and normalize route: blocks from index.yaml data structure.

    Validates and normalizes:
    - Index is a dict, reviewers is a list.
    - Every `review`-context reviewer has a `route:` block.
    - `route:` has only allowed keys.
    - No string where list is expected; list items are non-empty strings.
    - Shape predicates: int or dict form with required `points` and optional `n`.
    - `file_count_ge` and `top_dirs_ge` require dict form with explicit `n` (scalar raises).
    - All thresholds are non-bool non-negative ints; 0 <= candidate_at <= include_at.
    - Paths do not end in `/` (must be `dir/**` not `dir/`).
    - Strong/weak words are `\\w`-bounded at both ends.
    - No duplicate slugs.
    - All shape/hard_requires names are in the registry.
    - `always: true` is the only key if present; must be exact bool True, not truthy.
    - After parsing, the set of `always: true` slugs matches ALWAYS_RUN_SLUGS exactly.

    Returns {slug: RouteConfig}, slug = file: stem (e.g. "north-star-nick").
    Raises RouteConfigError with slug and field context.
    """
    # Type-check top-level structure first.
    if not isinstance(index_data, dict):
        raise RouteConfigError("index data must be a dict")

    reviewers_raw = index_data.get("reviewers", [])
    if not isinstance(reviewers_raw, list):
        raise RouteConfigError("reviewers must be a list")

    configs = {}
    always_run_observed = set()

    for entry in reviewers_raw:
        if not isinstance(entry, dict):
            raise RouteConfigError("each reviewer entry must be a dict")

        file_path = entry.get("file", "")
        if not file_path:
            continue

        slug = Path(file_path).stem
        if slug in configs:
            raise RouteConfigError(f"{slug}: duplicate slug in reviewers")

        contexts = entry.get("contexts", {})
        if not isinstance(contexts, (dict, list)):
            raise RouteConfigError(f"{slug}: contexts must be a dict or list")

        # Check if this reviewer has a review context.
        has_review_context = "review" in contexts if isinstance(contexts, dict) else "review" in contexts

        route_block = entry.get("route")

        if has_review_context and route_block is None:
            raise RouteConfigError(f"{slug}: review-context reviewer missing route: block")

        if route_block is None:
            continue

        # Parse the route block.
        if not isinstance(route_block, dict):
            raise RouteConfigError(f"{slug}: route: must be a dict")

        # If always is present, it must be exactly True and the only key.
        always = route_block.get("always")
        if always is not None:
            if always is not True:  # Strict bool check, reject 1, "yes", truthy non-bool
                raise RouteConfigError(f"{slug}: always must be exactly True (bool), not {type(always).__name__} {always!r}")
            if len(route_block) > 1:
                raise RouteConfigError(f"{slug}: always: true must be the only key in route:")
            configs[slug] = RouteConfig(always=True)
            always_run_observed.add(slug)
            continue

        # Validate allowed keys.
        allowed_keys = {"paths", "strong", "weak", "shape", "include_at", "candidate_at", "hard_requires", "always"}
        for key in route_block.keys():
            if key not in allowed_keys:
                raise RouteConfigError(f"{slug}: unknown key in route:: {key}")

        # Parse and normalize fields.
        try:
            # Paths: list of non-empty strings, no trailing /
            paths = route_block.get("paths", [])
            if isinstance(paths, str):
                raise RouteConfigError(f"{slug}.paths: must be a list, not a string")
            if not isinstance(paths, list):
                raise RouteConfigError(f"{slug}.paths: must be a list")
            paths_list = []
            for p in paths:
                if not isinstance(p, str) or not p:
                    raise RouteConfigError(f"{slug}.paths: entries must be non-empty strings, got {p!r}")
                if p.endswith("/"):
                    raise RouteConfigError(f"{slug}.paths: entry {p!r} ends with `/`; use `dir/**` instead")
                paths_list.append(p)
            paths_tuple = tuple(paths_list)

            # Strong words: list of non-empty strings, \w-bounded at both ends
            strong = route_block.get("strong", [])
            if isinstance(strong, str):
                raise RouteConfigError(f"{slug}.strong: must be a list, not a string")
            if not isinstance(strong, list):
                raise RouteConfigError(f"{slug}.strong: must be a list")
            strong_list = []
            for w in strong:
                if not isinstance(w, str) or not w:
                    raise RouteConfigError(f"{slug}.strong: entries must be non-empty strings, got {w!r}")
                if not (w[0].isalnum() or w[0] == "_") or not (w[-1].isalnum() or w[-1] == "_"):
                    raise RouteConfigError(f"{slug}.strong: word {w!r} must start and end with \\w (alphanumeric or underscore)")
                strong_list.append(w)
            strong_tuple = tuple(strong_list)

            # Weak words: list of non-empty strings, \w-bounded at both ends
            weak = route_block.get("weak", [])
            if isinstance(weak, str):
                raise RouteConfigError(f"{slug}.weak: must be a list, not a string")
            if not isinstance(weak, list):
                raise RouteConfigError(f"{slug}.weak: must be a list")
            weak_list = []
            for w in weak:
                if not isinstance(w, str) or not w:
                    raise RouteConfigError(f"{slug}.weak: entries must be non-empty strings, got {w!r}")
                if not (w[0].isalnum() or w[0] == "_") or not (w[-1].isalnum() or w[-1] == "_"):
                    raise RouteConfigError(f"{slug}.weak: word {w!r} must start and end with \\w (alphanumeric or underscore)")
                weak_list.append(w)
            weak_tuple = tuple(weak_list)

            # Resolve defaults first, then validate thresholds.
            include_at = route_block.get("include_at")
            if include_at is None:
                include_at = 6
            else:
                if isinstance(include_at, bool) or not isinstance(include_at, int):
                    raise RouteConfigError(f"{slug}.include_at: must be an int, not {type(include_at).__name__}")
                if include_at < 0:
                    raise RouteConfigError(f"{slug}.include_at: must be non-negative, got {include_at}")

            candidate_at = route_block.get("candidate_at")
            if candidate_at is None:
                candidate_at = 3
            else:
                if isinstance(candidate_at, bool) or not isinstance(candidate_at, int):
                    raise RouteConfigError(f"{slug}.candidate_at: must be an int, not {type(candidate_at).__name__}")
                if candidate_at < 0:
                    raise RouteConfigError(f"{slug}.candidate_at: must be non-negative, got {candidate_at}")

            # Validate threshold invariant.
            if candidate_at > include_at:
                raise RouteConfigError(
                    f"{slug}: candidate_at ({candidate_at}) must be <= include_at ({include_at})"
                )

            # Shape predicates: normalize to ShapeRule.
            shape_raw = route_block.get("shape", {})
            if not isinstance(shape_raw, dict):
                raise RouteConfigError(f"{slug}.shape: must be a dict")

            shape_list: List[Tuple[str, ShapeRule]] = []
            for pred_name, pred_config in shape_raw.items():
                if pred_name not in SHAPE_PREDICATES:
                    raise RouteConfigError(f"{slug}.shape: unknown predicate: {pred_name}")

                # Scalar form: int points, or dict form: {points, n?}
                pred_points: int
                pred_n: Optional[int] = None

                if isinstance(pred_config, dict):
                    # Dict form
                    if not all(k in {"points", "n"} for k in pred_config.keys()):
                        invalid_keys = set(pred_config.keys()) - {"points", "n"}
                        raise RouteConfigError(f"{slug}.shape.{pred_name}: unknown keys {invalid_keys}")
                    if "points" not in pred_config:
                        raise RouteConfigError(f"{slug}.shape.{pred_name}: required key `points` missing")

                    pred_points = pred_config["points"]
                    if isinstance(pred_points, bool) or not isinstance(pred_points, int):
                        raise RouteConfigError(f"{slug}.shape.{pred_name}.points: must be an int, not {type(pred_points).__name__}")
                    if pred_points < 0:
                        raise RouteConfigError(f"{slug}.shape.{pred_name}.points: must be non-negative, got {pred_points}")

                    if "n" in pred_config:
                        pred_n = pred_config["n"]
                        if isinstance(pred_n, bool) or not isinstance(pred_n, int):
                            raise RouteConfigError(f"{slug}.shape.{pred_name}.n: must be an int, not {type(pred_n).__name__}")
                        if pred_n <= 0:
                            raise RouteConfigError(f"{slug}.shape.{pred_name}.n: must be positive, got {pred_n}")

                elif isinstance(pred_config, int) and not isinstance(pred_config, bool):
                    # Scalar form (int points)
                    pred_points = pred_config
                    if pred_points < 0:
                        raise RouteConfigError(f"{slug}.shape.{pred_name}: must be non-negative, got {pred_points}")
                    # Threshold predicates cannot use scalar form.
                    if pred_name in {"file_count_ge", "top_dirs_ge"}:
                        raise RouteConfigError(f"{slug}.shape.{pred_name}: scalar form not allowed; use dict form with explicit `n`")
                else:
                    raise RouteConfigError(f"{slug}.shape.{pred_name}: must be int or dict, got {type(pred_config).__name__}")

                shape_list.append((pred_name, ShapeRule(points=pred_points, n=pred_n)))

            shape_tuple = tuple(shape_list)

            # Hard requires: list of predicate names.
            hard_requires = route_block.get("hard_requires", [])
            if isinstance(hard_requires, str):
                raise RouteConfigError(f"{slug}.hard_requires: must be a list, not a string")
            if not isinstance(hard_requires, list):
                raise RouteConfigError(f"{slug}.hard_requires: must be a list")

            hard_requires_list = []
            for pred_name in hard_requires:
                if not isinstance(pred_name, str) or not pred_name:
                    raise RouteConfigError(f"{slug}.hard_requires: entries must be non-empty strings, got {pred_name!r}")
                if pred_name not in SHAPE_PREDICATES:
                    raise RouteConfigError(f"{slug}.hard_requires: unknown predicate: {pred_name}")
                hard_requires_list.append(pred_name)
            hard_requires_tuple = tuple(hard_requires_list)

            config = RouteConfig(
                paths=paths_tuple,
                strong=strong_tuple,
                weak=weak_tuple,
                shape=shape_tuple,
                include_at=include_at,
                candidate_at=candidate_at,
                hard_requires=hard_requires_tuple,
                always=False,
            )
            configs[slug] = config

        except (KeyError, TypeError) as e:
            if isinstance(e, RouteConfigError):
                raise
            raise RouteConfigError(f"{slug}: {e}")

    # Validate that the set of always: true slugs matches ALWAYS_RUN_SLUGS.
    if always_run_observed != ALWAYS_RUN_SLUGS:
        missing = ALWAYS_RUN_SLUGS - always_run_observed
        extra = always_run_observed - ALWAYS_RUN_SLUGS
        msg = f"always: true slugs mismatch: "
        if missing:
            msg += f"missing {missing} "
        if extra:
            msg += f"extra {extra}"
        raise RouteConfigError(msg.strip())

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


_DEP_MANIFESTS = {"package.json", "Cargo.toml", "pyproject.toml", "go.mod", "Gemfile", "setup.py"}
_UI_EXTS = {".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss"}
_PREDICATE_DEFAULT_N = {"file_count_ge": 3, "top_dirs_ge": 2}


def _is_dep_manifest(name: str) -> bool:
    """Check if a file name is a dependency manifest."""
    return name in _DEP_MANIFESTS or re.match(r"^requirements.*\.txt$", name) is not None


@dataclass(frozen=True)
class _DiffFacts:
    """Per-diff inputs to shape predicates, computed once and shared across reviewers."""
    files: List[Dict[str, Any]]
    file_count: int
    top_dirs: int
    cross_file_symbol: bool


def _cross_file_symbol(files: List[Dict[str, Any]], limits: Limits) -> Tuple[bool, bool]:
    """
    True if a definition-shaped identifier from one file's hunk lines appears in another file's.
    Bounded by limits; returns (result, degraded) where degraded means scanning stopped early.
    Skips excluded paths in both extraction and search phases.
    """
    degraded = False
    definitions_per_file: Dict[str, List[str]] = {}
    all_definitions: Set[str] = set()
    scan_line_count = 0

    # First phase: extract definitions from each file (skip excluded paths).
    for f in files:
        if _is_excluded_path(f["path"]):
            continue
        if scan_line_count > limits.max_scan_lines:
            degraded = True
            break
        defs_in_file: List[str] = []
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

    # Second phase: search for definitions in other files (skip excluded paths, apply line budget).
    search_scan_line_count = 0
    for path, defs in definitions_per_file.items():
        for definition in defs:
            word_re = _word_to_regex(definition)
            for other in files:
                if other["path"] == path or _is_excluded_path(other["path"]):
                    continue
                for line in other["added_lines"] + other["removed_lines"]:
                    search_scan_line_count += 1
                    if search_scan_line_count > limits.max_scan_lines:
                        degraded = True
                        break
                    if re.search(word_re, line):
                        return True, degraded
                if search_scan_line_count > limits.max_scan_lines:
                    degraded = True
                    break
            if search_scan_line_count > limits.max_scan_lines:
                degraded = True
                break
        if search_scan_line_count > limits.max_scan_lines:
            degraded = True
            break

    return False, degraded


def _predicate_true(pred_name: str, pred_n: Optional[int], facts: _DiffFacts) -> bool:
    """Evaluate one SHAPE_PREDICATES entry against the diff; used for both shape points and hard_requires."""
    files = facts.files
    if pred_name == "new_files":
        return any(f["is_new"] for f in files)
    if pred_name == "adr_touched":
        return any("docs/adr/" in f["path"] for f in files)
    if pred_name == "dep_manifest":
        return any(
            _is_dep_manifest(Path(f["path"]).name)
            for f in files
        )
    if pred_name == "test_files":
        return any(
            "tests/" in f["path"]
            or re.search(r"(test_.*|.*_test|.*\.test|.*\.spec)\.(py|js|ts|jsx|tsx)$", f["path"])
            for f in files
        )
    if pred_name == "exports_changed":
        return any(
            re.search(r"^\s*export\b", line)
            or re.search(r"\bpub (fn|struct|enum|trait)\b", line)
            or "__all__" in line
            or "module.exports" in line
            for f in files
            for line in f["added_lines"] + f["removed_lines"]
        )
    if pred_name == "ui_paths":
        return any(Path(f["path"]).suffix in _UI_EXTS or "components/" in f["path"] for f in files)
    if pred_name in _PREDICATE_DEFAULT_N:
        n = pred_n if pred_n is not None else _PREDICATE_DEFAULT_N[pred_name]
        value = facts.file_count if pred_name == "file_count_ge" else facts.top_dirs
        return value >= n
    if pred_name == "cross_file_symbol":
        return facts.cross_file_symbol
    return False


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

    # Diff-level facts shared by every reviewer's predicates.
    file_count, top_dirs = count_shape(diff_text)
    cross_file, symbols_degraded = _cross_file_symbol(files, limits)
    degraded = degraded or symbols_degraded
    facts = _DiffFacts(files=files, file_count=file_count, top_dirs=top_dirs, cross_file_symbol=cross_file)

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

        # Compile word regexes once per reviewer (outside the per-file loops).
        strong_regexes = [(_word_to_regex(w), w) for w in config.strong]
        weak_regexes = [(_word_to_regex(w), w) for w in config.weak]

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
                for word_re, word in strong_regexes:
                    if re.search(word_re, line, re.IGNORECASE):
                        strong_words_in_file.add(word)

                for word_re, word in weak_regexes:
                    if re.search(word_re, line, re.IGNORECASE):
                        weak_words_in_file.add(word)

            strong_hits_per_file[path] = strong_words_in_file
            weak_hits_per_file[path] = weak_words_in_file

        # Add strong word hits (3 points each).
        strong_hits_all = set()
        for words_in_file in strong_hits_per_file.values():
            strong_hits_all.update(words_in_file)

        for word in sorted(strong_hits_all):
            reason = Reason(kind="strong", detail=word, points=3)
            reasons.append(reason)
            score += 3

        # Add weak word hits (1 point each, capped at 3 total).
        weak_hits_all = set()
        for words_in_file in weak_hits_per_file.values():
            weak_hits_all.update(words_in_file)

        weak_points = 0
        for word in sorted(weak_hits_all):
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

        # Shape predicates (now stored as tuples of (name, ShapeRule)).
        for pred_name, rule in config.shape:
            if _predicate_true(pred_name, rule.n, facts):
                reasons.append(Reason(kind="shape", detail=pred_name, points=rule.points))
                score += rule.points

        # Check hard_requires; a threshold predicate uses the same n as its shape entry.
        hard_requires_failed = None
        for pred_name in config.hard_requires:
            # Find the shape rule for this predicate.
            pred_n = None
            for shape_pred_name, rule in config.shape:
                if shape_pred_name == pred_name:
                    pred_n = rule.n
                    break
            if not _predicate_true(pred_name, pred_n, facts):
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
    # Resolve --out before full parsing so that argparse errors can still write the artifact.
    out_path = None
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)  # Make a copy to avoid modifying the caller's list

    # First pass: find --out value without full parsing.
    i = 0
    while i < len(argv):
        if argv[i] == "--out" and i + 1 < len(argv):
            out_path = argv[i + 1]
            break
        i += 1

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

    except (Exception, SystemExit) as e:
        # Fail open: catch all exceptions (including argparse's SystemExit) and write error JSON.
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

        # Try to write error JSON using args.out if available, otherwise use out_path from first pass.
        output_file = None
        if "args" in locals() and args.out:
            output_file = args.out
        elif out_path:
            output_file = out_path

        if output_file:
            try:
                output_path = Path(output_file)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(error_output, sort_keys=True, indent=2))
            except Exception:
                pass  # If write fails, just continue silently.

        # Log to stderr.
        print(f"route-score.py: {exc_type}: {exc_msg}", file=sys.stderr)

        return 0


if __name__ == "__main__":
    sys.exit(main())
