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

for dir in commands reviewers prompts agents; do
    target_dir="$CLAUDE_DIR/$dir"
    source_dir="$REPO_DIR/$dir"

    # If it's a directory-level symlink, remove it so we can replace with a real dir
    if [ -L "$target_dir" ]; then
        echo "Removing directory symlink $target_dir"
        rm "$target_dir"
    fi

    mkdir -p "$target_dir"

    for source_file in "$source_dir"/*; do
        [ -e "$source_file" ] || continue
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
done

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
echo "Verify with: ls -la ~/.claude/commands/ ~/.claude/reviewers/ ~/.claude/prompts/ ~/.claude/agents/"
