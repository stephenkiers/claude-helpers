# fix

## Given

- Finding r0-1 [low] n parsing is looser than the documented 'positive integer in decimal form' at src/cli.py:18
  Problem: int(argv[1]) accepts surrounding whitespace, a leading '+', and underscore digit separators (for example ' 3', '+3', '1_0' all parse). The module docstring says n is a positive integer in decimal form, so the parser accepts more than the contract states. It does not create a vulnerability: argv is a local CLI argument, errors are caught, and no input is echoed back. It is still an input-validation gap at the boundary, and the inconsistency could surprise callers or scripts that rely on strict parsing.
  Fix: Validate n strictly before converting: in src/cli.py, reject argv[1] unless re.fullmatch(r'[0-9]+', argv[1]) matches (add `import re`), using the existing 'n must be a positive integer' stderr message and return 2. Keep int() and the n > 0 check in top_words. Merged with r0-13 and r0-16.
- Finding r0-2 [medium] Direct script invocation fails: `from src.textstats import top_words` needs the repo root on sys.path at src/cli.py:6
  Problem: Flow `python src/cli.py TEXT N` (cli.py:33 __main__ block) -> Python sets sys.path[0] to the src/ directory, not the repo root -> cli.py:6 `from src.textstats import top_words` raises ModuleNotFoundError: No module named 'src' before main() runs. src/__init__.py exists, so `src` is only importable when the root is on sys.path, which happens with `python -m src.cli` from the root or when tests insert the root path manually. The __main__ block therefore advertises an entry point that cannot be started the way its own usage line implies. The tests pass only because they insert the root path themselves, so this is not caught by the test suite.
  Fix: Make `python src/cli.py TEXT N` work by adding a sys.path bootstrap at the top of src/cli.py, before the `from src.textstats import top_words` import: insert the repo root (Path(__file__).resolve().parent.parent) into sys.path, as tests/test_cli.py does. Do not add a test file; the separate test author owns that.
- Finding r0-3 [low] Two different error messages for the same mistake (invalid N) at src/cli.py:20
  Problem: A non-numeric N ('x') prints 'n must be a positive integer' from the CLI's own int() handler, but N='0' or '-1' falls through to top_words and prints 'n must be an int greater than 0'. Both are the same user error, so the user sees different wording depending on which kind of bad input they typed. This is a small usability inconsistency that could confuse someone scripting against the tool, and tests only check that stderr is non-empty, so nothing would catch it.
  Fix: Use one wording for the invalid-n rule. In src/textstats.py top_words, change the n check's ValueError message from 'n must be an int greater than 0' to 'n must be a positive integer', matching the CLI's int() failure message in src/cli.py. Merged with r0-8 and r0-17.
- Finding r0-7 [medium] Direct script invocation fails on the package import at src/cli.py:32
  Problem: Desk-check of the entry point: `if __name__ == "__main__"` calls main(sys.argv[1:]), but `from src.textstats import top_words` (line 8) only resolves if the repo root is on sys.path. Running `python src/cli.py a 2` puts `<repo>/src` at sys.path[0], not the repo root, so `import src` raises ModuleNotFoundError before main() runs. Only `python -m src.cli` from the repo root works. The tests do not catch this because tests/test_cli.py inserts the repo root into sys.path itself, which masks the problem. The usage string 'usage: cli.py TEXT N' suggests direct invocation is the intended interface.
  Fix: Same defect as r0-2. Apply the same sys.path bootstrap in src/cli.py before the package import so the script runs directly. Merged with r0-2; no separate test file.
- Finding r0-8 [medium] Same invalid-n error gets two different messages at src/cli.py:20
  Problem: A non-numeric n ('x') prints 'n must be a positive integer' from cli.py:20, but '0' or '-1' prints 'n must be an int greater than 0' from textstats.top_words (src/textstats.py, the n check in top_words). The user is told the same thing two different ways depending on what they typed, and the wording also differs from the CLI's own usage text. Users comparing messages will wonder whether these are two separate rules.
  Fix: Same change as r0-3: make the top_words n-check message in src/textstats.py read 'n must be a positive integer', identical to src/cli.py. Merged with r0-3 and r0-17.
- Finding r0-10 [low] Unquoted multi-word text fails with only a usage line at src/cli.py:14
  Problem: The CLI requires exactly two argv entries, so 'cli.py the quick fox 2' fails with the usage line and gives no hint that the text must be quoted. The error does not say how many arguments were received or which one looks wrong. Users who forget the quotes get a message that does not point at the cause.
  Fix: Extend the usage line in src/cli.py main() to 'usage: cli.py TEXT N (quote TEXT if it contains spaces)'. Do not change argv handling or add flags.
- Finding r0-12 [low] Docstring does not state that returned words are normalised at src/textstats.py:14
  Problem: The contract says Most frequent words of text, but the returned words come from words(text), which lower-cases and tokenises the input. A caller passing 'The the' gets ('the', 2), and cannot tell from the docstring that the case is folded or what counts as a word. The guarantee exists only in the implementation of words().
  Fix: Extend the top_words docstring in src/textstats.py: state that returned words are as produced by words(), i.e. lower-cased, so the returned strings are the normalised forms rather than original spellings.
- Finding r0-13 [low] n parsing accepts more than the documented 'decimal form' at src/cli.py:4
  Problem: The module docstring promises n is a positive integer in decimal form. int() also accepts surrounding whitespace (' 2 '), a leading '+', and underscores ('1_0' gives 10). So the documented input contract and the real accepted set disagree. Callers relying on the docstring will not get the rejection they expect for these inputs.
  Fix: Same change as r0-1: enforce the documented decimal-form contract with re.fullmatch(r'[0-9]+', argv[1]) before int() in src/cli.py, so ' 2', '+2', '1_0' and non-ASCII digits are rejected with return 2. Merged with r0-1 and r0-16.
- Finding r0-14 [low] main() docstring omits its return-code and error contract at src/cli.py:10
  Problem: main's docstring describes output only. The return codes (0 success, 2 error), the stderr channel, and the usage message on bad argv live only in the module docstring. A caller or test reading main's docstring alone does not learn that a return value is always 0 or 2, or that it never raises for bad input. Error messages for n also differ: a non-integer gives 'n must be a positive integer', while n=0 gives 'n must be an int greater than 0', which is a message inconsistency the contract does not mention.
  Fix: Expand the main() docstring in src/cli.py with a Returns section: 0 on success, 2 on any usage or input error; errors go to stderr and nothing is raised. The n message consistency is handled by the r0-3 fix.
- Finding r0-16 [low] n parsing accepts more forms than the documented 'positive integer in decimal form' at src/cli.py:16
  Problem: Path: entry → len(argv)==2 → int(argv[1]) succeeds for ' 2', '+2', '1_0' (→ 10), or non-ASCII digits such as '٢' → top_words(argv[0], n) runs normally → exit 0.
Expected: per the module docstring, n is a positive integer in decimal form; inputs outside that form should be rejected with return 2.
Actual: int() applies Python's lenient literal rules (surrounding whitespace, explicit '+' sign, underscore digit separators, Unicode decimal digits), so these inputs are accepted and produce output. The error paths themselves are correct: a ValueError from int() and a ValueError from top_words() both return 2, and no path performs a side effect before returning 2. The issue is only that the accepted-input set is wider than the stated contract. Impact is small, since the value is still a valid positive int.
  Fix: Same change as r0-1: add the re.fullmatch(r'[0-9]+', argv[1]) pre-check in src/cli.py before int(), rejecting mismatches with the existing message and return 2. Merged with r0-1 and r0-13.
- Finding r0-17 [low] Two wordings for the same 'n must be positive' rule at src/cli.py:18
  Problem: src/cli.py:18 reports a non-integer n as 'n must be a positive integer'. src/textstats.py (top_words, the n check) reports the same rule as 'n must be an int greater than 0'. The CLI forwards n=0 or n=-1 to top_words, so the user sees 'n must be an int greater than 0' for '0' but 'n must be a positive integer' for 'x'. Both cases are the same rule, and the message depends on which layer rejects the value first.
  Fix: Same change as r0-3: align the top_words message in src/textstats.py with the CLI wording 'n must be a positive integer'. Merged with r0-3 and r0-8.

## Did

- a1-1: commit 144247871d8a3e8ceacb4ded510ea532e1a9f32d. All listed review findings are fixed, in src/cli.py and src/textstats.py only. src/cli.py: added a sys.path bootstrap so `python src/cli.py TEXT N` resolves the src package (r0-2, r0-7); n must fullmatch [0-9]+ before int() so ' 2', '+2', '1_0' and non-ASCII digits are rejected with return 2 (r0-1, r0-13, r0-16); usage line now says to quote TEXT when it contains spaces (r0-10); main() docstring gained a Returns section (r0-14). src/textstats.py: the top_words n-check message is now 'n must be a positive integer', matching the CLI (r0-3, r0-8, r0-17); the docstring states returned words are normalised as produced by words() (r0-12). No new files were added and the protected test files were not touched. The project's check was not run, per instructions.

- Attempt 1: addressed r0-1, r0-2, r0-3, r0-7, r0-8, r0-10, r0-12, r0-13, r0-14, r0-16, r0-17; not addressed none.

## Fixed

- r0-1
- r0-2
- r0-3
- r0-7
- r0-8
- r0-10
- r0-12
- r0-13
- r0-14
- r0-16
- r0-17

## Left open

(none)

## Given

- Finding r1-1 [low] Unreachable try/except ValueError after regex validation at src/cli.py:30
  Problem: The new re.fullmatch(r"[0-9]+", argv[1]) check at line 27 guarantees argv[1] is ASCII digits, so int(argv[1]) at line 31 cannot raise ValueError. The except block at lines 32-34 is dead code and its message duplicates the one at line 28. It reads as two validation paths for one condition, which invites drift when one message changes and not the other.
  Fix: Remove the re.fullmatch regex check and its message in main(). Keep one path: wrap int(argv[1]) in try/except ValueError, print 'n must be an int greater than 0' to stderr, and return 2. top_words() stays the sole owner of the n > 0 rule, so n=0 and negatives are reported by the existing generic handler with the same message. Delete the unreachable branch.
- Finding r1-4 [low] Unreachable except branch after regex validation at src/cli.py:31
  Problem: The fullmatch on r'[0-9]+' at line 27 guarantees argv[1] is an ASCII digit string, so int(argv[1]) cannot raise ValueError. The except ValueError block at lines 32-34 is dead code that suggests the validation is not the single source of truth. The duplicated 'n must be a positive integer' message also appears in two places, so the two checks can drift apart.
  Fix: Same as r1-1: drop the regex pre-check and keep only the try/except around int(), with the message text defined once in the CLI and matching top_words. Let top_words() own the range check.
- Finding r1-5 [low] main() signature is untyped and its contract is only in prose at src/cli.py:20
  Problem: main(argv) has no annotations, so the contract 'argv is list[str], returns int in {0, 2}' lives only in the docstring. Nothing enforces it, and a caller passing a non-list or a non-str element gets no static feedback.
  Fix: Annotate the signature as def main(argv: list[str]) -> int. Do not add mypy or other tooling; the annotation alone documents the contract.
- Finding r1-7 [low] n validation split across CLI regex and top_words; the CLI's int() guard is now dead code at src/cli.py:27
  Problem: Flow `main (cli.py:27) -> regex fullmatch [0-9]+ -> int() (cli.py:31) -> top_words (textstats.py:29)`. The regex guard admits only ASCII digit strings, so int() on line 31 can never raise ValueError; the except branch at lines 32-34 is unreachable. For n='0' the regex passes, int() gives 0, and the error comes from top_words (textstats.py:29-30), which prints 'n must be a positive integer' via the fallback at lines 37-39. So the CLI's exit-2 behaviour for invalid n currently depends on top_words raising, and the same message string is hardcoded in three places (cli.py:28, cli.py:33, textstats.py:30). If someone edits the top_words wording or the n<=0 rule, the CLI message for '0' changes but the regex-path message does not, and the two can silently diverge.
  Fix: Same as r1-1: the CLI owns only parsing (int() in try/except). top_words() is the single source for n <= 0. Remove the regex and the unreachable except path. Make the CLI message text identical to the textstats message so the two cannot diverge.
- Finding r1-9 [low] The 'positive integer' rule for n is duplicated in the CLI adapter and the domain at src/cli.py:27
  Problem: The CLI now checks n with a regex (`[0-9]+`), then calls int() inside a try/except ValueError, and then top_words() validates n again (rejecting 0 and bool). The rule 'n is a positive integer' therefore lives in two places, and the CLI's message is worded separately from the domain's. The domain's ValueError is already caught and turned into exit code 2 at line 37, so the regex pre-check is redundant for correctness and only adds a second definition of the business rule. The ValueError branch at line 32 is also now unreachable for anything the regex accepts. This is a mild mixed-concern issue: the adapter knows domain validity rules.
  Fix: Same as r1-1: the adapter parses argv with int() and maps ValueError to exit 2. The domain (top_words) validates the value. Remove the separate regex rule from the adapter.
- Finding r1-10 [low] Unrequested change to an existing error message at src/textstats.py:30
  Problem: The ValueError text changed from 'n must be an int greater than 0' to 'n must be a positive integer'. This is not part of the stated feature (top words plus CLI entry point). It is a user-visible string change with no stated value behind it, and any caller or spec test matching the old text will break. The new wording is also no more accurate for n=0, which is the case users are most likely to hit. Scope discipline says to leave working behavior alone unless there is a reason.
  Fix: Revert the ValueError text in top_words to 'n must be an int greater than 0' (the original wording). The top_words docstring at line 19 already uses that wording, so it stays unchanged. The CLI message must match it.
- Finding r1-12 [medium] sys.path mutation at import time to support one invocation style at src/cli.py:11
  Problem: The sys.path.insert(0, ...) runs whenever the module is imported, not only when it is run as a script. Any code that imports src.cli gets its global import path changed, and it needs the '# noqa: E402' workaround. The only reason is to support `python src/cli.py`. Running `python -m src.cli` from the repo root needs no hack at all, and that is the standard way to run a package module.
  Fix: Remove lines 11-12 and the noqa on line 14. Document `python -m src.cli TEXT N` as the run command. If `python src/cli.py` must keep working, guard the path insert with `if __name__ == "__main__":` so imports are not affected. Human note: Remove the sys.path insert; document `python -m src.cli TEXT N` as the run command.
- Finding r1-13 [low] Error message reworded without a need at src/textstats.py:30
  Problem: The ValueError text changed from 'n must be an int greater than 0' to 'n must be a positive integer'. Nothing in this diff requires it. It is churn that can break any test or caller matching the old text, and it adds a second wording for the same rule now that cli.py also has its own 'positive integer' message.
  Fix: Same as r1-10: revert the message in src/textstats.py:30 to 'n must be an int greater than 0' and use that single wording in src/cli.py.
- Finding r1-15 [low] Why a regex instead of int() is not explained at src/cli.py:27
  Problem: The new `re.fullmatch(r"[0-9]+", argv[1])` check rejects inputs that int() would accept (for example '+3', ' 3', '1_000'). The reason for this stricter parsing is not stated anywhere. The message 'n must be a positive integer' also appears for n=0, which matches the regex but is then rejected by top_words, so the message is correct only by accident of two layers both using it. A newcomer would have to work out both facts.
  Fix: Removing the regex (per r1-1) removes the need for its explanation. Add one docstring line to main() stating that N is parsed with int() and that n <= 0 or non-integer input exits 2 with the message 'n must be an int greater than 0'.
- Finding r1-17 [medium] Changed ValueError message is an unannounced contract change at src/textstats.py:30
  Problem: top_words changed its ValueError text from 'n must be an int greater than 0' to 'n must be a positive integer'. The docstring says only 'Raises ValueError on bad input' and does not promise any message, so the message was never a documented part of the contract. Callers that match on the text (for example `if 'greater than 0' in str(exc)`) will silently change behaviour. The diff does not flag this as a contract change, and the PR description has to call it out. Note that the diff also changes the message for the cli.py regex path, which is a separate user-visible change.
  Fix: Same as r1-10: restore the original message text so no caller-visible contract changes. No new docstring promise is needed.
- Finding r1-19 [low] Strict regex silently changes which N values are accepted versus int() at src/cli.py:27
  Problem: Before this diff, int(argv[1]) decided what counted as a valid N, so inputs like ' 3', '+3' and '3_0' were accepted. The new re.fullmatch(r"[0-9]+") gate rejects them with 'n must be a positive integer'. The docstring says 'decimal form', so this may be intended, but nothing in the diff or the tests records the change. The except ValueError branch at lines 32-34 is now unreachable for non-digit input, so it is dead code that suggests the two checks are redundant. Note that 0 is not a problem: it passes the regex and top_words() raises the same message, so the lead's concern about a misleading message does not hold. I checked that no test in the worktree matches on the old or new message text.
  Fix: Resolve in favour of int() parsing: accept whatever int() accepts (' 3', '+3', '1_0'), so no separate format rule exists. Document this in the main() docstring in one line. Do not add a test file; the test author owns tests.
- Finding r1-20 [low] top_words docstring wording differs from raised message at src/textstats.py:19
  Problem: The docstring says 'n must be an int greater than 0' (line 19), but the ValueError raised at line 30 now says 'n must be a positive integer'. The same requirement is described two ways in one function, and the commit changed the message without updating the docstring.
  Fix: Same as r1-10: the docstring at textstats.py:19 already says 'n must be an int greater than 0', so once the raised message is reverted to match, the two agree. No docstring edit needed.
- Finding r1-21 [low] Same error message duplicated in two CLI branches and in textstats at src/cli.py:28
  Problem: The literal 'n must be a positive integer' is printed at cli.py:28 (regex failure) and cli.py:33 (int() ValueError), and raised with the same text at textstats.py:30. The regex check and the int() except branch cover the same failure, so the message is maintained in three places and the two CLI copies can drift.
  Fix: Remove the regex branch so the literal appears only once in the CLI (the int() except branch) and once in textstats.py, with identical text. Covered by the r1-1 change.

## Did

- a1-1: commit 56f13f59e770756db3f57ac1f1f5c7b9392d59e7. All listed review findings are fixed in src/cli.py and src/textstats.py. In src/cli.py I removed the sys.path hack and its noqa, the regex pre-check, and the duplicate message. N is now parsed only with int() in try/except ValueError, which prints 'n must be an int greater than 0' and returns 2. main is annotated as main(argv: list[str]) -> int, and the docstring documents int() parsing and the run command 'python -m src.cli TEXT N'. In src/textstats.py I reverted the top_words ValueError text to 'n must be an int greater than 0'. n <= 0 is still rejected by top_words, and the CLI reports it through the existing handler with the same message. Behaviour change: inputs like ' 3', '+3' and '1_0' are now accepted, as the fix for r1-19 intends. No new files were added, protected test files were not touched, and the project check was not run.

- Attempt 1: addressed r1-1, r1-4, r1-5, r1-7, r1-9, r1-10, r1-12, r1-13, r1-15, r1-17, r1-19, r1-20, r1-21; not addressed none.

## Fixed

- r1-1
- r1-4
- r1-5
- r1-7
- r1-9
- r1-10
- r1-12
- r1-13
- r1-15
- r1-17
- r1-19
- r1-20
- r1-21

## Left open

(none)

## Given

- Finding r2-1 [low] Runtime PEP 585 annotation with no declared minimum Python version at src/cli.py:1
  Problem: `def main(argv: list[str]) -> int:` evaluates `list[str]` at function definition time, so the module fails to import on Python 3.8 and earlier. The repo declares no `requires-python` or python-version in any config I could find (grep for requires-python, python_requires and python-version returned nothing), so the minimum version is unverified. The old file had no such annotation, so this diff introduces the constraint.
  Fix: In src/cli.py, add `from __future__ import annotations` as the first statement after the module docstring so `list[str]` is not evaluated at definition time. Do not add a requires-python declaration.
- Finding r2-2 [low] Direct execution `python src/cli.py` now fails with ModuleNotFoundError at src/cli.py:7
  Problem: The sys.path bootstrap was removed, so `from src.textstats import top_words` only resolves when the working directory is the repo root and the module is run with `-m` (or the package is otherwise on sys.path). Anyone running `python src/cli.py ...`, which the old comment explicitly supported, now gets an import error before any usage message is printed. The new docstring documents `python -m src.cli`, so this is a deliberate break, but nothing in the diff mentions it as a breaking change.
  Fix: If the old invocation should keep working, restore the sys.path insert. If the break is intended, call it out in the commit or PR description so callers update their invocation. Human note: Restore the sys.path bootstrap
- Finding r2-3 [low] CLI error string duplicated and can drift from the library message at src/cli.py:29
  Problem: The int() parse-failure branch hard-codes 'n must be an int greater than 0', duplicating the ValueError text in src/textstats.py:30. The two literals are only kept in sync by convention. The non-integer path never reaches the library check, so a future message change in textstats.py would silently leave the CLI out of step. The leads' concern about a string-matching test or caller is checked: a repo-wide grep finds no test, spec, or caller that matches the old 'n must be a positive integer' text, so nothing breaks today.
  Fix: In src/textstats.py, add a module constant `N_ERROR = 'n must be a positive integer'` and use it in the top_words ValueError for bad n. In src/cli.py, import N_ERROR from src.textstats and print it in the int() parse-failure branch instead of the hard-coded literal.
- Finding r2-6 [low] Entry point invocation changed; python src/cli.py no longer works at src/cli.py:3
  Problem: NORTH STAR BLIND: nick=north-star briefs, in-flight.md and in-flight-issues.md were not provided, and docs/adr/ does not exist, so no ADR, issue, or PR could be checked. **Category**: QUESTION. The sys.path shim was removed and the docstring now documents `python -m src.cli TEXT N`. Running `python src/cli.py` from the repo root now fails with ModuleNotFoundError. src/__init__.py exists, so the module form works. Nothing in the repo (README, scripts) still references the old form, so the break is only for anyone who used the old invocation. The change is not called out in the commit or diff as an intentional interface change. The usage string on line 24 still says `cli.py TEXT N`, which no longer matches the documented invocation.
  Fix: Change the usage string in src/cli.py (the bad-argument-count message) from `usage: cli.py TEXT N ...` to `usage: python -m src.cli TEXT N (quote TEXT if it contains spaces)` so it matches the module docstring.
- Finding r2-8 [medium] Validation message for n is duplicated and can drift from the domain's own rule at src/cli.py:22
  Problem: The rule that n must be a positive count is owned by top_words in the domain module (textstats.py). cli.py repeats the message text as a literal in its int() failure path, so the same business rule now has two sources of wording. The CLI's int() guard also duplicates part of the domain check: top_words already rejects n <= 0 and non-int values with a ValueError. If the wording or the rule changes in textstats.py, the CLI can keep reporting the old text for non-integer input, and users will see inconsistent errors for the same concept. The lead noted the same issue.
  Fix: Same change as r2-3: N_ERROR in src/textstats.py is the single source of the n-validation wording; src/cli.py imports it and the literal is removed from cli.py.
- Finding r2-9 [low] Ubiquitous language drifted from 'positive integer' to 'int greater than 0' at src/textstats.py:30
  Problem: The business term for n is a positive integer (a count of top words). The message now reads 'an int greater than 0', which names the Python type and the comparison instead of the concept. The docstring and the CLI usage text still say 'positive integer', so the domain term and the error message now disagree. Users and product docs will use the first phrase, and the error should match it.
  Fix: Use the domain term 'positive integer' in the error message: the N_ERROR constant in src/textstats.py reads 'n must be a positive integer' (see r2-3), and no wording uses 'int greater than 0'.
- Finding r2-11 [medium] Input acceptance widened without a stated spec change at src/cli.py:26
  Problem: The old regex accepted only ASCII digits. Replacing it with int() also accepts ' 3', '+3', and '1_0' (which parses as 10). The docstring now documents this, but nothing in the diff says it was requested. Users and any scripts built on the CLI now get different behaviour for inputs that used to be rejected, and '1_0' is an unexpected result for most people. This is scope drift, not a value-driven change.
  Fix: Confirm with the spec owner that accepting these forms is intended. If not, keep the digit-only check (re.fullmatch(r'[0-9]+', argv[1])) before int(), or add an explicit test that pins the accepted forms. Human note: Restore the ASCII digit check before int()
- Finding r2-12 [low] Error message reworded to awkward text with no user benefit at src/cli.py:34
  Problem: 'n must be a positive integer' was replaced with 'n must be an int greater than 0' in both cli.py and textstats.py. The new text is harder to read and describes the type rather than the requirement. It does not help users fix their input, and it breaks any tests, docs, or scripts that match the old string.
  Fix: Revert the error wording to 'n must be a positive integer' in src/textstats.py and src/cli.py via the N_ERROR constant (see r2-3).
- Finding r2-21 [low] Usage line still shows the old invocation at src/cli.py:24
  Problem: The docstring now says to run `python -m src.cli TEXT N`, but the usage message printed on bad argument count still says `usage: cli.py TEXT N`. A user who gets this message after following the docstring will try a command that does not match what they were told, which is the kind of inconsistency that makes users distrust the help text.
  Fix: Change the usage string in src/cli.py to `usage: python -m src.cli TEXT N (quote TEXT if it contains spaces)`.
- Finding r2-28 [medium] Public library error message changed; callers matching on the text break at src/textstats.py:30
  Problem: top_words() raises ValueError('n must be an int greater than 0') where it used to raise ValueError('n must be a positive integer'). The message is part of the public surface of a library function. Any caller that matches on the string, or any test in a downstream project, now fails with no type change to signal it. The contract never said the message was stable, so this is an undocumented change, and the new wording is also awkward. The CLI message now matches it exactly, so the two are coupled in a way the diff does not explain.
  Fix: Revert the public ValueError message from top_words in src/textstats.py to 'n must be a positive integer' via the N_ERROR constant. Keep the ValueError type unchanged.
- Finding r2-31 [low] Usage text and module docs now disagree about how to invoke the CLI at src/cli.py:3
  Problem: The module docstring now says 'python -m src.cli TEXT N', but the usage message on line 24 still prints 'usage: cli.py TEXT N'. Someone who types the usage line into a shell gets the old `python cli.py` form, which no longer works because the sys.path bootstrap was removed. Anyone running `python src/cli.py` now gets ModuleNotFoundError, and nothing in the diff warns about that behaviour change.
  Fix: Change the usage string in src/cli.py to `usage: python -m src.cli TEXT N (quote TEXT if it contains spaces)`.
- Finding r2-32 [low] Reworded error message is user-visible and grammatically awkward; no check that anything depends on the old text at src/cli.py:29
  Problem: The message changed from 'n must be a positive integer' to 'n must be an int greater than 0', and the same wording was copied into textstats.py:30. The new text is also now emitted for non-integer input such as 'abc', where 'an int greater than 0' is misleading. Nothing in the diff shows that a test or spec asserts the old string, so any existing assertion on the old text would fail or go unnoticed.
  Fix: Revert the CLI and library wording to 'n must be a positive integer' via the N_ERROR constant (see r2-3).
- Finding r2-33 [low] Usage line names cli.py, docstring recommends python -m src.cli at src/cli.py:24
  Problem: The module docstring (line 3) says to run the entry point as `python -m src.cli TEXT N`, but the usage message on line 24 says `usage: cli.py TEXT N`. The two show different invocation forms for the same program.
  Fix: Change the usage string in src/cli.py to `usage: python -m src.cli TEXT N (quote TEXT if it contains spaces)`.
- Finding r2-34 [low] Module docstring wording differs from the error text for the same n contract at src/cli.py:5
  Problem: Line 5 says 'n is a positive integer', while the error message on line 29, the main() docstring (lines 18-19) and the message in src/textstats.py say 'an int greater than 0'. The same contract is described with two wordings.
  Fix: Change the module header line in src/cli.py that says 'n is a positive integer' only if needed so it uses the same wording as N_ERROR ('n must be a positive integer'); keep the phrase 'positive integer' and do not introduce 'int greater than 0'.
- Send-back notes from the merge gate:
Do the two fixes I asked for and nothing else: r2-11, put the ASCII digit check back before int(); r2-2, restore the sys.path bootstrap so python src/cli.py TEXT N works.

## Did

- a1-1: commit afb90bef4215dfa68ad194264b4db491f6026962. All listed findings are fixed in src/cli.py and src/textstats.py. textstats.py: added N_ERROR = 'n must be a positive integer' and used it in the top_words ValueError (type unchanged); docstring now says "positive integer". cli.py: added the __future__ annotations import; restored the sys.path bootstrap before the src import; restored an ASCII-only check (re.fullmatch [0-9]+) before int(); the CLI prints N_ERROR instead of a hard-coded literal; usage line and main() docstring now match 'python -m src.cli TEXT N' and the positive-integer wording. Choices: the sys.path insert runs unconditionally; the import carries noqa E402. Module header line 5 was left unchanged since it already matches. No test files or new files were touched. The project check was not run, as instructed.
- sendback: commit (no change). I made no edits. Both requested fixes are already present in src/cli.py as it stands in this worktree:
- r2-11: line 31 has `if not re.fullmatch(r"[0-9]+", argv[1]):`, which rejects anything that is not ASCII decimal digits, and it runs before `int(argv[1])` on line 34. On a non-match it prints N_ERROR and returns 2.
- r2-2: line 15 has `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`, placed before `from src.textstats import ...`, so `python src/cli.py TEXT N` can import the package.
I could not run git diff against HEAD to confirm what the committed version looks like, because Bash is disabled in this session. The check is by reading the current file only. Protected test files were not touched.

- Attempt 1: addressed r2-1, r2-2, r2-3, r2-6, r2-8, r2-9, r2-11, r2-12, r2-21, r2-28, r2-31, r2-32, r2-33, r2-34; not addressed none.

## Fixed

- r2-1
- r2-2
- r2-3
- r2-6
- r2-8
- r2-9
- r2-11
- r2-12
- r2-21
- r2-28
- r2-31
- r2-32
- r2-33
- r2-34

## Left open

(none)

