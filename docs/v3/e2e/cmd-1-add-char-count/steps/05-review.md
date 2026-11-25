# review

## Round 0: what was reviewed

Diff aa5969eb03..ac007ba751; 25 lenses swept by scouts.

## Round 0: lenses with candidates

Code Rot Cody, Consistency Checker, Contract Chris, Contrarian Carl, Curious Casey, Fragile Feynman, Know-It-All Nigel, Tara TypeSafe, Vera Verifier

## Round 0: promoted to a full reviewer

Know-It-All Nigel, Security Sage, Tara TypeSafe, North Star Nick, Curious Casey, Vera Verifier, Code Rot Cody, Fragile Feynman, Contract Chris, Contrarian Carl, Consistency Checker

## Round 0: findings

- r0-1 [medium, confirmed] tests/test_chars.py:5 Tests are module-level asserts, so pytest never runs them as tests
- r0-2 [low, confirmed] tests/test_chars.py:3 Tests rely on bare assert, which is stripped under python -O
- r0-3 [medium, confirmed] src/chars.py:7 Public function has no type annotations
- r0-4 [medium, confirmed] tests/test_chars.py:5 Tests rely on bare assert statements that vanish under python -O
- r0-5 [low, refuted] src/chars.py:6 NORTH STAR BLIND: in-flight work and milestone scope could not be checked
- r0-6 [medium, confirmed] tests/test_chars.py:6 Tests are bare module-level asserts that vanish under python -O and are invisible to pytest
- r0-7 [low, confirmed] src/chars.py:3 Bare '# registered: yes' comment is unexplained
- r0-8 [low, confirmed] src/chars.py:12 'P-errors' jargon and deviation from TypeError lack a rationale
- r0-9 [low, refuted] src/chars.py:7 Docstring does not call out that zero-width characters are counted
- r0-10 [low, confirmed] tests/test_chars.py:1 Documented code-point-not-grapheme behaviour is untested
- r0-11 [low, confirmed] tests/test_chars.py:21 T5 depends on an invisible precomposed-character encoding
- r0-12 [low, confirmed] src/chars.py:3 Stray '# registered: yes' marker with no registry in the repo
- r0-13 [low, confirmed] src/chars.py:6 char_count is test-only: no production callers
- r0-14 [low, confirmed] tests/test_chars.py:5 Test checks are bare module-level asserts that vanish under python -O
- r0-15 [low, confirmed] tests/test_chars.py:3 Contract tests are bare asserts that vanish under python -O

## Round 0: needs you

none

## Round 0: doing it

- r0-1: In tests/test_chars.py, wrap each module-level check (T1-T6) in its own named test function (e.g. test_excludes_whitespace, test_empty_and_whitespace_only_are_zero, test_non_str_raises_value_error). Use pytest.raises(ValueError) for the non-str case, or unittest assertRaises if pytest is not already a dependency. Remove the import-time asserts and the assert False fallback. Result: pytest collects named tests and the checks survive python -O.
- r0-2: Same change as r0-1: convert the bare asserts to test functions using pytest/unittest assertions. Not a separate edit; implemented by the r0-1 change.
- r0-4: Same change as r0-1: replace bare asserts and the assert False in T6 with assertEqual/assertRaises (or pytest equivalents) inside test functions. Not a separate edit; implemented by the r0-1 change.
- r0-6: Same change as r0-1: one test_* function per behaviour, with a descriptive name. Not a separate edit; implemented by the r0-1 change.
- r0-14: Same change as r0-1: test functions plus pytest.raises(ValueError) in place of try/except with assert False. Not a separate edit; implemented by the r0-1 change.
- r0-15: Same change as r0-1: test functions with pytest.raises for the T6 case. Not a separate edit; implemented by the r0-1 change.
- r0-3: Change the signature in src/chars.py to `def char_count(text: str) -> int:`. Keep the runtime isinstance check. Do not change behaviour.
- r0-8: In src/chars.py, replace the bare '# P-errors requires ValueError...' comment with one that says what P-errors is (the project principle 'Bad input raises': a function given bad input raises ValueError rather than returning a sentinel) and why ValueError is used instead of TypeError. Add the same one-sentence rationale to the docstring's Raises section.
- r0-10: Add a test in tests/test_chars.py asserting char_count('é') == 2, written with an escape so the combining mark is visible. Its docstring/comment should say the count is per code point, not per grapheme cluster.
- r0-11: In tests/test_chars.py, write the literal in the T5 assertion as 'héllo' (escaped) instead of the raw precomposed character, so normalisation cannot change it silently. Expected value stays 5.

## Round 0: needs measurement

- r0-7: grep -rn -i "registered" --exclude-dir=.git . ; ls -la .claude 2>/dev/null; cat .claude/flow.yaml 2>/dev/null
- r0-12: grep -rn -i "registered\|registr\|manifest" --exclude-dir=.git . ; ls -la .claude 2>/dev/null; cat .claude/flow.yaml 2>/dev/null

## Round 0: deferred

- r0-13: The plan's non-goals say no CLI wiring and no caller changes outside the library. char_count is meant to be a library function that nothing calls yet, so having no production callers is the intended state for this ticket, not a defect. Revisit when a real caller exists.

## Round 0: gut check

Most findings share one root cause: tests/test_chars.py uses module-level bare asserts, so pytest collects no tests and python -O strips the checks. That accounts for r0-1, r0-2, r0-4, r0-6, r0-14 and r0-15, so one rewrite fixes all six; the reviewers agree on it. The other findings are small documentation and typing nits, and the two '# registered: yes' findings (r0-7, r0-12) are the same comment seen from two angles. Nothing here drifts from the goal or the non-goals, apart from r0-13, which is expected by the plan. The one unverified premise is whether a registry such as .claude/flow.yaml reads that marker, which is why it goes to measurement rather than removal.

## Round 0: measurements

- r0-7: `grep -rn -i "registered" --exclude-dir=.git . ; ls -la .claude 2>/dev/null; cat .claude/flow.yaml 2>/dev/null` not run (listed at the merge gate)
- r0-12: `grep -rn -i "registered\|registr\|manifest" --exclude-dir=.git . ; ls -la .claude 2>/dev/null; cat .claude/flow.yaml 2>/dev/null` not run (listed at the merge gate)

## Round 0: questions and answers

none

## Round 1: what was reviewed

Diff ac007ba751..9be1a23a83; 25 lenses swept by scouts.

## Round 1: lenses with candidates

Code Rot Cody, Consistency Checker, Contract Chris, Contrarian Carl, Curious Casey, North Star Nick

## Round 1: promoted to a full reviewer

Security Sage, North Star Nick, Curious Casey, Code Rot Cody, Contract Chris, Contrarian Carl, Consistency Checker

## Round 1: findings

- r1-1 [low, confirmed] src/chars.py:15 Lost link to the P-errors principle source
- r1-2 [low, confirmed] src/chars.py:6 Symbol only called from tests: char_count
- r1-3 [low, confirmed] src/chars.py:15 ValueError-for-type-errors departs from Python convention, and the rationale is duplicated
- r1-4 [low, confirmed] src/chars.py:15 Principle cited by name only, not by its defined tag P-errors

## Round 1: needs you

none

## Round 1: doing it

- r1-4: In src/chars.py, cite the principle by its defined tag, as 'P-errors' (e.g. 'per P-errors, Bad input raises'), matching docs/principles.md:4. Keep the ValueError rationale in the docstring only, and reduce the inline comment at about line 15 to a bare pointer such as '# See P-errors.'
- r1-1: Restore the P-errors tag in the code comment and point it at docs/principles.md, so a search for 'P-errors' finds the rule. Do this in the same edit as r1-4 so the comment is written once. Target wording: '# Bad input raises ValueError (P-errors, see docs/principles.md), not TypeError.' Keep the docstring to one rationale statement that also names P-errors.
- r1-3: Remove the duplicated rationale. State the ValueError-over-TypeError deviation once, in the docstring, with a pointer to P-errors. Drop the inline comment body, leaving at most a bare pointer. Do not add a test for the speculative TypeError-catching concern. Apply this in the same edit as r1-1 and r1-4.

## Round 1: needs measurement

none

## Round 1: deferred

- r1-2: char_count is meant as a library function for later use, and the plan sets no CLI or production caller in this ticket. Test-only use is expected here, so no change is needed now. Revisit when a caller is added.

## Round 1: gut check

All four findings concern the same few lines of src/chars.py: the P-errors citation and a duplicated rationale. They are comment and docstring wording, not behaviour. The reviewers agree the ValueError decision is sound and documented, so nothing drifts from the goal or principles. The three doing_it items touch the same comment and should be applied as one edit to avoid conflicting changes. The r1-3 interop concern about callers catching TypeError is speculative and was not acted on.

## Round 1: measurements

none

## Round 1: questions and answers

none

## Round 2: what was reviewed

Diff 9be1a23a83..f5675e01a2; 25 lenses swept by scouts.

## Round 2: lenses with candidates

Contrarian Carl, Curious Casey, Danielle the Designer, Data Scientist Dana, Fact-Check Fiona, Know-It-All Nigel, Sam System, Tara TypeSafe

## Round 2: promoted to a full reviewer

Know-It-All Nigel, Security Sage, Tara TypeSafe, Sam System, North Star Nick, Curious Casey, Danielle the Designer, Data Scientist Dana, Contrarian Carl, Fact-Check Fiona

## Round 2: findings

- r2-1 [low, confirmed] src/chars.py:6 Runtime input contract is not expressed in the signature
- r2-2 [low, refuted] src/chars.py:7 NORTH STAR BLIND: in-flight and ADR inputs were not available
- r2-3 [low, confirmed] src/chars.py:14 Rationale for ValueError over TypeError moved out of the code
- r2-4 [low, refuted] src/chars.py:3 Unexplained '# registered: yes' marker
- r2-5 [low, confirmed] src/chars.py:14 Rationale for ValueError over TypeError reduced to a pointer
- r2-6 [medium, confirmed] src/chars.py:10 Docstring attributes the ValueError-over-TypeError choice to P-errors, which does not state it

## Round 2: needs you

none

## Round 2: doing it

- r2-6: In the char_count docstring and the comment at the raise (around src/chars.py:9-14), stop attributing the TypeError contrast to P-errors. Cite only what the principle states: bad input raises ValueError and never returns a sentinel (docs/principles.md:5). State the TypeError-vs-ValueError choice as a local decision, with its rationale: ValueError is used so every rejected input is handled the same way. Keep the pointer to P-errors for the ValueError-on-bad-input part only.
- r2-3: Restore the one-line rationale next to the raise in src/chars.py, in the same edit as r2-6. For example: '# ValueError, not TypeError, so every rejected input is handled the same way. Bad input raises (P-errors, docs/principles.md).' Do not leave a bare '# See P-errors.' comment.
- r2-1: Keep the signature as `text: str`, since the plan describes a str-only function and the principle is about runtime behaviour. Make the runtime guard explicit in the docstring: state that non-str input raises ValueError even though the annotation says str, because untyped callers (Any, json data) can still pass other values. Do not add hypothesis or any other dependency, because the plan's goal is a dependency-free library. If a test is wanted, use plain pytest parametrize over non-str values (int, None, bytes) asserting ValueError, and do not touch tests/test_textstats.py.

## Round 2: needs measurement

none

## Round 2: deferred

- r2-5: Duplicate of r2-3: both flag the same removed rationale for ValueError over TypeError at the same line. The r2-3 fix restores that rationale, so this finding is closed by that change and needs no separate work.

## Round 2: left open

This was the last review round allowed; the doing-it items above will not be fixed automatically and are listed as open.

## Round 2: gut check

All four findings come from one premise: the ValueError-over-TypeError choice is documented, and the in-code explanation was lost when the comment became a bare pointer to P-errors. The fix is to put the rationale back and cite the principle only for what it actually says. The change stays inside the plan's non-goals and does not drift from the principles. Reviewers disagreed on severity: r2-5 called it probably fine and not blocking, while r2-6 rated it medium for citation accuracy. I treated the citation problem as the real one, since the comment attributes a claim to a principle that does not make it. Since this is the last review round, the three doing_it items must land in this pass or they will be listed open at the merge gate.

## Round 2: measurements

none

## Round 2: questions and answers

none

