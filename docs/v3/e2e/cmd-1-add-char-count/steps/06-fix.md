# fix

## Given

- Finding r0-3 [medium] Public function has no type annotations at src/chars.py:7
  Problem: `char_count(text)` is unannotated, so its contract (str in, int out) lives only in the docstring and the runtime check. Type checkers cannot verify callers, and the diff adds no typed contract for the one function it introduces. The runtime isinstance guard exists precisely because the type is not expressed statically.
  Fix: Change the signature in src/chars.py to `def char_count(text: str) -> int:`. Keep the runtime isinstance check. Do not change behaviour.
- Finding r0-8 [low] 'P-errors' jargon and deviation from TypeError lack a rationale at src/chars.py:12
  Problem: The comment cites 'P-errors' without defining it or linking to a source. The deviation from Python's convention (ValueError rather than TypeError for wrong type) is a decision a newcomer would question, and nothing explains it. The docstring repeats the ValueError behaviour but not why.
  Fix: In src/chars.py, replace the bare '# P-errors requires ValueError...' comment with one that says what P-errors is (the project principle 'Bad input raises': a function given bad input raises ValueError rather than returning a sentinel) and why ValueError is used instead of TypeError. Add the same one-sentence rationale to the docstring's Raises section.

## Did

- a1-1: commit 9be1a23a837e99b3dada023f33c15cf24ba65354. Edited src/chars.py only. (1) r0-3: changed the signature to `def char_count(text: str) -> int:`; the isinstance check is kept and behaviour is unchanged. (2) r0-8: replaced the bare "P-errors" comment with one naming the project principle "Bad input raises" (bad input raises ValueError rather than returning a sentinel) and explaining that ValueError is used instead of TypeError so callers handle all rejected input the same way. The same rationale is added to the docstring's Raises section. I did not run the project's check, and I did not touch tests/test_chars.py.

- Left open, the finding is in a protected file a fixer may not edit: r0-1, r0-2, r0-4, r0-6, r0-10, r0-11, r0-14, r0-15.
- Attempt 1: addressed r0-3, r0-8; not addressed none.

## Fixed

- r0-3
- r0-8

## Left open

- r0-1
- r0-2
- r0-4
- r0-6
- r0-10
- r0-11
- r0-14
- r0-15

## Given

- Finding r1-1 [low] Lost link to the P-errors principle source at src/chars.py:15
  Problem: The old comment tagged the rationale as "P-errors", which points to the principle defined in docs/principles.md ("## P-errors: Bad input raises"). The new docstring and comment name the principle only by its wording, and the tag is gone. A newcomer searching the code for P-errors or following the reference now finds nothing, so the rule for why ValueError is used instead of TypeError is harder to trace back to its definition. The principle is documented, so this is a traceability loss rather than a missing explanation.
  Fix: Restore the P-errors tag in the code comment and point it at docs/principles.md, so a search for 'P-errors' finds the rule. Do this in the same edit as r1-4 so the comment is written once. Target wording: '# Bad input raises ValueError (P-errors, see docs/principles.md), not TypeError.' Keep the docstring to one rationale statement that also names P-errors.
- Finding r1-3 [low] ValueError-for-type-errors departs from Python convention, and the rationale is duplicated at src/chars.py:15
  Problem: The choice to raise ValueError instead of TypeError is deliberate and documented: docs/principles.md P-errors ('Bad input raises') states it, and its 'Revisit if' clause (a caller needs to probe inputs cheaply) is not triggered by this change. So this is not a defect in the decision itself. The risk is interop: any caller or library that uses the idiomatic `except TypeError` around a wrong-type argument will not catch this, and the function's signature now advertises `text: str` while the runtime rejects non-str with a different exception family than Python tooling expects. Separately, the same justification now appears in both the docstring (lines 9-13) and the inline comment (lines 15-16), which will drift the next time the principle changes. Rejected candidates: P-reuse was checked. src/textstats.py only has words/word_count, and char_count does not reimplement either, so no violation. The all-whitespace and code-point semantics are covered by tests/test_chars.py.
  Fix: Remove the duplicated rationale. State the ValueError-over-TypeError deviation once, in the docstring, with a pointer to P-errors. Drop the inline comment body, leaving at most a bare pointer. Do not add a test for the speculative TypeError-catching concern. Apply this in the same edit as r1-1 and r1-4.
- Finding r1-4 [low] Principle cited by name only, not by its defined tag P-errors at src/chars.py:15
  Problem: docs/principles.md:4 defines the principle as 'P-errors: Bad input raises'. The old inline comment cited the tag 'P-errors'; the new comment at line 15 and the docstring at line 10 cite only the name 'Bad input raises' and drop the tag. The tag is the stable identifier other code and docs would use to cite this principle, so the citation is now inconsistent with how the principle is referenced in the repo. The rationale (ValueError instead of TypeError) is also stated twice, in the docstring (lines 10-12) and the inline comment (lines 15-16).
  Fix: In src/chars.py, cite the principle by its defined tag, as 'P-errors' (e.g. 'per P-errors, Bad input raises'), matching docs/principles.md:4. Keep the ValueError rationale in the docstring only, and reduce the inline comment at about line 15 to a bare pointer such as '# See P-errors.'

## Did

- a1-1: commit f5675e01a2f55e970b968be7dfb4b00c037a9929. Edited only src/chars.py. The ValueError-over-TypeError rationale now appears once, in the char_count docstring, and cites the principle by its defined tag, P-errors (Bad input raises; see docs/principles.md). The inline comment is reduced to the bare pointer "# See P-errors." This covers r1-1, r1-3 and r1-4 in one edit. No tests were run, per the instructions. tests/test_chars.py was not touched.

- Attempt 1: addressed r1-1, r1-3, r1-4; not addressed none.

## Fixed

- r1-1
- r1-3
- r1-4

## Left open

(none)

