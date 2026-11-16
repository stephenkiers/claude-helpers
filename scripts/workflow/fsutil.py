"""
Generic file helpers shared by scripts/workflow/ modules (#417 review, item 17).

Kept out of deps.py so that a module needing only file I/O does not depend on install logic.
deps.py re-exports read_json_object and atomic_write_text, because checks.py imports them from there.
"""

import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


def read_json_object(path: Path) -> Optional[Dict[str, Any]]:
    """The parsed JSON object at path, or None when absent, unreadable, unparseable or not an object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def atomic_write_text(path: Path, text: str, *, fsync: bool = False) -> None:
    """
    Write text to the real file behind path, via a temp file in its directory and os.replace. Raises OSError.

    A symlink at path is resolved first, so the link's target is replaced and the link itself survives.
    The temp file is created with mode 0o666, so the kernel applies the process umask and the umask is
    never toggled. An existing target keeps its permission bits; a new file gets the umask default.

    With fsync=True the temp file is fsynced before os.replace and the parent directory is fsynced after
    it. That narrows the window in which a crash loses the write. It is not a durability guarantee.
    """
    # Resolve any symlinks to get the real file path
    target_str = os.path.realpath(str(path))
    target = Path(target_str)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    temp_name = str(target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(temp_name, existing_mode)
        os.replace(temp_name, target_str)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    if fsync:
        _fsync_directory(target.parent)


def _fsync_directory(directory: Path) -> None:
    """Flush a directory's entries (a rename into it) to disk. Opened read-only; closed on every path."""
    dir_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class LockHeld(OSError):
    """Another open file description holds the lock that nonblocking_flock was asked for."""


@contextmanager
def nonblocking_flock(path: Path) -> Iterator[int]:
    """
    Hold an exclusive, non-blocking flock on path for the with block, and yield its fd. Raises LockHeld.

    The lock file is opened O_APPEND|O_CREAT and is never truncated, replaced, renamed or unlinked, so the
    lock stays on one inode. flock locks an open file description, not a path: take the lock once per
    process, because a second open in the same process is a second description and blocks against the
    first. The fd is non-inheritable (PEP 446), so no child process can keep the lock after release.
    Releasing happens when the fd closes, on every exit path. This makes no durability claim.
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise LockHeld(f"lock held: {path}") from exc
    except BaseException:
        os.close(fd)
        raise
    try:
        yield fd
    finally:
        os.close(fd)


def open_private_exclusive(path: Path) -> int:
    """
    Create path as a new mode-0600 file and return its write-only fd; the caller closes it.

    Fails with FileExistsError if anything (including a symlink) is already there, and never follows a
    symlink at the final component. Raises OSError.
    """
    return os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
