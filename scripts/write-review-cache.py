#!/usr/bin/env python3
"""
Write the `review` section of `.claude/github-cache.json` for /expert-review's Step 13.

Exists because that write used to be assembled ad hoc: the command doc listed the required
fields in prose, and the LLM executing the command built the `jq --argjson` object by hand a
hundred-odd lines later. That drifted in practice — one real run wrote a `review` object with
no `branch` key at all (silently making expert-review-status.py's `reviewed` check permanently
false for that entry) while inventing an undocumented `decisions` key nothing reads. A fixed
CLI schema removes the LLM's opportunity to drop or invent a field: required arguments are
enforced by argparse, and there is exactly one place the object shape is defined.

Fails loudly (non-zero exit, message to stderr) on any required field missing or empty, and
on an unwritable cache path. Never silently drops a field or writes a partial object.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict


def build_review_object(args: argparse.Namespace) -> Dict[str, Any]:
    reviewers = [r.strip() for r in args.reviewers.split(",") if r.strip()]

    review: Dict[str, Any] = {
        "lastRun": args.last_run,
        "commit": args.commit,
        "branch": args.branch,
        "reviewDir": args.review_dir,
        "reviewers": reviewers,
        "findings": {
            "critical": args.findings_critical,
            "high": args.findings_high,
            "medium": args.findings_medium,
            "low": args.findings_low,
        },
    }

    if args.panel_model:
        review["panelModel"] = args.panel_model
    if args.metrics_path:
        review["metricsPath"] = args.metrics_path

    return review


def write_cache(cache_path: Path, review: Dict[str, Any]) -> None:
    """
    Merge `review` into the cache file, preserving every other top-level section.

    Writes to a sibling temp file and renames over the target only on success, so a
    mid-write failure (disk full, permission error) never truncates an existing cache —
    the same atomicity Step 13 already required of the inline jq version.
    """
    existing: Dict[str, Any] = {}
    if cache_path.exists():
        try:
            existing = json.loads(cache_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"ERROR: failed to read existing cache {cache_path}: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(existing, dict):
            print(f"ERROR: existing cache {cache_path} is not a JSON object", file=sys.stderr)
            sys.exit(1)

    existing["review"] = review

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=cache_path.name + ".", dir=str(cache_path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, cache_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write the review section of .claude/github-cache.json (fixed schema)."
    )
    parser.add_argument("--cache-path", required=True, help="Path to .claude/github-cache.json")
    parser.add_argument("--last-run", required=True, help="ISO 8601 timestamp")
    parser.add_argument("--commit", required=True, help="Short git commit hash")
    parser.add_argument("--branch", required=True, help="Branch name (slashes as written)")
    parser.add_argument("--review-dir", required=True, help="Absolute path to the review checkpoint dir")
    parser.add_argument("--reviewers", required=True, help="Comma-separated reviewer names that ran")
    parser.add_argument("--findings-critical", required=True, type=int)
    parser.add_argument("--findings-high", required=True, type=int)
    parser.add_argument("--findings-medium", required=True, type=int)
    parser.add_argument("--findings-low", required=True, type=int)
    parser.add_argument("--panel-model", default=None, help="Optional: PANEL_MODEL used")
    parser.add_argument("--metrics-path", default=None, help="Optional: effort-2 review-metrics.json path")

    args = parser.parse_args()

    for name, value in (("--cache-path", args.cache_path), ("--last-run", args.last_run),
                         ("--commit", args.commit), ("--branch", args.branch),
                         ("--review-dir", args.review_dir)):
        if not value.strip():
            print(f"ERROR: {name} must not be empty", file=sys.stderr)
            sys.exit(1)

    review = build_review_object(args)
    write_cache(Path(args.cache_path), review)
    print(f"Wrote review section to {args.cache_path}")


if __name__ == "__main__":
    main()
