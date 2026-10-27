#!/usr/bin/env python3
"""
Edge-case pins for the /expert-plan slug pipeline and the stray `--model` flag arm (#377).

The slug lines and the flag loop are read from the real command docs at run time; nothing here
re-types the pipeline. Every case runs the extracted `SLUG=$(printf ...)` line plus its
`[ -n "$SLUG" ] || SLUG="plan"` fallback through `bash -c`, with the title passed as the
TICKET_TITLE environment variable (never interpolated into the script).

Sed: when /usr/bin/sed exists, PATH puts /usr/bin first so a Homebrew gnubin sed cannot mask the
BSD behaviour. Otherwise the host's native sed is used and a notice says so.

Expected values were derived by hand from the pipeline's stages (lowercase, map every run of
non-[a-z0-9] to one dash, cut to 50 bytes, strip leading and trailing dashes, fall back to `plan`).

Covered here, beyond the cases in the slug test file: non-ASCII letters and non-ASCII-only titles,
an all-dash title, mixed case, tabs and newlines, underscores and digits, a cut that lands on a
dash inside a long title, a cut at exactly 50 characters, and a BSD-unsafe-construct check on the
extracted slug lines. Also a flag-loop boundary: `--model-foo` must fall through to the generic
unknown-flag arm, not the `--model` did-you-mean arm.

Run with: python3 tests/test_slug_edge_cases.py
"""

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import (
    Harness,
    REPO_ROOT,
    EXPERT_PLAN,
    EXPERT_PLAN_V2,
    detect_sed_flavor,
    sed_path_env,
    run_slug,
    extract_slug_sites_edge_cases,
    extract_flag_loop,
    run_flag_loop,
)

SLUG_DOCS = [EXPERT_PLAN, EXPERT_PLAN_V2]

# Backslash escapes that BSD sed (basic regex) does not share with GNU sed.
BSD_UNSAFE = re.compile(r"\\[+|?<>bBwWsS]")

# (case name, TICKET_TITLE, expected slug). Expected values are hand-derived, see module docstring.
SLUG_CASES = [
    # Non-ASCII letters become separators; the ASCII letters around them survive.
    ("accented letter and punctuation", "Café au lait: 2026 plan!!", "caf-au-lait-2026-plan"),
    # Non-ASCII-only title: nothing survives, so the fallback runs.
    ("non-ASCII only falls back", "漢字 テスト", "plan"),
    # Only dashes in the title: squeezed to one dash, stripped, fallback.
    ("dashes only falls back", "-----", "plan"),
    ("mixed separators only falls back", "--- ___ ---", "plan"),
    ("mixed case folds to lower", "ALL CAPS Title Here", "all-caps-title-here"),
    ("single uppercase letter", "A", "a"),
    ("tabs and newlines are separators", "Line one\nLine two\tthree", "line-one-line-two-three"),
    ("underscores and digits", "v2_api  v3", "v2-api-v3"),
    ("surrounding whitespace", "   Spaced Out   ", "spaced-out"),
    ("trailing punctuation", "Done.", "done"),
    ("digits only", "2026", "2026"),
    # Five 9-char words joined by dashes = 49 chars; char 50 is the separator, the cut keeps it,
    # and the trailing-dash strip must remove it. Result is exactly the 49 chars.
    (
        "cut lands on a separator inside a long title",
        "abcdefghi abcdefghi abcdefghi abcdefghi abcdefghi tail",
        "abcdefghi-abcdefghi-abcdefghi-abcdefghi-abcdefghi",
    ),
    # Exactly 50 letters then a separator: the cut is a no-op on the letters, nothing to strip.
    ("exactly fifty characters, no separator in the cut", "a" * 50 + " b", "a" * 50),
    # Longer than 50 with no separator in the cut: cut to exactly 50.
    ("over fifty characters, no separator", "a" * 51, "a" * 50),
]


def main():
    h = Harness("SLUG EDGE CASES AND --model FLAG BOUNDARY (#377)")

    sed_type, flavor_notice = detect_sed_flavor()
    env, path_notice = sed_path_env()
    if flavor_notice:
        notice = flavor_notice
    else:
        notice = f"sed under test: /usr/bin/sed ({sed_type.upper()}, first on PATH)"
    print(notice)
    print()

    print("[Section 1] Extracted slug lines exist and avoid BSD-unsafe sed constructs")
    sites = extract_slug_sites_edge_cases()
    for doc in SLUG_DOCS:
        rel = doc.relative_to(REPO_ROOT)
        doc_sites = [line for path, line in sites if path == rel and line is not None]
        h.test_result(
            f"{rel}: at least one SLUG line followed by the fallback line",
            len(doc_sites) >= 1,
            "no extractable SLUG line" if not doc_sites else "",
        )
    for path, line in sites:
        if line is None:
            continue
        h.test_result(
            f"{path}: slug line has no BSD-unsafe sed escape (\\+ \\| \\? \\< \\> \\b \\w \\s)",
            not BSD_UNSAFE.search(line),
            f"found in: {line}" if BSD_UNSAFE.search(line) else "",
        )

    print()
    print("[Section 2] Slug edge cases, run at every extracted site")
    live_sites = [(path, line) for path, line in sites if line is not None]
    for name, title, expected in SLUG_CASES:
        for path, line in live_sites:
            rc, out, err = run_slug(line, title, env)
            passed = rc == 0 and out == expected
            detail = ""
            if not passed:
                detail = f"expected {expected!r}, got rc={rc} stdout={out!r} stderr={err.strip()!r}"
            h.test_result(f"{path}: {name} -> {expected!r}", passed, detail)

    print()
    print("[Section 3] Flag loop boundary: --model-foo is not the --model did-you-mean arm")
    doc_text = EXPERT_PLAN.read_text()
    loop = extract_flag_loop(doc_text)
    h.test_result(
        "exactly one flag loop block extractable from commands/expert-plan.md",
        loop is not None,
        "could not extract a single `while [ $# -gt 0 ]; do` ... `done` block",
    )
    if loop is not None:
        with tempfile.TemporaryDirectory() as tmp_home:
            rc, stdout, stderr = run_flag_loop(loop, ["--model-foo"], tmp_home)
        h.test_result(
            "--model-foo exits 1",
            rc == 1,
            f"exit {rc}",
        )
        h.test_result(
            "--model-foo reaches the generic unknown-flag message",
            "supported flags are" in stderr,
            f"stderr: {stderr.strip()!r}",
        )
        h.test_result(
            "--model-foo does not get the --model did-you-mean hint",
            "did you mean" not in stderr,
            f"stderr: {stderr.strip()!r}",
        )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
