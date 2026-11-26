# plan

## Goal

Add a dependency-free `char_count(text)` library function in a new `src/chars.py` that returns how many characters in `text` are not whitespace.

## Non-goals

- No CLI wiring or reuse of CLI code.
- No changes to src/textstats.py or words(); words() lowercases and splits, so it does not fit a per-character count and no helper is reused. This is a deliberate exception to P-reuse for this ticket.
- No README or docs changes (ticket does not ask; can be a follow-up).
- No new dependencies.
- No try/except that swallows errors and returns a sentinel.

## Acceptance criteria

- AC1: char_count(text) returns the number of code points in text for which str.isspace() is False.
- AC2: char_count('') and whitespace-only strings return 0 without raising.
- AC3: char_count raises ValueError for any non-str argument (None, bytes, int, list), per P-errors. It never returns a value for bad input.
- AC4: The module has no imports beyond the standard library's built-ins and no dependencies are added.

## Units

### U1: Implement char_count in src/chars.py
Files: `src/chars.py`
Satisfies: AC1, AC2, AC3, AC4
1. Create src/chars.py.
2. Define `def char_count(text: str) -> int:` with a docstring stating the contract: counts code points for which str.isspace() is false; returns 0 for empty or all-whitespace input; raises ValueError if text is not a str; whitespace means str.isspace() (Unicode whitespace such as U+00A0 and U+3000 is excluded; zero-width space U+200B is counted).
3. At the top of the function, `if not isinstance(text, str): raise ValueError(...)` with a message naming the received type.
4. Return `sum(1 for ch in text if not ch.isspace())`.
5. Do not add try/except, imports of third-party packages, or any call into src/textstats.py.

## Interfaces

- `def char_count(text: str) -> int` (src/chars.py)

## Test spec

- T1: char_count('a b') == 2 (ASCII space excluded). (tests/test_chars.py)
- T2: char_count('a\tb\nc') == 3 (tab and newline excluded). (tests/test_chars.py)
- T3: char_count(' x') == 1 (NBSP is whitespace under str.isspace). (tests/test_chars.py)
- T4: char_count('​x') == 2 (zero-width space is not whitespace, so it is counted). (tests/test_chars.py)
- T5: char_count('') == 0. (tests/test_chars.py)
- T6: char_count(' \n\t　') == 0 (whitespace-only input returns 0, does not raise). (tests/test_chars.py)
- T7: char_count(5), char_count(None), char_count(b'abc') each raise ValueError specifically; the test uses try/except ValueError with an explicit raise AssertionError in the else path, not a broad except Exception. (tests/test_chars.py)
- T8: Multi-character text with mixed letters, digits, and punctuation counts each non-whitespace code point (e.g. 'Hi, there!\n' == 9). (tests/test_chars.py)

## Advised by

- North Star Nick: NORTH STAR BLIND: nick=scout briefs north-star-adrs.md north-star-docs.md north-star-in-flight.md in-flight.md and in-flight-issues.md were not provided, and no docs/adr/ index or local override exists in the repo, so I could not check ADRs, milestone scope or open issues/PRs; the only strategic sources I could read were README.md, docs/principles.md and src/textstats.py.; **DRIFT/QUESTION, MEDIUM**: docs/principles.md P-reuse says new code must live in or reuse src/textstats.py ('One helper per idea'), and textstats.py is the library's only stats module (it already holds word_count). The plan puts char_count in a new src/chars.py, which splits the stats surface across two modules. Either place char_count in src/textstats.py beside word_count, or state explicitly that the new module is an intentional exception to P-reuse.; Make char_count raise ValueError for non-str input, per docs/principles.md P-errors ('Bad input raises', 'never returns an empty or sentinel value'). Do not coerce None or bytes to 0. Use ValueError, not TypeError, so the rule the project chose is the one followed.; Define 'not whitespace' with str.isspace() so it matches the existing split()-based whitespace notion in textstats.words, and add a test in tests/test_textstats.py alongside word_count's tests rather than a separate test file, keeping one place for stats behaviour.; Keep the change scoped to the ticket: one function, no CLI wiring, no new dependencies (README and principles say 'no dependencies'). Confirm this is in the current milestone before merging; the repo has no milestone document I could read.
- Contract Chris: Specify the contract in the `src/chars.py` docstring: `char_count(text: str) -> int` counts code points for which `str.isspace()` is false, and returns 0 for the empty string. Write what callers can rely on, not how the loop is written.; State the non-str precondition explicitly. `char_count(None)` or `char_count(b'abc')` must either raise ValueError (per P-errors) or TypeError; the plan should pick one, document it in the docstring, and add a test for it. Do not let it fall through to an AttributeError from `.isspace()` or `iter()`.; Define 'whitespace' in the docstring. Using `str.isspace()` means Unicode whitespace (e.g. U+00A0, U+3000) is excluded; combining marks and zero-width joiners are counted as characters. Callers need to know this to predict results.; Do not wrap the implementation in a try/except that returns 0 or None on failure. A silenced error here would turn bad input into a plausible-looking count.; P-reuse: `textstats.words()` lower-cases its input and splits on whitespace, so it does not fit a per-character count. The plan should say explicitly that no helper is reused and why, so the reviewer doesn't read it as a P-reuse violation. Do not change `words()` to serve this.; Tests in `tests/test_textstats.py` use bare asserts and import from `src.textstats`. Add the new cases (empty string, all-whitespace, mixed whitespace/newlines, non-str rejection) in a matching style, importing from `src.chars`, and cover the rejection path, not only the happy path.; The docs/README should get a one-line entry for `char_count` and its error behaviour, since this is a public library function and its contract is otherwise only in the code.
- Vera Verifier: Write tests/test_chars.py in the same plain-assert style as tests/test_textstats.py (sys.path insert, then `from src.chars import char_count`, bare `assert` lines), so it runs under the same harness with no new dependency.; Pin the whitespace definition with inputs that separate the candidate implementations: `'a b'` gives 2, `'a\tb\nc'` gives 3, `' x'` gives 1 (NBSP is whitespace under str.isspace), and `'​x'` gives 2 (zero-width space is not whitespace). Without these, a `split()`-based and an `isspace()`-based implementation can both pass the simple cases while disagreeing on Unicode.; Test that empty string and whitespace-only strings return 0 (`char_count('') == 0`, `char_count(' \n\t') == 0`). These are valid inputs under P-errors, so they must not raise; a test that only checks the empty string can't tell apart an implementation that wrongly raises on 'nothing to count'.; For bad input, assert the specific exception type, not a broad catch. Write something like `try: char_count(5)` / `except ValueError: ok` with an explicit `raise AssertionError` in the else path. A `except Exception` form would pass on the TypeError that a naive `for c in text` loop raises for an int, which hides exactly the P-errors violation the test should catch.; Add a mutation-style check for each assertion: `char_count('a b')` must be 2, not 3. If the implementation counted all characters or forgot to filter, the assertion fails; if it counted only ASCII letters, the NBSP and tab cases catch it. Each assertion should discriminate between at least one plausible wrong implementation.; Do not reuse `words()` from src/textstats.py for this. It lowercases and splits, which discards the character positions that char_count needs, so P-reuse does not fit here; the plan should say so explicitly so a reviewer does not flag the absence of reuse as a defect.

## Audit

Verdict: pass

## Questions and how each was answered

(no questions were asked)

