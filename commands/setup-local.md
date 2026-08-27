---
name: setup-local
description: Use when the user says "/setup-local", "setup this machine", "re-sync", or "install claude-helpers". Symlinks this repo's commands, reviewers, prompts, agents, and scripts into ~/.claude/ so they're available everywhere. Idempotent and safe to re-run.
---

# Setup Local

Install (or re-sync) this repo's helpers into `~/.claude/` using file-level symlinks, so that
edits in this repo are immediately live and personal/project-specific files can coexist alongside
repo files. This is idempotent — safe to run any time.

## What it does

Runs `./install.sh` from the repo root, which:
- Creates **file-level symlinks** from `~/.claude/{commands,reviewers,prompts,agents,scripts}/` into
  this repo's corresponding directories, so your personal files can coexist alongside repo files.
- Links only regular files (skips directories like `__pycache__`).
- Prunes stale symlinks that point into this repo (dangling links or non-regular-file targets).
- Creates `~/.claude/preferences.yaml` from `prompts/preferences.yaml.template` if missing
  (never overwrites an existing file).
- Registers telemetry hooks in `~/.claude/settings.json` (opt-in via `--with-telemetry`; see below).

Because these are file-level symlinks (not directory symlinks), you can drop your own personal or
project-specific commands/reviewers into `~/.claude/{dir}/` and they will coexist with the repo's
files untouched.

## Steps

1. Resolve the repo root (the directory this command lives in).
2. Run `./install.sh` from the repo root (which performs all the symlinking, pruning, and
   preferences bootstrapping described above).
3. **Verify** by listing the five target directories and confirming the new symlinks resolve.
4. Report a concise summary: how many files were linked, how many were pruned, how many were backed
   up, and whether `preferences.yaml` was created.

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
