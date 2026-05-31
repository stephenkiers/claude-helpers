---
name: setup-local
description: Use when the user says "/setup-local", "setup this machine", "re-sync", or "install claude-helpers". Symlinks this repo's commands, reviewers, prompts, and agents into ~/.claude/ so they're available everywhere. Idempotent and safe to re-run.
---

# Setup Local

Install (or re-sync) this repo's helpers into `~/.claude/` using file-level symlinks, so that
edits in this repo are immediately live and personal/project-specific files can coexist alongside
repo files. This is idempotent — safe to run any time.

## What it does

For each directory in `commands`, `reviewers`, `prompts`, `agents`:

1. Determine `{repo}` = the absolute path of this repository (the directory containing this file's
   repo root). Determine `{dir}` = each of the four directories above.
2. Ensure `~/.claude/{dir}/` exists as a **real directory** (create it if missing). If
   `~/.claude/{dir}` is itself a symlink (an old directory-level link), remove that symlink first
   and replace it with a real directory.
3. For every file in `{repo}/{dir}/`:
   - If `~/.claude/{dir}/{file}` is already a correct symlink to `{repo}/{dir}/{file}` → skip.
   - If it exists but points elsewhere or is a real file → back it up to `{file}.bak`, then create
     the symlink.
   - Otherwise → create the symlink `~/.claude/{dir}/{file}` → `{repo}/{dir}/{file}`.

Because these are file-level symlinks (not directory symlinks), you can drop your own personal or
project-specific commands/reviewers into `~/.claude/{dir}/` and they will coexist with the repo's
files untouched.

## Steps

1. Resolve the repo root (the directory this command lives in, walked up to the git root).
2. Perform the symlinking described above for all four directories.
3. **Verify** by listing the four target directories and confirming the new symlinks resolve.
4. Report a concise summary: how many files were linked, skipped, or backed up per directory.

A no-Claude fallback exists: running `./install.sh` from the repo root does the same thing.

## Optional: Option+Arrow word jumping (opt-in)

This is **not** done by default and only matters for zsh users on macOS who want Option+Left/Right to
jump word-by-word in the terminal. **Only do this if the user explicitly asks for it.**

If asked, append the following to `~/.zshrc` (only if not already present), and tell the user what
you changed and that they should restart their shell:

```zsh
# Option+Arrow word jumping (added by claude-helpers /setup-local)
bindkey "^[[1;3D" backward-word
bindkey "^[[1;3C" forward-word
```

Never modify `~/.zshrc` unless the user opts in.
