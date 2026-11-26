# Flow v3: feedback from real use

Findings from running v3 on real repositories, recorded as they come up. Nothing here is decided or
built yet; decisions go in `PROGRESS.md`.

## 1. Pay for the full check once, right before merge (2026-11-25, from the human)

**Feedback.** Do not prove the base green with a full check. The base is `main`; assume it is green.
The branch is rebased before merge anyway, so a full check earlier proves little. On a repo with an
expensive check, the full check should run about once per ticket, right before merge.

**What v3 does today.** Seen on the first real repo, a private project whose full check builds and
tests a Rust workspace and a desktop app:

| When | Check | How often |
|---|---|---|
| `doctor` | full, on a throwaway checkout of the base | once per `flow.yaml` content, not once per run |
| Worktree step | fast, on the untouched base | every ticket |
| After implement | fast | every ticket |
| After each fix round | fast | up to three times per ticket |
| Merge gate | full | every ticket |

`init` sets `check.fast` to the same command as `check.full` when it cannot detect a cheaper one,
so in that repo every row is a full check.

**What the base check buys.** Attribution: if the base is red, every later failure looks like the
ticket's fault and fixers chase problems they cannot fix. It also caught a real setup gap here: a
fresh worktree fails the check until a per-worktree setup script has run, and `doctor` found that
before any model call was spent.

**Options to weigh.**

- Drop the base check from the worktree step; trust the base. On a red check mid-flow, check the
  base then, and only then, to decide whose failure it is.
- Make `doctor` prove only that install and setup work in a fresh worktree, not that the full check
  passes.
- Require a real `check.fast` (or ask for one at `init`) instead of silently reusing the full
  command; warn when both are the same and the full check is slow.
- Run mid-flow checks scoped to what the ticket changed.
- Keep one full check at the merge gate, on the tree that will merge, as the only authority.

## 2. A fresh worktree may need more than the package install (2026-11-25)

`init` detected `bun install` as the install step. The repo's check also needs a build artefact that
a per-worktree setup script creates. `doctor` failed until that script was added to `install`.
`init` cannot know this; the `doctor` failure message printed the repo's own hint, which was enough.
Cost: one wasted `doctor` run.

## 3. `doctor` gives no progress (2026-11-25)

While `doctor` runs a long check it prints nothing, and its output shows only the last error line on
failure. A human watching cannot tell a slow check from a hung one.

## 4. Is `doctor` required at all? (2026-11-25, from the human)

**Feedback.** The human is not sure `doctor` should be a requirement before `run`.

**What it does.** Five steps: tools present, config parses, base fetches, install works in a
throwaway checkout, full check passes there. `run` and `fleet start` refuse until all five passed
for the current `flow.yaml`.

**Assessment.** The first four cost seconds and each prevents a stop a few minutes into a paid run.
The fifth is the expensive one and is the same base-green proof as entry 1. On the first real repo
it did catch a real setup gap, but a ticket's first check would have caught the same gap, at the
cost of one stopped flow.

**Option.** Keep `doctor` as a cheap required preflight (the first four steps) and make the full
check on the base opt-in (`doctor --full`), never a condition of `run`.

**Done (PROGRESS decision 32).** `doctor` no longer runs the checks unless asked (`--checks`), and the
per-ticket base check is opt-in (`check.base: true`). Entries 1 and 4 are otherwise still open: the
mid-flow checks run as before.
