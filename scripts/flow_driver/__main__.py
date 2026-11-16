"""
Entry point for the v2 skeleton driver: `python3 <path>/scripts/flow_driver/__main__.py <subcommand> ...`.

The runner is invoked by script path (through the ~/.claude/scripts symlink), so the repo root that holds the
`scripts` package is put on sys.path here, resolved through symlinks.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.flow_driver.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
