# review

## Round 0: what was reviewed

Diff 9d0616e557..99fa2322fa; 25 lenses swept by scouts.

## Round 0: lenses with candidates

Ariadne, Business Beth, Code Rot Cody, Consistency Checker, Contract Chris, Contrarian Carl, Curious Casey, Danielle the Designer, Data Scientist Dana, Eric Evans, Fragile Feynman, Know-It-All Nigel, North Star Nick, Sam System, Tara TypeSafe, Vera Verifier

## Round 0: promoted to a full reviewer

Know-It-All Nigel, Security Sage, Tara TypeSafe, Sam System, North Star Nick, Eric Evans, Business Beth, Curious Casey, Vera Verifier, Danielle the Designer, Data Scientist Dana, Code Rot Cody, Fragile Feynman, Contract Chris, Ariadne, Contrarian Carl, Consistency Checker

## Round 0: findings

- r0-1 [low, confirmed] src/longest_cli.py:3 Contract is duplicated in module and function docstrings
- r0-2 [low, confirmed] src/longest_cli.py:3 Claim 'registered: yes' is unverified in the repo
- r0-3 [low, confirmed] src/longest_cli.py:3 CLI contract is written twice and can drift
- r0-4 [low, confirmed] src/longest_cli.py:10 Unexplained '# registered: yes' marker
- r0-5 [medium, confirmed] src/longest_cli.py:31 Bad input shows a Python traceback to the user
- r0-6 [low, confirmed] tests/test_longest_cli.py:63 T8 claims a check.py verification that does not exist, so the success signal is misleading
- r0-7 [medium, confirmed] tests/test_longest_cli.py:63 T8 points to a check.py that is not in the change
- r0-8 [medium, confirmed] tests/test_longest_cli.py:14 Checks are module-level asserts, not test functions
- r0-9 [low, confirmed] tests/test_longest_cli.py:3 sys.path hack is undocumented
- r0-10 [low, confirmed] src/longest_cli.py:8 '# registered: yes' comment is unexplained
- r0-11 [low, confirmed] src/longest_cli.py:1 Contract is written twice and can drift
- r0-12 [medium, confirmed] tests/test_longest_cli.py:14 Bare asserts make the whole suite vacuous under python -O
- r0-13 [low, confirmed] tests/test_longest_cli.py:59 T8 comment overstates what the script run verifies; module defines no test functions
- r0-14 [medium, confirmed] src/longest_cli.py:33 Bad input produces a raw Python traceback instead of a helpful message
- r0-15 [low, confirmed] src/longest_cli.py:25 Wrong-count error gives no usage example
- r0-16 [medium, confirmed] src/longest_cli.py:11 Running the file directly fails to import the package
- r0-17 [medium, confirmed] tests/test_longest_cli.py:8 Test suite relies on bare assert statements, so it passes vacuously under python -O
- r0-18 [medium, confirmed] src/longest_cli.py:34 Documented failure mode is a raw traceback at the CLI boundary
- r0-19 [low, confirmed] src/longest_cli.py:26 argv type precondition is undocumented and unchecked
- r0-20 [low, confirmed] src/longest_cli.py:5 Contract text is duplicated between module and main() docstrings
- r0-21 [medium, confirmed] src/longest_cli.py:32 Direct script invocation fails with ModuleNotFoundError
- r0-22 [low, confirmed] tests/test_longest_cli.py:60 T8 references a check.py that is not part of this change

## Round 0: needs you

none

## Round 0: doing it

- r0-1: Keep the full CLI contract only in the module docstring of src/longest_cli.py. Reduce main()'s docstring to a one-line summary that says 'See the module docstring for the contract.'
- r0-3: Same edit as r0-1: the module docstring is the single statement of the contract; main()'s docstring refers to it.
- r0-11: Same edit as r0-1: one authoritative contract in the module docstring; main()'s docstring points to it.
- r0-20: Same edit as r0-1. Direction chosen: the module docstring keeps the contract (it already describes the CLI), and main() refers to it. This reverses the reviewer's suggestion; both are acceptable, but only one copy must remain.
- r0-2: Delete the bare '# registered: yes' comment in src/longest_cli.py. Nothing reads it and the diff cannot verify it.
- r0-4: Delete the bare '# registered: yes' comment in src/longest_cli.py.
- r0-10: Delete the bare '# registered: yes' comment in src/longest_cli.py (same edit as r0-2 and r0-4).
- r0-6: Remove the 'see check.py' reference from the T8 comment, and remove the unconditional print('ok') and sys.exit(0) block. Success comes from the test run itself.
- r0-7: Same edit as r0-6: remove the dangling check.py reference and the unconditional 'ok' block. If any run-instructions remain, say 'python -m unittest tests.test_longest_cli' (or the runner actually used).
- r0-13: Same edit as r0-6 (drop the check.py reference and the unconditional exit block) plus the test-function conversion in r0-8.
- r0-22: Same edit as r0-6: delete the T8 'if __name__ == "__main__"' block that prints 'ok' and exits 0 unconditionally.
- r0-8: Rewrite the T1-T7 module-level checks as named test functions (unittest.TestCase methods are preferred, since they do not depend on assert being enabled), e.g. test_empty_string_raises_value_error. Keep the T-numbers in docstrings for traceability. Use self.assertEqual / assertRaises, not bare assert.
- r0-12: Covered by the r0-8 rewrite: use unittest assertion methods (assertEqual, assertRaises), which work under python -O. No bare assert statements remain in the test file.
- r0-17: Same root cause as r0-12; covered by the r0-8 rewrite to unittest methods. Remove the raises_value_error_silently helper's bare 'assert raised' in favour of assertRaises.
- r0-9: Add a one-line comment above the sys.path.insert in tests/test_longest_cli.py saying it lets 'from src.longest_cli import main' resolve when the file is run from outside the repo root.
- r0-15: Change the wrong-count ValueError message in src/longest_cli.py main() to include the invocation form, e.g. 'expected one quoted text argument, got %d. Usage: python -m src.longest_cli "some text"'. This is an error message, not stdout, so it does not conflict with the non-goals.
- r0-16: Do not add sys.path manipulation. Document the supported invocation in the module docstring as 'python -m src.longest_cli "text"' (run from the repo root). Remove any implication that 'python src/longest_cli.py' works.
- r0-21: Same as r0-16: state in the module docstring that the supported invocation is 'python -m src.longest_cli "text"' from the repo root. Do not add a subprocess test in this ticket.
- r0-19: Add one docstring line to main(): 'argv must be a list of str; other types are not supported.' Do not add an isinstance check: P-errors says bad input raises ValueError, and a TypeError would be a new error category outside the plan.

## Round 0: needs measurement

none

## Round 0: deferred

- r0-5: The plan's non-goals rule out catching ValueError in the entry point or turning it into an exit code, and this finding asks for exactly that. If the team wants friendly CLI errors, that is a change to the plan first, then a new ticket.
- r0-14: Same as r0-5: asks for a try/except in the __main__ block that converts ValueError to an exit code, which the plan lists as a non-goal. Revisit only if the plan changes.
- r0-18: Same as r0-5 and r0-14: a stderr handler and exit status 2 in __main__ conflicts with the non-goal on converting ValueError to an exit code. Revisit with the plan.

## Round 0: gut check

Most findings share one premise: that a shell user should get a friendly error, not a traceback. Three of them (r0-5, r0-14, r0-18) ask for the try/except in __main__ that the plan explicitly rules out, so they are deferred rather than fixed. If the team wants that UX, it is a plan change, not a bug fix. The second theme is drift risk: the contract is written twice (r0-1, r0-3, r0-11, r0-20) and T8 cites a missing check.py (r0-6, r0-7, r0-13, r0-22). Reviewers disagreed on the fix direction for the duplicated contract: r0-3 and r0-11 keep the contract in the module docstring, r0-20 keeps it in main(). I chose the module docstring. The assert-under-python -O issue (r0-12, r0-17) and the module-level checks (r0-8) are one root cause, so they are grouped into one unittest rewrite. Registration and check.py findings are about the same thing: unverifiable claims in comments, so I removed the comments instead of verifying them.

## Round 0: measurements

none

## Round 0: questions and answers

none

## Round 1: what was reviewed

Diff 99fa2322fa..cdf009d72b; 25 lenses swept by scouts.

## Round 1: lenses with candidates

Ariadne, Business Beth, Code Rot Cody, Consistency Checker, Contract Chris, Contrarian Carl, Curious Casey, Danielle the Designer, Sam System, Tara TypeSafe, Vera Verifier

## Round 1: promoted to a full reviewer

Security Sage, Tara TypeSafe, Sam System, North Star Nick, Business Beth, Curious Casey, Vera Verifier, Danielle the Designer, Code Rot Cody, Contract Chris, Ariadne, Contrarian Carl, Consistency Checker

## Round 1: findings

- r1-1 [low, confirmed] src/longest_cli.py: Usage hint reaches the user only as a Python traceback
- r1-2 [low, confirmed] src/longest_cli.py:19 'Not supported' for non-str argv is unexplained and untested
- r1-3 [medium, confirmed] src/longest_cli.py:32 Usage errors surface as a raw Python traceback
- r1-4 [low, confirmed] src/longest_cli.py:19 Argv type precondition has no stated consequence and main's docstring omits Raises
- r1-5 [low, confirmed] src/longest_cli.py:22 New usage hint only reaches the user inside an unhandled traceback
- r1-6 [low, confirmed] src/longest_cli.py:24 Error message wording differs from the module contract

## Round 1: needs you

none

## Round 1: doing it

- r1-2: Reword the main() docstring line about argv so it states the consequence: a non-str element raises ValueError (from longest_word). Add a test in tests/test_longest_cli.py that main([1]) raises ValueError, so the documented behaviour is pinned. Do not add an isinstance check in this change.
- r1-4: In the main() docstring, replace 'other types are not supported' with a statement that argv must be a list of str, and add a Raises: section listing ValueError for wrong argument count, blank text, and non-str elements. Do not add type enforcement or change behaviour for str or tuple argv in this change; that is a contract decision and is noted in the gut check.
- r1-6: Change the wrong-argument-count message to match the module contract wording, e.g. 'expected exactly one text argument, got %d'. Drop the word 'quoted' from the raised message. Keep the shell-quoting hint only in the Usage text if it is still wanted. Update any test that matches the old message text.

## Round 1: needs measurement

none

## Round 1: deferred

- r1-1: Same traceback issue as r1-3. Its outcome follows the r1-3 decision, so it is not handled separately.
- r1-5: Same traceback issue as r1-3. Its outcome follows the r1-3 decision, so it is not handled separately.
- r1-3: you answered: Leave the traceback as it is

## Round 1: gut check

Three findings (r1-1, r1-3, r1-5) share one premise: that usage errors should look like CLI errors and not tracebacks. Whether that is in scope depends on reading the non-goal 'converting it to an exit code', so it is sent to a person rather than decided here. The reviewers disagreed on whether a __main__ handler exists: r1-3 says it exists but does not catch, and r1-1 says it is missing. Both agree it does not catch ValueError, so the disagreement does not change the outcome. Not raised by any reviewer: main('a') is silently accepted because a str of length 1 indexes to a character. That is a contract gap worth a decision, and r1-4 deliberately leaves it unenforced.

## Round 1: measurements

none

## Round 1: questions and answers

- r1-3: Leave the traceback as it is (human)

## Round 2: what was reviewed

Diff cdf009d72b..9b9c5d4776; 25 lenses swept by scouts.

## Round 2: lenses with candidates

Business Beth, Consistency Checker, Contract Chris, Contrarian Carl, Curious Casey, Danielle the Designer, Fragile Feynman, Know-It-All Nigel, Sam System, Tara TypeSafe

## Round 2: promoted to a full reviewer

Know-It-All Nigel, Security Sage, Tara TypeSafe, Sam System, North Star Nick, Business Beth, Curious Casey, Danielle the Designer, Fragile Feynman, Contract Chris, Contrarian Carl, Consistency Checker

## Round 2: findings

- r2-1 [low, confirmed] src/longest_cli.py:19 main() documents, rather than enforces, the argv type check
- r2-2 [low, confirmed] src/longest_cli.py:16 main() signature and docstring disagree about accepted argv types
- r2-3 [medium, confirmed] src/longest_cli.py:19 Docstring codifies a str/tuple argv pass-through that contradicts P-errors
- r2-4 [low, confirmed] src/longest_cli.py:30 Unexplained change to user-facing usage error wording
- r2-5 [low, refuted] src/longest_cli.py:21 Docstring codifies a str argv being silently accepted as a list of characters
- r2-6 [low, confirmed] src/longest_cli.py:19 Docstring misdescribes str and tuple argv handling
- r2-7 [low, confirmed] src/longest_cli.py:12 Unexplained '# registered: yes' marker
- r2-8 [medium, confirmed] src/longest_cli.py:40 Usage hint is only visible inside a raw traceback
- r2-9 [low, confirmed] src/longest_cli.py:20 Docstring documents accidental str/tuple behaviour as supported
- r2-10 [low, confirmed] src/longest_cli.py:24 Undocumented TypeError for non-sized argv
- r2-11 [low, confirmed] src/longest_cli.py:19 Precondition contradicts the accepted-input note
- r2-12 [low, confirmed] src/longest_cli.py:24 Raises section omits the 'no words' ValueError
- r2-13 [low, confirmed] src/longest_cli.py:20 Docstring turns an unvalidated str argv into documented behaviour
- r2-14 [low, confirmed] src/longest_cli.py:25 Raises block narrower than actual no-word behavior

## Round 2: needs you

none

## Round 2: doing it

- r2-1: At the top of main(), before the len check, add `if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv): raise ValueError(...)` with a message naming the expected type. Keep the len check after it. Update the docstring to state that non-list or non-str input raises ValueError, and remove the 'not rejected' sentence.
- r2-2: Same guard as r2-1, raising ValueError rather than TypeError because P-errors says bad input raises ValueError. Remove the 'not rejected' sentence so the docstring states one rule: argv must be a list of str, and anything else raises ValueError. Do not touch src/longest.py.
- r2-3: Enforce P-errors at the boundary: add the isinstance guard at the top of main() (raising ValueError), and delete the docstring sentence that documents str/tuple as accepted. No principles amendment is needed; the guard matches P-errors as written.
- r2-4: Make the usage error match the usage hint on the same line: restore 'quoted' so the message reads along the lines of 'expected one quoted text argument, got N', keeping the usage hint. Keep the count in the message.
- r2-6: Covered by the argv guard from r2-1. Once non-list input raises ValueError, delete the sentence 'A str or tuple argv is not rejected: argv[0] is then a single character' so no inaccurate tuple description remains.
- r2-9: Covered by the argv guard from r2-1. The guard rejects str input before len() runs, so the 'got N' count can no longer count characters. Remove the accidental str/tuple description from the docstring.
- r2-10: Place the isinstance(argv, list) guard before len(argv), so None or int raises ValueError with a clear message instead of an undocumented TypeError from len(). The Raises section then covers it. Keep ValueError as the only documented exception.
- r2-11: Reduce the precondition to one rule: 'argv must be a list of str; any other input raises ValueError.' Remove the 'not rejected' note. This is the same docstring edit as r2-1.
- r2-12: Reword the Raises entry in src/longest_cli.py to cover every no-word case: 'if the text contains no words (blank, whitespace-only, or punctuation-only)'. Optionally add a test that main(['!!!']) raises ValueError if a test file exists for the CLI. Do not edit src/longest.py.
- r2-13: Covered by the argv guard from r2-1, which rejects str input. Remove the 'not rejected' docstring sentence. Add a test that main('a') raises ValueError, if a test file exists for the CLI, so the behaviour is pinned.
- r2-14: Update the module contract at line 8 and the Raises entry at line 26 to say 'the text contains no words (blank, whitespace-only, or punctuation-only)', matching the longest_word behaviour. Docstring only; no behaviour change.

## Round 2: needs measurement

- r2-7: grep -rn 'registered' --include='*' . | grep -v '^./.git/'

## Round 2: deferred

- r2-8: The plan lists 'catching ValueError ... or converting it to an exit code' as a non-goal, so a try/except in the __main__ block that exits with status 2 is out of scope for this ticket. A follow-up ticket can handle a clean stderr message for the CLI if the traceback is a real problem for users.

## Round 2: left open

This was the last review round allowed; the doing-it items above will not be fixed automatically and are listed as open.

## Round 2: gut check

Most findings share one bad premise: main() accepts str or tuple argv by accident, and the docstring then records that accident as intended behaviour. Eight reviewers flagged it from different angles (r2-1, 2, 3, 6, 9, 10, 11, 13). One guard at the top of main() fixes all of them, and it matches P-errors. Reviewers disagreed on the exception type: r2-2 suggested TypeError, while P-errors says bad input raises ValueError, so I chose ValueError. The change also drifts a little from the plan, which does not list non-list argv, but P-errors covers it. This is the last review round, so the doing_it items will be listed as open at the merge gate and will not be fixed automatically. The argv guard and the docstring edits are the important ones to land; the copy tweak in r2-4 can be dropped if the human prefers the current wording.

## Round 2: measurements

- r2-7: `grep -rn 'registered' --include='*' . | grep -v '^./.git/'` not run (listed at the merge gate)

## Round 2: questions and answers

none

