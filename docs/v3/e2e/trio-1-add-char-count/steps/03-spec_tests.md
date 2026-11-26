# spec_tests

## Spec tests

Wrote tests/test_chars.py covering T1–T8, following the existing tests/test_textstats.py style: plain asserts, a sys.path insert so `from src.chars import char_count` works, and run as a script. Non-ASCII characters (NBSP, zero-width space, ideographic space) are written as \u escapes so the intent is visible in the source. T7 loops over 5, None and b"abc", catching only ValueError and raising AssertionError if nothing is raised. I did not run the tests.

Risk outside my allowed files: tools/style.py fails for any src/*.py other than __init__.py and textstats.py unless the file contains the exact line `# registered: yes`. Creating src/chars.py will therefore fail check.py until that line is added. I was not permitted to edit src/chars.py, so this needs to be handled by whoever implements it.

## Files

- tests/test_chars.py

## Commit

4dfaedbb748a6b94e388f824ee8b9993d1da55c2

