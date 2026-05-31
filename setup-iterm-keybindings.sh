#!/bin/bash
# Fix iTerm2 Option+Arrow key bindings for word jumping
# Option+Left  → ESC+b (move back one word)
# Option+Right → ESC+f (move forward one word)

set -e

if pgrep -x "iTerm2" > /dev/null; then
    echo "Warning: iTerm2 is running. Quit and relaunch it after this script completes."
fi

defaults write com.googlecode.iterm2 GlobalKeyMap -dict-add \
    "0xf702-0x300000" '{ Action = 10; Text = "b"; }'

defaults write com.googlecode.iterm2 GlobalKeyMap -dict-add \
    "0xf703-0x300000" '{ Action = 10; Text = "f"; }'

echo "Done. Relaunch iTerm2 for changes to take effect."
