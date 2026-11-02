"""
Generic file helpers shared by scripts/workflow/ modules (#417 review, item 17).

Kept out of deps.py so that a module needing only file I/O does not depend on install logic.
deps.py re-exports read_json_object and atomic_write_text, because checks.py imports them from there.
"""

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, Optional


def read_json_object(path: Path) -> Optional[Dict[str, Any]]:
    """The parsed JSON object at path, or None when absent, unreadable, unparseable or not an object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def atomic_write_text(path: Path, text: str) -> None:
    """
    Write text to the real file behind path, via a temp file in its directory and os.replace. Raises OSError.

    A symlink at path is resolved first, so the link's target is replaced and the link itself survives.
    The temp file is created with mode 0o666, so the kernel applies the process umask and the umask is
    never toggled. An existing target keeps its permission bits; a new file gets the umask default.
    """
    target = Path(os.path.realpath(str(path)))
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    temp_name = str(target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        if existing_mode is not None:
            os.chmod(temp_name, existing_mode)
        os.replace(temp_name, str(target))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
