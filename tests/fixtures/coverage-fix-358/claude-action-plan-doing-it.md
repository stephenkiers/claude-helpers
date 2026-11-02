## Doing it: 18

Accepted as written. No **decision** needed from you, but these still need doing.

⤴ F1, F2, F3, F6, F7 and F20 dissolve if cluster option A (Needs you #1) is chosen.

| # | Severity | Finding | File | Raised by | Fix |
|---|----------|---------|------|-----------|-----|
| 1 | HIGH | F1 Tokenizer drops `word=value` tokens | scripts/flow-bash-guard.py:~70-80 | ariadne | Strip assignments only for leading words, or `shlex` per segment; add tests for each `=` spelling |
| 2 | HIGH | F2 `git -c` skipped before the deny test | scripts/flow-bash-guard.py:~121-143 | tara-typesafe | Test `-c`/`--git-dir`/`--exec-path` first, then skip `-C`; share one git option walker |
| 3 | HIGH | F3 `remote add`/`set-url` verbs never match | scripts/flow-bash-guard.py:~866-871 | security-sage | Compare token windows, store verbs as tuples, drop the loose `startswith` |
| 4 | HIGH | F4 Wrapper tests pass via `git push`; Test 8.5 pins `git -c` allow | tests/test_flow_hook_guards.py:1690-1697, 1758-1777 | vera-verifier | Use inner commands only the wrapper path can catch; flip 8.5 once F2 lands |
| 5 | MEDIUM | F6 Verb detection misses global options, quoting, subshells | scripts/flow-bash-guard.py:~856-882 | security-sage | Shared option walker, `gh -R/--repo` skip, `$(`/`(`/`{` separators, `shlex` quotes |
| 6 | MEDIUM | F7 Flattened segments cause false positives | scripts/flow-bash-guard.py:733-752 | know-it-all-nigel | Return per-command token lists, run checks per segment, delete the dead branch |
| 7 | MEDIUM | F8 `flow-review` has no Bash guard but the docs say "as `flow-implement`" | agents/flow-review.md:5-12; CLAUDE.md | north-star-nick | State in ADR-0008 and CLAUDE.md that the Bash guard is `flow-implement`-only, and why |
| 8 | MEDIUM | F9 `flow-review` grants `Edit` and `AskUserQuestion` for one file | agents/flow-review.md:5 | contrarian-carl | Drop `AskUserQuestion` unless needed; add a tool-list test; acknowledge `Edit` in the ADR |
| 9 | MEDIUM | F11 Guard tests miss error paths and per-entry behaviour | tests/test_flow_hook_guards.py | curious-casey | Parametrize one test per verb and wrapper entry; add edge cases; make the drift equality unconditional |
| 10 | MEDIUM | F12 STOP line list omits the hot-load variant | commands/expert-flow.md:~971, ~1350, ~1648 | consistency-checker | List every variant in Stop lines; distinguish the three states and the `live` value each leaves |
| 11 | LOW | F13 Never-started detection keys on an exact message | commands/expert-flow.md:~966-972 | fragile-feynman | Record the probed version beside the string and add a test pinning it |
| 12 | LOW | F14 Attempt counter consumed on never-started spawn | commands/expert-flow.md:~970 | mozart | State that no failure is recorded; decrement `attempts` or document that `k` may skip |
| 13 | LOW | F15 Hard-coded cutover date and probe count | scripts/flow_agent_buckets.py:~20 | fragile-feynman | Set the date at merge time; count distinct session ids or relabel; document the exclusion |
| 14 | LOW | F17 Allowlist test checks overlap, scans the wrong file | tests/test_flow_step_agents.py:2244-2260 | curious-casey | Assert `spawns <= allowed` and non-empty; include `prompts/expert-review-panel.md` |
| 15 | LOW | F19 Hook `main()` contracts undocumented; non-dict payload | scripts/flow-agent-guard.py:~625-660 | contract-chris | Validate `isinstance(payload, dict)` after `json.load`; add exit-code docstrings |
| 16 | LOW | F20 `check_wrapper_forms` too long; `WRAPPER_FORMS` unread | scripts/flow-bash-guard.py | uncle-bob | Split into predicates; drive from the table or rename and test per entry |
| 17 | LOW | F21 Brittle test parsing and stale comments | tests/test_flow_step_agents.py | curious-casey | Fail rather than skip on missing files; real YAML parser; behavioural comments |
| 18 | LOW | F22 Skill guard namespaced names untested | scripts/flow-skill-guard.py:998-1004 | security-sage | Add a test for the namespaced form |
