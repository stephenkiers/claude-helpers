# spec_tests

## Spec tests

Wrote tests/test_top_words.py and tests/test_cli.py, covering T1–T9. Both use the same sys.path bootstrap as tests/test_textstats.py and bare asserts, so each runs as a script. test_top_words.py covers counts, tie ordering, n larger than the distinct count, empty/whitespace text, and ValueError for non-str text and bad n (0, -1, True, '2', 1.5). test_cli.py covers the exact stdout for a valid run, return 2 with empty stdout and a non-empty stderr for bad n, and return 2 with 'usage' on stderr for wrong arg count. I did not run the tests, per the instructions. No other files were touched.

## Files

- tests/test_top_words.py
- tests/test_cli.py

## Commit

6bca6a70330571d3e373d37824d39a2f65559b08

