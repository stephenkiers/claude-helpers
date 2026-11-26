# Using Flow v3

Flow v3 takes a ticket through worktree, plan, spec tests, implement, review, fix, ship, the merge gate and
merge, using headless `claude -p`. It stops only when it needs you. This page is the operator's guide. The
design lives in `PLAN.md` and `INTERFACES.md`.

Run everything as `python3 -m scripts.flow3 <command>` from the claude-helpers checkout, or through the
`/flow3` slash command from any project. State lives under `$FLOW3_HOME` (default `~/.claude/flow3`).

## First time in a repo

```
python3 -m scripts.flow3 init --repo-root <project>
python3 -m scripts.flow3 doctor --repo-root <project>
python3 -m scripts.flow3 principles init --repo-root <project>
```

Nothing has to be checked into the project or added to its `.gitignore`. `init` writes the repo config to
`~/.claude/claude-helpers/<owner>.<name>.flow.yaml` (`--set KEY=VALUE` overrides a field, `--force` rewrites,
`--in-repo` writes `.claude/flow.yaml` in the project instead, for a config you want to share). When both
exist, the file outside the repo wins whole; the two are never merged. `principles init` writes
`~/.claude/claude-helpers/<owner>.<name>.principles.md` the same way (`--in-repo` writes the path `principles:`
names, and a principles file in the repo wins). `$FLOW3_CONFIG_HOME` moves the directory. `doctor` proves the
config parses and the install works in a fresh checkout of the base; `run` refuses until it has passed. It
does not run the checks: the base is assumed green. `doctor --checks` also runs them on the untouched base,
and `check.base: true` in `flow.yaml` runs the fast check on the base at the start of every ticket. The full
check runs at the merge gate. `principles init` asks for a goal and themes and writes
the starter principles the experts read.

If the repo has a commit hook that checks formatting, set `format: <command>` in `flow.yaml`: the driver
runs it before each commit it makes. If the hook also validates code (typecheck, tests), set `commit_hooks: false`: spec
tests are written before the implementation and cannot pass such a hook. Only the driver's commits skip it.

## One ticket

```
run <issue> --repo-root <project> [--title TEXT] [--detach]
```

`--detach` creates the flow, starts a background driver and returns at once. Without it the driver runs in
the foreground and prints each stage. `resume <issue>` (or `resume <repo> <issue>`) continues a flow that was
interrupted. A flow holds a lock, so two drivers never run the same flow.

When more than one repo has the same issue number, add `--repo <name>`.

## Seeing what is going on

- `status [--repo R] [--json]`: every flow, its stage, running or not, and today's cost.
- `inbox [--repo R] [--json]`: what needs you, oldest first. Four groups: questions, merge gates, stops,
  flows in progress.

## Answering

Questions arrive as cards with options. Answer with `reply`, reading stdin:

```
python3 -m scripts.flow3 reply <<'EOF'
q-12-plan: go
EOF
```

Each line is `<card id>: <option id>`; free text is also accepted and is saved as a direction. `answer <issue>`
does the same for one flow. Both detach by default; add `--wait` to run in the foreground. `say <issue> TEXT`
gives a direction without answering a card. Directions are binding for the stages after them.

`overrule <issue> <question> <option> --reason TEXT` changes an earlier answer and records why.

## The merge gate

Nothing merges without you. At the gate:

```
gate <issue>                      show the screen: the diff summary, checks, open risks
merge <issue> --merge             merge it
merge <issue> --send-back NOTES   run fix again with your notes
merge <issue> --stop [NOTES]      stop the flow
```

## When a flow stops

Exit code 2 means a human stop. `stop-screen <issue>` explains why and what the options are. `why <issue>
<query>` searches the flow's decisions and events. Fix the cause, then `resume`.

## Fleets

```
fleet start 12,13>14 --max 2 --repo-root <project>
fleet status [id]
```

`12,13>14` means 12 and 13 are independent and 14 waits for 13. A cycle or a non-number is an error. At most
`--max` tickets run at once (default 3). A ticket that is waiting for you (a question or a merge gate) does not
hold a slot. A dependant starts only once its prerequisite is merged and done; if the prerequisite stops, the
dependant shows as blocked. The fleet supervisor never answers a question or merges: you still use `inbox`,
`reply` and `merge`. It exits when nothing is left that it can start or advance.

## Exit codes

0 success, 1 an error (one line on stderr, argparse errors included), 2 the flow stopped for a human.

## Testing without a model

`FLOW3_STAGES=<module>` swaps in a stage registry from that module (see `tests/flow3_fake_stages.py`).
