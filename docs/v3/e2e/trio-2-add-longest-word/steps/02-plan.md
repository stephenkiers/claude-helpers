# plan

## Goal

Add src/longest.py with longest_word(text), returning the longest word in text as produced by the existing words() helper, with the owner's decisions applied: blank or non-string input raises ValueError; ties go to the first word in text order; the return is lower-cased; punctuation stays attached to words.

## Non-goals

- Changing words() in src/textstats.py in any way (including its lower-casing and whitespace-only splitting).
- Stripping or otherwise treating punctuation specially.
- Returning original-case words.
- Adding a CLI command, exporting from src/__init__.py, or adding any dependency.
- Adding a pytest or other test framework; tests use plain asserts like tests/test_textstats.py.
- Changing README.md or docs.

## Acceptance criteria

- AC1: longest_word(text) returns the longest whitespace-separated word from words(text), lower-cased, with punctuation kept as part of the word.
- AC2: When two or more words tie for longest, the one that appears first in the text is returned.
- AC3: When text has no words (empty or whitespace-only), longest_word raises ValueError.
- AC4: When text is not a str, longest_word raises ValueError rather than an AttributeError from inside words().
- AC5: longest_word reuses words() from src/textstats.py and adds no imports beyond it and no new helpers.

## Units

### U1: Implement longest_word in src/longest.py
Files: `src/longest.py`
Satisfies: AC1, AC2, AC3, AC4, AC5
1. Create src/longest.py.
2. Import only `words` from src.textstats (follow the import style used by existing modules and tests; check tests/test_textstats.py for how it imports).
3. Define `def longest_word(text: str) -> str:` with a docstring stating: input is a str; returns the longest word as words() produces it (lower-cased, punctuation attached); ties resolved by first occurrence in text; raises ValueError if text is not a str or contains no words.
4. If not isinstance(text, str), raise ValueError with a clear message.
5. Set ws = words(text). If ws is empty, raise ValueError with a clear message saying the text contains no words.
6. Return max(ws, key=len). max returns the first maximal element, which gives the first-in-text tie rule; add a one-line comment saying so.
7. Do not modify src/textstats.py.

## Interfaces

- `def longest_word(text: str) -> str` (src/longest.py)

## Test spec

- T1: longest_word('the quick brown fox') == 'quick' (single longest word is returned). (tests/test_longest.py)
- T2: Mixed case is lower-cased: longest_word('Hello World') == 'hello' (AC1). (tests/test_longest.py)
- T3: Punctuation is kept: longest_word('hi world, friend') == 'world,' (AC1). (tests/test_longest.py)
- T4: Tie returns the first in text order: longest_word('cat dog bird fish') == 'bird' and longest_word('ab cd') == 'ab' (AC2). The tie test must fail if the rule were alphabetical or last-wins. (tests/test_longest.py)
- T5: Longest word is neither first nor last: longest_word('a longestword b') == 'longestword' (AC1). (tests/test_longest.py)
- T6: Empty string raises ValueError: try/except ValueError around longest_word(''), with an assert that the exception was raised (AC3). Use a flag variable, not a test framework. (tests/test_longest.py)
- T7: Whitespace-only string ('   \n\t ') raises ValueError (AC3). (tests/test_longest.py)
- T8: Non-str input such as None or 42 raises ValueError (AC4). (tests/test_longest.py)

## Advised by

- North Star Nick: NORTH STAR BLIND: nick=the scout briefs (north-star-adrs.md, north-star-docs.md, north-star-in-flight.md), in-flight.md and in-flight-issues.md were not provided, and this worktree has no docs/adr/ directory, so no ADR, open issue, epic or PR could be checked for overlap or drift.; Build src/longest.py on the existing words() helper from src/textstats.py (P-reuse) and add no new imports or dependencies, which fits the library's stated goal of having no dependencies.; Keep the signature as the ticket states: longest_word(text) taking one str. Nothing in README.md or docs/principles.md suggests a second parameter.; The README and docs/principles.md name no milestone, so scope cannot be checked from here; treat the ticket as the scope.; Pin both owner decisions in the plan before coding: the no-words case and the tie-break rule. Do not settle either silently, since the ticket reserves them for the owner.; Call out the casing consequence in the plan: words() lower-cases its input, so longest_word would return a lower-cased word unless the plan reads the original tokens. The ticket does not say which the owner wants.
- Contract Chris: Document the return contract in `longest_word`'s docstring: `words()` lower-cases and splits on whitespace only, so the returned word is lower-cased and punctuation stays attached ("end." has length 4). Callers must be told this, either in the docstring or by the plan deciding to return the original-case token.; Make the empty-text behaviour an explicit, documented branch in `src/longest.py`, whichever the owner picks: raise `ValueError` (matching P-errors) or return `""` with a docstring stating that it is a valid answer. Do not let it fall out implicitly from `max(words(text), default="")` without a comment saying the caller gets the empty string on purpose.; The tie-break rule must be written into the docstring and covered by a test in `tests/test_textstats.py` style (plain asserts). Under `max(..., key=len)` the first word wins; an alphabetical rule needs a different key, so the plan should name the exact rule, not just 'longest'.; Do not reimplement tokenising inside `src/longest.py`; call `words` from `src/textstats.py` as the ticket and P-reuse say. Note that `words` filters empty strings, so no extra guard is needed for them.; Plan a docstring for the new public function stating inputs (str), outputs, and the failure mode for non-str input. Non-string input currently fails inside `words` with an AttributeError; the contract should either state that or check and raise `TypeError`/`ValueError` explicitly.; Plan a test for the empty and whitespace-only cases and a test for the tie case, so both contract clauses are pinned rather than only documented.
- Vera Verifier: In src/longest.py, do not return `max(words(text), key=len)` directly without checking the empty case: `words()` returns `[]` for blank text, so `max` raises a bare ValueError from inside `max`. The plan should name the empty-input branch explicitly and raise a ValueError with a clear message, matching P-errors, unless the owner decides the answer is ''.; Note that `words()` lower-cases its output, so `longest_word('Hello')` would return 'hello'. The plan should state whether the returned word keeps its original casing, which means either not reusing `words()` for the return value or recovering the original token; reusing the helper (P-reuse) makes the lowercased form the default.; Specify tie-breaking exactly in terms of the code. `max(..., key=len)` returns the first maximal element, so 'first in text' is the natural result. 'Alphabetically first' would need `key=lambda w: (-len(w), w)`, and that comparison runs on the lowercased words from `words()`. Whichever the owner picks, the plan should say which comparison is used.; Define what counts as a word. `split()` only splits on whitespace, so 'hello,' is 6 characters including the comma and 'a-b' is one word. The plan should say whether punctuation is stripped before measuring length, since this changes which word wins.; For tests in tests/test_textstats.py style (bare module-level asserts, no pytest), the plan should add asserts for: a tie case that pins the chosen tie rule, a case where the longest word is not the first or last, the empty/whitespace-only case that pins the chosen empty behaviour, and a mixed-case case that pins the casing behaviour. Each assert should be one that would fail if the tie or casing rule were flipped.; Put the empty-input and tie assertions in tests with `pytest.raises` or explicit equality against the expected string, not just `assert longest_word(x)`, because a truthy return would pass a broken implementation. Also note that bare `assert` statements are stripped under `python -O`, so if the project runs tests that way the checks silently pass.
- Business Beth: Ship the smallest version: one function in `src/longest.py` that takes `words(text)` and picks the max, with tests in `tests/`. Skip any extra helpers, options, or CLI until a user asks for them.; Before coding, confirm what the returned word should look like. `words()` in `src/textstats.py` lowercases its output, so `longest_word("Hello")` would return `hello`, not the word as the user wrote it. If the caller wants the original casing, the plan has to say how to get it without re-implementing `words()` (P-reuse).; Decide how punctuation counts. `words()` splits on whitespace only, so `"done."` has length 5 and `"world,"` counts as a longer word than `"world"`. The plan should state whether that is acceptable for a first release or whether the result should strip punctuation.; Use the tie rule the owner picks and test it explicitly, including a tie case and a case with repeated longest words. Keep the implementation simple: for the first-in-text rule, `max()` over the list already returns the first maximal element.; Write down how success will be checked after release, for example whether anyone calls `longest_word` in the project or whether the docs example works. Without that, nobody will know if the feature is used or useful.

## Audit

Verdict: pass

## Questions and how each was answered

- empty-text: When the text contains no words (empty or whitespace-only), should longest_word raise ValueError or return the empty string? -> Raise ValueError (decided by human; blank text is bad input)
- tie-break: When two words tie for longest, should the first one in the text win, or the alphabetically first one? -> First in text order (decided by human)
- casing: Should longest_word return the word in its original casing, or the lower-cased form that words() produces? -> Return the lower-cased word (decided by human)
- punctuation: Should punctuation count toward a word's length, so that 'done.' has length 5 and beats 'hello' only when it is longer? -> Keep punctuation as part of the word (decided by human)

