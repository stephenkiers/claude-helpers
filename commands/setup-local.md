---
name: setup-local
argument-hint: [--check]
description: Use when the user says "/setup-local", "setup this machine", "re-sync", or "install claude-helpers". Symlinks this repo's commands, reviewers, prompts, agents, and scripts into ~/.claude/ so they're available everywhere. Idempotent and safe to re-run.
---

# Setup Local

Install (or re-sync) this repo's helpers into `~/.claude/` using file-level symlinks, so that
edits in this repo are immediately live and personal/project-specific files can coexist alongside
repo files. This is idempotent — safe to run any time.

## What it does

Requires `python3` 3.8+ (used by `scripts/workflow/`) — `install.sh` checks this first and fails
with a clear message if it's missing or too old.

Runs `./install.sh` from the repo root, which:
- Creates **file-level symlinks** from `~/.claude/{commands,reviewers,prompts,agents,scripts}/` into
  this repo's corresponding directories, so your personal files can coexist alongside repo files.
- Links only regular files (skips directories like `__pycache__`).
- Prunes stale symlinks that point into this repo (dangling links or non-regular-file targets).
- Creates `~/.claude/preferences.yaml` from `prompts/preferences.yaml.template` if missing
  (never overwrites an existing file).
- Symlinks `prompts/style-guide.json` (the shipped default) into `~/.claude/prompts/style-guide.json`
  like any other file under `prompts/`; note that `~/.claude/style-guide.json` (the personal style
  guide) is never auto-created from a template (unlike preferences.yaml), only via `/generate-style-guide`
  or manual copying of `prompts/style-guide.json.template`.
- Registers telemetry hooks in `~/.claude/settings.json` (opt-in via `--with-telemetry`; see below).

Because these are file-level symlinks (not directory symlinks), you can drop your own personal or
project-specific commands/reviewers into `~/.claude/{dir}/` and they will coexist with the repo's
files untouched.

**Note on project-specific overrides:** When adopting this helpers repo in an existing project, first
add `.claude/project.yaml` to `.gitignore` before creating it, so it stays local and never gets
tracked. See `~/.claude/reviewers/README.md` for the full context on local overrides.

## Steps

1. Resolve the repo root (the directory this command lives in).
2. Run `./install.sh` from the repo root (which performs all the symlinking, pruning, and
   preferences bootstrapping described above). From a linked worktree it refuses and prints the main
   worktree's path: run it there instead, unless the user explicitly asked to install this branch
   (then pass `--from-worktree`).
3. **Verify** by listing the five target directories and confirming the new symlinks resolve.
4. Report a concise summary: how many files were linked, how many were pruned, how many were backed
   up, and whether `preferences.yaml` was created.

## `/setup-local --check` (read-only drift report)

When the argument is `--check`, run `./install.sh --check` from the repo root instead of installing
and report its output verbatim. It creates, removes and moves nothing, and it runs from a linked
worktree too (the linked-worktree refusal in step 2 does not apply to `--check`). Lines: `unlinked:` (repo file
with no link in `~/.claude`), `wrong-link:` (link points elsewhere), `stale:` (link into this repo
whose target is gone), `not-ours:` (a real user file in the way; informational). Exit 0 means no
drift, 1 means drift: tell the user to re-run `/setup-local`. Missing links are the usual reason
North Star Nick's inputs are absent.

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

## Optional: Usage telemetry (opt-in)

This is **not** done by default. Telemetry registration requires passing `--with-telemetry` to
`./install.sh`. **Only do this if the user explicitly asks for it.**

If asked, run:

```bash
./install.sh --with-telemetry
```

This registers four hooks (SessionStart, SessionEnd, SubagentStart, SubagentStop) in
`~/.claude/settings.json` that call `scripts/run-metrics.py` to log local, observational usage
telemetry — no data leaves your machine. See [`docs/metrics.md`](../docs/metrics.md) for the full
schema and privacy details.

Without the flag, no hooks are registered — this is the default, and running `/setup-local` without
the flag is exactly as safe and inert on this front as before. Never register telemetry hooks
unless the user opts in.
