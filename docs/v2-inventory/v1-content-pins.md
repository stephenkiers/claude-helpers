# v1 content pins

This file is the watch list for the v1 files that `rules.md` depends on. The drift test reads the table
below, not any list in test code. Each row pins one file by the SHA-256 of its raw bytes (no line-ending
normalisation).

## Bump procedure

When a watched v1 file changes:

1. Re-read the changed file.
2. Add or update the `rules.md` rows that the change affects, with a disposition for each (the closed set
   is in `README.md`).
3. Update the `sha256`, `reviewed` date and `ref` for that file, in the same PR as the v1 edit.

`reviewed` is the ISO date the change was reviewed. `ref` names the tracking issue or PR of the reviewing
change; this inventory uses the tracking issue (#538), not the PR.

## Human-only edit

**A pin is a human-only edit.** An automated worker (implementer, `flow-healer`, or the rebase worker) that
hits a pin mismatch must stop with `NEEDS_HUMAN` or `Decision: FAILED` and must never re-hash the file to
make the drift test pass (W2). A follow-up issue (#543) adds this file to the never-edit rules of those
workers.

## Watched files

Changes to these files are sequenced with #547 and #532. The pin bump lands in the same PR as the v1 edit, as the
bump procedure above requires.

| path | sha256 | reviewed | ref |
|---|---|---|---|
| commands/expert-flow.md | 61fdcfc9f8964ec3b04c810ead8860fa7f99a47241c591a4df48e5fc5859692e | 2026-11-23 | #538 |
| prompts/flow-step-contract.md | 1c64d110d1429a9f6752e1ee8cec7477960053984c5f432152b5964b97c82eeb | 2026-11-23 | #538 |
| prompts/flow-reference.md | 284091779252430b8a758f34c1e767b24f1e36a6b32a48488df504a2bf7ce0a5 | 2026-11-23 | #538 |
