"""
Path containment helpers shared by scripts/workflow/ modules.
"""

import os


def is_within(path: str, root: str) -> bool:
    """
    True iff path is strictly inside root, compared component-wise.

    Both path and root must be absolute paths; relative paths raise ValueError.

    Contract:
    - Strictly inside: a path that is a descendant of root returns True.
    - Equal paths return False: is_within(root, root) is False.
    - Symlinks are resolved: both sides go through os.path.realpath, so a
      symlinked spelling of the same directory compares as the real one.
    - Component-wise comparison, not string-prefix: is_within("/a/bc", "/a/b")
      returns False.
    - Trailing slashes on root are ignored.
    - Non-existent paths are allowed (no existence check required).

    Args:
        path: An absolute path to check (raises ValueError if relative).
        root: An absolute root path (raises ValueError if relative).

    Returns:
        True if path is strictly inside root (component-wise).
        False otherwise.

    Raises:
        ValueError: If path or root is not an absolute path.
    """
    if not os.path.isabs(path):
        raise ValueError(f"path must be absolute, got relative path: {path!r}")
    if not os.path.isabs(root):
        raise ValueError(f"root must be absolute, got relative path: {root!r}")

    real_root = os.path.realpath(root).rstrip("/")
    return os.path.realpath(path).startswith(real_root + "/")
