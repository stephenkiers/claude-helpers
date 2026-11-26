# plan

## Goal

Add src/longest_cli.py whose main(argv) prints the longest word of a single text argument (via longest_word from src/longest.py) and returns 0. Bad input (wrong argument count, or text with no words) raises ValueError, per P-errors.

## Non-goals

- Reading text from stdin or from files.
- Stripping punctuation or changing case in the CLI (output matches longest_word exactly).
- Changing longest_word or its docstring.
- Adding a 'Longest word:' label or any other decoration to stdout.
- Catching ValueError inside main or converting it to an exit code.
- Adding a packaging entry point, setup.py console script, or README change.
- Calling sys.exit inside main (only the __main__ block exits).

## Acceptance criteria

- AC1: main(['hello world']) returns 0 and writes exactly the longest word plus a newline to stdout, with nothing on stderr.
- AC2: The printed word is exactly what longest_word returns: lower-cased, punctuation attached, first occurrence wins ties (e.g. 'Hello, world!' prints 'hello,').
- AC3: When the text argument has no words (empty or whitespace-only), main lets the ValueError from longest_word escape, writes nothing to stdout, and returns nothing (no exit code is produced by main).
- AC4: When main receives zero or more than one argument, it raises ValueError and does not print anything.
- AC5: longest_cli.py uses longest_word from src.longest and does not call src.textstats.words or re-split text itself (P-reuse).
- AC6: The module and main docstrings state the argv contract (list of user arguments without the program name), the return value (0 on success), that stdout holds only the word, that ValueError escapes for blank input and wrong argument count, and that words are lower-cased with punctuation attached as longest_word returns them.
- AC7: Running the module as a script with one argument prints the longest word and exits 0; with bad input it exits non-zero via the uncaught exception.

## Units

### U1: Implement longest_cli.main
Files: `src/longest_cli.py`
Satisfies: AC1, AC2, AC3, AC4, AC5, AC6, AC7
1. Create src/longest_cli.py starting with a module docstring that documents: argv is the list of user arguments without the program name; success prints the longest word to stdout and returns 0; a blank or whitespace-only argument raises ValueError from longest_word; a wrong argument count raises ValueError; words are lower-cased and punctuation stays attached as longest_word returns them.
2. Add the marker line '# registered: yes' under the module docstring to match src/longest.py.
3. Import longest_word with 'from src.longest import longest_word'.
4. Define main(argv: list[str]) -> int. If len(argv) != 1, raise ValueError('expected exactly one text argument, got %d' % len(argv)).
5. Otherwise word = longest_word(argv[0]) (no try/except; the ValueError from blank input propagates). Print word to stdout with print(word) and return 0.
6. Add an if __name__ == '__main__': block that does import sys and calls sys.exit(main(sys.argv[1:])). An uncaught ValueError there exits non-zero with a traceback.
7. Ensure the main docstring lists the same contract as the module docstring.

## Interfaces

- `def main(argv: list[str]) -> int` (src/longest_cli.py)
- `def longest_word(text: str) -> str  (existing; raises ValueError when text has no words)` (src/longest.py)

## Test spec

- T0: The test file begins with the same sys.path header as tests/test_longest.py (lines 1-4): insert the repo root (Path(__file__).resolve().parent.parent) at sys.path[0] before importing src, so it runs as a standalone script from check.py and from the command line. (tests/test_longest_cli.py)
- T1: main(['hello world']) returns 0 and stdout is 'hello\n'; stderr is empty. (tests/test_longest_cli.py)
- T2: main(['Hello, world!']) returns 0 and stdout is 'hello,\n', showing punctuation stays attached and case is lowered. (tests/test_longest_cli.py)
- T3: main(['a bb cc']) returns 0 and prints 'bb' (first occurrence of the longest word wins ties). (tests/test_longest_cli.py)
- T4: main(['']) raises ValueError and writes nothing to stdout. (tests/test_longest_cli.py)
- T5: main(['   ']) raises ValueError and writes nothing to stdout. (tests/test_longest_cli.py)
- T6: main([]) raises ValueError and writes nothing to stdout. (tests/test_longest_cli.py)
- T7: main(['a', 'b']) raises ValueError and writes nothing to stdout. (tests/test_longest_cli.py)
- T8: The test file is runnable as a script (python tests/test_longest_cli.py) and exits 0 when all checks pass, matching how check.py runs tests/test_*.py. (tests/test_longest_cli.py)

## Advised by

- North Star Nick: NORTH STAR BLIND: nick=no scout briefs, in-flight.md, in-flight-issues.md or docs/adr index were provided and no docs/adr/ directory exists in the repo, so ADR compliance and open-issue overlap (including #2 and any PR touching src/longest.py) could not be checked. Nothing below asserts that no conflicts exist.; Reuse longest_word from src/longest.py exactly as the ticket says, and do not re-tokenise text in longest_cli.py; this satisfies P-reuse and keeps the tie-break (first occurrence) and lower-casing defined in one place.; The ticket depends on #2, and commit 9d0616e (longest_word) is already on this branch, so the dependency looks satisfied locally. Confirm #2 is closed or merged before the PR is opened, so the CLI does not land against a helper that is still in review.; Specify the no-words case explicitly in the plan. longest_word raises ValueError for text with no words (for example an empty or whitespace-only argument), but P-quiet says an empty result prints nothing extra and succeeds. The plan must choose one, and the ticket's 'returns 0' only covers the success path.; Keep stdout for the answer only. Any error message should go to stderr, per P-quiet's 'standard error is for errors only', so the output stays safe to pipe.; If the new module needs the '# registered: yes' marker that src/longest.py carries, or a check.py entry, the plan should include that. I did not read check.py, so this is unverified.
- Contract Chris: src/longest_cli.py main(argv): the docstring must state the argv contract: argv is the list of user arguments without the program name, exactly one element is required, and the return value is an int exit status. The plan should say this explicitly, because a caller cannot tell from the ticket whether argv includes sys.argv[0].; Wrong argument count (zero or two or more arguments) is an unstated precondition. The plan should pick one behaviour (raise ValueError, consistent with P-errors, or return a non-zero usage code) and document it in main's docstring and in a test.; Empty or whitespace-only text: longest_word in src/longest.py raises ValueError (T6, T7). The ticket says main 'returns 0', which would silently swallow that. The plan must not catch ValueError and return 0. Either let it propagate or catch it, print a message to stderr, and return a non-zero code, and document which one.; Only report success on the success path. Any printed result goes to stdout; any failure message goes to stderr, never to stdout, so output stays safe to pipe (P-quiet).; Reuse longest_word from src/longest.py (P-reuse). Do not re-split the text in the CLI or call src.textstats.words directly, so the tie rule and lower-casing stay defined in one place.; Docstring policy: the new main and module docstring must list the return codes, what is written to stdout and stderr, and which exceptions can escape. Update the longest_word docstring only if its contract changes; the plan currently implies no change.; Add tests/test_longest_cli.py covering: one normal word, a punctuated word, an empty string, a whitespace-only string, zero arguments, and two arguments. Assert the return value and the stdout/stderr split for each. The existing tests cover only longest_word, so the CLI's failure modes are currently untested.; The non-str branch in longest_word is unreachable from the CLI, since argv items are always str. The plan does not need to exercise it through main, but it should not describe main as handling non-str input.
- Danielle the Designer: In `src/longest_cli.py`, wrap the `longest_word` call so the `ValueError` for empty or no-word input becomes a one-line message on stderr with a non-zero return. The ticket says "returns 0", but that only holds for the success path. Otherwise a user who types `longest_cli.py "   "` sees a traceback or silence.; Give a clear usage message on stderr and a non-zero exit when the argument is missing or extra arguments are passed (e.g. `usage: longest_cli.py TEXT`). Do not let argparse-style defaults silently read stdin or print nothing.; Print exactly the word and a newline to stdout, with no labels or extra decoration, so the output stays safe to pipe (P-quiet). Do not add a 'Longest word:' prefix unless the plan explicitly wants it for humans.; Document in the CLI help or module docstring that words are lower-cased and punctuation is kept (e.g. `world,` not `world`), because `longest_word` returns them that way. Users will otherwise read the output as a bug. Pick one: document it or strip punctuation in the CLI, and say which in the plan.; Reuse `longest_word` from `src/longest.py` and do not call `words()` directly in the CLI (P-reuse). That keeps the tie-breaking rule (first occurrence) consistent between the library and the command.; Add a test that runs `main` with an empty-ish argument and with a normal sentence, and checks stdout, stderr and the return code. Users hit the error path more often than the happy path, so it needs coverage.
- Ariadne: In `src/longest_cli.py`, wrap the `longest_word(argv[0])` call in a `try/except ValueError` so bad input (empty or whitespace-only argument, which the library rejects) is reported on stderr with a nonzero return code. Do not let it escape as a traceback, and keep the success path returning 0 as the ticket says.; Validate the argument count before calling the library: if `len(argv) != 1`, write a usage message to stderr and return a nonzero code. This keeps the invalid-argument path from reaching `longest_word` at all.; Print to stdout only on the success path. On every error path stdout must stay empty, so the output stays safe to pipe (P-quiet).; Make `main(argv)` return the exit code and do not call `sys.exit` inside it. Put the `sys.exit(main(sys.argv[1:]))` call under `if __name__ == "__main__":` so tests can call `main` directly and check its return value.; Import `longest_word` from `src.longest` and do not re-split or re-lower the text in the CLI. The CLI should only handle argument checking, output, and exit codes (P-reuse).; Add tests for each path: one argument returns 0 and prints the word, empty and whitespace-only arguments return nonzero with stderr output and nothing on stdout, and zero or two-plus arguments return nonzero. The current `tests/test_longest.py` covers only the library, so no test exercises the CLI yet.

## Audit

Verdict: gaps
- (plan_gap) T8 / tests/test_longest_cli.py: the plan never says the test must put the repo root on sys.path. `python tests/test_longest_cli.py` puts tests/ on sys.path, not the root, so `from src.longest import longest_word` and `from src.longest_cli import main` fail with ModuleNotFoundError. tests/test_longest.py lines 1-4 do `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` before importing src. The plan should require the same header, because check.py runs each tests/test_*.py as a subprocess and T8 cannot pass without it.
- (unasked_question) Blank input behaviour (AC3, T4, T5, and the U1 step 'blank input writes an error line to stderr and returns 1') departs from P-errors without a recorded decision. longest_word raises ValueError on blank text (src/longest.py lines 18-19), and P-errors says bad input raises. The plan catches that in main and returns 1, while wrong argument count (AC4) does raise. The plan does not say why the two cases differ, and the ticket only says 'takes one text argument' and 'returns 0'. See question Q1.
Sent back to synthesis once.

## Questions and how each was answered

- Q1: When the text argument is empty or whitespace-only, should longest_cli.main print an error to stderr and return 1, or return 0 with no output as the ticket literally says? -> Let ValueError escape main (decided by principle P-errors; A function given bad input raises ValueError. It never returns an empty or sentinel value to signal bad input.; P-errors says bad input raises ValueError and must never return an empty or sentinel value to signal it, which rules out silent-zero and rules out stderr-plus-return-1 as a non-raising signal; letting the ValueError from longest_word escape main is the only option that raises.)

