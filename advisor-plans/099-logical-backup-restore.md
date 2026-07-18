# Plan 099: Logical backup/restore for the graph store (audit first — pg_dump is probably silent data loss)

> **Executor instructions**: AUDIT BEFORE FIXING. Step 1's findings determine Steps 2-4; report
> the audit result in full even if it contradicts this plan's hypothesis. Skip the advisor index
> update.
>
> **Drift check (run first)**:
> `git diff --stat da4d1e8..HEAD -- src/graph_store/ test/ scripts/ docs/` (expect 097's merge)

## Status

- **Priority**: P1 (spike→product item 2a — DBMS table stakes)
- **Effort**: M
- **Risk**: MED (touches the extension SQL surface; data-integrity domain)
- **Depends on**: 091 (typed batch insert — the restore lever), 097 (merged base)
- **Planned at**: 2026-07-18

## Why this matters (the hypothesis to prove)

`gstore` is `CREATE TABLE gstore (dummy "char")` whose pages are custom-formatted via the buffer
manager (`graph_store_am--0.1.0.sql:10`) — its heap is NOT heap-tuple-formatted. Extension member
tables are skipped by `pg_dump` unless marked with `pg_extension_config_dump`, and none of
`gstore`, `edge_type`, `gph_vid_map`, `gph_am_meta` are marked. Expected consequence: a
`pg_dump`/`pg_restore` cycle silently loses the ENTIRE graph (topology + id map + type
dictionary) while relational + vector data round-trip fine — the worst failure mode a tri-modal
one-WAL store can have, because the restored database looks healthy.

## Steps

### Step 1: The audit (evidence, not assumption)

In a stock PG17 container (`tridb/pg17-unfork:dev` harness conventions): create the three
extensions, load a small tri-modal corpus (entities + vectors + typed graph), run
`pg_dump -Fc` → fresh database → `pg_restore`, then assert per leg: relational rows, vector
search works, `gph_vertex_count`/`gph_visible_edge_count`/`gph_traverse_typed` outputs. Also
test plain-SQL `pg_dump` (no -Fc). Record EXACTLY what survives, what's lost, and what errors.
Also check: does `pg_dump` choke on gstore's non-heap pages (error) or skip cleanly?

**Verify**: a written finding table (leg × dump-mode × survived?). If the graph DOES round-trip
(hypothesis wrong), STOP after Step 1 and report — the remaining steps would be unnecessary.

### Step 2: Logical export/import functions

Add to the graph extension SQL (+ C only if SQL/plpgsql over existing SRFs is insufficient):
- `gph_dump_vertices() RETURNS SETOF (vid bigint)` and
  `gph_dump_edges() RETURNS SETOF (src bigint, dst bigint, type_id int)` — streaming SRFs over
  the existing read paths (visible, non-tombstoned records only; document that tombstone history
  and frozen-xid state are NOT preserved — a logical dump is a logical snapshot).
- A documented restore procedure: recreate extension → replay `edge_type` + vid map → vertices
  in vid order → edges via the typed batched `gph_insert_edges` (plan 091). Prefer a
  `gph_restore_edges(...)`-shaped helper only if plain SQL over COPY'd staging can't express it.
- Mark the HEAP-formatted config tables (`edge_type`, `gph_vid_map`, `gph_am_meta`) with
  `pg_extension_config_dump` so their DATA rides pg_dump natively — and verify gph_am_meta's
  semantics survive a restore (if a row is meaningless post-restore, exclude it and document).

### Step 3: The round-trip gate

`scripts/graph_dump_restore_test.sh` (stock harness conventions, fail-loud): load corpus →
logical dump (pg_dump for the marked tables + `COPY (SELECT * FROM gph_dump_edges()) TO` for
topology) → restore into a fresh database → assert BYTE-EQUAL traversal outputs
(`gph_traverse_typed` ordered comparison), counts, and a `tjs_open` query returning identical
ids pre/post. Negative control: corrupt one edge in the dump file → the assert must fail.

### Step 4: Docs + wiring

`docs/INSTALL_stock_pg.md` backup/restore section: what physical backup covers (basebackup/WAL —
everything), what logical covers (this procedure), what is NOT preserved. Add the round-trip
script to `STOCK_TESTS`-adjacent wiring (a make target + CI dispatch note; it needs two
databases so per-PR CI inclusion is the executor's call — justify either way).

## Scope

**In scope**: `src/graph_store/graph_store_am--0.1.0.sql` (+ graph_am.c ONLY if SQL cannot
express the dump SRFs — justify), `scripts/graph_dump_restore_test.sh` (new),
`test/` fixtures as needed, `docs/INSTALL_stock_pg.md`, `Makefile`.
**Out of scope**: physical replication/backup (works via WAL already — do not touch);
tjs_pg; fork patches; changing on-disk formats.

## STOP conditions

- Step 1 disproves the hypothesis (report and stop).
- Export requires reading pages in a way the existing read paths don't support (report the C
  gap rather than writing new page-walk code without flagging it).
- `pg_extension_config_dump` interacts badly with the REVOKEd/system-ish tables — report.

## Git workflow

Branch `advisor/099-logical-dump`. Commits: `fix(graph): logical dump/restore surface`,
`test(graph): dump-restore round-trip gate`, `docs: backup/restore contract`.

REPORT FORMAT: STATUS / STEPS+verifications (incl. the Step-1 finding table verbatim) /
FILES CHANGED / NOTES / WORKTREE+commits.
