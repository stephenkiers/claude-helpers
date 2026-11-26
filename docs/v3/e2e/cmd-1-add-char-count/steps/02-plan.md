# plan

## Goal

Add src/chars.py with char_count(text), returning the number of characters in text that are not whitespace, as a library function only.

## Non-goals

- No CLI wiring, and no changes to check.py, tools/, or README.md.
- No reuse of words() or any other src/textstats.py helper. words() lower-cases and splits, which discards character positions, so it cannot count characters. P-reuse says its 'Revisit if: The helper does not fit' clause applies here; no other record of this decision exists in the repo.
- No grapheme-cluster counting. Characters are Python code points.
- No change to existing tests in tests/test_textstats.py.

## Acceptance criteria

- AC1: char_count(text) returns the count of code points in text for which str.isspace() is False.
- AC2: char_count returns 0 for an empty string and for a string made only of whitespace, and does not raise for either.
- AC3: Spaces, tabs, newlines and Unicode whitespace (for example U+00A0 and U+3000) are excluded from the count.
- AC4: A non-str argument (None, int, etc.) raises ValueError. It never returns 0 or None for bad input (P-errors).
- AC5: src/chars.py contains the line '# registered: yes' (required by tools/style.py), and running check.py exits 0 overall: every tests/test_*.py exits 0 and tools/style.py passes, not only the test files.

## Units

### U1: Implement char_count in src/chars.py
Files: `src/chars.py`
Satisfies: AC1, AC2, AC3, AC4, AC5
1. Create src/chars.py with a module docstring.
2. Add the line '# registered: yes' to the file, as tools/style.py requires for src/*.py modules other than __init__.py and textstats.py.
3. Define def char_count(text): with a docstring stating: returns the number of code points in text for which str.isspace() is False; returns 0 for empty or all-whitespace input; raises ValueError for non-str input; counts code points, not grapheme clusters.
4. At the top of the body, if not isinstance(text, str), raise ValueError with a message naming the received type. Add a one-line comment that P-errors requires ValueError instead of the usual TypeError.
5. Return sum(1 for c in text if not c.isspace()). Use no module-level state and no imports.

## Interfaces

- `def char_count(text: str) -> int` (src/chars.py)

## Test spec

- T1: char_count('a b\tc\nd') == 4 (mixed spaces, tab and newline are excluded) (tests/test_chars.py)
- T2: char_count('') == 0 and char_count('   \t\n') == 0 (tests/test_chars.py)
- T3: char_count('abc') == 3 (non-whitespace characters counted, not len of whitespace) (tests/test_chars.py)
- T4: char_count('a b　c') == 3 (non-breaking and ideographic spaces excluded) (tests/test_chars.py)
- T5: char_count('héllo') == 5 (non-ASCII letters each count as one) (tests/test_chars.py)
- T6: char_count(None), char_count(123) each raise ValueError, checked with try/except ValueError and a failing assert if no exception is raised. Do not use pytest, because check.py runs test files as plain scripts and the project has no dependencies. (tests/test_chars.py)

## Advised by

- North Star Nick: NORTH STAR BLIND: nick=no scout briefs or in-flight.md/in-flight-issues.md were provided, and there is no docs/adr/ directory or .claude/reviewers/north-star-nick-local.yaml in the repo, so I could not check ADRs, open issues, epics, open PRs, or the current milestone; no 'no conflicts' claim is made.; Scope: the repo's only stated direction is docs/principles.md ('A tiny, dependable text statistics library with no dependencies'). char_count fits that goal, so the work is directionally aligned. Nothing in the readable docs names a milestone, so confirm the ticket belongs to the current one before merging.; Principle P-reuse ('New code reuses the existing helpers in src/textstats.py') is in tension with the ticket's new src/chars.py module. Keep the separate module if the ticket is intended, but note in the commit or PR that P-reuse was considered and why no existing helper applies (words() splits on whitespace and lower-cases, which does not fit a character count).; Principle P-errors says a function given bad input raises ValueError and never returns a sentinel. The plan should specify that char_count raises ValueError for non-str input (for example None or an int) rather than returning 0 or None, and add a test for it in tests/.; The ticket says 'Reuse nothing from the CLI', but no CLI module is visible in the repo file list (tools/merge.sh, tools/style.py and check.py are the only tooling). The plan should not assume a CLI exists; if one is expected, the ticket's reference is stale and should be corrected.; Keep the change to src/chars.py plus its test. Do not wire char_count into check.py, tools/ or any CLI output, which would expand scope beyond the ticket.
- Vera Verifier: Write the char_count tests in tests/test_chars.py using the same plain `assert` style and the same sys.path bootstrap as tests/test_textstats.py, importing from src.chars. Do not add a test for word_count or textstats as part of this change.; Pin the whitespace definition with concrete cases: "a b\tc\nd" == 4, "   " == 0, "" == 0, and "abc" == 3. Each of these fails if the implementation counts whitespace or counts len(text) instead, so they actually distinguish the behaviour.; Include a non-ASCII whitespace case such as "a b　c" == 3, because str.split() and str.isspace() both treat Unicode spaces as whitespace. Without it, a byte-based or ASCII-only implementation would pass the other cases.; Cover the ValueError principle explicitly in src/chars.py: for a non-str argument (for example None or 123), raise ValueError with a clear message. Test it with `try/except ValueError` or pytest.raises, not by asserting on a return value, because plain asserts cannot express an expected exception.; Implement char_count as a single pass, for example `sum(1 for c in text if not c.isspace())`, with no mutable state and no reuse of CLI code. Check that it returns 0 for empty input rather than raising, since the ticket does not ask for empty input to be an error.
- Contract Chris: Give `char_count(text)` a docstring that states the contract: it returns the count of characters in `text` for which `str.isspace()` is False, it returns 0 for an empty string, and it raises on non-str input. Note in the docstring that the count is in code points, not grapheme clusters, so callers know what a 'character' means.; Decide and document the exception type for non-str input. P-errors says bad input raises ValueError, but Python convention for a wrong type is TypeError. The plan should pick one, name it in the docstring under Raises, and add a test that asserts it; leaving it implicit means `char_count(None)` surfaces as whatever `.isspace()` or iteration happens to throw.; Define 'whitespace' explicitly in the plan. `str.isspace()` and `str.split()` treat some Unicode characters differently, and the ticket says only 'not whitespace'. Pin the definition to one of them in the docstring and tests so callers are not surprised by, for example, zero-width or non-breaking spaces.; State in the plan that `words()` in src/textstats.py cannot be reused for this (it lowercases and splits, which discards character positions), so the new function will iterate characters directly. This is a legitimate reason to not reuse the helper under P-reuse, and the plan should say so rather than leave a reviewer to wonder why.; Add tests in tests/test_textstats.py or a new tests file for: empty string returns 0, all-whitespace returns 0, mixed whitespace (spaces, tabs, newlines) excluded, a non-ASCII letter counted as one, and the non-str input error. Each test documents one promise to the caller.; Do not catch and swallow any exception inside `char_count`. If the plan includes a try/except for non-str input, it must re-raise the specific type rather than return 0, because returning 0 for bad input is a sentinel that hides the caller's mistake.
- Curious Casey: Add tests for `char_count` in `tests/test_textstats.py` (or a new `tests/test_chars.py`) covering the empty string, whitespace-only input, mixed spaces/tabs/newlines, and a multi-line string, since the existing file only asserts `word_count`.; Specify in `src/chars.py` what counts as whitespace (e.g. `str.isspace()` per character, so Unicode spaces like ` ` are excluded), and add a test pinning that choice so the behaviour is explicit rather than implicit.; Per P-errors, make `char_count` raise `ValueError` for non-string input (e.g. `None`, `int`) and add a test asserting that, rather than returning 0 for bad input. Note this departs from Python's usual `TypeError` convention, so a one-line comment in `src/chars.py` explaining why would help future readers.; Reuse check per P-reuse: `words()` in `src/textstats.py` splits on whitespace and lowercases, so it does not fit a character count and should not be used. Say so in the plan so a reviewer does not ask why it was skipped.; Add a brief docstring to `char_count` stating that it counts non-whitespace code points, and update `README.md` (currently one line that only mentions `check.py`) if the library's public functions are meant to be discoverable.; Make sure `check.py` picks up the new module and tests; I did not read `check.py`, `docs/principles.md`, or `.claude/flow.yaml`, so confirm the new test file is run by the check command.

## Audit

Verdict: gaps
- (plan_gap) check.py also runs tools/style.py, which (decoded from its base64 payload) fails the run for any src/*.py other than __init__.py and textstats.py that does not contain the exact line '# registered: yes'. The plan's U1 creates src/chars.py without that line, so check.py would fail even if every test passes. AC5 and U1 should require the line '# registered: yes' in src/chars.py, and the plan should say that check.py must exit 0 overall, not only that the test files run.
- (plan_gap) The non-goal on reuse says not reusing words() is 'the documented reason P-reuse is not applied'. No such documentation exists: docs/principles.md only says P-reuse 'Revisit if: The helper does not fit', and nothing else in the repo records this decision. The departure from P-reuse is defensible under that clause, but the plan should cite the P-reuse 'Revisit if' clause as the justification, or record the decision, instead of claiming an existing documented reason.
Sent back to synthesis once.

## Questions and how each was answered

(no questions were asked)

