#!/usr/bin/env python3
"""Local smoke test for scripts/precedent-digest.py against the real ~/.claude (#292).

Deliberately NOT named test_*.py: run_all.py and `just check` never collect it. Run by hand:

    python3 tests/local_smoke_precedent_digest.py [<repo-root>]

<repo-root> defaults to the repo containing the script. The run reads the real ~/.claude and
writes nothing. It skips (exit 0) when ~/.claude/plan-sessions, flows and reviews are all absent.
It prints counts only, never ruling text.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "precedent-digest.py"
KEYS = ["id", "source", "repo_key", "date", "ref", "path", "header", "question",
        "options", "recommended", "answer", "reason", "deviated"]
SOURCES = ("plan-session", "flow", "review")


def git_origin(repo):
    proc = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    m = re.search(r"[:/]([^/:\s]+)/([^/\s]+?)(?:\.git)?/?$", proc.stdout.strip())
    return (m.group(1), m.group(2)) if m else None


def matched_dirs(root, owner, name):
    """Child dirs of a source root whose name matches owner-name or owner_name, case-insensitively."""
    if not root.is_dir():
        return []
    wanted = {f"{owner}-{name}".lower(), f"{owner}_{name}".lower()}
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.lower() in wanted)


def main():
    h = Harness("precedent-digest local smoke (real ~/.claude)")
    claude = Path(os.path.expanduser("~")) / ".claude"
    roots = {"plan-session": claude / "plan-sessions", "flow": claude / "flows", "review": claude / "reviews"}
    if not any(p.exists() for p in roots.values()):
        print("SKIP: ~/.claude/plan-sessions, flows and reviews are all absent; nothing to smoke-test.")
        sys.exit(0)

    repo = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else REPO_ROOT
    origin = git_origin(repo)
    if origin is None:
        print(f"NOTE: no origin remote for {repo}; key-based assertions skipped (exit still checked).")
    else:
        print(f"repo key: {origin[0]}-{origin[1]}")

    proc = subprocess.run([sys.executable, str(SCRIPT), str(repo)], capture_output=True, text=True)
    h.test_result("smoke: script exits 0 against the real ~/.claude", proc.returncode == 0,
                  f"rc={proc.returncode}")
    stderr_summary = [ln for ln in proc.stderr.splitlines() if ln.strip()]
    print("stderr summary: " + (stderr_summary[-1] if stderr_summary else "(none)"))

    entries = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
    bad_shape = [i for i, e in enumerate(entries) if list(e.keys()) != KEYS]
    h.test_result("smoke: every entry has the pinned 13 keys in order", not bad_shape,
                  f"{len(bad_shape)} entries off-shape, first index {bad_shape[0]}" if bad_shape else "")

    counts = {src: sum(1 for e in entries if e.get("source") == src) for src in SOURCES}
    print("counts: " + ", ".join(f"{src}={counts[src]}" for src in SOURCES) + f", total={len(entries)}")

    if origin is not None:
        owner, name = origin
        for src in SOURCES:
            matched = matched_dirs(roots[src], owner, name)
            print(f"matched dirs for {src}: {len(matched)}")
            if not matched:
                h.test_result(f"smoke: {src} has zero entries when no directory matched",
                              counts[src] == 0, f"count={counts[src]}")
        keys = {e.get("repo_key") for e in entries}
        h.test_result("smoke: every entry's repo_key is the hyphen spelling",
                      keys <= {f"{owner}-{name}"}, f"keys={sorted(k for k in keys if k)}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
