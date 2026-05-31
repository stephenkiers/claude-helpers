Disregard any plan file referenced earlier in this conversation. You are forking this session into an independent planning context.

Do the following:

1. Generate a new plan file path in the format: `~/.claude/plans/<slug>-<4-char-random>.md`
   - `<slug>`: short kebab-case description of the current task — infer from conversation context, or use the argument provided: $ARGUMENTS
   - `<4-char-random>`: 4 random alphanumeric characters to ensure uniqueness
2. Create the new file with just a `# Plan` heading — always empty, never copy content from any previous plan.
3. Declare this new file your active plan — all future plan reads and writes go here. Do not touch any previously referenced plan file.
4. Output the new plan file path clearly so it's visible in the session.
