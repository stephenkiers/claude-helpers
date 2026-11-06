# Test Cleanup Reference (Round 4.3–4.6)

## Caller contract

This document is read by `/implement-with-haiku` at Round 4 time and by `/expert-test-cleanup` (#463).

**Inputs the caller supplies:**
- `ROUND2_START_SHA` — the SHA before Round 2 was applied (used to compute the working set and to feed to `names --hygiene`)
- `SCRATCH_PATH` — the orchestrator's scratch directory
- `IMPL_FILES` — list of implementation files
- `round4-candidates.md` (optional) — hints from `overlaps:` lines; may be absent or `unavailable`

**Outputs the caller reads:**
- `round4-deletions.md` — the markdown table of deletions with reason and survivor or proof
- `round4-merged.txt` — the mapping of pre-merge test ids to post-merge ids
- One commit with subject `test: clean up spec-blind tests (<D> deleted, <M> merged)` and body listing deletions and merges

---

### 4.3 Classify each test in the working set

Classify in this order. The first class that applies wins.

- **(0) Exempt.** `test-correct-blocking` entries, and tests a human ruled to keep. **Never deleted.** They
  still go through 4.5 as Unique (merged and renamed), with the test body byte-identical: only the location,
  enclosing names and import lines change, so the oracle is untouched and no spec-blind name survives.
- **(1) Duplicate.** Part C's `comm` dedup against `START_SHA`, or a Sonnet-judged duplicate of a sibling
  test. It **must name the surviving test id**. If the caller supplies a candidates file, start class-1 review from its rows. A row is a hint. The deletion still needs its named survivor (4.4) and the 4.6 gates. `unreported` ids get the full class-1 review like any other test.
- **(2) No-op, mechanical.** Any of: reference-check `vacuous` (the file references no `IMPL_FILES` symbol),
  a hand-built copy of the logic (the "would still pass if the implementation were deleted" defect the Round 2
  prompt already forbids), or `snapshot-pinned` (Q5b). **Record the proof kind.** The No-op bucket is the
  slot a future at-base proof (#367) joins.
- **(3) Judgment delete.** Sonnet may delete any other test that adds no regression value. It **must name the
  surviving test that covers the behaviour**. This class is the interim trim until the test budget in #367
  lands; it is not itself a budget, and it decides no per-file or per-suite count.
- **(4) Unique.** Everything else. Merged in 4.5.

Two evidence rules still hold:
- Part C's **mutation-smoke** `vacuous` flag is **suite-level, not per-file**: it reverts one impl file and
  re-runs the whole `TEST_FILES` set, so it never names which test is junk. Treat it like `weak-assertion`:
  surface it, and at most hand the tests that *reference the reverted file* to Round 3 to strengthen. **Never**
  expand a suite-level mutation-smoke flag into a deletion. It may only support a judgment delete that also
  names a survivor.
- A `weak-assertion` flag is fixable, not worthless. It is never a deletion reason on its own.

### 4.4 Delete

Write `<SCRATCH_PATH>/round4-deletions.md` **before** the first deletion, so resume knows what was decided.
It is one markdown table with this header row:

`| id | class | change | reason | survivor or proof | commit |`

- `id` is the deleted test's id as collected (`file::name`).
- `class` is `human-spec-wrong`, `duplicate`, `no-op` or `judgment`.
- `change` is `deleted` for a cleanup row; a `human-spec-wrong` row is `amended` or `deleted`.
- `reason` is the reason. For a `human-spec-wrong` row it is the human's reason, and the ruler is always `human`.
- `survivor or proof` is the surviving test's id, or `proof:<kind>` when the proof is mechanical (for example
  `proof:comm-dedup`, `proof:reference-check`, `proof:snapshot-pinned`). A `human-spec-wrong` row uses
  `proof:human-ruling`.
- `commit` is empty for a cleanup row, and holds the full SHA of the ruling commit for a `human-spec-wrong` row.

The table starts with one row per register entry ruled `human-spec-wrong`, then one row per cleanup deletion.
Test ids are parsed from the `id` and `survivor or proof` cells. The `proof:` markers and the SHA cells are the
only cells exempt from being parsed as test ids. A malformed row (wrong cell count, an empty `id`, an
unparseable test id) makes the cleanup helper exit 1; it is never skipped. A header-only table is a
valid empty table.

Then delete each (1)–(3) test, one at a time, with `git rm` or by deleting the test function. Record path
and reason for the final summary.

**Record what evidence was available.** Part C's mutation smoke can be skipped whole-cloth (dirty-file
precondition, or no comparison operator found — see Part C). When it was skipped, the reference-check
is Round 4's *only* machine-checked junk evidence, so a `Deleted (junk): 0` result means "not vetted
by the smoke," not "vetted and clean." Note in the summary whether the mutation smoke ran or was
skipped (with reason) so a human can tell those two cases apart.

### 4.5 Relocate, rename and merge the survivors

The **default for each Unique test (Exempt included) is to merge it into the module's normal test file**,
under 4.2's convention. A new file is created only when the module has none. Move each survivor to the
4.2 convention:

- **Record the target before writing it.** Before the first write to any target file, append the target
  path to `<SCRATCH_PATH>/round4-merged.txt`, so resume knows every path Round 4 may have touched. Then
  record each merged or renamed survivor as `<old id> -> <new id>` in the same file, where `<old id>` is the
  id the survivor carries in `round4-deletions.md` and `<new id>` is its id in the final tree. This file is the
  `--merged` input to `survivors` in 4.6(a), so a survivor named by its pre-merge id is looked up under its new id.
- Write each Exempt test's new id into its register entry's `final_id`.
- **Drop every spec-blind name** (file, function, describe and class names). The body of an Exempt test
  stays byte-identical.
- Move with `git mv` where a whole file moves, so history is preserved.
- Rewrite the moved tests' own imports for their new location, and update any references to them
  elsewhere (test config include globs, `conftest.py`, barrel files) — grep for the old path/name first.
- **Merge rather than clobber:** if a survivor's target path collides with an existing test file, fold
  the new tests into the existing file under the repo's conventions instead of overwriting. A pass-named file (`*_spec_blind`, `*_spec`, `*_hardening`, `*_blackbox`, `*_review_fixes`, `*_followups`, `*_phase_N`) is never a valid end state.
- **Snapshot-pinned tests (Q5b).** Before deleting one, check it against 4.6(b)'s behaviour floor. If its
  deletion would leave a changed public symbol with no test, keep it and flag it in the summary as
  "snapshot-pinned, not spec-derived".

### 4.6 Verify green, then commit

Run these post-conditions in this order, before the commit. Each is a hard gate. Use a **fresh full
collection under the new layout** for (a) and (c), never a path-scoped re-run of the pre-move set (the old
paths no longer exist, and a stale include target would silently run the wrong set while still reporting
a plausible count). Each helper call must exit 0; a non-zero exit (exit 1 with a named reason) is a hard
failure that stops the gate immediately.

- **(a) Named survivors.** Collect the test ids of the final tree (one per line, into
  `<SCRATCH_PATH>/collected.txt`), then run:
  ```bash
  python3 "$HOME/.claude/scripts/test-cleanup-check.py" survivors --deletions "<SCRATCH_PATH>/round4-deletions.md" --collected "<SCRATCH_PATH>/collected.txt" --merged "<SCRATCH_PATH>/round4-merged.txt"
  ```
  Each missing survivor is printed with the deleted test that named it. That deleted test is restored from
  the pre-Round-4 SHA, removed from `round4-deletions.md`, and sent back through 4.5 as Unique (merged and
  renamed). This is how two duplicates are kept from deleting each other.
- **(b) Behaviour floor.** Each public symbol added or changed in `IMPL_FILES` across `START_SHA..HEAD` is
  referenced by at least one test in the final tree. A miss restores the last deleted test that referenced
  the symbol, removes it from the deletions list, and sends it through 4.5 as Unique.
- Repeat (a) and (b) until neither restores anything. This is bounded: each pass only restores, never deletes.
- **(c) Count.** Only then, a fresh full collection. The total must match pre-cleanup minus the final
  deletions. **For any file merged in 4.5,** the target file's collected test count must rise by exactly the
  folded-in count. A same-name collision (e.g. two `test_boundary()` in one module) silently shadows one test
  with no error, and only a per-file count check catches it.
- **(d) Red set.** The set of red tests must equal **exactly** the `final_id`s of the register's
  `test-correct-blocking` entries. Every other test must be green, and every blocking test must still be red.
  Any other red test is a mistake to fix (an import or path, as today) before committing. It is not cleanup.
- **(e) No spec-blind name survives.**
  ```bash
  python3 "$HOME/.claude/scripts/test-cleanup-check.py" names --base "$ROUND2_START_SHA" --head WORKTREE
  ```
  `--head WORKTREE` checks the working tree, including untracked files, which is the tree the commit is about to
  record. Checking only committed HEAD would miss a name that is still uncommitted. A hit is a hard failure: fix the name, then re-run. Exempt tests were renamed in 4.5, so a blocking test
  cannot cause a names hit.
- **(f) Round 2 branch and worktree gone.** `git worktree list` no longer shows `<R2_WT_PATH>`, and
  `git rev-parse --verify --quiet "refs/heads/<R2_BRANCH>"` prints nothing.
- **(g) Gate step 1.** Re-run gate step 1 (build/type-check), since moves can break import paths.

Then commit, as one commit. The deletions and merges travel in its body, so review and audit can see them:
```bash
git commit -m "test: clean up spec-blind tests (<D> deleted, <M> merged)"
```
The body lists every deletion with its reason and its survivor or proof, and every merged id as
`<old id> -> <new id>`. That is the provenance for #367 and later audits.

Once the commit has landed and check (d) holds, write the Round 4 evidence. The first line keeps its
current shape, because the Final summary's verbatim render and the `coverage-fix-358` fixture read it.
`<R>` counts tests merged or relocated in 4.5, Exempt included. The later lines point at the blocking set
and the working files:
```bash
printf '%s\n' "round4: <D> deleted, <R> relocated" > "<SCRATCH_PATH>/round4.md"
printf '%s\n' "blocking: <B> (final ids: <comma-separated final_id list, or none>)" >> "<SCRATCH_PATH>/round4.md"
printf '%s\n' "deletions: <SCRATCH_PATH>/round4-deletions.md  merged: <SCRATCH_PATH>/round4-merged.txt  register: <SCRATCH_PATH>/disagreements.json" >> "<SCRATCH_PATH>/round4.md"
printf '%s DONE round4 %s\n' "<RUN_KEY>" "<SCRATCH_PATH>/round4.md" >> "<LEDGER_PATH>"
```
With blocking entries present the row is still `DONE`. The Final check is where the run fails loudly (see
"Spec-blind gate" in the Final check).

If tests go red after a move, fix the import or path (the expected failure mode). A relocation that changes
pass/fail counts beyond the intended deletions is a mistake, not cleanup. A second run over a cleaned tree
must be a no-op: Step 0 writes the row from the cleanup commit instead of re-running (see "Spec-blind state
on resume").
