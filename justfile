# Local CI. No GitHub Actions in this repo — this is the gate.
# `just check` is what /shipit, /cleanup, and /merge-and-cleanup call via .claude/repo-cache.json.

# Run everything: lint + tests. Exits non-zero on first failure.
check: lint test

# ruff (Python: unused imports, undefined names, bare excepts — see ruff.toml for the
# deliberately narrow rule set) + shellcheck (every tracked *.sh file).
lint:
    ruff check .
    shellcheck $(find . -name '*.sh' -not -path './.git/*')

test:
    python3 tests/run_all.py

# Auto-fix what ruff can fix safely, leave the rest for manual review.
fix:
    ruff check . --fix
