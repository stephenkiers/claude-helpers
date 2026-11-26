# spec_tests

## Spec tests

Wrote tests/test_longest_cli.py covering T0–T8. It uses the same sys.path header as tests/test_longest.py and plain module-level asserts, since that is the only style the existing tests use. Tests capture stdout and stderr with contextlib, so they check output without touching the terminal. T1–T3 check exit code, stdout and empty stderr for the success cases. T4–T7 check that ValueError is raised and nothing is printed. T8 is an `if __name__ == "__main__": sys.exit(0)` block. I did not run the tests, per the instructions. No other files were changed.

## Files

- tests/test_longest_cli.py

## Commit

1d3ed6a392f547c112048172591a1ea2c190f778

