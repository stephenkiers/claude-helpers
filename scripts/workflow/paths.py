"""
Path containment helpers shared by scripts/workflow/ modules.
"""

import os


def is_within(path: str, root: str) -> bool:
    """
    True iff path is strictly inside root, compared component-wise.

    Contract:
    - Strictly inside: a path that is a descendant of root is True.
    - Equal paths are False: is_within(root, root) is False.
    - Symlinks are resolved: both sides go through os.path.realpath, so a
      symlinked spelling of the same directory compares as the real one.
    - Component-wise, not string-prefix: is_within("/a/bc", "/a/b") is False.
    - Trailing slashes on root are ignored.
    """
    real_root = os.path.realpath(root).rstrip("/")
    return os.path.realpath(path).startswith(real_root + "/")
