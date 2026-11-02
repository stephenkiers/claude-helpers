# coverage-fix-358 fixture

Real artifacts from the #358 `/expert-flow` run, used to reproduce and then pin the fix-mode
coverage classification (#409). Nothing here is edited except the trimming noted below.

| File | Source (local, not in the repo) |
|------|---------------------------------|
| `claude-action-plan-doing-it.md` | Lines 77-102 of `~/.claude/reviews/stephenkiers-claude-helpers/feature-358-expert-flow-flow-review-flow-implement-step-agents-1904b06-20261027T130808-08164/claude-action-plan.md`: the `## Doing it: 18` section, copied verbatim. Only the text before the next `##` heading is kept, so the Needs-you and Deferred sections are dropped. All 18 Doing-it rows are kept. |
| `05-fix.md` | `~/.claude/flows/stephenkiers_claude-helpers/358-expert-flow-flow-review-flow-implement-20261027T200714/steps/05-fix.md`, copied verbatim. Fix receipt with `OUTSTANDING: none` and `Decision: OK`. |
| `05-fix.coverage.txt` | `~/.claude/flows/stephenkiers_claude-helpers/358-expert-flow-flow-review-flow-implement-20261027T200714/steps/05-fix.coverage.txt`, copied verbatim. The observed output: `UNITS: 18`, and every row `unknown` with `no extractable evidence (no files in table row)`. This is the all-`unknown` baseline that #409 fixes. |

The Doing-it table has six columns (`# | Severity | Finding | File | Raised by | Fix`). The File
column is index 3 by header text. Rows 1/2/3/5/6/16 name `scripts/flow-bash-guard.py`, rows 7 and 8
name `agents/flow-review.md`, rows 4 and 9 name `tests/test_flow_hook_guards.py`, rows 10/11/12 name
`commands/expert-flow.md`, and rows 14 and 17 name `tests/test_flow_step_agents.py`. Row 7 also names
`CLAUDE.md` (no `/`), which is only in the diff when the test supplies it. Fragments such as
`1758-1777`, `~1350` and `~1648` are not paths.

The tests in `tests/test_flow_verify_receipt_coverage_fix_table.py` use these files. They stub the
diff list (`_git_diff_names`) rather than reading a git repo.
