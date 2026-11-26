# implement

## Unit U1: Implement longest_cli.main

Questions asked: 0. Commit: 99fa2322fad0b305b42722bcea96f0c8a27afaec. Heals: 0. Files: src/longest_cli.py. Created src/longest_cli.py. It has a module docstring stating the argv contract, return value, stdout-only-the-word, ValueError cases, and lower-case/punctuation behaviour, followed by the '# registered: yes' marker. It imports longest_word from src.longest and defines main(argv: list[str]) -> int. main raises ValueError('expected exactly one text argument, got %d') when len(argv) != 1, otherwise calls longest_word(argv[0]), prints the result, and returns 0. A blank argument's ValueError propagates uncaught. An __main__ block calls sys.exit(main(sys.argv[1:])). The main docstring repeats the module contract. No other files were touched, and I did not run the project's check.

## Test edits

(none)

## Judgements

(none)

