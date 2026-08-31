#!/bin/bash
set -e

# Usage: ./install.sh [--with-zsh-keybindings] [--with-telemetry]
#   --with-zsh-keybindings   Also add Option+Arrow word jumping to ~/.zshrc (opt-in).
#   --with-telemetry         Register telemetry hooks in ~/.claude/settings.json (opt-in).
WITH_ZSH_KEYBINDINGS=0
WITH_TELEMETRY=0
for arg in "$@"; do
    case "$arg" in
        --with-zsh-keybindings) WITH_ZSH_KEYBINDINGS=1 ;;
        --with-telemetry) WITH_TELEMETRY=1 ;;
    esac
done

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"

# scripts/workflow/ (ADR-0013) requires python3 3.8+. Fail loudly and early rather than
# mid-workflow with an opaque "command not found" or traceback.
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
        FOUND_VERSION="$(python3 -c 'import platform; print(platform.python_version())')"
        echo "Error: python3 ${FOUND_VERSION} found, but scripts/workflow/ (used by /track-and-start, /shipit," >&2
        echo "/cleanup, /merge-and-cleanup) requires Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+." >&2
        echo "Install a newer python3 via https://www.python.org/downloads/ or your platform package" >&2
        echo "manager (e.g. 'brew install python3' on macOS), then re-run ./install.sh." >&2
    fi
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
    done < <(find "$source_dir" -type f -not -path '*/__pycache__/*')

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

echo ""
echo "Claude helpers installed!"
echo ""
echo "Verify with: ls -la ~/.claude/commands/ ~/.claude/reviewers/ ~/.claude/prompts/ ~/.claude/agents/ ~/.claude/scripts/"
