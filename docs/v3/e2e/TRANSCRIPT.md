# Flow v3: sandbox end-to-end runs (2026-11-25)

Three sandbox repositories, each a bare origin plus a clone under a temporary directory, `local`
provider, every role on `haiku` (`models: {"*": haiku}`) except the one escalation the driver makes
itself. `FLOW3_HOME` pointed into the sandbox. Paths in the saved records are scrubbed
(`<sandbox>`, `<tmp>`, `<home>`).

Each sandbox is a tiny Python library (`src/textstats.py`, plain-script tests, `check.py`) with a
`docs/principles.md` holding one principle, `P-errors` ("a function given bad input raises
`ValueError`"). The injected check failure: `tools/style.py`, run by the check, fails any new
`src/*.py` that lacks the line `# registered: yes`. No ticket mentions it.

Costs are the figures the binary reported, which are notional.

## 1. Single ticket (`solo`): issue text to merged

```
python3 -m scripts.flow3 doctor --repo-root <sandbox>/solo-box/solo
python3 -m scripts.flow3 run 1 --repo-root <sandbox>/solo-box/solo
python3 -m scripts.flow3 inbox --repo solo
printf '%s' "Keep it silent, an empty result is not an error. Also from here on, do not add any new files beyond what the plan names." | python3 -m scripts.flow3 answer 1 --repo solo --wait
printf '%s' "r1-12: fix" | python3 -m scripts.flow3 answer 1 --repo solo --wait
printf '%s' "Fix both: put the ASCII digit check back, and restore the sys.path bootstrap so running the file by path works. After this, do not raise further behaviour questions about the CLI; leave anything else as it is." | python3 -m scripts.flow3 answer 1 --repo solo --wait
python3 -m scripts.flow3 gate 1 --repo solo
python3 -m scripts.flow3 merge 1 --repo solo --send-back "Do the two fixes I asked for and nothing else: r2-11 ..., r2-2 ..."
python3 -m scripts.flow3 merge 1 --repo solo --merge
```

- Parked three times on review cards; each was answered in free text. The interpreter split the
  third reply into two answers and two standing directions (`human/` holds the reply, its
  interpretation, and one file per direction).
- The two `fix` answers given in the last allowed review round were not run until the send-back
  (changed since: see REPORT, question 1). The send-back ran one fix unit, a review of the fix diff, and shipped again.
- Result: `done`, $0.28, 122 calls. Sandbox `main`:

```
65aa7c8 Add top_words to textstats and a CLI entry point
634bb4a init
```

Records: `solo-1-add-top-words-to-textstats/`.

## 2. Fleet of three (`trio`)

Tickets: #1 add `char_count`, #2 add `longest_word`, #3 add a `longest` command that depends on #2.

```
python3 -m scripts.flow3 fleet start "1,2>3" --max 2 --repo-root <sandbox>/trio-box/trio
python3 -m scripts.flow3 fleet status 20261125-210129-d13e
python3 -m scripts.flow3 inbox --repo trio
printf '%s' "#2: blank text is bad input so raise ValueError, and when two words tie the first one in the text wins. Lower-cased is fine, reuse words() as it is. And keep the tests plain scripts with bare asserts like the existing test file, no test framework." | python3 -m scripts.flow3 reply --repo trio
printf '%s' "Punctuation stays part of the word; keep whatever words() gives." | python3 -m scripts.flow3 answer 2 --repo trio
python3 -m scripts.flow3 merge 1 --repo trio --merge     # then 2, then 3, each after reading its gate
```

- #1 and #2 ran at once; #3 waited for #2 to merge, as its dependency requires. With `--max 2` and
  that dependency, three tickets were never running in the same instant.
- #2 parked at plan with four questions while #1 kept going to its merge gate. One free-text reply
  answered three of them and set two directions; the interpreter left the fourth (punctuation) open
  rather than guess, and a second short answer closed it.
- Injected check failure: in #1 and #2 (and #3) the check went red on the missing marker line and
  was green after fixer round 1 (`Heal (check-work)` in each journal; six such events in #1).
- #2 was behind base after #1 merged: `Heal (base-stale): merged origin/main into the branch`.
- #3: its plan question Q1 was answered by principle `P-errors` with no human. One review card was
  answered in free text. Its fix stage stopped twice on a fixer that could not finish (the remaining
  work was in protected spec tests); both were driver defects, fixed during the run (PROGRESS
  decisions 29 and 30), and the flow then resumed to its gate.
- Merged one at a time, each from its gate. Results: #1 $0.10, #2 $0.13, #3 $0.37. Sandbox `main`:

```
de68ac5 Add longest_cli command to print the longest word
9d0616e Add longest_word helper that returns the longest word in text
2c3ed29 Add char_count library function
efc8a80 init
```

Records: `trio-1-add-char-count/`, `trio-2-add-longest-word/`, `trio-3-add-a-longest-command/`; gate
screens `trio-2-gate.txt`, `trio-3-gate.txt`.

## 3. Command merge (`cmd`)

`.claude/flow.yaml` has `merge: {method: command, command: "sh tools/merge.sh {branch} {base} {head}"}`;
the script pushes the tested head to base.

```
python3 -m scripts.flow3 run 1 --repo-root <sandbox>/cmd-box/cmd
python3 -m scripts.flow3 merge 1 --repo cmd --merge
python3 -m scripts.flow3 resume cmd 1
```

- The first merge ran the script correctly, then stopped: the local provider still said the PR was
  open. Fixed (decisions 27 and 28); the resume saw the PR as merged without running the script
  again and finished.
- Result: `done`, $0.12. Sandbox `main` (the script pushes the branch as it is, so no squash):

```
f5675e0 flow3 #1 fix a1-1: r1-1, r1-3, r1-4
9be1a23 flow3 #1 fix a1-1: r0-3, r0-8
ac007ba flow3 #1 unit U1: Implement char_count in src/chars.py
728a996 flow3 #1: spec tests
aa5969e init
```

Records: `cmd-1-add-char-count/`, gate screen `cmd-gate.txt`.

## 4. Doctor rejects a red check

`doctor-red.txt`: a sandbox whose full check exits 1; doctor fails and `run` refuses to start.

## 5. `why`, one decision per source

`why.txt` holds the output of `flow3 why` for:

| Source | Where it came from |
|---|---|
| human | trio #2 `empty-text`, from the fleet reply |
| principle | trio #3 `Q1`, answered by `P-errors` in the run |
| assumed | `punctuation` in an earlier, abandoned fleet run |
| fact | a question fed by hand through the resolver against a finished sandbox flow |

No step asked a `fact` question on its own in these runs, so that source was exercised only by the
hand-fed probe (same resolver code, real scout call). The one `assumed` answer inside a merged flow
(trio #3, `r1-2-test-location`) was a defect: see REPORT.
