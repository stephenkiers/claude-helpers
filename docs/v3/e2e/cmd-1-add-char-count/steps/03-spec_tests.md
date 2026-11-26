# spec_tests

## Spec tests

Wrote tests/test_chars.py covering T1–T6. It follows the existing tests/test_textstats.py style: plain asserts, a sys.path insert, and `from src.chars import char_count`. T1 checks mixed spaces, tab and newline give 4. T2 checks the empty string and whitespace-only input give 0. T3 checks "abc" gives 3. T4 checks the ideographic space (U+3000) and non-breaking space (U+00A0) are excluded. T5 checks "héllo" gives 5. T6 checks None and 123 each raise ValueError, using try/except with a failing assert if nothing is raised. Non-ASCII characters are written as escapes so the file stays ASCII. I did not run the tests, as instructed. I did not touch any other file.

## Files

- tests/test_chars.py

## Commit

728a9966c7edfe148f5b252429256f0d025e92c8

