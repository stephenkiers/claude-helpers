---
name: setup-repo
description: Use when user says "/setup-repo", "add a repo", "clone <repo>", or wants a repo cloned into the preferred layout (by default `~/Repositories/<repo>/worktrees/<default-branch>`) so it's ready for the worktree workflow (/track-and-start, /shipit, /cleanup). The destination root is configurable via CLAUDE_REPOS_ROOT or ~/.claude/preferences.yaml. After the clone it can also offer optional direnv setup (`worktrees/.envrc` loads `<default-branch>/.env`; an existing `.envrc` is never overwritten).
---

# Setup Repo — Clone Into the Preferred Worktree Layout

Clone a remote into this machine's standard layout so every downstream command
(`/track-and-start`, `/shipit`, `/cleanup`, `/expert-review`) finds the worktree parent where it
expects it.

## The preferred layout

```
~/Repositories/<repo>/
  worktrees/
    <default-branch>/    ← the clone lives here (e.g. main or master), NOT at the repo root
    <issue-or-slug>/     ← later worktrees land here as siblings, created by /track-and-start
```

The clone is placed at `worktrees/<default-branch>` — **not** at `~/Repositories/<repo>/` directly.
This is deliberate: it makes the default-branch checkout just another worktree, so
`git worktree add ../<name>` from inside it drops new worktrees into `worktrees/` as siblings, which
is exactly the `WORKTREE_PARENT = <main-worktree>/../` shape that `~/.claude/prompts/worktree-reference.md`
and `/track-and-start` assume. Keep it plural (`worktrees/`) — the shared detection blocks look for
that name.

## Resolving the remote (never guess the org)

The argument is a git remote. Accept, in order:

1. **A full URL** — `git@github.com:owner/repo.git`, `https://github.com/owner/repo.git`, etc. Use verbatim.
2. **`owner/name` shorthand** — expand to `git@github.com:owner/name.git`.
3. **A bare name** with no owner and no URL (e.g. `my-service`) — **do not assume an org.** Ask the user
   for the full remote via `AskUserQuestion` (or plain prompt): "What's the remote for `<name>`?
   Give a full URL or `owner/name`." Only proceed once you have an owner. If your repos usually live
   under a single org, offer that org as a hint — never apply one silently.

```bash
set -euo pipefail

ARG="${1:-}"
REMOTE=""   # initialize so `set -u` doesn't trip on the no-arg / bare-name paths below
# The empty "" arm is deliberate: no argument leaves REMOTE empty, so the guard below stops and asks.
case "$ARG" in
  "")                       ;;  # no arg → ask the user what to clone
  *://*|git@*)  REMOTE="$ARG" ;;                         # full URL, use as-is
  */*)          REMOTE="git@github.com:${ARG}.git" ;;    # owner/name shorthand
  *)            REMOTE="" ;;                              # bare name → must ask for owner/URL
esac

# Guard: REMOTE must be non-empty and not start with '-'
if [ -z "$REMOTE" ]; then
  echo "Bare repo name requires a full remote. Provide a full URL or owner/name." >&2
  exit 1
fi
case "$REMOTE" in
  -*) echo "Refusing remote starting with '-': '$REMOTE'" >&2; exit 1 ;;
esac
```

Derive the repo name from the resolved remote (strip trailing `.git` and any path):

```bash
set -euo pipefail

REPO_NAME=$(basename "$REMOTE" .git)

# Guard: reject unsafe repo names (traversal, special dirs, slashes)
case "$REPO_NAME" in
  ""|.|..|*/*|*..*) echo "Refusing unsafe repo name: '$REPO_NAME'" >&2; exit 1 ;;
esac
```

## Steps

1. **Resolve the remote** (above). If it can't be resolved to an owner, stop and ask — don't clone.
2. **Compute the destination** and guard against an existing checkout:

   ```bash
   set -euo pipefail

   # Read repos root from env, preferences.yaml, or default
   REPOS_ROOT="${CLAUDE_REPOS_ROOT:-}"
   if [ -z "$REPOS_ROOT" ] && [ -f "$HOME/.claude/preferences.yaml" ]; then
     # First `repos_root:` line; strip an inline comment, surrounding quotes, and stray whitespace.
     # `grep -m1 … || true` keeps a no-match from tripping `set -e`/`pipefail`.
     line=$(grep -m1 '^repos_root:' "$HOME/.claude/preferences.yaml" 2>/dev/null || true)
     REPOS_ROOT=$(printf '%s\n' "$line" | sed -e 's/^repos_root:[[:space:]]*//' \
       -e 's/[[:space:]][[:space:]]*#.*$//' -e 's/[[:space:]]*$//' \
       -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")
   fi
   REPOS_ROOT="${REPOS_ROOT:-$HOME/Repositories}"
   REPOS_ROOT="${REPOS_ROOT/#\~/$HOME}"   # expand a leading ~ (bare or ~/…) to $HOME

   REPO_DIR="${REPOS_ROOT}/${REPO_NAME}"
   ```

   If `${REPO_DIR}/worktrees` already exists and is non-empty, this repo is likely already set up —
   report the existing path and ask before touching it rather than cloning over it.

   The `REPOS_ROOT` defaults to `~/Repositories` but is configurable via the `CLAUDE_REPOS_ROOT`
   environment variable or the `repos_root:` entry in `~/.claude/preferences.yaml`.

3. **Detect the default branch from the remote** (before cloning, so the directory is named right):

   ```bash
   set -euo pipefail

   # Query the remote for its default branch; fail loudly if ls-remote fails
   SYMREF=$(git ls-remote --symref -- "$REMOTE" HEAD) || {
     echo "git ls-remote failed for '$REMOTE' — check the remote or run 'gh auth status'." >&2
     exit 1
   }
   # sed -n with p prints only the HEAD symref line; no match leaves the value empty, which the default below catches.
   DEFAULT_BRANCH=$(printf '%s\n' "$SYMREF" | sed -n 's|^ref: refs/heads/\(.*\)\tHEAD$|\1|p')
   DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"   # reachable but no HEAD symref advertised → assume main
   ```

   If `git ls-remote` fails (auth, typo, unreachable), stop and surface the error — a failed
   `ls-remote` almost always means the remote is wrong or you're not authenticated (`gh auth status`).

4. **Clone into the layout:**

   ```bash
   set -euo pipefail

   DEST="${REPO_DIR}/worktrees/${DEFAULT_BRANCH}"
   mkdir -p "${REPO_DIR}/worktrees"
   git clone -- "$REMOTE" "$DEST"
   ```

4a. **Before creating `.claude/project.yaml` in the new project**, add it to `.gitignore` first so it doesn't accidentally get tracked. If it's already tracked, run `git rm --cached .claude/project.yaml` before ignoring it. See `~/.claude/reviewers/README.md` for the full context.

**5. Optional: direnv.** Runs only after a successful clone in this run, including a clone the user approved at step 2's "already set up, ask first" prompt. That approval is the only route to the kept-existing branch below, because a pre-existing `worktrees/.envrc` makes `worktrees/` non-empty. There is no re-run mode for existing repos. Every sub-step is non-fatal: a failure prints the command to run by hand, and the hand-off still runs. The generated `.envrc` holds `source_up_if_exists` and the dotenv convention line; project-specific blocks are added by hand (Compose: see ADR-0014; merge-queue: see ADR-0021).

   Ask via `AskUserQuestion` (plain-prompt fallback, as in the remote-resolution step): **"Use direnv for environment variables in this project?"** Options, No first, with consequence labels:
   - `No: skip direnv setup`
   - `Yes: create worktrees/.envrc that loads <default-branch>/.env`

   On No, do nothing else in this step. The hand-off gets exactly one line: `direnv: skipped`.

   On Yes, run sub-steps 5a to 5e in order. Shell variables do not carry across fenced blocks, so `REPO_DIR`, `DEST` and `DEFAULT_BRANCH` are carried in from the earlier steps. `DIRENV_AVAILABLE` is not carried: 5c to 5e check `command -v direnv` themselves, so a direnv installed in 5b is picked up once it is on `PATH`.

   **5a. Validate `DEFAULT_BRANCH` before anything is written.** Allowed: characters `A-Za-z0-9._/-` only, no `..`, not starting with `-`, not empty. Slashed names such as `release/x` pass. Reject everything else: the check is whole-string, so a value with a newline fails too. On failure, skip the rest of the step and set the summary to `direnv: skipped (unsafe branch name '<name>' — create worktrees/.envrc by hand if you want it)`. This check does not change step 3 or `DEST`.

   ```bash
   set +e
   set -uo pipefail

   # 5a. Validate DEFAULT_BRANCH before anything is written.
   # No -e on purpose: each check is a condition that records a result, not a stop.
   # An empty or unset DEFAULT_BRANCH stops the block here, before any output.
   : "${DEFAULT_BRANCH:?DEFAULT_BRANCH is unset (step 3)}"
   # Branch allowlist (#444): keep the two copies in 5a and 5c to 5e textually identical.
   BRANCH_OK=1
   case "$DEFAULT_BRANCH" in
     ""|-*|*..*) BRANCH_OK=0 ;;
   esac
   case "$DEFAULT_BRANCH" in
     *[!A-Za-z0-9._/-]*) BRANCH_OK=0 ;;
   esac
   printf 'DIRENV_PRECHECK=%s\n' "$BRANCH_OK"

   # 5b. Detect direnv and brew (read-only).
   DIRENV_PRESENT=0
   command -v direnv >/dev/null 2>&1 && DIRENV_PRESENT=1
   BREW_PRESENT=0
   command -v brew >/dev/null 2>&1 && BREW_PRESENT=1
   printf 'DIRENV_PRESENT=%s\nDIRENV_BREW=%s\n' "$DIRENV_PRESENT" "$BREW_PRESENT"
   ```

   **5b. Install direnv, only when it is missing and 5a passed.** If `DIRENV_PRESENT=1`, direnv is usable and the rest of 5b is skipped. If `DIRENV_BREW=0`, print `https://direnv.net/docs/installation.html` and continue. Otherwise ask as a separate question, so the consent reads as its own choice and not part of the first Yes: **"direnv is not installed. Run `brew install direnv`?"** Options, No first:
   - `No: continue without installing (the .envrc is still written, but inactive until direnv is installed and hooked)`
   - `Yes: install direnv with Homebrew`

   On Yes, run `brew install direnv`. On success, direnv is usable. On failure or No, it is not; after a failure, print `brew install direnv` to run by hand. A failed install never stops the step.

   **5c to 5e. Keep `.env` out of git, create it, write `.envrc`, allow it.** One block, run only when 5a passed. It repeats the allowlist check at its top, so it writes nothing when that check fails. It has no `-e`: each probe is a condition, and the block ends by printing the `DIRENV_*` lines the hand-off reads. Fill in the variables first: `REPO_DIR` (step 2), `DEST` (step 4), `DEFAULT_BRANCH` (step 3). If a variable is unset, or `DEST` has no `.git`, the block stops before printing, and the hand-off treats the missing lines as unknown.

   ```bash
   set +e
   set -uo pipefail

   # No -e on purpose: each probe is a condition that records a result, not a stop.
   # Fill in from earlier steps first: REPO_DIR (step 2), DEST (step 4), DEFAULT_BRANCH (step 3).
   # Empty or unset values stop the block here, before anything is written or printed.
   : "${REPO_DIR:?REPO_DIR is unset (step 2)}"
   : "${DEST:?DEST is unset (step 4)}"
   : "${DEFAULT_BRANCH:?DEFAULT_BRANCH is unset (step 3)}"
   # DEST must be a real clone before any git probe runs against it.
   if [ ! -e "${DEST}/.git" ]; then
     echo "No clone at ${DEST} (no .git); direnv step stopped." >&2
     exit 1
   fi
   # Derived here, not carried from 5b: direnv is available when it is on PATH.
   DIRENV_AVAILABLE=0
   command -v direnv >/dev/null 2>&1 && DIRENV_AVAILABLE=1

   # Re-check the branch allowlist here, so this block does not depend on 5a having run.
   # Branch allowlist (#444): keep the two copies in 5a and 5c to 5e textually identical.
   BRANCH_OK=1
   case "$DEFAULT_BRANCH" in
     ""|-*|*..*) BRANCH_OK=0 ;;
   esac
   case "$DEFAULT_BRANCH" in
     *[!A-Za-z0-9._/-]*) BRANCH_OK=0 ;;
   esac

   ENVRC="${REPO_DIR}/worktrees/.envrc"
   TRACKED=0
   IGNORED=0
   ENV_CREATED=0
   ENVRC_STATE=skipped
   SHADOWED=0
   ALLOW_STATE=not-run

   if [ "$BRANCH_OK" -eq 1 ]; then
     # 5c. Keep .env out of git. Tracked: leave it alone. Otherwise write the exclude entry before the file exists.
     if git -C "$DEST" ls-files --error-unmatch -- .env >/dev/null 2>&1; then
       TRACKED=1
     elif ! git -C "$DEST" check-ignore -q -- .env; then
       EXCLUDE=$(git -C "$DEST" rev-parse --git-path info/exclude 2>/dev/null) || EXCLUDE=""
       case "$EXCLUDE" in
         "") ;;
         /*) ;;
         *) EXCLUDE="${DEST}/${EXCLUDE}" ;;
       esac
       # Anchored to the clone root (/.env), so a nested .env elsewhere in the tree is not excluded.
       if [ -n "$EXCLUDE" ] && mkdir -p "$(dirname "$EXCLUDE")" && touch "$EXCLUDE"; then
         if ! grep -qxF '/.env' "$EXCLUDE"; then
           # tail -c1 | wc -l prints 0 when the last byte is not a newline; add one before appending.
           if [ -s "$EXCLUDE" ] && [ "$(tail -c1 "$EXCLUDE" | wc -l)" -eq 0 ]; then printf '\n' >> "$EXCLUDE"; fi
           printf '/.env\n' >> "$EXCLUDE"
         fi
       fi
     fi
     # Measured, not assumed: .env counts as ignored only when git now reports it as ignored.
     if [ "$TRACKED" -eq 0 ] && git -C "$DEST" check-ignore -q -- .env; then IGNORED=1; fi

     # 5c (cont.). Create the empty .env, mode 600, only when it is untracked, ignored and absent.
     # The subshell keeps noclobber and umask from leaking into the rest of the block.
     if [ "$TRACKED" -eq 0 ] && [ "$IGNORED" -eq 1 ] && [ ! -e "${DEST}/.env" ] && [ ! -L "${DEST}/.env" ]; then
       if ( set -o noclobber; umask 077; : > "${DEST}/.env" ) 2>/dev/null; then ENV_CREATED=1; fi
     fi

     # 5d. Write worktrees/.envrc. Never overwrite, never follow a symlink.
     # The format is single-quoted and the branch was validated above, so nothing else expands.
     # source_up_if_exists comes first so an ancestor .envrc is loaded, not shadowed. The dotenv line is unchanged.
     # noclobber in the subshell refuses to overwrite a file that appeared after the check above.
     if [ -e "$ENVRC" ] || [ -L "$ENVRC" ]; then
       ENVRC_STATE=kept
     elif ( set -o noclobber; printf 'source_up_if_exists\nif [[ -f "%s/.env" ]]; then dotenv "%s/.env"; fi\n' "$DEFAULT_BRANCH" "$DEFAULT_BRANCH" > "$ENVRC" ) 2>/dev/null; then
       ENVRC_STATE=written
     else
       ENVRC_STATE=failed
     fi

     # 5d (cont.). A clone with its own .envrc shadows the parent file. Do not read, edit or allow it.
     if [ -e "${DEST}/.envrc" ] || [ -L "${DEST}/.envrc" ]; then SHADOWED=1; fi

     # 5e. direnv allow only on the file this run wrote, and only when direnv is available.
     # Withheld when the clone tracks .env: direnv's trust hash covers .envrc only, not the tracked .env it loads.
     if [ "$ENVRC_STATE" = written ] && [ "$DIRENV_AVAILABLE" = 1 ]; then
       if [ "$TRACKED" -eq 1 ]; then
         ALLOW_STATE=withheld-tracked-env
       elif direnv allow "$ENVRC" >/dev/null 2>&1; then
         ALLOW_STATE=ok
       else
         ALLOW_STATE=failed
       fi
     fi
   fi

   # DIRENV_DONE=1 is the sentinel: the hand-off trusts this block's result only when it is present.
   printf 'DIRENV_VALIDATED=%s\nDIRENV_TRACKED=%s\nDIRENV_IGNORED=%s\nDIRENV_ENV_CREATED=%s\nDIRENV_ENVRC=%s\nDIRENV_SHADOWED=%s\nDIRENV_ALLOW=%s\nDIRENV_AVAILABLE=%s\nDIRENV_DONE=1\n' \
     "$BRANCH_OK" "$TRACKED" "$IGNORED" "$ENV_CREATED" "$ENVRC_STATE" "$SHADOWED" "$ALLOW_STATE" "$DIRENV_AVAILABLE"
   ```

   Why `direnv allow` runs only on a file written in this run: its content comes only from the validated branch name. A kept existing `.envrc` or a cloned `.envrc` is content nobody has reviewed, so it is never allowed automatically. It is also withheld when the clone tracks `.env` (`DIRENV_ALLOW=withheld-tracked-env`): the hand-off tells the user to review the tracked `.env` first and then prints the allow command, which the user runs. For a kept existing file, review it first: print `direnv allow <path>` for the user and never run it. Never run `direnv allow` on `${DEST}` or on anything inside the clone.

**6. Confirm and hand off:**

```
## Repo ready

**<repo>** cloned to `~/Repositories/<repo>/worktrees/<default-branch>`

### Start working:

cd ~/Repositories/<repo>/worktrees/<default-branch>

New worktrees for issues/tickets: use `/track-and-start` from inside it — they'll land in
`~/Repositories/<repo>/worktrees/<name>` as siblings.

### direnv

<one status line from the mapping below>
<follow-ups, only when the step did not skip>
```

The template below is printed at column 0, so its `### direnv` line renders as a heading. The `### direnv` section prints one status line for the outcome, then any shadowing line, then the follow-ups. The 5c to 5e result is trusted only when both `DIRENV_DONE=1` and `DIRENV_ENVRC` are present. If either is missing (a variable was unset, the `.git` check failed, or the block did not finish), print `direnv: unknown — check worktrees/.envrc and the exclude entry by hand`, with no follow-ups. A missing line is never read as success:

   - **No answer, or 5a failed:** `direnv: skipped` (No), or `direnv: skipped (unsafe branch name '<name>' — create worktrees/.envrc by hand if you want it)` (5a). No follow-ups.
   - **`DIRENV_ENVRC=kept`:** `direnv: kept existing worktrees/.envrc — review it, then run: direnv allow <absolute path to worktrees/.envrc>`. The command is never run for the user. With `DIRENV_AVAILABLE=0`, also print the install hint from the next line (`brew install direnv`, then `eval "$(direnv hook zsh)"` in the zsh startup file), because `direnv allow` does nothing until direnv is installed.
   - **`DIRENV_ENVRC=written`, `DIRENV_AVAILABLE=0`:** `direnv: .envrc written but inactive until direnv is installed and hooked` with the hint `brew install direnv`, then `eval "$(direnv hook zsh)"` in the zsh startup file.
   - **`DIRENV_ENVRC=written`, `DIRENV_ALLOW=ok`:** `direnv: set up — worktrees/.envrc loads <branch>/.env`.
   - **`DIRENV_ENVRC=written`, `DIRENV_ALLOW=withheld-tracked-env`:** `direnv: .envrc written, but direnv allow withheld — .env is tracked by this repo; review the tracked .env first, then run: direnv allow <absolute path to worktrees/.envrc>`.
   - **`DIRENV_ENVRC=written`, `DIRENV_ALLOW=failed`:** `direnv: .envrc written, but direnv allow failed — run: direnv allow <absolute path to worktrees/.envrc>`.
   - **`DIRENV_ENVRC=failed`:** `direnv: could not write worktrees/.envrc — remove any empty worktrees/.envrc first, then create it by hand with the convention lines from 5d`.
   - **`DIRENV_SHADOWED=1`** (added to any line above): `the repo's own .envrc shadows worktrees/.envrc; add source_up_if_exists to it to inherit <branch>/.env`.

   Follow-ups, only when the step did not skip, in this order. Item 1 applies only when `DIRENV_ENVRC=written`:

   1. **Secrets** (only when `DIRENV_ENVRC=written`). Use the measured values, never a fixed claim. If `DIRENV_TRACKED=1`: `.env is tracked by this repo; keep secrets out of it`. If `DIRENV_TRACKED=0`, `DIRENV_IGNORED=1` and `DIRENV_ENV_CREATED=1`: `put secrets in <branch>/.env (git-ignored, mode 600)`. If `DIRENV_TRACKED=0`, `DIRENV_IGNORED=1` and `DIRENV_ENV_CREATED=0`: `this run did not create <branch>/.env (it may already exist); if it is missing, create it by hand with mode 600 before putting secrets in it`. If `DIRENV_TRACKED=0` and `DIRENV_IGNORED=0`: `.env is not git-ignored and was not created; add /.env to the clone's info/exclude by hand before putting secrets in it`. Either way, do not say the values are isolated from checks: tools that load the main worktree's `.envrc` see them too.
   2. **Hook check (read-only; never edit the file).**

      ```bash
      set +e
      set -uo pipefail
      HOOK=not-found
      for f in "$HOME/.zshrc" "$HOME/.zshenv"; do
        if [ -f "$f" ] && grep -q 'direnv hook zsh' "$f"; then HOOK=found; fi
      done
      printf 'DIRENV_HOOK=%s\n' "$HOOK"
      ```

      Report `hook found`, or the advisory `hook not found in ~/.zshrc or ~/.zshenv; if direnv is hooked elsewhere (another startup file or a plugin) ignore this, otherwise add eval "$(direnv hook zsh)"`.
   3. **ADR line.** `Project-specific blocks are not generated; add them to worktrees/.envrc by hand. Compose COMPOSE_PROJECT_NAME: see ADR-0014 and ~/.claude/prompts/worktree-reference.md (Docker Compose Project Isolation). The merge-queue config reads the main worktree's .envrc: see ADR-0021.`

## Error Handling

| Condition | Action |
|-----------|--------|
| No argument or bare name without owner | Ask for the full remote (URL or `owner/name`); never assume the org. |
| Remote starts with `-` or is empty after parsing | Reject it as unsafe; ask for a valid remote. |
| `REPO_NAME` contains unsafe chars (empty, `.`, `..`, `/`, or `..*`) | Reject it; ask for a valid remote. |
| `git ls-remote` fails | Stop; surface the error (likely wrong remote or need `gh auth status`). Do not clone. |
| `${REPO_DIR}/worktrees` already exists and non-empty | Report the existing path; ask before cloning again. |
| `git clone` fails | Surface git's error verbatim; leave any partial `worktrees/<branch>` for the user to inspect or remove. |
| Unsafe `DEFAULT_BRANCH` in direnv step 5a (empty, outside `A-Za-z0-9._/-`, contains `..`, or starts with `-`) | Skip direnv visibly (`direnv: skipped (unsafe branch name ...)`); the clone stands; write nothing. |
| direnv missing and `brew` missing | Print the install link (`https://direnv.net/docs/installation.html`); still write `.envrc`; hand-off says it is inactive until direnv is installed and hooked. |
| `brew install direnv` declined or fails | Non-fatal; print `brew install direnv` to run by hand; still write `.envrc`. |
| `worktrees/.envrc` exists or is a symlink | Keep it; never overwrite, never follow the symlink, never run `direnv allow` on it; print the review-and-allow command. |
| `.envrc` cannot be written (write error) | Non-fatal; hand-off says to remove any empty `worktrees/.envrc` first, then create it by hand with the convention lines from 5d. |
| Clone has its own `.envrc` | Write the parent `worktrees/.envrc` anyway; report the shadowing with the `source_up_if_exists` fix; never read, edit or allow the tracked file. |
| `.env` is tracked by the clone | No exclude entry, no `.env` created; `.envrc` is still written, but `direnv allow` is withheld (`DIRENV_ALLOW=withheld-tracked-env`); hand-off warns to keep secrets out of it and prints the allow command for the user to run after reviewing the tracked `.env`. |
| Exclude entry cannot be written, or `.env` is not git-ignored after it | No `.env` created; hand-off says `.env` is not git-ignored and to add `/.env` to `info/exclude` by hand before putting secrets in it. |
| `.env` cannot be created (already exists, is a symlink, or write error) | Non-fatal; `DIRENV_ENV_CREATED=0`; `.envrc` is still written; hand-off says this run did not create `<branch>/.env`, makes no `mode 600` claim, and says to create it by hand with mode 600 if it is missing. |
| Block result missing `DIRENV_DONE=1` or `DIRENV_ENVRC` (unset variable, failed `.git` check, or unfinished block) | Hand-off prints `direnv: unknown — check worktrees/.envrc and the exclude entry by hand`, with no follow-ups; never read as success. |
| `direnv allow` fails | Non-fatal; hand-off prints the `direnv allow <path>` command to run by hand. |

## Notes

- This creates a **plain clone** placed at `worktrees/<default-branch>`, not a bare repo. That's
  intentional and the standard for machine-local working copies.
- After the clone, `git worktree add` run from the default-branch checkout uses `worktrees/` as the
  parent automatically, so no extra setup is needed for the rest of the toolchain to work.
- The optional direnv file `worktrees/.envrc` lives beside `<default-branch>/`, so direnv's walk-up
  from every sibling worktree under `worktrees/` reaches it and the shared `<default-branch>/.env`,
  unless that worktree has its own `.envrc`, which shadows it (see the shadowing line in the hand-off).
- The generated `worktrees/.envrc` freezes the default branch as it was when the clone was made, so it
  names `<default-branch>/.env` for that name only. If the default branch is later renamed, the `-f`
  guard fails silently and the dotenv line is skipped; edit `worktrees/.envrc` by hand to match.
