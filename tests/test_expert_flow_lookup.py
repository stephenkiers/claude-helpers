#!/usr/bin/env python3
"""
Fixture test for the shared newest-flow lookup in commands/expert-flow.md.

The first ```bash block under "### Newest-flow lookup (shared)" is extracted at test
time, its <PREFIX> placeholder is substituted, and it runs under bash with real jq
against a temporary FLOWS_ROOT. Skips cleanly when jq is not installed.

Run with: python3 tests/test_expert_flow_lookup.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

COMMAND = REPO_ROOT / "commands" / "expert-flow.md"
SECTION_HEADING = "### Newest-flow lookup (shared)"


def extract_lookup_snippet(text, prefix):
    """Return the shared lookup bash block with <PREFIX> replaced by prefix, or ''."""
    start = text.find(SECTION_HEADING)
    if start < 0:
        return ""
    m = re.search(r"```bash\n(.*?)```", text[start:], re.DOTALL)
    if not m:
        return ""
    return m.group(1).replace("<PREFIX>", prefix)


def make_flow(root, dirname, state):
    """Create FLOWS_ROOT/<dirname>/state.json from a dict, or a raw string when state is str."""
    d = root / dirname
    d.mkdir(parents=True)
    body = state if isinstance(state, str) else json.dumps(state)
    (d / "state.json").write_text(body)


def run_lookup(snippet, flows_root):
    """Run the snippet under bash against flows_root. Returns (returncode, stdout, stderr)."""
    env = {"PATH": os.environ.get("PATH", ""), "FLOWS_ROOT": str(flows_root)}
    result = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


if __name__ == "__main__":
    if shutil.which("jq") is None:
        print("SKIP: jq is not installed; newest-flow lookup fixture test not run")
        sys.exit(0)

    h = Harness("EXPERT-FLOW NEWEST-FLOW LOOKUP FIXTURE TEST SUITE")
    t = h.test_result

    cmd_text = COMMAND.read_text() if COMMAND.is_file() else ""
    snippet = extract_lookup_snippet(cmd_text, "5-")
    t("the shared lookup block is present in expert-flow.md", bool(snippet))

    good_old = {"issue": 5, "updated": "2026-10-10T09:00:00Z", "status": "running", "step": "plan"}
    good_new = {"issue": 5, "updated": "2026-10-12T09:00:00Z", "status": "running", "step": "review"}

    print("[Section 1] Tie on updated breaks by directory name")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        tie = {"issue": 5, "updated": "2026-10-12T09:00:00Z", "status": "running", "step": "plan"}
        make_flow(root, "5-alpha", tie)
        make_flow(root, "5-beta", tie)
        _, out, _ = run_lookup(snippet, root)
        t("equal updated picks the higher directory name",
          out.strip() == str(root / "5-beta"), repr(out))

    print()
    print("[Section 2] Malformed updated is skipped")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        make_flow(root, "5-good", good_old)
        make_flow(root, "5-malformed", {"issue": 5, "updated": "yesterday", "status": "running"})
        _, out, err = run_lookup(snippet, root)
        t("malformed updated does not win over a valid older flow",
          out.strip() == str(root / "5-good"), repr(out))
        t("malformed updated is reported on stderr",
          "skipped" in err and "5-malformed" in err, repr(err))

    print()
    print("[Section 3] Unparseable state.json is skipped")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        make_flow(root, "5-good", good_old)
        make_flow(root, "5-broken", "{not json")
        _, out, _ = run_lookup(snippet, root)
        t("unparseable state.json does not win and does not abort the lookup",
          out.strip() == str(root / "5-good"), repr(out))

    print()
    print("[Section 4] Missing state.json is skipped silently")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        make_flow(root, "5-good", good_old)
        (root / "5-nostate").mkdir()
        _, out, err = run_lookup(snippet, root)
        t("directory without state.json is not selected",
          out.strip() == str(root / "5-good"), repr(out))
        t("directory without state.json produces no skip note",
          "5-nostate" not in err, repr(err))

    print()
    print("[Section 5] Newest valid flow wins by updated")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        make_flow(root, "5-old", good_old)
        make_flow(root, "5-new", good_new)
        _, out, _ = run_lookup(snippet, root)
        t("later updated wins over a lexically higher directory name",
          out.strip() == str(root / "5-new"), repr(out))

    print()
    print("[Section 6] No valid flow prints nothing")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        make_flow(root, "5-malformed", {"issue": 5, "updated": "yesterday"})
        empty_snippet = extract_lookup_snippet(cmd_text, "9-")
        rc, out, _ = run_lookup(empty_snippet, root)
        t("empty result for a prefix with no flows prints no output",
          out == "", repr(out))
        t("empty result for a prefix with no flows exits 0", rc == 0, f"rc={rc}")

    h.summarize_and_exit()
