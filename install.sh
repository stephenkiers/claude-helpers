#!/bin/bash
set -e

# Usage: ./install.sh [--with-zsh-keybindings]
#   --with-zsh-keybindings   Also add Option+Arrow word jumping to ~/.zshrc (opt-in).
WITH_ZSH_KEYBINDINGS=0
for arg in "$@"; do
    case "$arg" in
        --with-zsh-keybindings) WITH_ZSH_KEYBINDINGS=1 ;;
    esac
done

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"

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

    for source_file in "$source_dir"/*; do
        [ -f "$source_file" ] || continue
        fname="$(basename "$source_file")"
        target_file="$target_dir/$fname"

        if [ -L "$target_file" ] && [ "$(readlink "$target_file")" = "$source_file" ]; then
            continue  # already correct
        fi

        if [ -e "$target_file" ] && [ ! -L "$target_file" ]; then
            echo "Backing up $target_file to $target_file.bak"
            mv "$target_file" "$target_file.bak"
        fi

        ln -sf "$source_file" "$target_file"
        echo "Linked $target_file -> $source_file"
    done

    # Prune stale symlinks that point into this repo
    for entry in "$target_dir"/*; do
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
    done
done

# Validate the expert-review model registry.
# The resolver script is symlinked into ~/.claude/scripts/ by the loop above; confirm it landed,
# then (if python3 is available) run it to validate config/expert-review-models.json in place.
# setup-local is idempotent and should warn, not hard-fail, so all checks here are advisory.

RESOLVER_LINK="$CLAUDE_DIR/scripts/resolve-expert-review-models.py"
if [ ! -e "$RESOLVER_LINK" ]; then
    echo ""
    echo "WARNING: $RESOLVER_LINK not found."
    echo "         The expert-review model resolver was not symlinked. Re-run this script from"
    echo "         the repo root so scripts/resolve-expert-review-models.py is linked into ~/.claude/scripts/."
fi

REGISTRY="$REPO_DIR/config/expert-review-models.json"
if [ ! -f "$REGISTRY" ]; then
    echo ""
    echo "NOTE: $REGISTRY not found — registry validation skipped (no config/expert-review-models.json)."
elif ! command -v python3 >/dev/null 2>&1; then
    echo ""
    echo "NOTE: python3 not found — expert-review model registry validation skipped."
else
    if python3 "$REPO_DIR/scripts/resolve-expert-review-models.py" >/dev/null 2>/dev/null; then
        echo "Expert-review model registry validated (config/expert-review-models.json)."
    else
        echo ""
        echo "WARNING: expert-review model registry failed validation."
        echo "         Fix config/expert-review-models.json. The resolver's stderr (re-run"
        echo "         'python3 scripts/resolve-expert-review-models.py') has the precise error."
    fi
fi

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

echo ""
echo "Claude helpers installed!"
echo ""
echo "Verify with: ls -la ~/.claude/commands/ ~/.claude/reviewers/ ~/.claude/prompts/ ~/.claude/agents/ ~/.claude/scripts/"
