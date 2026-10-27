#!/usr/bin/env python3
"""
Test suite for the portable slug pipeline used by /expert-plan (#377).

What this proves, and where:
- The SLUG= pipeline at every site is extracted from the real command docs and executed,
  not re-typed here. Sites: two in commands/expert-plan.md, one in
  commands/expert-plan-deprecated-v2.md (count and identity are pinned).
- Each site runs under /usr/bin/sed when that binary exists, with /usr/bin and /bin first on
  PATH so a Homebrew gnubin sed cannot mask BSD behaviour. On macOS this exercises BSD sed;
  on Linux /usr/bin/sed is GNU sed. When /usr/bin/sed is absent the native sed is tested and
  a notice says so.
- Guard cases (tagged GUARD below) fail the old GNU-only pipeline on BSD and GNU sed alike,
  so the test still proves something on a host where only one sed is present. Do not trim them.
- A narrow exact-string scan fails if the old GNU-only sed expression reappears under
  commands/ or prompts/. tests/ is deliberately not scanned (this file contains the needle).

Run with: python3 tests/test_slug_pipeline.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import (
    Harness,
    REPO_ROOT,
    EXPECTED_SITES,
    FALLBACK_LINE,
    detect_sed_flavor,
    extract_slug_sites,
    sed_path_env,
    run_pipeline,
)

# The old GNU-only expression, assembled as a constant. Scanned for under commands/ and prompts/.
OLD_GNU_SED_NEEDLE = r"s/^-\|-$//"
SCAN_DIRS = [REPO_ROOT / "commands", REPO_ROOT / "prompts"]

# (title, expected slug, is_guard)
CASES = [
    ("Fix: Thing", "fix-thing", False),
    # GUARD: old pipeline gives "fix-thing-" on GNU and "-fix-thing-" on BSD (leading punctuation).
    (": Fix Thing", "fix-thing", True),
    # GUARD: old non-global strip removes only one end, so a dash survives at the other end.
    ("---Hi!!", "hi", True),
    ("abc", "abc", False),
    ("a  --  b", "a-b", False),
    # GUARD: cut -c1-50 lands on a dash. Strip-before-cut (the old order) leaves a trailing dash.
    ("a" * 49 + " b", "a" * 49, True),
    ("!!!", "plan", False),
    ("", "plan", False),
]


def main():
    h = Harness("EXPERT-PLAN SLUG PIPELINE TEST SUITE")

    # Test 1: extraction and fallback-line assertion at every site.
    all_sites = {}
    for path in EXPECTED_SITES:
        sites = extract_slug_sites(path)
        all_sites[path] = sites
        for line_no, _slug_line, following in sites:
            h.test_result(
                f"{path.name}:{line_no} SLUG line is followed by the fallback line",
                following == FALLBACK_LINE,
                f"next line is {following!r}, expected {FALLBACK_LINE!r}" if following != FALLBACK_LINE else "",
            )

    # Test 2: site inventory (D2) and identity across sites.
    found_slug_lines = set()
    for path, expected in EXPECTED_SITES.items():
        found = len(all_sites[path])
        h.test_result(
            f"{path.name} has exactly {expected} SLUG site(s)",
            found == expected,
            (
                f"found {found}, expected {expected}; a new or removed slug site needs a deliberate "
                "edit to EXPECTED_SITES in tests/test_slug_pipeline.py"
            ) if found != expected else "",
        )
        for _line_no, slug_line, _following in all_sites[path]:
            found_slug_lines.add(slug_line)

    total_found = sum(len(sites) for sites in all_sites.values())
    total_expected = sum(EXPECTED_SITES.values())
    h.test_result(
        f"total slug sites is {total_expected}",
        total_found == total_expected,
        f"found {total_found}, expected {total_expected}" if total_found != total_expected else "",
    )
    h.test_result(
        "all SLUG= lines across both docs are identical",
        len(found_slug_lines) == 1,
        f"{len(found_slug_lines)} distinct SLUG= lines found" if len(found_slug_lines) != 1 else "",
    )

    # Test 3: execute every site against every case.
    sed_type, flavor_notice = detect_sed_flavor()
    env, path_notice = sed_path_env()
    if flavor_notice:
        notice = flavor_notice
    else:
        notice = f"sed under test: /usr/bin/sed ({sed_type.upper()}, first on PATH)"
    print(notice)
    print()
    for path in EXPECTED_SITES:
        for line_no, slug_line, following in all_sites[path]:
            for title, expected, is_guard in CASES:
                label = "GUARD " if is_guard else ""
                name = f"{path.name}:{line_no} {label}{title!r} -> {expected!r}"
                returncode, stdout, stderr = run_pipeline(slug_line, following, title, env)
                if returncode != 0:
                    h.test_result(name, False, f"bash exited {returncode}: {stderr.strip()}")
                    continue
                h.test_result(
                    name,
                    stdout == expected,
                    f"got {stdout!r}" if stdout != expected else "",
                )

    # Test 4: recurrence scan for the old GNU-only expression under commands/ and prompts/.
    offenders = []
    for scan_dir in SCAN_DIRS:
        for file_path in sorted(scan_dir.rglob("*")):
            if not file_path.is_file():
                continue
            text = file_path.read_text(errors="replace")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if OLD_GNU_SED_NEEDLE in line:
                    offenders.append(f"{file_path.relative_to(REPO_ROOT)}:{line_no}")
    h.test_result(
        "no commands/ or prompts/ file contains the old GNU-only slug sed expression",
        not offenders,
        "found in: " + ", ".join(offenders) if offenders else "",
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
