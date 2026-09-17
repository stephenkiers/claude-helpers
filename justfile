# Local CI. No GitHub Actions in this repo — this is the gate.
# `just check` is what /shipit, /cleanup, and /merge-and-cleanup call via .claude/repo-cache.json.

# Run everything: lint + typecheck + tests. Exits non-zero on first failure.
check: lint typecheck test

# ruff (Python: unused imports, undefined names, bare excepts — see ruff.toml for the
# deliberately narrow rule set) + shellcheck (every tracked *.sh file).
lint:
    ruff check .
    shellcheck $(find . -name '*.sh' -not -path './.git/*')

test:
    python3 tests/run_all.py

# mypy over scripts/workflow/ only (see mypy.ini). Run from the repo root so the
# cache never lands under an install.sh-mirrored directory. Invoked via `python3 -m`
# rather than the bare `mypy` binary since a `pip install --user` console script
# isn't guaranteed to be on PATH, but the module always is once installed.
typecheck:
    @python3 -c "import mypy" 2>/dev/null || { echo "mypy not found. Install with: pip install -r requirements-dev.txt"; exit 1; }
    python3 -m mypy

# Auto-fix what ruff can fix safely, leave the rest for manual review.
fix:
    ruff check . --fix
