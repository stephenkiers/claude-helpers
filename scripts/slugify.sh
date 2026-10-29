#!/usr/bin/env bash
# Convert a ticket title (from stdin) into a slug: [a-z0-9-]{1,50} with no leading/trailing -, or "plan".
# Takes no arguments — the title MUST come from stdin only, never from argv.
# Output: a single line containing the slug.

set -uo pipefail
export LC_ALL=C

# Reject any arguments — title must be on stdin only
if [ "$#" -ne 0 ]; then
  printf 'slugify.sh: takes no arguments; redirect the title on stdin\n' >&2
  exit 2
fi

# Run the slug pipeline on stdin
SLUG=$(tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | cut -c1-50 | sed -e 's/^-*//' -e 's/-*$//')

# Print "plan" on empty slug, otherwise print the slug
if [ -z "$SLUG" ]; then
  printf 'plan\n'
else
  printf '%s\n' "$SLUG"
fi
