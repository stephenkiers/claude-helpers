"""
Path containment helpers shared by scripts/workflow/ modules.
"""

import os


def is_within(path: str, root: str) -> bool:
    """
    True iff path is inside root or equal to root, compared component-wise.

    Both path and root must be absolute paths; relative paths raise ValueError.

    Contract:
    - Inside or equal: a path that is a descendant of root or equal to root returns True.
    - Equal paths return True: is_within(root, root) is True (F8: inclusive containment).
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
        True if path is inside or equal to root (component-wise).
        False otherwise.

    Raises:
        ValueError: If path or root is not an absolute path.
    """
    if not os.path.isabs(path):
        raise ValueError(f"path must be absolute, got relative path: {path!r}")
    if not os.path.isabs(root):
        raise ValueError(f"root must be absolute, got relative path: {root!r}")

    real_root = os.path.realpath(root).rstrip("/")
    real_path = os.path.realpath(path)
    # F8: Inclusive containment - equal or startswith with /
    return real_path == real_root or real_path.startswith(real_root + "/")
