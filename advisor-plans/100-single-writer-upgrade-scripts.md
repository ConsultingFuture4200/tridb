# Plan 100: Enforce the single-writer contract + establish extension upgrade paths (0.1.0 → 0.2.0)

> **Executor instructions**: Two table-stakes items, one plan because both restructure the same
> extension packaging. Serialize internally: upgrade-script scaffolding FIRST (it changes where
> the enforcement SQL lands). Runs AFTER plan 099 merges (same extension SQL surface). Skip the
> advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat <base>..HEAD -- src/graph_store/ src/tjs_pg/ test/ docs/ Makefile .github/`
> (expect 097 + 099 merges in range; read 099's final extension SQL before editing)

## Status

- **Priority**: P1 (spike→product item 2b)
- **Effort**: M
- **Risk**: MED (packaging restructure + a new lock in the write path)
- **Depends on**: 099 (merged — same files), 091/094 (write-path C context)
- **Planned at**: 2026-07-18

## Part A — versioned upgrade scripts (the mechanism, not a big migration)

Current state: both extensions are `default_version = '0.1.0'` and every change edits
`--0.1.0.sql` in place — fine pre-release, fatal after the first real install (there is no
`ALTER EXTENSION ... UPDATE` path; plan 074's STOP condition already anticipated this).

1. Bump both extensions to **0.2.0**: rename the base scripts to `--0.2.0.sql` (content = current
   state + this plan's Part B), set `default_version = '0.2.0'`, and add upgrade scripts
   `graph_store_am--0.1.0--0.2.0.sql` / `tjs_pg--0.1.0--0.2.0.sql` that carry a genuine 0.1.0
   install forward (new functions/GUC-comment changes since the last real release boundary — the
   practical definition of "0.1.0" here is the last PUSHED master before this batch, `a780b46`;
   diff the extension SQL against that to derive the upgrade DDL, and say so in the script
   header).
2. Update every build/harness reference to the versioned filename (Makefile, harness scripts,
   Dockerfiles — grep `--0.1.0.sql`).
3. The convention lands in CONTRIBUTING/INSTALL: from 0.2.0 on, released surface changes ship as
   `--X--Y.sql` upgrade scripts; in-place base edits are only allowed pre-release within a
   version.
4. **Gate**: a new harness step that installs 0.1.0 (from the `a780b46` SQL, vendored as a test
   fixture), loads data, runs `ALTER EXTENSION ... UPDATE TO '0.2.0'` for both extensions, and
   proves the post-upgrade suite passes on the pre-existing data (the graph survives an upgrade).

## Part B — enforce the single-writer contract

Current state: the contract lives in comments (`graph_am.c:27` "LOGICAL graph structure assumes
a SINGLE WRITER"). Nothing stops a second backend from concurrent structural writes.

1. Take a session-scoped advisory lock (or a lock on the gstore relation with a documented mode)
   at every structural-write entry point (`gph_upsert_vertex`, `gph_insert_edge(s)`,
   `gph_tombstone_*`, `gph_freeze`, batch loaders): `pg_advisory_xact_lock`-class, keyed on the
   gstore relation OID — writers serialize; a SECOND concurrent writer BLOCKS (normal Postgres
   behavior) rather than corrupting. Readers unaffected. If blocking (rather than erroring) is
   the chosen semantic, document it as such — pick ONE semantic and justify it in the docs; do
   not invent a GUC for it.
2. Concurrency test: extend `graph_concurrency_test.sh`-style coverage — two sessions attempt
   interleaved edge inserts; assert serialization (both succeed sequentially, final counts
   exact) and that reader traversal during a held write lock still works.
3. Docs: the contract moves from a C comment to `docs/INSTALL_stock_pg.md` + the extension
   comment: "structural writes serialize per graph; concurrent writers block; readers are
   MVCC-consistent" — stated exactly as tested.

## Verification (all)

Upgrade gate green (0.1.0 install → UPDATE → suite on old data); full stock suites PG16+17;
crash drivers (write path touched); parity harness 11/11; concurrency test green;
`make test && make lint && git diff --check`.

## Scope

**In scope**: both extensions' control/SQL files (rename + upgrade scripts), `graph_am.c` +
`tjs_pg.c` (lock calls only), harness/Makefile/Dockerfile filename references, the new upgrade +
concurrency test scripts, INSTALL/CONTRIBUTING.
**Out of scope**: any behavior change beyond the lock; multi-writer support (explicitly NOT
built — the posture is enforced-single-writer); fork patches.

## STOP conditions

- The 0.1.0→0.2.0 upgrade DDL cannot be derived cleanly from the `a780b46` diff (report the
  ambiguous hunk).
- The advisory lock interacts badly with the batch loaders' own locking (deadlock in the
  concurrency test) — report with the lock graph.
- Any suite regresses.

## Git workflow

Branch `advisor/100-writer-upgrade`. Commits: `build(ext): versioned upgrade scripts 0.1.0→0.2.0`,
`fix(graph): enforce single-writer via advisory lock`, `test: upgrade + concurrency gates`.

REPORT FORMAT: STATUS / STEPS+verifications / FILES CHANGED / NOTES / WORKTREE+commits.
