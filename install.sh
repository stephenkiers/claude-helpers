#!/bin/bash
set -e

# Usage: ./install.sh [--with-zsh-keybindings] [--with-telemetry] [--check] [--from-worktree]
#   --check                  Read-only drift report: repo files not linked into ~/.claude, wrong or
#                            stale links. Exit 0 = no drift, 1 = drift. Changes nothing.
#   --with-zsh-keybindings   Also add Option+Arrow word jumping to ~/.zshrc (opt-in).
#   --with-telemetry         Register telemetry hooks in ~/.claude/settings.json (opt-in).
#   --from-worktree          Allow installing from a linked git worktree (refused by default).
WITH_ZSH_KEYBINDINGS=0
FROM_WORKTREE=0
WITH_TELEMETRY=0
CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --with-zsh-keybindings) WITH_ZSH_KEYBINDINGS=1 ;;
        --with-telemetry) WITH_TELEMETRY=1 ;;
        --check) CHECK_ONLY=1 ;;
        --from-worktree) FROM_WORKTREE=1 ;;
    esac
done

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"

# Resolve a git path (e.g. --git-dir, --git-common-dir) to an absolute, symlink-free path.
# Prints nothing when REPO_DIR is not a git checkout. Uses only rev-parse + cd/pwd, so it does
# not depend on newer git flags such as --path-format (git 2.31+).
abs_git_path() {
    local p
    p=$(git -C "$REPO_DIR" rev-parse "$1" 2>/dev/null) || return 0
    [ -n "$p" ] && (cd "$REPO_DIR" && cd "$p" && pwd -P)
    return 0
}

# Refuse to install from a linked worktree: every ~/.claude link would point into a feature
# branch that lags main and is deleted at /cleanup, so merged work silently never goes live.
# Exempt: --check (read-only, so it is safe from any checkout), --from-worktree (explicit opt-in),
# a non-git copy (no git dir, so the guard does not apply), and the main worktree (git-dir ==
# git-common-dir). Bare-repo layouts are skipped too: their first worktree entry is the bare repo,
# which has no working tree to point the user at.
if [ "$CHECK_ONLY" -eq 0 ] && [ "$FROM_WORKTREE" -eq 0 ]; then
    GIT_DIR_ABS=$(abs_git_path --git-dir)
    GIT_COMMON_ABS=$(abs_git_path --git-common-dir)
    # Inside a work tree but unable to resolve either git dir (an unusual or very old git): say so
    # rather than skipping the guard silently. Not a git copy at all stays quiet, as documented.
    if { [ -z "$GIT_DIR_ABS" ] || [ -z "$GIT_COMMON_ABS" ]; } \
        && git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "WARNING: could not resolve the git dirs of $REPO_DIR; the linked-worktree guard did not run" >&2
    fi
    if [ -n "$GIT_DIR_ABS" ] && [ -n "$GIT_COMMON_ABS" ] && [ "$GIT_DIR_ABS" != "$GIT_COMMON_ABS" ]; then
        # Only a linked worktree reaches here, so the worktree list is read just for the message.
        WORKTREE_LIST=$(git -C "$REPO_DIR" worktree list --porcelain 2>/dev/null || true)
        MAIN_CHECKOUT=$(printf '%s\n' "$WORKTREE_LIST" | sed -n '1s/^worktree //p')
        MAIN_IS_BARE=$(printf '%s\n' "$WORKTREE_LIST" | sed -n '2p')
        if [ -n "$MAIN_CHECKOUT" ] && [ "$MAIN_IS_BARE" != "bare" ]; then
            echo "ERROR: $REPO_DIR is a linked worktree; install from the main worktree instead:" >&2
            echo "  cd $MAIN_CHECKOUT && ./install.sh" >&2
            echo "(pass --from-worktree to install from this worktree anyway)" >&2
            exit 1
        fi
    fi
fi

# The one enumeration of repo files that get linked; install and --check both use it.
# A real file at a managed path is informational only (reported as not-ours, not drift):
# install backs it up to .bak, --check just reports it. The exit code ignores it.
list_source_files() {
    find "$1" -type f -not -path '*/__pycache__/*'
}

# --check: read-only. Reports drift, creates/removes/moves nothing.
if [ "$CHECK_ONLY" -eq 1 ]; then
    drift=0

    # Check for missing flow agent hooks in settings.json
    SETTINGS_FILE="$CLAUDE_DIR/settings.json"
    if command -v jq &> /dev/null && [ -e "$SETTINGS_FILE" ] && jq empty "$SETTINGS_FILE" 2>/dev/null; then
        # Define the three expected flow agent hooks
        declare -A flow_hooks=(
            ["Agent|Task"]="$HOME/.claude/scripts/flow-agent-guard.py --agent auto"
            ["Skill"]="$HOME/.claude/scripts/flow-skill-guard.py --agent auto"
            ["Bash"]="$HOME/.claude/scripts/flow-bash-guard.py --agent auto"
        )

        for matcher in "Agent|Task" "Skill" "Bash"; do
            hook_command="${flow_hooks[$matcher]}"
            if ! jq --arg matcher "$matcher" --arg cmd "$hook_command" \
                '(.hooks.PreToolUse // []) | any(.matcher == $matcher and .hooks[]?.command == $cmd)' \
                "$SETTINGS_FILE" 2>/dev/null | grep -q "true"; then
                echo "hook-missing: PreToolUse $matcher"
                drift=1
            fi
        done
    elif ! command -v jq &> /dev/null || [ ! -e "$SETTINGS_FILE" ] || ! jq empty "$SETTINGS_FILE" 2>/dev/null; then
        # settings.json is missing or invalid
        if [ ! -e "$SETTINGS_FILE" ]; then
            echo "hook-missing: settings.json does not exist"
            drift=1
        fi
    fi

    for dir in commands reviewers prompts agents scripts; do
        target_dir="$CLAUDE_DIR/$dir"
        source_dir="$REPO_DIR/$dir"
        [ -d "$source_dir" ] || continue
        if [ -L "$target_dir" ]; then
            echo "dir-symlink: $target_dir (./install.sh replaces it with a real directory)"
            drift=1
            # Report once per directory: the per-file checks would only repeat the same drift.
            continue
        fi
        while IFS= read -r source_file; do
            rel_path="${source_file#"$source_dir"/}"
            target_file="$target_dir/$rel_path"
            if [ -L "$target_file" ]; then
                if [ "$(readlink "$target_file")" != "$source_file" ]; then
                    echo "wrong-link: $target_file"
                    drift=1
                fi
            elif [ -e "$target_file" ]; then
                echo "not-ours: $target_file"
            else
                echo "unlinked: $target_file"
                drift=1
            fi
        done < <(list_source_files "$source_dir")
        if [ -d "$target_dir" ]; then
            while IFS= read -r entry; do
                link_target=$(readlink "$entry")
                case "$link_target" in
                    "$REPO_DIR"/*)
                        if [ ! -f "$link_target" ]; then
                            echo "stale: $entry"
                            drift=1
                        fi
                        ;;
                esac
            done < <(find "$target_dir" -type l)
        fi
    done
    if [ "$drift" -eq 0 ]; then
        echo "No drift: every repo file is linked into $CLAUDE_DIR"
    else
        echo "Drift found: run ./install.sh (or /setup-local) to fix" >&2
    fi
    exit "$drift"
fi

# scripts/workflow/ (ADR-0013) requires python3 3.8+ and PyYAML for reading project.yaml.
# Fail loudly and early rather than mid-workflow with an opaque "command not found" or traceback.
MIN_PY_MAJOR=3
MIN_PY_MINOR=8
if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 not found, but scripts/workflow/ (used by /track-and-start, /shipit," >&2
    echo "/cleanup, /merge-and-cleanup) requires Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+." >&2
    echo "Install it via https://www.python.org/downloads/ or your platform package manager" >&2
    echo "(e.g. 'brew install python3' on macOS), then re-run ./install.sh." >&2
    exit 1
fi

# Capture stderr from version check to detect broken shims (e.g., corrupt symlink).
# The check is the `if` condition itself (not a separate assignment) so `set -e`
# doesn't abort the script the instant python3 exits non-zero.
if ! stderr_out=$(python3 -c "import sys; sys.exit(0 if sys.version_info >= (${MIN_PY_MAJOR}, ${MIN_PY_MINOR}) else 1)" 2>&1); then
    if [ -n "$stderr_out" ]; then
        # Interpreter ran but errored (e.g., broken shim)
        echo "Error: python3 failed to run: $stderr_out" >&2
    else
        # Version check failed (interpreter is too old)
        # Wrap the version retrieval as an if condition to handle failure under set -e
        if stderr_out=$(python3 -c 'import platform; print(platform.python_version())' 2>&1); then
            FOUND_VERSION="$stderr_out"
            echo "Error: python3 ${FOUND_VERSION} found, but scripts/workflow/ (used by /track-and-start, /shipit," >&2
            echo "/cleanup, /merge-and-cleanup) requires Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+." >&2
            echo "Install a newer python3 via https://www.python.org/downloads/ or your platform package" >&2
            echo "manager (e.g. 'brew install python3' on macOS), then re-run ./install.sh." >&2
        else
            echo "Error: python3 version check failed (could not retrieve version string) — this may indicate a broken or corrupted python3 installation." >&2
        fi
    fi
    exit 1
fi

# PyYAML is required for reading project.yaml (ADR-0013 Amendment 7)
if ! python3 -c "import yaml" 2>/dev/null; then
    echo "Error: PyYAML not found, but scripts/workflow/ requires it for reading project.yaml." >&2
    echo "Install it via: pip3 install PyYAML" >&2
    echo "Or on macOS with Homebrew: brew install pyyaml" >&2
    echo "Then re-run ./install.sh." >&2
    exit 1
fi

echo "Installing Claude helpers from $REPO_DIR"

mkdir -p "$CLAUDE_DIR"

for dir in commands reviewers prompts agents scripts; do
    target_dir="$CLAUDE_DIR/$dir"
    source_dir="$REPO_DIR/$dir"

    # If it's a directory-level symlink, remove it so we can replace with a real dir
    if [ -L "$target_dir" ]; then
        echo "Removing directory symlink $target_dir"
        rm "$target_dir"
    fi

    mkdir -p "$target_dir"

    # Recursively link all files (except those in __pycache__ directories)
    while IFS= read -r source_file; do
        # Compute the relative path from source_dir to source_file
        rel_path="${source_file#"$source_dir"/}"
        target_file="$target_dir/$rel_path"
        target_subdir="$(dirname "$target_file")"

        # Create subdirectories as needed
        mkdir -p "$target_subdir"

        if [ -L "$target_file" ] && [ "$(readlink "$target_file")" = "$source_file" ]; then
            continue  # already correct
        fi

        if [ -e "$target_file" ] && [ ! -L "$target_file" ]; then
            echo "Backing up $target_file to $target_file.bak"
            mv "$target_file" "$target_file.bak"
        fi

        ln -sf "$source_file" "$target_file"
        echo "Linked $target_file -> $source_file"
    done < <(list_source_files "$source_dir")

    # Prune stale symlinks that point into this repo (including nested ones)
    while IFS= read -r entry; do
        [ -L "$entry" ] || continue
        link_target=$(readlink "$entry")
        case "$link_target" in
            "$REPO_DIR"/*)
                if [ ! -f "$link_target" ]; then
                    echo "Pruned $entry (target gone: $link_target)"
                    rm "$entry"
                fi
                ;;
        esac
    done < <(find "$target_dir" -type l)

    # Clean up empty subdirectories left behind by pruning
    find "$target_dir" -type d -empty -delete 2>/dev/null || true
done

if [ ! -e "$CLAUDE_DIR/preferences.yaml" ]; then
    cp "$REPO_DIR/prompts/preferences.yaml.template" "$CLAUDE_DIR/preferences.yaml"
    echo "Created $CLAUDE_DIR/preferences.yaml from template"
fi

# Opt-in only: add Option+Arrow word jumping to ~/.zshrc (pass --with-zsh-keybindings)
if [ "$WITH_ZSH_KEYBINDINGS" -eq 1 ]; then
    ZSHRC="$HOME/.zshrc"
    if [ -f "$ZSHRC" ] && ! grep -q 'bindkey.*backward-word' "$ZSHRC"; then
        cat >> "$ZSHRC" << 'KEYBINDINGS'

# Option+Arrow word jumping (added by claude-helpers install.sh)
bindkey "^[[1;3D" backward-word
bindkey "^[[1;3C" forward-word
KEYBINDINGS
        echo "Added Option+Arrow word jumping to $ZSHRC (restart your shell to apply)"
    else
        echo "Option+Arrow word jumping already in $ZSHRC (skipped)"
    fi
fi

# Register telemetry hooks in ~/.claude/settings.json (idempotent)
if [ "$WITH_TELEMETRY" -eq 1 ]; then
    if command -v jq &> /dev/null; then
        SETTINGS_FILE="$CLAUDE_DIR/settings.json"

        # Create settings.json if it doesn't exist
        if [ ! -e "$SETTINGS_FILE" ]; then
            echo '{}' > "$SETTINGS_FILE"
        fi

        # Validate existing JSON
        if ! jq empty "$SETTINGS_FILE" 2>/dev/null; then
            echo "Warning: $SETTINGS_FILE is not valid JSON, skipping telemetry hook registration"
        else
            # Define the four telemetry hooks
            for hook_event in "SessionStart" "SessionEnd" "SubagentStart" "SubagentStop"; do
                case "$hook_event" in
                    SessionStart)
                        subcommand="session-begin"
                        ;;
                    SessionEnd)
                        subcommand="session-end"
                        ;;
                    SubagentStart)
                        subcommand="agent-begin"
                        ;;
                    SubagentStop)
                        subcommand="agent-end"
                        ;;
                esac

                hook_command="python3 \$HOME/.claude/scripts/run-metrics.py $subcommand 2>/dev/null || true"

                # Hooks live under the top-level "hooks" key; each event maps to an array of
                # matcher groups, each with its own nested "hooks" array — this two-level nesting
                # is Claude Code's actual hook config schema (confirmed by live-capturing a real
                # SubagentStart/SubagentStop payload against this exact config shape).
                # Check if a run-metrics.py hook for this subcommand already exists (pattern match,
                # so it works even if old unguarded entries exist without the 2>/dev/null || true suffix).
                if jq --arg event "$hook_event" --arg subcommand "$subcommand" \
                    '((.hooks[$event] // []) | any(.hooks[]?.command | test("run-metrics\\.py.*" + $subcommand)))' \
                    "$SETTINGS_FILE" 2>/dev/null | grep -q "true"; then
                    # Hook already registered for this event/subcommand
                    continue
                fi

                # Add a new matcher group with this hook to the event's array
                TMP_SETTINGS=$(mktemp "$SETTINGS_FILE.XXXXXX")
                jq --arg event "$hook_event" --arg cmd "$hook_command" \
                    '.hooks[$event] = ((.hooks[$event] // []) + [{"hooks": [{"type": "command", "command": $cmd}]}])' \
                    "$SETTINGS_FILE" > "$TMP_SETTINGS"
                if jq empty "$TMP_SETTINGS" 2>/dev/null; then
                    mv "$TMP_SETTINGS" "$SETTINGS_FILE"
                else
                    echo "Warning: jq produced invalid JSON while registering $hook_event hook; leaving $SETTINGS_FILE unchanged" >&2
                    rm -f "$TMP_SETTINGS"
                fi
            done
            echo "Telemetry hooks registered in $SETTINGS_FILE"
        fi
    else
        echo "Warning: jq not found; skipping telemetry hook registration (manual setup available in docs/metrics.md)" >&2
    fi
else
    SETTINGS_FILE="$CLAUDE_DIR/settings.json"
    if command -v jq &> /dev/null && [ -e "$SETTINGS_FILE" ] && jq empty "$SETTINGS_FILE" 2>/dev/null && \
        jq -e '[(.hooks.SessionStart // []), (.hooks.SessionEnd // []), (.hooks.SubagentStart // []), (.hooks.SubagentStop // [])] | flatten | any(.hooks[]?.command | test("run-metrics\\.py"))' \
            "$SETTINGS_FILE" &> /dev/null; then
        echo "Telemetry hooks already registered in $SETTINGS_FILE (pass --with-telemetry to re-check)"
    else
        echo "Telemetry hooks not registered (pass --with-telemetry to opt in; see docs/metrics.md)"
    fi
fi

# Register PreToolUse hooks for flow agents (always, not opt-in)
if command -v jq &> /dev/null; then
    SETTINGS_FILE="$CLAUDE_DIR/settings.json"

    # Create settings.json if it doesn't exist
    if [ ! -e "$SETTINGS_FILE" ]; then
        echo '{}' > "$SETTINGS_FILE"
    fi

    # Validate existing JSON
    if ! jq empty "$SETTINGS_FILE" 2>/dev/null; then
        echo "Warning: $SETTINGS_FILE is not valid JSON, skipping flow agent hook registration" >&2
    else
        # Define the three flow agent hooks
        declare -A flow_hooks=(
            ["Agent|Task"]="$HOME/.claude/scripts/flow-agent-guard.py --agent auto"
            ["Skill"]="$HOME/.claude/scripts/flow-skill-guard.py --agent auto"
            ["Bash"]="$HOME/.claude/scripts/flow-bash-guard.py --agent auto"
        )

        for matcher in "Agent|Task" "Skill" "Bash"; do
            hook_command="${flow_hooks[$matcher]}"

            # Check if this hook already exists by matching the command exactly
            if jq --arg matcher "$matcher" --arg cmd "$hook_command" \
                '(.hooks.PreToolUse // []) | any(.matcher == $matcher and .hooks[]?.command == $cmd)' \
                "$SETTINGS_FILE" 2>/dev/null | grep -q "true"; then
                # Hook already registered
                continue
            fi

            # Add a new matcher group with this hook to the PreToolUse array
            TMP_SETTINGS=$(mktemp "$SETTINGS_FILE.XXXXXX")
            jq --arg matcher "$matcher" --arg cmd "$hook_command" \
                '.hooks.PreToolUse = ((.hooks.PreToolUse // []) + [{"matcher": $matcher, "hooks": [{"type": "command", "command": $cmd}]}])' \
                "$SETTINGS_FILE" > "$TMP_SETTINGS"
            if jq empty "$TMP_SETTINGS" 2>/dev/null; then
                mv "$TMP_SETTINGS" "$SETTINGS_FILE"
            else
                echo "Warning: jq produced invalid JSON while registering PreToolUse hook for $matcher; leaving $SETTINGS_FILE unchanged" >&2
                rm -f "$TMP_SETTINGS"
            fi
        done
        echo "Flow agent hooks registered in $SETTINGS_FILE"
    fi
else
    echo "Warning: jq not found; skipping flow agent hook registration (manual setup available in docs/adr/0008-machine-enforced-agent-guardrails.md)" >&2
fi

echo ""
echo "Claude helpers installed!"
echo ""
echo "Verify with: ls -la ~/.claude/commands/ ~/.claude/reviewers/ ~/.claude/prompts/ ~/.claude/agents/ ~/.claude/scripts/"
