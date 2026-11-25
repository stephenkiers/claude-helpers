# spec_tests

## Spec tests

Wrote tests/test_longest.py covering T1–T8, following tests/test_textstats.py: same sys.path setup, bare asserts, and a try/except flag for the ValueError cases (T6–T8, with T8 looping over None and 42). Tests import longest_word from src/longest.py, which does not exist yet, so they will fail with an ImportError until the change is built. I did not run them, and no other files were touched. The tests check only the public behaviour of longest_word and do not depend on words(). Note that T2 is also satisfied by words() lower-casing, so the implementation must keep that.

## Files

- tests/test_longest.py

## Commit

5d836640f5facd60a8334ba47307088a5e065823

