# plan

## Goal

Add top_words(text, n) to src/textstats.py and a src/cli.py entry point main(argv) that prints the top words as 'word count' lines, reusing the existing words() helper and following P-errors (bad input raises ValueError). Keep the repo passing python3 check.py.

## Non-goals

- Changing words() or word_count() behaviour.
- Adding a test framework or converting existing tests/test_textstats.py.
- Adding argparse, flags, or a --help option beyond the usage line.
- Changing README unless it documents the API function list; check it and edit only if it does.
- Returning [] for bad input (rejected by P-errors).
- Listing new test files in any unit: tests are written by a separate author. Those tests must follow the tests/test_textstats.py pattern: sys.path bootstrap, bare module-level asserts, runnable as plain scripts with exit code 0, so check.py passes them.

## Acceptance criteria

- AC1: top_words(text, n) returns a list of (word, count) tuples, most frequent first, built on words(text) with no re-tokenizing.
- AC2: Ties in count are ordered alphabetically by word, so output is deterministic; the docstring states this.
- AC3: top_words raises ValueError when text is not a str, and when n is not an int, is a bool, or is not greater than 0. The check happens before words() is called so no AttributeError escapes.
- AC4: Empty or whitespace-only text returns [] without error; n larger than the number of distinct words returns all of them.
- AC5: main(argv) takes a list of two strings [text, n], prints one 'word count' line per result to stdout, and returns 0 on success.
- AC6: main(argv) returns a nonzero code (2) and writes a one-line message to stderr for wrong argument count, non-integer n, or any ValueError from top_words; it never prints a partial result for an error and never swallows an error silently.
- AC7: src/cli.py ends with the exact line '# style: ok' as required by tools/style.py, so python3 check.py passes style.

## Units

### U1: Implement top_words in textstats
Files: `src/textstats.py`
Satisfies: AC1, AC2, AC3, AC4
1. Add def top_words(text, n) after word_count.
2. Docstring: text must be str, n must be int > 0 (bool rejected); returns list of (word, count) most frequent first; ties ordered alphabetically by word; may return fewer than n items; empty text returns []. Raises ValueError on bad input.
3. Check isinstance(text, str) else raise ValueError naming the argument.
4. Check isinstance(n, int) and not isinstance(n, bool) and n > 0 else raise ValueError naming n.
5. Call words(text), count with collections.Counter, return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n].
6. Import Counter from collections at top of file. No new dependency.
7. Keep the existing '# style: ok' final line of src/textstats.py as the last line of the file.

### U2: Add CLI entry point main(argv)
Files: `src/cli.py`
Satisfies: AC5, AC6, AC7
After: U1
1. Create src/cli.py with a module docstring and def main(argv) -> int.
2. Docstring states: argv is [text, n]; n is a positive integer in decimal form; returns 0 on success, 2 on error; errors go to stderr.
3. If len(argv) != 2, write a one-line usage message to stderr ('usage: cli.py TEXT N') and return 2.
4. Parse n with int(argv[1]) inside try; on ValueError write 'n must be a positive integer' to stderr and return 2.
5. Call top_words(argv[0], n) inside try; on ValueError write its message to stderr and return 2.
6. Print each result as f'{word} {count}' to stdout, then return 0. No statistics logic in this file.
7. Add if __name__ == '__main__': raise SystemExit(main(sys.argv[1:])). Import sys and top_words from src.textstats.
8. Make the last line of src/cli.py exactly '# style: ok' (same as src/textstats.py) so tools/style.py passes.

## Interfaces

- `def top_words(text: str, n: int) -> list[tuple[str, int]]` (src/textstats.py)
- `def main(argv: list[str]) -> int` (src/cli.py)

## Test spec

- T1: top_words('a a b', 1) == [('a', 2)] and top_words('a a b', 2) == [('a', 2), ('b', 1)] (counts correct, not just top word). Written as bare asserts after the sys.path bootstrap used in tests/test_textstats.py, runnable as a script. (tests/test_top_words.py)
- T2: Tie order: top_words('b a', 2) == [('a', 1), ('b', 1)] and all-same-count text returns alphabetical order. (tests/test_top_words.py)
- T3: n greater than distinct word count returns all distinct words, e.g. top_words('a b', 10) has two items. (tests/test_top_words.py)
- T4: Empty and whitespace-only text return []. (tests/test_top_words.py)
- T5: Non-str text (None, 5, ['a']) raises ValueError, checked with try/except ValueError and a bare assert that the exception was raised. (tests/test_top_words.py)
- T6: n of 0, -1, True, '2', and 1.5 raises ValueError, checked the same way. (tests/test_top_words.py)
- T7: main(['the quick the', '2']) returns 0 and stdout is exactly 'the 2\nquick 1\n' (captured via contextlib.redirect_stdout). Bootstrap sys.path as in tests/test_textstats.py so 'from src.cli import main' works; bare asserts only. (tests/test_cli.py)
- T8: main(['a b', 'x']) and main(['a b', '0']) return 2, print nothing to stdout, and write a message to stderr (captured via contextlib.redirect_stderr). (tests/test_cli.py)
- T9: main(['a b']) (wrong arg count) returns 2 with usage on stderr. (tests/test_cli.py)

## Advised by

- North Star Nick: NORTH STAR BLIND: nick=no docs/adr directory or ADR index exists in this repo and no north-star scout briefs or in-flight.md/in-flight-issues.md were provided, so ADR compliance, milestone scope, open-issue overlap and open-PR collisions could not be checked.; Settle the bad-input question by docs/principles.md P-errors: top_words should raise ValueError when text is not a str or n is not a positive integer, not return []. The ticket's open question is answered by the project's own principle, so the plan should record that choice rather than leave it open.; Build top_words on the existing words helper in src/textstats.py, as the ticket asks, and do not re-tokenize text in the new code (P-reuse). Any new counting logic belongs next to words, not duplicated in cli.py.; For ties, use a deterministic order (for example sort by (-count, word)) so the output is stable; the ticket allows any stable order, and this keeps the tests reproducible.; src/cli.py is explicitly requested by the ticket, so it is in scope as written; the plan should keep it to argument parsing and printing, with no new statistics logic, and have main(argv) return or print without side effects beyond output so it stays testable.
- Contract Chris: Specify the `top_words(text, n)` contract in its docstring: `text` must be a `str`, `n` must be an `int` greater than 0, the return is a list of `(word, count)` tuples in descending count order, and it may hold fewer than `n` items when the text has fewer distinct words. Callers cannot know any of this from the signature alone.; Validate inputs before calling `words(text)` in src/textstats.py. Today `words` fails on a non-string with AttributeError, which is an undocumented and wrong-typed failure. The plan should check `isinstance(text, str)` and raise ValueError (per P-errors, not TypeError or a quiet empty list), then check `n` and raise ValueError.; Reject `bool` explicitly when checking `n`, because `True` passes `isinstance(n, int)` and would silently act as 1. The plan should state this in the check, not leave it to chance.; Define tie ordering in the docstring, not just in the code. Use `sorted` with key `(-count, first_index)` or `Counter.most_common`, which keeps first-appearance order for ties. Either way the order is a documented promise, and a test should pin it.; Empty text, or text of only whitespace, should return `[]`. This is a valid answer to a valid question, not bad input, so it must not raise. The docstring should say so, which keeps it distinct from the ValueError cases.; In src/cli.py, `main(argv)` should let ValueError from `top_words` propagate or convert it to a clear stderr message with a nonzero return code. It must not catch broad `Exception` and print nothing. Document the return code (0 on success, nonzero on error) in the `main` docstring.; The CLI parses `n` from argv, so `int()` will raise ValueError on non-numeric input. The plan should say whether that surfaces as a user-facing error message, and the docstring should state the accepted format.; Update docstrings, and the README if it documents the API, in the same change. Add tests that pin the raise cases (non-str text, n=0, negative n, n=True, n as a string), the fewer-than-n case, the empty-text case, and the tie order.
- Vera Verifier: In `src/textstats.py`, build `top_words` on the existing `words(text)` helper rather than re-splitting the text, and count with `collections.Counter` (no new dependency), so the P-reuse principle holds.; Make tie order explicit in `top_words`: sort by `(-count, word)` so ties come out alphabetically and the result does not depend on dict insertion order. Then the planned test can assert an exact list rather than a set.; Guard the edges in `top_words`: `n == 0` and `n` larger than the number of distinct words must be specified. Empty text should return `[]` with no error. Desk-check `Counter.most_common(n)` and slicing for `n` larger than the vocabulary, since both return a shorter list rather than failing.; Decide the bad-input behaviour in the plan, and check that `words()` does what the plan assumes. `words()` calls `text.lower()`, so a non-string raises `AttributeError`, not `ValueError`. If P-errors is applied, `top_words` must validate `text` with `isinstance(text, str)` and `n` with `isinstance(n, int) and not isinstance(n, bool) and n > 0` before calling `words`, raising `ValueError` in both cases. Otherwise the plan has two error types for the same kind of mistake.; In `src/cli.py`, `main(argv)` receives strings. Plan the `int()` conversion of `n` so that a non-numeric value raises `ValueError` with a message, and a zero or negative value fails the same way as the library. Test `main` by capturing stdout and asserting the exact lines, e.g. `"the 2\nfox 1\n"`, not by checking only that it did not crash. Assert on the printed text, so a test would fail if the printing were broken.; Tests in `tests/test_textstats.py` are bare module-level `assert` statements that run on import, with the `sys.path` setup at the top. Follow that exact style for the new cases, so a failure shows up when the file is run, and do not add a test framework just for this change. Each new assert should name a case where the result would differ if the behaviour were wrong, e.g. a tie (`top_words("b a", 2) == [("a",1),("b",1)]`), where a naive implementation would give a different order. An assertion like `top_words("a a b", 1) == [("a", 2)]` only proves the top word is returned, not that the counts are right.; Test the n-exceeds-vocabulary case and the all-same-count case, because they are where a `heapq.nlargest` or slicing shortcut could go wrong silently.
- Danielle the Designer: In `src/cli.py`, give the user a clear message when `text` is missing or `n` is not a number or is not positive. Print a one-line error to stderr that names the bad argument and the expected form, then exit non-zero, so a typo does not look like a successful empty result.; In `src/cli.py`, print nothing silently for an empty or all-whitespace text. Either exit with a message such as 'no words found' on stderr or raise the same error the library raises, so the user knows the input was read.; In `src/textstats.py` `top_words`, make the tie order deterministic and easy to explain to users, for example by count descending then word ascending. Document it in the docstring and cover it with a test, so repeated runs of the CLI produce the same output.; Make `main(argv)` accept the arguments as a list and show usage text when they are missing (`-h`/`--help` or no args). Users discover the command by running it, so the first thing they see should explain how to use it.; Reuse `words()` in `top_words` as the ticket asks, and keep the CLI's output format exactly `word count` per line, matching the ticket. Consistent output is easier to pipe into other tools and to scan.

## Audit

Verdict: gaps
- (plan_gap) U2 (src/cli.py) omits the project's style rule. tools/style.py requires every src/*.py file to end with the exact line '# style: ok', and check.py runs it. Without that last line, the new src/cli.py fails `python3 check.py`. Add the step: end src/cli.py with '# style: ok'. The plan's acceptance criteria don't cover this, so add a check for it too.
- (plan_gap) The test units (T1-T9 in tests/test_top_words.py and tests/test_cli.py) don't say how the files must be written. check.py runs each tests/test_*.py as a plain script and fails on a nonzero exit, and the existing tests/test_textstats.py uses bare asserts plus a sys.path insert so that `from src...` imports work. The plan should require the new test files to follow that same pattern (sys.path bootstrap, bare asserts, runnable as scripts). Otherwise they pass under pytest-style thinking but fail under check.py, or fail to import src.cli at all.
Sent back to synthesis once.

## Questions and how each was answered

(no questions were asked)

