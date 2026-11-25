# implement

## Unit U1: Implement top_words in textstats

Questions asked: 0. Commit: 75595e7be3816ca66cd6e8cd6a909402e63354fc. Heals: 0. Files: src/textstats.py. U1 done. In src/textstats.py I added `from collections import Counter` at the top and a new `top_words(text, n)` after `word_count`. It raises ValueError if text is not a str, or if n is not an int, is a bool, or is not greater than 0; these checks run before words() is called. It then counts words(text) with Counter and returns the items sorted by (-count, word), sliced to n. The docstring states the ordering, the fewer-than-n case, and the empty-text case. The `# style: ok` line is still the last line of the file. I did not run the check, per instructions. Spec tests for later units were still red here: tests/test_cli.py.

## Unit U2: Add CLI entry point main(argv)

Questions asked: 0. Commit: 1194c4ab4d8b5c0f5247fed27767ad203fbd8f34. Heals: 0. Files: src/cli.py. Created src/cli.py (the only file touched). It has a module docstring and main(argv) -> int. Wrong argument count prints 'usage: cli.py TEXT N' to stderr and returns 2. A non-integer n prints 'n must be a positive integer' to stderr and returns 2. A ValueError from top_words prints its message to stderr and returns 2. On success it prints 'word count' per result to stdout and returns 0. The __main__ guard calls raise SystemExit(main(sys.argv[1:])). The file ends with '# style: ok'. It imports top_words from src.textstats, which already exists with the specified signature. I did not run check.py, per instructions.

## Test edits

(none)

## Judgements

(none)

