# review

## Round 0: what was reviewed

Diff 634bb4a1ed..1194c4ab4d; 25 lenses swept by scouts.

## Round 0: lenses with candidates

Ariadne, Business Beth, Code Rot Cody, Consistency Checker, Contract Chris, Curious Casey, Danielle the Designer, Know-It-All Nigel, North Star Nick, Sam System, Vera Verifier

## Round 0: promoted to a full reviewer

Know-It-All Nigel, Security Sage, Sam System, North Star Nick, Business Beth, Curious Casey, Vera Verifier, Danielle the Designer, Code Rot Cody, Contract Chris, Ariadne, Consistency Checker

## Round 0: findings

- r0-1 [low, confirmed] src/cli.py:18 n parsing is looser than the documented 'positive integer in decimal form'
- r0-2 [medium, confirmed] src/cli.py:6 Direct script invocation fails: `from src.textstats import top_words` needs the repo root on sys.path
- r0-3 [low, confirmed] src/cli.py:20 Two different error messages for the same mistake (invalid N)
- r0-4 [medium, confirmed] tests/test_top_words.py:1 Tests are module-level asserts, so pytest reports no test items
- r0-5 [low, confirmed] src/cli.py:34 Stray '# style: ok' marker at end of file with no explanation
- r0-6 [low, confirmed] tests/test_cli.py:1 No CLI test for empty text, a valid edge case
- r0-7 [medium, confirmed] src/cli.py:32 Direct script invocation fails on the package import
- r0-8 [medium, confirmed] src/cli.py:20 Same invalid-n error gets two different messages
- r0-9 [low, confirmed] src/cli.py:27 Empty result gives silent success
- r0-10 [low, confirmed] src/cli.py:14 Unquoted multi-word text fails with only a usage line
- r0-11 [low, refuted] src/textstats.py:18 Wrong-type input raises ValueError instead of TypeError
- r0-12 [low, confirmed] src/textstats.py:14 Docstring does not state that returned words are normalised
- r0-13 [low, confirmed] src/cli.py:4 n parsing accepts more than the documented 'decimal form'
- r0-14 [low, confirmed] src/cli.py:10 main() docstring omits its return-code and error contract
- r0-15 [low, confirmed] src/cli.py:26 Stray '# style: ok' marker committed into both source files
- r0-16 [low, confirmed] src/cli.py:16 n parsing accepts more forms than the documented 'positive integer in decimal form'
- r0-17 [low, confirmed] src/cli.py:18 Two wordings for the same 'n must be positive' rule

## Round 0: needs you

none

## Round 0: doing it

- r0-2: Make `python src/cli.py TEXT N` work by adding a sys.path bootstrap at the top of src/cli.py, before the `from src.textstats import top_words` import: insert the repo root (Path(__file__).resolve().parent.parent) into sys.path, as tests/test_cli.py does. Do not add a test file; the separate test author owns that.
- r0-7: Same defect as r0-2. Apply the same sys.path bootstrap in src/cli.py before the package import so the script runs directly. Merged with r0-2; no separate test file.
- r0-1: Validate n strictly before converting: in src/cli.py, reject argv[1] unless re.fullmatch(r'[0-9]+', argv[1]) matches (add `import re`), using the existing 'n must be a positive integer' stderr message and return 2. Keep int() and the n > 0 check in top_words. Merged with r0-13 and r0-16.
- r0-13: Same change as r0-1: enforce the documented decimal-form contract with re.fullmatch(r'[0-9]+', argv[1]) before int() in src/cli.py, so ' 2', '+2', '1_0' and non-ASCII digits are rejected with return 2. Merged with r0-1 and r0-16.
- r0-16: Same change as r0-1: add the re.fullmatch(r'[0-9]+', argv[1]) pre-check in src/cli.py before int(), rejecting mismatches with the existing message and return 2. Merged with r0-1 and r0-13.
- r0-3: Use one wording for the invalid-n rule. In src/textstats.py top_words, change the n check's ValueError message from 'n must be an int greater than 0' to 'n must be a positive integer', matching the CLI's int() failure message in src/cli.py. Merged with r0-8 and r0-17.
- r0-8: Same change as r0-3: make the top_words n-check message in src/textstats.py read 'n must be a positive integer', identical to src/cli.py. Merged with r0-3 and r0-17.
- r0-17: Same change as r0-3: align the top_words message in src/textstats.py with the CLI wording 'n must be a positive integer'. Merged with r0-3 and r0-8.
- r0-10: Extend the usage line in src/cli.py main() to 'usage: cli.py TEXT N (quote TEXT if it contains spaces)'. Do not change argv handling or add flags.
- r0-12: Extend the top_words docstring in src/textstats.py: state that returned words are as produced by words(), i.e. lower-cased, so the returned strings are the normalised forms rather than original spellings.
- r0-14: Expand the main() docstring in src/cli.py with a Returns section: 0 on success, 2 on any usage or input error; errors go to stderr and nothing is raised. The n message consistency is handled by the r0-3 fix.

## Round 0: needs measurement

- r0-5: git grep -n 'style: ok' -- ':!src/cli.py' ':!src/textstats.py' ; git grep -n -i 'style' -- '*.toml' '*.cfg' '*.ini' '.github' 'Makefile' 'check.py'
- r0-15: git grep -n 'style: ok' -- ':!src/cli.py' ':!src/textstats.py' ; git grep -n -i 'style' -- '*.toml' '*.cfg' '*.ini' '.github' 'Makefile' 'check.py'

## Round 0: deferred

- r0-4: The plan requires the new tests to use the tests/test_textstats.py pattern (bare module-level asserts, runnable as scripts). Converting them to pytest test functions would break that pattern, and the test files belong to a separate author.
- r0-6: Adding a CLI test for empty input is a test-file change, and tests are written by a separate author under the plan. The behaviour itself is covered by the r0-9 decision; the test can follow once that is settled.
- r0-9: you answered: Keep silent success, exit 0

## Round 0: gut check

Most findings cluster into three duplicated defects: the direct-run import failure (r0-2, r0-7), the lenient n parsing (r0-1, r0-13, r0-16), and the two invalid-n wordings (r0-3, r0-8, r0-17). Each is merged under one id per fix and I've noted the merges. The reviewers agreed on the import defect, which is the most serious. The main drift risk is the test-style conflict (r0-4): the plan's test pattern is a deliberate choice, so the finding is deferred rather than acted on. The '# style: ok' marker is unexplained, so I've asked for a read-only check before anyone removes it.

## Round 0: measurements

- r0-5: `git grep -n 'style: ok' -- ':!src/cli.py' ':!src/textstats.py' ; git grep -n -i 'style' -- '*.toml' '*.cfg' '*.ini' '.github' 'Makefile' 'check.py'` not run (listed at the merge gate)
- r0-15: `git grep -n 'style: ok' -- ':!src/cli.py' ':!src/textstats.py' ; git grep -n -i 'style' -- '*.toml' '*.cfg' '*.ini' '.github' 'Makefile' 'check.py'` not run (listed at the merge gate)

## Round 0: questions and answers

- r0-9: Keep silent success, exit 0 (human)

## Round 1: what was reviewed

Diff 1194c4ab4d..144247871d; 25 lenses swept by scouts.

## Round 1: lenses with candidates

Ariadne, Business Beth, Code Rot Cody, Consistency Checker, Contract Chris, Contrarian Carl, Curious Casey, Dependency Skeptic, Eric Evans, Fragile Feynman, Know-It-All Nigel, North Star Nick, Penny Pincher, Sam System, Security Sage, Tara TypeSafe, Vera Verifier

## Round 1: promoted to a full reviewer

Know-It-All Nigel, Security Sage, Tara TypeSafe, Sam System, North Star Nick, Eric Evans, Business Beth, Penny Pincher, Curious Casey, Vera Verifier, Dependency Skeptic, Code Rot Cody, Fragile Feynman, Contract Chris, Ariadne, Contrarian Carl, Consistency Checker

## Round 1: findings

- r1-1 [low, confirmed] src/cli.py:30 Unreachable try/except ValueError after regex validation
- r1-2 [low, confirmed] src/cli.py:12 sys.path mutated at import time
- r1-3 [low, confirmed] src/cli.py:11 Repo root prepended to sys.path ahead of the stdlib
- r1-4 [low, confirmed] src/cli.py:31 Unreachable except branch after regex validation
- r1-5 [low, confirmed] src/cli.py:20 main() signature is untyped and its contract is only in prose
- r1-6 [low, confirmed] src/cli.py:12 Import-time mutation of sys.path is a hidden global side effect
- r1-7 [low, confirmed] src/cli.py:27 n validation split across CLI regex and top_words; the CLI's int() guard is now dead code
- r1-8 [low, confirmed] src/cli.py:12 Import-time sys.path mutation is an undocumented entry-point decision
- r1-9 [low, confirmed] src/cli.py:27 The 'positive integer' rule for n is duplicated in the CLI adapter and the domain
- r1-10 [low, confirmed] src/textstats.py:30 Unrequested change to an existing error message
- r1-11 [low, refuted] src/cli.py:27 Redundant regex check duplicates validation that already exists
- r1-12 [medium, confirmed] src/cli.py:11 sys.path mutation at import time to support one invocation style
- r1-13 [low, confirmed] src/textstats.py:30 Error message reworded without a need
- r1-14 [low, confirmed] src/cli.py:13 Undocumented sys.path mutation at import time
- r1-15 [low, confirmed] src/cli.py:27 Why a regex instead of int() is not explained
- r1-16 [low, confirmed] src/cli.py:12 Import-time sys.path mutation shadows stdlib modules
- r1-17 [medium, confirmed] src/textstats.py:30 Changed ValueError message is an unannounced contract change
- r1-18 [low, confirmed] src/cli.py:12 Importing the module mutates sys.path as a side effect
- r1-19 [low, confirmed] src/cli.py:27 Strict regex silently changes which N values are accepted versus int()
- r1-20 [low, confirmed] src/textstats.py:19 top_words docstring wording differs from raised message
- r1-21 [low, confirmed] src/cli.py:28 Same error message duplicated in two CLI branches and in textstats

## Round 1: needs you

none

## Round 1: doing it

- r1-1: Remove the re.fullmatch regex check and its message in main(). Keep one path: wrap int(argv[1]) in try/except ValueError, print 'n must be an int greater than 0' to stderr, and return 2. top_words() stays the sole owner of the n > 0 rule, so n=0 and negatives are reported by the existing generic handler with the same message. Delete the unreachable branch.
- r1-4: Same as r1-1: drop the regex pre-check and keep only the try/except around int(), with the message text defined once in the CLI and matching top_words. Let top_words() own the range check.
- r1-7: Same as r1-1: the CLI owns only parsing (int() in try/except). top_words() is the single source for n <= 0. Remove the regex and the unreachable except path. Make the CLI message text identical to the textstats message so the two cannot diverge.
- r1-9: Same as r1-1: the adapter parses argv with int() and maps ValueError to exit 2. The domain (top_words) validates the value. Remove the separate regex rule from the adapter.
- r1-21: Remove the regex branch so the literal appears only once in the CLI (the int() except branch) and once in textstats.py, with identical text. Covered by the r1-1 change.
- r1-15: Removing the regex (per r1-1) removes the need for its explanation. Add one docstring line to main() stating that N is parsed with int() and that n <= 0 or non-integer input exits 2 with the message 'n must be an int greater than 0'.
- r1-19: Resolve in favour of int() parsing: accept whatever int() accepts (' 3', '+3', '1_0'), so no separate format rule exists. Document this in the main() docstring in one line. Do not add a test file; the test author owns tests.
- r1-5: Annotate the signature as def main(argv: list[str]) -> int. Do not add mypy or other tooling; the annotation alone documents the contract.
- r1-10: Revert the ValueError text in top_words to 'n must be an int greater than 0' (the original wording). The top_words docstring at line 19 already uses that wording, so it stays unchanged. The CLI message must match it.
- r1-13: Same as r1-10: revert the message in src/textstats.py:30 to 'n must be an int greater than 0' and use that single wording in src/cli.py.
- r1-17: Same as r1-10: restore the original message text so no caller-visible contract changes. No new docstring promise is needed.
- r1-20: Same as r1-10: the docstring at textstats.py:19 already says 'n must be an int greater than 0', so once the raised message is reverted to match, the two agree. No docstring edit needed.
- r1-12: Remove lines 11-12 and the noqa on line 14. Document `python -m src.cli TEXT N` as the run command. If `python src/cli.py` must keep working, guard the path insert with `if __name__ == "__main__":` so imports are not affected. Human note: Remove the sys.path insert; document `python -m src.cli TEXT N` as the run command.

## Round 1: needs measurement

none

## Round 1: deferred

- r1-2: Same import-time sys.path side effect as r1-12. It is decided there; if the user picks the fix option, this is resolved with it.
- r1-3: Shadowing by repo-root files is the same sys.path insert as r1-12, and the risk is low because whoever controls the repo already controls the code run. Resolved by the r1-12 decision.
- r1-6: Same import-time sys.path side effect as r1-12. Resolved by the r1-12 decision.
- r1-8: Asks that the sys.path decision be written down. That only makes sense after r1-12 is decided, so it waits on that answer.
- r1-14: Asks for a comment explaining the sys.path insert. The r1-12 decision removes or relocates the insert, so a comment written now would be stale.
- r1-16: Same import-time sys.path side effect and stdlib shadowing as r1-3 and r1-12. Resolved by the r1-12 decision.
- r1-18: Same import-time sys.path side effect as r1-12. Resolved by the r1-12 decision.

## Round 1: gut check

Most of the 21 findings collapse into four issues. (1) The sys.path insert at import time, raised 8 times, is one decision (r1-12). (2) The n-validation split between a regex and int() leaves a dead except branch, raised about 7 times. I resolved it in favour of int() parsing with top_words as the single range owner, which accepts forms like ' 3' and '+3' that the regex rejected. That is a small behaviour change; the user can redirect. (3) The reworded ValueError message is unrequested churn that broke the original docstring wording; reverting it resolves four findings. (4) The annotation is a minor gain. The reviewers disagreed on which validation path to keep: the regex (r1-15, r1-19) or int() (r1-1, r1-4, r1-9). I went with int() because P-reuse says the domain owns the rule. The shared premise is that the CLI should be a thin adapter with no import-time side effects; the sys.path question is the one place where a human's usage choice matters.

## Round 1: measurements

none

## Round 1: questions and answers

- r1-12: Remove the sys.path insert; document `python -m src.cli TEXT N` as the run command. (human)

## Round 2: what was reviewed

Diff 144247871d..56f13f59e7; 25 lenses swept by scouts.

## Round 2: lenses with candidates

Business Beth, Code Rot Cody, Consistency Checker, Contract Chris, Contrarian Carl, Curious Casey, Danielle the Designer, Eric Evans, Fragile Feynman, Know-It-All Nigel, North Star Nick, Sam System, Security Sage, Tara TypeSafe, Vera Verifier

## Round 2: promoted to a full reviewer

Know-It-All Nigel, Security Sage, Tara TypeSafe, Sam System, North Star Nick, Eric Evans, Business Beth, Curious Casey, Vera Verifier, Danielle the Designer, Code Rot Cody, Fragile Feynman, Contract Chris, Contrarian Carl, Consistency Checker

## Round 2: findings

- r2-1 [low, confirmed] src/cli.py:1 Runtime PEP 585 annotation with no declared minimum Python version
- r2-2 [low, confirmed] src/cli.py:7 Direct execution `python src/cli.py` now fails with ModuleNotFoundError
- r2-3 [low, confirmed] src/cli.py:29 CLI error string duplicated and can drift from the library message
- r2-4 [low, confirmed] src/cli.py:5 Input contract silently widened from ASCII decimal to anything int() accepts
- r2-5 [low, confirmed] src/cli.py: Direct script invocation no longer works after sys.path bootstrap removal
- r2-6 [low, confirmed] src/cli.py:3 Entry point invocation changed; python src/cli.py no longer works
- r2-7 [low, confirmed] src/cli.py:5 Docstring says 'positive integer' while the accepted input and error text changed
- r2-8 [medium, confirmed] src/cli.py:22 Validation message for n is duplicated and can drift from the domain's own rule
- r2-9 [low, confirmed] src/textstats.py:30 Ubiquitous language drifted from 'positive integer' to 'int greater than 0'
- r2-10 [low, confirmed] src/cli.py:16 CLI input contract is now defined by Python's int() rather than by the domain
- r2-11 [medium, confirmed] src/cli.py:26 Input acceptance widened without a stated spec change
- r2-12 [low, confirmed] src/cli.py:34 Error message reworded to awkward text with no user benefit
- r2-13 [medium, confirmed] src/cli.py:1 Direct script invocation removed, breaking existing usage
- r2-14 [medium, confirmed] src/cli.py:24 Loosened n parsing (' 3', '+3', '1_0') has no test and no recorded rationale
- r2-15 [medium, confirmed] src/cli.py:3 Removed sys.path bootstrap makes `python src/cli.py` fail with no test or note
- r2-16 [low, confirmed] src/cli.py:29 Error message string duplicated between cli.py and textstats.py
- r2-17 [low, confirmed] tests/test_cli.py:16 Error-path tests check only that stderr is non-empty
- r2-18 [low, confirmed] src/cli.py:24 Lead confirmed: int() now accepts '+3', '1_0', and non-ASCII digits; existing tests still pass
- r2-19 [medium, confirmed] src/cli.py:8 Removed sys.path shim silently breaks `python src/cli.py` invocation
- r2-20 [medium, confirmed] src/cli.py:1 Documented invocation changed without a migration path; old command fails with a raw traceback
- r2-21 [low, confirmed] src/cli.py:24 Usage line still shows the old invocation
- r2-22 [low, confirmed] src/cli.py:24 Error message is less clear than before and is now shared by very different mistakes
- r2-23 [medium, confirmed] src/cli.py:20 Input validation silently widened from decimal regex to int()
- r2-24 [medium, confirmed] src/cli.py:1 Removed sys.path bootstrap breaks the direct 'python src/cli.py' invocation
- r2-25 [low, confirmed] src/textstats.py:30 Error message text is now an unpinned contract
- r2-26 [medium, confirmed] src/cli.py:3 Module docstring and function docstring contradict each other on what n accepts
- r2-27 [medium, confirmed] src/cli.py:6 Removing the sys.path shim silently breaks the documented-by-prior-behaviour invocation
- r2-28 [medium, confirmed] src/textstats.py:30 Public library error message changed; callers matching on the text break
- r2-29 [low, confirmed] src/cli.py:21 Misleading error for oversized digit strings
- r2-30 [medium, confirmed] src/cli.py:27 Removing the regex silently widens accepted N input, and the change is not pinned by any test
- r2-31 [low, confirmed] src/cli.py:3 Usage text and module docs now disagree about how to invoke the CLI
- r2-32 [low, confirmed] src/cli.py:29 Reworded error message is user-visible and grammatically awkward; no check that anything depends on the old text
- r2-33 [low, confirmed] src/cli.py:24 Usage line names cli.py, docstring recommends python -m src.cli
- r2-34 [low, confirmed] src/cli.py:5 Module docstring wording differs from the error text for the same n contract
- r2-35 [low, refuted] src/cli.py:14 list[str] annotation is new; no declared minimum Python version found

## Round 2: needs you

none

## Round 2: doing it

- r2-1: In src/cli.py, add `from __future__ import annotations` as the first statement after the module docstring so `list[str]` is not evaluated at definition time. Do not add a requires-python declaration.
- r2-3: In src/textstats.py, add a module constant `N_ERROR = 'n must be a positive integer'` and use it in the top_words ValueError for bad n. In src/cli.py, import N_ERROR from src.textstats and print it in the int() parse-failure branch instead of the hard-coded literal.
- r2-8: Same change as r2-3: N_ERROR in src/textstats.py is the single source of the n-validation wording; src/cli.py imports it and the literal is removed from cli.py.
- r2-6: Change the usage string in src/cli.py (the bad-argument-count message) from `usage: cli.py TEXT N ...` to `usage: python -m src.cli TEXT N (quote TEXT if it contains spaces)` so it matches the module docstring.
- r2-21: Change the usage string in src/cli.py to `usage: python -m src.cli TEXT N (quote TEXT if it contains spaces)`.
- r2-31: Change the usage string in src/cli.py to `usage: python -m src.cli TEXT N (quote TEXT if it contains spaces)`.
- r2-33: Change the usage string in src/cli.py to `usage: python -m src.cli TEXT N (quote TEXT if it contains spaces)`.
- r2-9: Use the domain term 'positive integer' in the error message: the N_ERROR constant in src/textstats.py reads 'n must be a positive integer' (see r2-3), and no wording uses 'int greater than 0'.
- r2-12: Revert the error wording to 'n must be a positive integer' in src/textstats.py and src/cli.py via the N_ERROR constant (see r2-3).
- r2-28: Revert the public ValueError message from top_words in src/textstats.py to 'n must be a positive integer' via the N_ERROR constant. Keep the ValueError type unchanged.
- r2-32: Revert the CLI and library wording to 'n must be a positive integer' via the N_ERROR constant (see r2-3).
- r2-34: Change the module header line in src/cli.py that says 'n is a positive integer' only if needed so it uses the same wording as N_ERROR ('n must be a positive integer'); keep the phrase 'positive integer' and do not introduce 'int greater than 0'.
- r2-11: Confirm with the spec owner that accepting these forms is intended. If not, keep the digit-only check (re.fullmatch(r'[0-9]+', argv[1])) before int(), or add an explicit test that pins the accepted forms. Human note: Restore the ASCII digit check before int()
- r2-2: If the old invocation should keep working, restore the sys.path insert. If the break is intended, call it out in the commit or PR description so callers update their invocation. Human note: Restore the sys.path bootstrap

## Round 2: needs measurement

none

## Round 2: deferred

- r2-4: Depends on the answer to r2-11 (whether the wider int() grammar stays). Revisit the header and docstring wording after that decision.
- r2-5: Same entry-point decision as r2-2; the reviewer already rates it acceptable as-is. Resolved by the r2-2 answer.
- r2-7: Depends on the answer to r2-11. The header and docstring wording must follow whichever input rule is chosen.
- r2-10: Depends on the answer to r2-11. Whether int() defines the input contract is the same open question.
- r2-13: Duplicate of r2-2 (same removed bootstrap). Resolved by the r2-2 answer.
- r2-14: Adds tests for the accepted forms. Tests are written by a separate author under the plan, and the input rule is pending r2-11.
- r2-15: Duplicate of r2-2 for the entry point. The subprocess test it suggests is a test addition the plan assigns to a separate author.
- r2-16: The drift risk is removed by the shared N_ERROR constant (r2-3). The suggested stderr assertion is a test addition assigned to a separate author.
- r2-17: Asserts exact stderr text in tests/test_cli.py. Test changes are assigned to a separate author under the plan.
- r2-18: Verified behaviour that depends on r2-11. The module docstring fix follows that decision.
- r2-19: Duplicate of r2-2 (removed sys.path bootstrap). Resolved by the r2-2 answer.
- r2-20: Asks for a README or changelog note. The plan keeps README changes out of scope unless it documents the API function list, which it does not. The r2-2 answer covers the invocation.
- r2-22: Wording is fixed by the revert in r2-12. Per-case messages that tell 'abc' apart from '-2' are a UX decision the plan does not make.
- r2-23: Depends on the answer to r2-11. The regex-versus-int() question decides whether a check is restored.
- r2-24: Duplicate of r2-2. The smoke test it suggests is a test addition assigned to a separate author.
- r2-25: The drift concern is handled by the shared N_ERROR constant (r2-3). A typed exception subclass is a new API surface outside this ticket.
- r2-26: Depends on the answer to r2-11. The header and main() docstring must be aligned after that decision.
- r2-27: Duplicate of r2-2 (removed sys.path shim). Resolved by the r2-2 answer.
- r2-29: Edge case for very long digit strings on Python 3.11+. The message is still truthful enough for this ticket. A distinct message would be a new branch beyond this step's scope.
- r2-30: Depends on the answer to r2-11. Pinning the accepted inputs with a test is assigned to a separate author.

## Round 2: left open

This was the last review round allowed; the doing-it items above will not be fixed automatically and are listed as open.

## Round 2: gut check

Most findings share one bad premise: the diff made unrequested interface changes and did not record them. Three groups: (1) int() widening the input contract (r2-11 is the real decision); (2) removing the sys.path bootstrap, which breaks `python src/cli.py` (r2-2 is the real decision); (3) rewording the error message in both cli.py and textstats.py, which the plan does not ask for and which is a public library message. Group 3 is reverted in doing_it through a single N_ERROR constant, which also removes the duplicated literal. Reviewers disagreed mainly on whether the path invocation matters (r2-5 calls it acceptable as-is; r2-19 and r2-24 call it a regression). The changes in doing_it are small and reversible. They are the last automatic round, so anything not landed here will show as open at the merge gate.

## Round 2: measurements

none

## Round 2: questions and answers

- r2-11: Restore the ASCII digit check before int() (human)
- r2-2: Restore the sys.path bootstrap (human)

## Round 3: what was reviewed

Diff 56f13f59e7..afb90bef42; 25 lenses swept by scouts.

## Round 3: lenses with candidates

Ariadne, Business Beth, Code Rot Cody, Consistency Checker, Contract Chris, Contrarian Carl, Curious Casey, Danielle the Designer, Eric Evans, Fragile Feynman, Know-It-All Nigel, Mozart, North Star Nick, Penny Pincher, Sam System, Security Sage, Vera Verifier

## Round 3: promoted to a full reviewer

Know-It-All Nigel, Security Sage, Sam System, North Star Nick, Eric Evans, Business Beth, Penny Pincher, Curious Casey, Vera Verifier, Danielle the Designer, Mozart, Code Rot Cody, Fragile Feynman, Contract Chris, Ariadne, Contrarian Carl, Consistency Checker

## Round 3: findings

- r3-1 [low, confirmed] src/cli.py:34 int() on N can raise an uncaught ValueError and produce a traceback
- r3-2 [low, refuted] src/cli.py:1 Alignment with in-flight work could not be checked
- r3-3 [low, refuted] src/cli.py:6 Presentation message imported from the domain module
- r3-4 [low, confirmed] src/cli.py:34 Oversized N raises an uncaught ValueError instead of exiting 2
- r3-5 [high, confirmed] src/cli.py:34 Oversized N now raises an uncaught ValueError instead of exiting 2
- r3-6 [low, confirmed] src/cli.py:15 sys.path bootstrap and noqa import have no explanation
- r3-7 [medium, confirmed] src/cli.py:24 Oversized N raises an uncaught ValueError instead of exiting 2
- r3-8 [medium, confirmed] src/cli.py:34 Unguarded int() raises on very long digit strings, breaking the 'never raises for bad input' promise
- r3-9 [low, confirmed] src/textstats.py:32 Changed ValueError message is a silent change to the error contract
- r3-10 [medium, confirmed] src/cli.py:32 Uncaught ValueError from int() on very long digit strings

## Round 3: needs you

none

## Round 3: doing it

none

## Round 3: needs measurement

none

## Round 3: deferred

- r3-1: Real defect: on Python 3.11+, a digit string longer than 4300 digits passes the ASCII check and int() raises an uncaught ValueError, so the CLI exits 1 with a traceback instead of 2. Not fixed in this step because you limited it to r2-11 and r2-2. Same root cause as r3-4, r3-5, r3-7, r3-8 and r3-10, so one follow-up covers all six.
- r3-4: Same defect as r3-1 (uncaught ValueError from int() on very long digit strings). Deferred with the group for the same reason: this step is limited to r2-11 and r2-2.
- r3-5: Same defect as r3-1. Severity 'high' is overstated for a local argv crash, so it should be rated low when the group is picked up. Deferred with the group.
- r3-6: Comment-only readability gap about the sys.path bootstrap. Restoring the bootstrap is in scope (r2-2), but adding an explanatory comment is not one of the two requested fixes, so it is deferred.
- r3-7: Same defect as r3-1 (uncaught ValueError from int() on very long digit strings). Deferred with the group for the same reason.
- r3-8: Same defect as r3-1 (uncaught ValueError from int() on very long digit strings). Deferred with the group for the same reason.
- r3-9: Changing the ValueError message text in top_words is a contract-documentation question, not one of the two requested fixes. It stays as is for this step and can be called out in the PR description.
- r3-10: Same defect as r3-1 (uncaught ValueError from int() on very long digit strings). Deferred with the group for the same reason.

## Round 3: gut check

Six of the eight findings (r3-1, r3-4, r3-5, r3-7, r3-8, r3-10) are one defect reported by different lenses. The ASCII digit regex admits digit runs longer than Python's int max-str-digits limit, and int() sits outside any try block, so a long N crashes with a traceback and exit 1 instead of exit 2. The reviewers agree on the cause and the fix. The fix is clear and small (wrap int() in try/except ValueError), but the directions for this step limit the work to r2-11 and r2-2, so I deferred all six. Because this is the last review round, the group will show as open at the merge gate. Decide before merge whether to take the follow-up or accept the crash for absurdly long N. r3-5's 'high' severity is overstated; the real impact is a local argv crash. The two remaining findings (r3-6, r3-9) are documentation gaps, not defects. Nothing here suggests the change is drifting from the goal or principles.

## Round 3: measurements

none

## Round 3: questions and answers

none

