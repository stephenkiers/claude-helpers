#!/usr/bin/env python3
"""
Test suite for workflow.paths.is_within(path, root).

Contract: True iff realpath(path) is strictly inside realpath(root), compared
component-wise. Equal paths are False, sibling string-prefix paths are False,
and symlinks are resolved on both sides before comparing.

Run with: python3 tests/test_workflow_paths.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.paths import is_within
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW PATHS IS_WITHIN TEST SUITE")
    t = h.test_result

    with tempfile.TemporaryDirectory() as d:
        base = Path(os.path.realpath(d))
        root = base / "a" / "b"
        root.mkdir(parents=True)

        print("[Section 1] Component-wise containment")
        t("child directory is within root", is_within(root / "child", root))
        t("deeply nested path is within root", is_within(root / "x" / "y" / "z.txt", root))
        t("accepts str arguments", is_within(str(root / "child"), str(root)))
        t("equal paths are not within", not is_within(root, root))
        t("equal paths given with a trailing slash are not within",
          not is_within(str(root) + os.sep, root))
        t("parent of root is not within", not is_within(root.parent, root))
        t("sibling string-prefix (/a/bc vs /a/b) is not within",
          not is_within(base / "a" / "bc" / "file", root))
        t("sibling string-prefix with shared name stem is not within",
          not is_within(base / "a" / "b-other" / "file", root))
        t("'..' traversal out of root is not within",
          not is_within(root / ".." / "b-escape", root))
        t("'..' traversal that stays inside root is within",
          is_within(root / "x" / ".." / "y", root))
        t("non-existent path under root is within (no existence required)",
          is_within(root / "not-yet-created" / "f", root))

        print()
        print("[Section 2] Symlink resolution")
        outside = base / "outside"
        outside.mkdir()
        (root / "escape").symlink_to(outside)
        t("path reached through a symlink that points outside root is not within",
          not is_within(root / "escape" / "f", root))

        real_root = base / "real-root"
        real_root.mkdir()
        (real_root / "child").mkdir()
        link_root = base / "link-root"
        link_root.symlink_to(real_root)
        t("symlinked root: child under the real root is within the link",
          is_within(real_root / "child", link_root))
        t("symlinked path: child reached via link is within the real root",
          is_within(link_root / "child", real_root))
        t("symlinked root: root reached via its own link is not within itself",
          not is_within(link_root, real_root))

        inside_link_src = root / "inner-link"
        inside_link_src.symlink_to(root / "x")
        t("symlink inside root pointing to another path inside root is within",
          is_within(inside_link_src / "f", root))

    h.summarize_and_exit()
