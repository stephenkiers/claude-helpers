#!/usr/bin/env python3
"""
Write the 'review' section of .claude/github-cache.json after a completed /expert-review run.

Owns the schema so the orchestrator never hand-assembles this JSON from memory. A prior
run wrote a differently-shaped, differently-named key (lastExpertReview / timestamp instead
of review / lastRun) because the command doc only described the shape in prose next to a
freehand jq snippet — nothing enforced it. expert-review-status.py's read_review_cache()
and scripts/workflow/models.py both hard-code the key name 'review' and the field name
'lastRun'; a write under any other shape is invisible to both, silently, forever.

Schema written under the top-level 'review' key (must be kept in sync with both readers
above — see the NOTE in scripts/expert-review-status.py):
    lastRun    - ISO 8601 UTC timestamp, now
    commit     - short git hash
    branch     - branch name; slashes are normalized to dashes here (matches Step 0's form)
    reviewDir  - absolute path to the review checkpoint directory
    reviewers  - list of reviewer names that actually ran
    panelModel - PANEL_MODEL used for judgment roles
    findings   - {critical, high, medium, low} counts
    metricsPath - optional, only when effort 2

Merges into the existing cache, preserving all other top-level keys. Uses mktemp + mv so a
failure never truncates the cache (see commands/expert-review.md's note on why a bare
`> github-cache.json` redirect is unsafe).

Verification: reads back the written cache through the same schema readers (read_review_cache
and compute_status logic) use, ensuring schema drift is caught immediately, not silently weeks
later as a "not reviewed" false negative. If a corrupt cache exists, backs it up to {path}.bak
before overwriting.

Exit codes:
    0 - written and verified readable back via the same schema the readers expect
    1 - write failure (I/O, permission, schema mismatch on verification)
    2 - bad arguments (empty branch/commit)
"""

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _load_status_module():
    """Import the sibling expert-review-status.py (hyphenated name, so via importlib)."""
    import importlib.util

    path = Path(__file__).resolve().parent / "expert-review-status.py"
    spec = importlib.util.spec_from_file_location("expert_review_status", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    parser = argparse.ArgumentParser(
        description="Write the 'review' section of .claude/github-cache.json."
    )
    parser.add_argument("--cache-path", required=True, help="Path to .claude/github-cache.json")
    parser.add_argument("--commit", required=True, help="Short git hash reviewed")
    parser.add_argument("--branch", required=True, help="Branch name reviewed")
    parser.add_argument("--review-dir", required=True, help="Absolute path to the review checkpoint directory")
    parser.add_argument("--reviewer", action="append", default=[], dest="reviewers", help="Reviewer name that ran (repeatable)")
    parser.add_argument("--panel-model", default=None, help="PANEL_MODEL used for judgment roles")
    parser.add_argument("--critical", type=int, default=0)
    parser.add_argument("--high", type=int, default=0)
    parser.add_argument("--medium", type=int, default=0)
    parser.add_argument("--low", type=int, default=0)
    parser.add_argument("--metrics-path", default=None, help="Only set when EFFORT=2")
    args = parser.parse_args()

    # Reject empty required args
    if not args.commit or not args.commit.strip():
        print("Error: --commit cannot be empty", file=sys.stderr)
        sys.exit(2)
    if not args.branch or not args.branch.strip():
        print("Error: --branch cannot be empty", file=sys.stderr)
        sys.exit(2)

    args.branch = args.branch.strip().replace("/", "-")
    cache_path = Path(args.cache_path)

    review = {
        "lastRun": datetime.now(timezone.utc).isoformat(),
        "commit": args.commit,
        "branch": args.branch,
        "reviewDir": args.review_dir,
        "reviewers": args.reviewers,
        "panelModel": args.panel_model,
        "findings": {
            "critical": args.critical,
            "high": args.high,
            "medium": args.medium,
            "low": args.low,
        },
    }
    if args.metrics_path:
        review["metricsPath"] = args.metrics_path

    existing: dict = {}
    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                existing = json.load(f)
            if not isinstance(existing, dict):
                print(f"Warning: {cache_path} did not contain a JSON object, backing up and overwriting", file=sys.stderr)
                backup_path = Path(str(cache_path) + ".bak")
                try:
                    cache_path.replace(backup_path)
                    print(f"Warning: backed up corrupt cache to {backup_path}", file=sys.stderr)
                except OSError as e:
                    print(f"Warning: could not back up {cache_path}: {e}", file=sys.stderr)
                existing = {}
        except (OSError, json.JSONDecodeError) as e:
            print(f"Warning: failed to read existing cache {cache_path}: {e}, backing up and overwriting", file=sys.stderr)
            backup_path = Path(str(cache_path) + ".bak")
            try:
                cache_path.replace(backup_path)
                print(f"Warning: backed up unreadable cache to {backup_path}", file=sys.stderr)
            except OSError as backup_e:
                print(f"Warning: could not back up {cache_path}: {backup_e}", file=sys.stderr)
            existing = {}

    existing["review"] = review

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=cache_path.parent, prefix=cache_path.name + ".")
    try:
        with open(fd, "w") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")
        Path(tmp_path).replace(cache_path)
    except OSError as e:
        print(f"Error: failed to write {cache_path}: {e}", file=sys.stderr)
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
        sys.exit(1)

    # Read back through the real fast-path reader (expert-review-status.py), so a schema
    # drift between this writer and that reader is caught immediately, not discovered
    # weeks later as a silent "not reviewed" false negative.
    try:
        status_mod = _load_status_module()
        written = status_mod.read_review_cache(cache_path)
        status = status_mod.compute_status(args.branch, args.commit, False, written)
    except Exception as e:  # noqa: BLE001 - any reader failure must surface as exit 1
        print(f"Error: wrote {cache_path} but could not verify read-back: {e}", file=sys.stderr)
        sys.exit(1)
    if not (status["reviewed"] and status["current"]):
        print(
            f"Error: wrote {cache_path} but read-back verification failed — "
            f"the review cache will report 'not reviewed' despite this run completing. "
            f"Wrote: {json.dumps(written)}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Cached review metadata for {args.branch}@{args.commit} -> {cache_path}")
    sys.exit(0)


if __name__ == "__main__":
    main()
