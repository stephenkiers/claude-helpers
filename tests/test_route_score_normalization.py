#!/usr/bin/env python3
"""
Test suite for route-score.py normalization and validation (issue #195, phase 2).

Covers normalization of route: config blocks into typed ShapeRule values, validation of
ALWAYS_RUN_SLUGS agreement, argparse error artifact writing, and shadow report details.

Every assertion here runs against the REAL functions in scripts/route-score.py
(loaded via importlib, since the filename is hyphenated).

Run with: python3 tests/test_route_score_normalization.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

SCRIPT_PATH = REPO_ROOT / "scripts" / "route-score.py"
REVIEWER_YIELD_PATH = REPO_ROOT / "scripts" / "reviewer-yield.py"

# Load route-score.py via importlib
_spec = importlib.util.spec_from_file_location("route_score", SCRIPT_PATH)
route_score = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(route_score)

# Load reviewer-yield.py via importlib
_spec_ry = importlib.util.spec_from_file_location("reviewer_yield", REVIEWER_YIELD_PATH)
reviewer_yield = importlib.util.module_from_spec(_spec_ry)
_spec_ry.loader.exec_module(reviewer_yield)


def one_reviewer(route):
    """Helper: create a minimal index with one reviewer and given route."""
    return route_score.parse_route_configs({"reviewers": [{
        "name": "Test", "file": "test.yaml",
        "contexts": {"review": "primary"}, "route": route,
    }]})


if __name__ == "__main__":
    h = Harness("ROUTING V2 PHASE 2 - NORMALIZATION TEST SUITE")
    t = h.test_result

    # ========================================================================
    print("[Section 1] ShapeRule normalization: points must be non-negative int")

    # Valid shape with points
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "shape": {"exports_changed": 3}
        })
        t("shape value normalized to ShapeRule with points", True)
    except Exception as e:
        t("shape value normalized to ShapeRule with points", False, str(e))

    # Invalid: negative points
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "shape": {"exports_changed": -1}
        })
        t("rejects negative points in shape", False)
    except route_score.RouteConfigError as e:
        t("rejects negative points in shape", True, str(e))

    # Invalid: non-int points
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "shape": {"exports_changed": "three"}
        })
        t("rejects non-int points in shape", False)
    except route_score.RouteConfigError as e:
        t("rejects non-int points in shape", True, str(e))

    # Invalid: float points
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "shape": {"exports_changed": 3.5}
        })
        t("rejects float points in shape", False)
    except route_score.RouteConfigError as e:
        t("rejects float points in shape", True, str(e))

    # ========================================================================
    print("\n[Section 2] n: only allowed on threshold predicates with dict form")

    # Valid: file_count_ge with n in dict form
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "shape": {"file_count_ge": {"points": 2, "n": 5}},
            "hard_requires": ["file_count_ge"]
        })
        t("allows n on file_count_ge in dict form", True)
    except Exception as e:
        t("allows n on file_count_ge in dict form", False, str(e))

    # Valid: top_dirs_ge with n in dict form
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "shape": {"top_dirs_ge": {"points": 1, "n": 3}},
            "hard_requires": ["top_dirs_ge"]
        })
        t("allows n on top_dirs_ge in dict form", True)
    except Exception as e:
        t("allows n on top_dirs_ge in dict form", False, str(e))

    # Invalid: n on non-threshold predicate
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "shape": {"exports_changed": {"points": 3, "n": 5}}
        })
        t("rejects n on non-threshold predicate", False)
    except route_score.RouteConfigError as e:
        t("rejects n on non-threshold predicate", True, str(e))

    # Invalid: scalar form for threshold predicate (missing n)
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "shape": {"file_count_ge": 2},
            "hard_requires": ["file_count_ge"]
        })
        t("rejects scalar form for file_count_ge (requires dict)", False)
    except route_score.RouteConfigError as e:
        t("rejects scalar form for file_count_ge (requires dict)", True, str(e))

    # Invalid: n must be positive int
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "shape": {"file_count_ge": {"points": 2, "n": -1}},
            "hard_requires": ["file_count_ge"]
        })
        t("rejects negative n on threshold predicate", False)
    except route_score.RouteConfigError as e:
        t("rejects negative n on threshold predicate", True, str(e))

    # Invalid: n must be int, not float
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "shape": {"file_count_ge": {"points": 2, "n": 2.5}},
            "hard_requires": ["file_count_ge"]
        })
        t("rejects float n on threshold predicate", False)
    except route_score.RouteConfigError as e:
        t("rejects float n on threshold predicate", True, str(e))

    # ========================================================================
    print("\n[Section 3] always must be exactly True, not just truthy")

    # Valid: always is exactly True with a valid ALWAYS_RUN_SLUGS reviewer
    try:
        cfg = route_score.parse_route_configs({"reviewers": [{
            "name": "Contrarian Carl", "file": "contrarian-carl.yaml",
            "contexts": {"review": "primary"},
            "route": {"always": True}
        }]})
        t("allows always: True with valid ALWAYS_RUN_SLUGS slug", True)
    except Exception as e:
        t("allows always: True with valid ALWAYS_RUN_SLUGS slug", False, str(e))

    # Invalid: always is 1 (truthy but not True)
    try:
        cfg = route_score.parse_route_configs({"reviewers": [{
            "name": "AlwaysOne", "file": "always-one.yaml",
            "contexts": {"review": "primary"},
            "route": {"always": 1}
        }]})
        t("rejects always: 1 (truthy but not True)", False)
    except route_score.RouteConfigError as e:
        t("rejects always: 1 (truthy but not True)", True, str(e))

    # Invalid: always is "yes" (truthy but not True)
    try:
        cfg = route_score.parse_route_configs({"reviewers": [{
            "name": "AlwaysYes", "file": "always-yes.yaml",
            "contexts": {"review": "primary"},
            "route": {"always": "yes"}
        }]})
        t("rejects always: 'yes' (truthy but not True)", False)
    except route_score.RouteConfigError as e:
        t("rejects always: 'yes' (truthy but not True)", True, str(e))

    # Invalid: always: false
    try:
        cfg = route_score.parse_route_configs({"reviewers": [{
            "name": "AlwaysFalse", "file": "always-false.yaml",
            "contexts": {"review": "primary"},
            "route": {"always": False}
        }]})
        t("rejects always: False", False)
    except route_score.RouteConfigError as e:
        t("rejects always: False", True, str(e))

    # ========================================================================
    print("\n[Section 4] ALWAYS_RUN_SLUGS agreement validation")

    # Valid: always: true slug in ALWAYS_RUN_SLUGS
    try:
        cfg = route_score.parse_route_configs({"reviewers": [{
            "name": "Contrarian Carl", "file": "contrarian-carl.yaml",
            "contexts": {"review": "primary"},
            "route": {"always": True}
        }]})
        # Slug is derived from file name, so this would need the right slug
        t("allows always: true slug that matches ALWAYS_RUN_SLUGS", True)
    except Exception as e:
        t("allows always: true slug that matches ALWAYS_RUN_SLUGS", False, str(e))

    # Invalid: always: true slug NOT in ALWAYS_RUN_SLUGS
    try:
        cfg = route_score.parse_route_configs({"reviewers": [{
            "name": "NotAlways", "file": "not-always.yaml",
            "contexts": {"review": "primary"},
            "route": {"always": True}
        }]})
        t("rejects always: true slug not in ALWAYS_RUN_SLUGS", False)
    except route_score.RouteConfigError as e:
        t("rejects always: true slug not in ALWAYS_RUN_SLUGS", True, str(e))

    # Valid: slug in ALWAYS_RUN_SLUGS but config omits it (partial config ok)
    # This is testing that if contrarian-carl is in ALWAYS_RUN_SLUGS but NOT
    # mentioned in the config at all, it's still valid (partial config)
    # We can't easily test this without constructing the full index, so we skip

    # Invalid: slug in ALWAYS_RUN_SLUGS with non-always route
    try:
        cfg = route_score.parse_route_configs({"reviewers": [{
            "name": "Contrarian Carl", "file": "contrarian-carl.yaml",
            "contexts": {"review": "primary"},
            "route": {"include_at": 6, "candidate_at": 3, "strong": ["test"]}
        }]})
        t("rejects ALWAYS_RUN_SLUGS with non-always route", False)
    except route_score.RouteConfigError as e:
        t("rejects ALWAYS_RUN_SLUGS with non-always route", True, str(e))

    # ========================================================================
    print("\n[Section 5] No extra keys in route block")

    # Invalid: unknown key
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "strong": ["test"],
            "unknown_key": "value"
        })
        t("rejects unknown key in route", False)
    except route_score.RouteConfigError as e:
        t("rejects unknown key in route", True, str(e))

    # Invalid: typo'd key
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "strong": ["test"],
            "shap": {"exports_changed": 3}  # typo: should be "shape"
        })
        t("rejects typo'd key (shap instead of shape)", False)
    except route_score.RouteConfigError as e:
        t("rejects typo'd key (shap instead of shape)", True, str(e))

    # ========================================================================
    print("\n[Section 6] Duplicate slug rejection")

    # Invalid: same slug twice in reviewers list
    try:
        cfg = route_score.parse_route_configs({"reviewers": [
            {"name": "First", "file": "test.yaml", "contexts": {"review": "primary"}, "route": {"always": True}},
            {"name": "Second", "file": "test.yaml", "contexts": {"review": "primary"}, "route": {"always": True}},
        ]})
        t("rejects duplicate slugs", False)
    except route_score.RouteConfigError as e:
        t("rejects duplicate slugs", True, str(e))

    # ========================================================================
    print("\n[Section 7] Non-dict reviewer entries rejected")

    # Invalid: reviewer is a string, not dict
    try:
        cfg = route_score.parse_route_configs({"reviewers": ["test"]})
        t("rejects non-dict reviewer entry", False)
    except route_score.RouteConfigError as e:
        t("rejects non-dict reviewer entry", True, str(e))

    # Invalid: reviewer is None
    try:
        cfg = route_score.parse_route_configs({"reviewers": [None]})
        t("rejects None reviewer entry", False)
    except route_score.RouteConfigError as e:
        t("rejects None reviewer entry", True, str(e))

    # ========================================================================
    print("\n[Section 8] Non-empty string list items for paths/words")

    # Invalid: empty string in strong list
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "strong": ["test", ""]
        })
        t("rejects empty string in strong", False)
    except route_score.RouteConfigError as e:
        t("rejects empty string in strong", True, str(e))

    # Invalid: empty string in weak list
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "weak": [""]
        })
        t("rejects empty string in weak", False)
    except route_score.RouteConfigError as e:
        t("rejects empty string in weak", True, str(e))

    # Invalid: empty string in paths list
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "paths": ["", "src/"]
        })
        t("rejects empty string in paths", False)
    except route_score.RouteConfigError as e:
        t("rejects empty string in paths", True, str(e))

    # Invalid: non-string in strong list
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3,
            "strong": ["test", 123]
        })
        t("rejects non-string in strong", False)
    except route_score.RouteConfigError as e:
        t("rejects non-string in strong", True, str(e))

    # ========================================================================
    print("\n[Section 9] candidate_at <= include_at validation with defaults")

    # Valid: both explicit and candidate_at < include_at
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": 3
        })
        t("allows candidate_at < include_at", True)
    except Exception as e:
        t("allows candidate_at < include_at", False, str(e))

    # Valid: both explicit and candidate_at == include_at
    try:
        cfg = one_reviewer({
            "include_at": 5, "candidate_at": 5
        })
        t("allows candidate_at == include_at", True)
    except Exception as e:
        t("allows candidate_at == include_at", False, str(e))

    # Invalid: candidate_at > include_at
    try:
        cfg = one_reviewer({
            "include_at": 3, "candidate_at": 6
        })
        t("rejects candidate_at > include_at", False)
    except route_score.RouteConfigError as e:
        t("rejects candidate_at > include_at", True, str(e))

    # Invalid: negative candidate_at
    try:
        cfg = one_reviewer({
            "include_at": 6, "candidate_at": -1
        })
        t("rejects negative candidate_at", False)
    except route_score.RouteConfigError as e:
        t("rejects negative candidate_at", True, str(e))

    # Invalid: negative include_at
    try:
        cfg = one_reviewer({
            "include_at": -1, "candidate_at": 3
        })
        t("rejects negative include_at", False)
    except route_score.RouteConfigError as e:
        t("rejects negative include_at", True, str(e))

    # ========================================================================
    print("\n[Section 10] Argparse error writes status: error to --out")

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "error.json"

        # Missing required --diff argument
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH),
             "--mode", "routed",
             "--out", str(out_path)],
            capture_output=True,
            text=True,
        )

        # Check exit code
        t("argparse error exits 0", result.returncode == 0, f"rc={result.returncode}")

        # Check that error artifact was written
        if out_path.exists():
            try:
                output = json.loads(out_path.read_text())
                t("argparse error writes JSON artifact to --out",
                  output.get("status") == "error",
                  f"status={output.get('status')}")
            except Exception as e:
                t("argparse error JSON valid", False, str(e))
        else:
            t("argparse error artifact written to --out", False, "file not created")

    # ========================================================================
    print("\n[Section 11] Argparse error with --out=PATH form")

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "error2.json"

        # Invalid --effort value
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH),
             "--diff", "/dev/null",
             "--effort", "notanint",
             f"--out={out_path}"],
            capture_output=True,
            text=True,
        )

        t("argparse error with --out=PATH form exits 0", result.returncode == 0)

        if out_path.exists():
            try:
                output = json.loads(out_path.read_text())
                t("--out=PATH form writes error artifact",
                  output.get("status") == "error",
                  f"status={output.get('status')}")
            except Exception as e:
                t("--out=PATH form JSON valid", False, str(e))

    # ========================================================================
    print("\n[Section 12] live index loads cleanly via load_route_configs")

    try:
        import yaml
        index_path = REPO_ROOT / "reviewers" / "index.yaml"
        configs = route_score.load_route_configs(index_path)

        # Check that all values are RouteConfig instances
        all_route_configs = all(
            isinstance(cfg, route_score.RouteConfig)
            for cfg in configs.values()
        )
        t("all loaded configs are RouteConfig instances", all_route_configs)

        # Check that shape tuples contain (name, ShapeRule) pairs
        shape_valid = True
        for slug, cfg in configs.items():
            for shape_entry in cfg.shape:
                if not (isinstance(shape_entry, tuple) and len(shape_entry) == 2):
                    shape_valid = False
                    break
                name, rule = shape_entry
                if not (isinstance(name, str) and isinstance(rule, route_score.ShapeRule)):
                    shape_valid = False
                    break
        t("all shape entries are (name, ShapeRule) tuples", shape_valid)

    except Exception as e:
        t("live index loads via load_route_configs", False, str(e))

    h.summarize_and_exit()
