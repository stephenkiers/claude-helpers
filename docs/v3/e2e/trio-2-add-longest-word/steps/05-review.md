# review

## Round 0: what was reviewed

Diff efc8a80ffc..7826aa85f1; 25 lenses swept by scouts.

## Round 0: lenses with candidates

Curious Casey, Know-It-All Nigel, Sam System, Tara TypeSafe

## Round 0: promoted to a full reviewer

Know-It-All Nigel, Security Sage, Tara TypeSafe, Sam System, North Star Nick, Curious Casey

## Round 0: findings

- r0-1 [low, refuted] src/longest.py:9 Wrong-type input raises ValueError instead of TypeError
- r0-2 [medium, confirmed] src/longest.py:14 Return type str is unchecked because words() is untyped
- r0-3 [low, refuted] src/longest.py:7 Wrong-type input raises ValueError, not TypeError; annotation and runtime check disagree
- r0-4 [low, refuted] src/longest.py:1 Strategic review blind: no scout briefs, ADR index, or in-flight snapshot available
- r0-5 [low, confirmed] src/longest.py:2 Unexplained '# registered: yes' comment
- r0-6 [low, refuted] src/longest.py:13 Non-str input raises ValueError, not TypeError

## Round 0: needs you

none

## Round 0: doing it

- r0-2: In longest_word, annotate the local that receives the words() result as `ws: list[str] = words(text)` so the type checker verifies the element type against the declared str return. Do not modify words() in src/textstats.py. Keep the existing max(ws, key=len) call and the lower-casing and tie-breaking behaviour unchanged.
- r0-5: Delete the bare '# registered: yes' comment on line 2 of src/longest.py, between the module docstring and the import. Nothing else in the repo references it, so no other change is needed.

## Round 0: needs measurement

none

## Round 0: deferred

none

## Round 0: gut check

Both findings are small hygiene issues in the new file, with no shared bad premise. r0-2 concerns a hypothetical future change to words(), so its value is modest, but the annotation is a one-line, plan-compliant fix at the call site. r0-5 removes a comment with no known consumer; the only residual risk is a tool that reads that marker, and the repo search found none. Neither finding drifts from the goal or principles.

## Round 0: measurements

none

## Round 0: questions and answers

none

## Round 1: what was reviewed

Diff 7826aa85f1..e788bed2fb; 25 lenses swept by scouts.

## Round 1: lenses with candidates

Ariadne, Business Beth, Consistency Checker, Contract Chris, Contrarian Carl, Danielle the Designer, Know-It-All Nigel, North Star Nick, Penny Pincher, Sam System, Tara TypeSafe

## Round 1: promoted to a full reviewer

Know-It-All Nigel, Security Sage, Tara TypeSafe, Sam System, North Star Nick, Business Beth, Penny Pincher, Danielle the Designer, Contract Chris, Ariadne, Contrarian Carl, Consistency Checker

## Round 1: findings

- r1-1 [low, refuted] src/longest.py:15 list[str] annotation is unenforced and breaks the empty check if words() ever returns a non-list iterable
- r1-2 [medium, refuted] src/longest.py:15 Runtime check covers elements but not the list[str] container
- r1-3 [low, refuted] src/longest.py:20 New TypeError path is not in the documented contract
- r1-4 [low, confirmed] src/longest.py: Per-element type check guards a case words() does not produce
- r1-5 [medium, refuted] src/longest.py:20 Undocumented TypeError with an internal-facing message breaks the caller's error contract
- r1-6 [medium, refuted] src/longest.py:11 Docstring omits the TypeError the new loop can raise
- r1-7 [low, refuted] src/longest.py:15 Iterator returned by words() is consumed before the emptiness check
- r1-8 [low, confirmed] src/longest.py:18 Element check is unreachable and adds an undocumented TypeError path
- r1-9 [low, refuted] src/longest.py:20 Type violation raises TypeError here but ValueError on line 14

## Round 1: needs you

none

## Round 1: doing it

- r1-4: In longest_word, delete the per-element for-loop with isinstance checks (the O(n) pass and the TypeError path). Keep `ws: list[str] = words(text)`. Reword the comment to say the annotation is a static hint only, and that words() returns only str elements for str input. Do not change words().
- r1-8: Same change as r1-4: remove the element-check loop so the unreachable branch and the undocumented TypeError are gone. Keep the list[str] annotation and correct the comment to say it is a static hint, not a runtime guarantee. Leave the docstring as ValueError-only. Do not change words().

## Round 1: needs measurement

none

## Round 1: deferred

none

## Round 1: gut check

Both findings are the same issue reported from two lenses: a runtime element check that cannot fire, because words() returns only str for str input and text is already verified. Both reviewers agree, so there is no disagreement to weigh. The shared bad premise is that words() might need guarding; the fix follows directly from directive 1 (words() stays unchanged), and the P-errors principle favours not adding an undocumented exception path. No drift from the goal.

## Round 1: measurements

none

## Round 1: questions and answers

none

## Round 2: what was reviewed

Diff e788bed2fb..be800e9362; 25 lenses swept by scouts.

## Round 2: lenses with candidates

Ariadne, Code Rot Cody, Consistency Checker, Contract Chris, Contrarian Carl, Curious Casey, Danielle the Designer, Eric Evans, Fragile Feynman, North Star Nick, Sam System, Tara TypeSafe, Vera Verifier

## Round 2: promoted to a full reviewer

Security Sage, Tara TypeSafe, Sam System, North Star Nick, Eric Evans, Curious Casey, Vera Verifier, Danielle the Designer, Code Rot Cody, Fragile Feynman, Contract Chris, Ariadne, Contrarian Carl, Consistency Checker

## Round 2: findings

- r2-1 [low, confirmed] src/longest.py:15 Runtime element check removed; return type now relies on an unenforced contract in words()
- r2-2 [medium, confirmed] src/longest.py:17 Element type of words() is no longer checked at runtime, so a violation breaks the str return contract silently
- r2-3 [low, confirmed] src/longest.py:15 Runtime element check removed at the words() boundary; contract now relies on an unannotated function
- r2-4 [medium, refuted] src/longest.py:15 Runtime element-type guard removed without a recorded decision
- r2-5 [low, confirmed] src/longest.py:16 Element-type guarantee now rests on an unstated dependency on textstats.words()
- r2-6 [low, confirmed] src/longest.py:16 Comment asserts words() guarantee without pointing to where it is enforced
- r2-7 [low, confirmed] src/longest.py:15 Runtime element check removed; list[str] contract now rests on an unenforced invariant in words()
- r2-8 [medium, confirmed] src/longest.py:15 Removed runtime element check leaves longest_word's str return contract resting on an undocumented guarantee of words()
- r2-9 [low, confirmed] src/longest.py:15 Removed element guard leaves the str contract resting on an unannotated dependency

## Round 2: needs you

none

## Round 2: doing it

- r2-1: Restore the runtime element check in longest_word: after `ws = words(text)`, loop over ws and raise TypeError if any element is not a str (the loop removed in this diff). Replace the assertion-style comment with a short note that the check guards the words() contract. Leave words() untouched.
- r2-2: Same change as r2-1: restore the isinstance loop in longest_word so a non-str element raises TypeError at the boundary. Do not annotate words() (that would change it, which the directions forbid).
- r2-3: Same change as r2-1: restore the per-element isinstance check in longest_word raising TypeError. Keep words() unchanged and keep the existing `if not ws` ValueError path.
- r2-5: Same change as r2-1: restore the runtime str check in longest_word, which makes the dependency on words() enforced rather than only described. Remove or reword the comment so it no longer states the words() guarantee as fact.
- r2-6: Same change as r2-1: restore the runtime check so the guarantee is enforced in code. With the check in place, the comment that describes words() can be dropped.
- r2-7: Same change as r2-1: restore the isinstance loop that raises TypeError for non-str elements from words(), so drift fails at this boundary.
- r2-8: Restore the runtime str check in longest_word raising TypeError, and add to the longest_word docstring that it raises TypeError when words() yields a non-str element. Keep the ValueError line for bad input, and keep words() unchanged.
- r2-9: Same change as r2-1: restore the element check in longest_word. Keep words() unchanged. Reword the comment only if one remains, so it does not claim the guarantee holds here by itself.

## Round 2: needs measurement

none

## Round 2: deferred

none

## Round 2: left open

This was the last review round allowed; the doing-it items above will not be fixed automatically and are listed as open.

## Round 2: gut check

All nine findings are one issue: the removed per-element str check in longest_word. That removal is the shared bad premise. The reviewers agree the current code is correct for str input (words() is `[w for w in text.lower().split() if w]`), so this is drift protection, not a live bug. The fix is cheap, needs no decision, and fits the directions and principles: P-errors favours failing at the call site, and the directions only bar changing words(), not guarding its output. Reviewers did split on the remedy: some suggested annotating words() with `-> list[str]`, which the directions forbid, so I chose the runtime guard in longest_word instead. The test-based alternatives were not chosen because a test cannot easily force words() to yield non-str output. Because this is the last round, these items will be open at the merge gate if they are not fixed, so they are all in doing_it.

## Round 2: measurements

none

## Round 2: questions and answers

none

