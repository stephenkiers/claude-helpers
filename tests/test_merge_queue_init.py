#!/usr/bin/env python3
"""
Test suite for merge-queue setup offered by /merge-and-cleanup when no queue is configured.

- detect_merge_queue() absent-with-default-path carries the would-be config path
- queue_init() dry run proposes {base, steps} without writing
- queue_init() step detection: repo-cache check > justfile check; never `just merge`
- queue_init(write=True) validates, creates the file, and never overwrites
- queue_init() refuses when the layout is not queue-capable or a config exists
- commands/merge-and-cleanup.md wires the offer (Phase 2b) to the CLI and /queued-merge

Run with: python3 tests/test_merge_queue_init.py
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow import merge
from workflow.merge import detect_merge_queue, queue_init, QueueDetection
from _test_harness import Harness

DOC = Path(__file__).parent.parent / "commands" / "merge-and-cleanup.md"


def _absent(path: Path) -> QueueDetection:
    return QueueDetection(state="absent", path=str(path), source="default", reason="no-config")


if __name__ == "__main__":
    os.environ.pop("MERGE_QUEUE_CONFIG", None)
    h = Harness("MERGE QUEUE INIT TEST SUITE")
    t = h.test_result

    print("[Test 1] absent detection carries the default config path")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "merge-queue.json"
        with mock.patch("workflow.merge_queue.resolve_config_path", return_value=cfg):
            d = detect_merge_queue(Path(tmp))
        t("absent: path is the would-be config", d.state == "absent" and d.path == str(cfg),
          f"state={d.state} path={d.path}")
        t("absent: reason no-config", d.reason == "no-config", d.reason)

    print("[Test 2] dry run proposes repo-cache check and writes nothing")
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "wt"
        (wt / ".claude").mkdir(parents=True)
        (wt / ".claude" / "repo-cache.json").write_text(json.dumps({"commands": {"check": "make ci"}}))
        cfg = Path(tmp) / "merge-queue.json"
        with mock.patch.object(merge, "detect_merge_queue", return_value=_absent(cfg)), \
             mock.patch("workflow.git.get_default_branch", return_value=("main", None)):
            r = queue_init(wt)
        t("dry run: config proposed",
          r["config"] == {"base": "main", "steps": ["make ci"]}, str(r))
        t("dry run: steps_source repo-cache", r["steps_source"] == "repo-cache", str(r))
        t("dry run: nothing written", not cfg.exists() and r["written"] is False)

    print("[Test 3] justfile with merge + check recipes proposes `just check`, never `just merge`")
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "wt"
        wt.mkdir()
        (wt / "justfile").write_text("x")
        fake = mock.Mock(returncode=0, stdout="check merge test\n")
        with mock.patch("workflow.merge.subprocess.run", return_value=fake):
            steps, src = merge._propose_queue_steps(wt)
        t("justfile: just check", steps == ["just check"] and src == "justfile", f"{steps} {src}")

    print("[Test 4] no detectable gate → empty steps on dry run, error on write")
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "wt"
        wt.mkdir()
        cfg = Path(tmp) / "merge-queue.json"
        with mock.patch.object(merge, "detect_merge_queue", return_value=_absent(cfg)), \
             mock.patch("workflow.git.get_default_branch", return_value=("main", None)):
            r = queue_init(wt)
            t("no gate: empty steps", r["config"]["steps"] == [] and r["steps_source"] is None, str(r))
            try:
                queue_init(wt, write=True)
                t("no gate: write refuses", False, "expected RuntimeError")
            except RuntimeError as e:
                t("no gate: write refuses", "--step" in str(e), str(e))
        t("no gate: nothing written", not cfg.exists())

    print("[Test 5] write with explicit steps creates a valid config; second write refuses")
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "wt"
        wt.mkdir()
        cfg = Path(tmp) / "merge-queue.json"
        with mock.patch.object(merge, "detect_merge_queue", return_value=_absent(cfg)), \
             mock.patch("workflow.git.get_default_branch", return_value=("main", None)):
            r = queue_init(wt, steps=["just test"], write=True)
            t("write: written flag", r["written"] is True, str(r))
            on_disk = json.loads(cfg.read_text())
            t("write: on-disk config",
              on_disk == {"base": "main", "steps": ["just test"]}, str(on_disk))
            cfg.write_text('{"sentinel": true}')
            try:
                queue_init(wt, steps=["just test"], write=True)
                t("write: never overwrites", False, "expected RuntimeError")
            except RuntimeError as e:
                t("write: never overwrites", "already exists" in str(e), str(e))
            t("write: existing file untouched", json.loads(cfg.read_text()) == {"sentinel": True})

    print("[Test 6] refuses when not queue-capable or already configured")
    for det, needle in (
        (QueueDetection(state="absent", reason="layout-not-queue-capable"), "Cannot set up"),
        (QueueDetection(state="configured", path="/x/merge-queue.json", source="default"), "already exists"),
        (QueueDetection(state="unknown", reason="boom"), "Cannot set up"),
    ):
        with mock.patch.object(merge, "detect_merge_queue", return_value=det):
            try:
                queue_init(Path("/nonexistent"))
                t(f"refuse: {det.state}/{det.reason}", False, "expected RuntimeError")
            except RuntimeError as e:
                t(f"refuse: {det.state}/{det.reason or det.path}", needle in str(e), str(e))

    print("[Test 7] command doc wires the offer")
    doc = DOC.read_text()
    t("doc: Phase 1 prints QUEUE_SETUP_OFFER on absent+path",
      "QUEUE_SETUP_OFFER" in doc and ".queue.detection.path" in doc)
    t("doc: Phase 2b section exists", "### Phase 2b" in doc)
    t("doc: dry run and --write via the CLI",
      re.search(r"merge queue-init --cwd \"\$WT\"\)", doc) is not None
      and "merge queue-init --cwd \"$WT\" --write" in doc)
    t("doc: hands off to queued-merge skill", "`queued-merge` skill" in doc)
    t("doc: AskUserQuestion allowed", re.search(r"allowed-tools:.*AskUserQuestion", doc) is not None)

    h.summarize_and_exit()
