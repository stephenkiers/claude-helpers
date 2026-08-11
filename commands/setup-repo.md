---
name: setup-repo
description: Use when user says "/setup-repo", "add <repo> as a repository", "clone <repo>", or wants a repo cloned into the preferred `~/Repositories/<repo>/worktrees/<default-branch>` layout so it's ready for the worktree workflow (/track-and-start, /shipit, /cleanup).
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
3. **A bare name** with no owner and no URL (e.g. `corncob`) — **do not assume an org.** Ask the user
   for the full remote via `AskUserQuestion` (or plain prompt): "What's the remote for `<name>`?
   Give a full URL or `owner/name`." Only proceed once you have an owner. (Historically these repos
   live under `instacart/`, but that is a hint to offer, not a default to apply silently.)

```bash
ARG="${1:-}"
case "$ARG" in
  "")                       ;;  # no arg → ask the user what to clone
  *://*|git@*)  REMOTE="$ARG" ;;                         # full URL, use as-is
  */*)          REMOTE="git@github.com:${ARG}.git" ;;    # owner/name shorthand
  *)            REMOTE="" ;;                              # bare name → must ask for owner/URL
esac
```

Derive the repo name from the resolved remote (strip trailing `.git` and any path):

```bash
REPO_NAME=$(basename "$REMOTE" .git)
```

## Steps

1. **Resolve the remote** (above). If it can't be resolved to an owner, stop and ask — don't clone.
2. **Compute the destination** and guard against an existing checkout:

   ```bash
   REPOS_ROOT="$HOME/Repositories"
   REPO_DIR="${REPOS_ROOT}/${REPO_NAME}"
   ```

   If `${REPO_DIR}/worktrees` already exists and is non-empty, this repo is likely already set up —
   report the existing path and ask before touching it rather than cloning over it.

3. **Detect the default branch from the remote** (before cloning, so the directory is named right):

   ```bash
   DEFAULT_BRANCH=$(git ls-remote --symref "$REMOTE" HEAD 2>/dev/null \
     | sed -n 's|^ref: refs/heads/\(.*\)\tHEAD$|\1|p')
   DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"   # fall back to main if the remote doesn't advertise HEAD
   ```

   If `git ls-remote` fails (auth, typo, unreachable), stop and surface the error — a failed
   `ls-remote` almost always means the remote is wrong or you're not authenticated (`gh auth status`).

4. **Clone into the layout:**

   ```bash
   DEST="${REPO_DIR}/worktrees/${DEFAULT_BRANCH}"
   mkdir -p "${REPO_DIR}/worktrees"
   git clone "$REMOTE" "$DEST"
   ```

5. **Confirm and hand off:**

   ```
   ## Repo ready

   **<repo>** cloned to `~/Repositories/<repo>/worktrees/<default-branch>`

   ### Start working:

   cd ~/Repositories/<repo>/worktrees/<default-branch>

   New worktrees for issues/tickets: use `/track-and-start` from inside it — they'll land in
   `~/Repositories/<repo>/worktrees/<name>` as siblings.
   ```

## Error Handling

| Condition | Action |
|-----------|--------|
| No argument | Ask what to clone (full URL or `owner/name`). |
| Bare name, no owner/URL | Ask for the full remote — never assume the org. |
| `git ls-remote` fails | Stop; report likely cause (wrong remote, or run `gh auth status`). Do not clone. |
| `${REPO_DIR}/worktrees` already exists and non-empty | Report the existing path; ask before cloning again. |
| `git clone` fails | Surface git's error verbatim; leave any partial `worktrees/<branch>` for the user to inspect or remove. |

## Notes

- This creates a **plain clone** placed at `worktrees/<default-branch>`, not a bare repo. That's
  intentional and matches the existing repos on this machine (e.g. `corncob`, `caveat`).
- After the clone, `git worktree add` run from the default-branch checkout uses `worktrees/` as the
  parent automatically, so no extra setup is needed for the rest of the toolchain to work.
