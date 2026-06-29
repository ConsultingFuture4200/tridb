# Plan 006: Maintain a store-wide edge count on the graph metapage so the FR-6 join-order heuristic can bind

> **Executor instructions**: Follow this plan step by step. Run every verification command and
> confirm the expected result before moving on. If anything in "STOP conditions" occurs, stop and
> report — do not improvise. When done, update this plan's row in `advisor-plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 8b19cb5..HEAD -- src/graph_store/ src/planner/join_order_legstats.c`
> If any in-scope file changed since this plan was written, compare the "Current state" excerpts
> against the live code before proceeding; on a mismatch, treat it as a STOP condition.
>
> **Hardware gate**: the C in `src/graph_store/` and `src/planner/` compiles **only inside the
> MSVBASE fork on the GX10** (PG 13.4 server headers, `--with-blocksize=32`). It does NOT build on
> this x86 standin. Author + review here; build/run/verify on the GX10. Do not claim it "builds" or
> "passes" off-target. The Python layer (`make test`, `make lint`) DOES run here and gates the
> hardware-independent parts of this plan.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: MED (touches the WAL-logged edge-insert hot path + buffer lock ordering)
- **Depends on**: none
- **Category**: tech-debt / perf (unblocks a shipped-but-inert feature)
- **Planned at**: commit `8b19cb5`, 2026-06-28
- **Horizon**: v1.1

## Why this matters

TriDB's one v1 planner decision — filter-first vs. vector-first leg ordering (FR-6, the "20%" that
drives the SM-1 intermediate-result reduction) — is **shipped but inert**. The decision core
(`tridb_choose_join_order`) is frozen and tested, but its `LegStats.avg_out_degree` input is
hardwired to `0.0` because the graph store exposes no mean out-degree. `src/planner/join_order_legstats.c:89-103`
documents the exact gap and the exact fix. This plan closes it: add a store-wide edge count to the
graph metapage, maintained transactionally under the shared WAL, and derive `avg_out_degree` from
it. It is the cheapest high-leverage change in the audit — one struct field plus an increment — and
it also supplies the graph fan-out term that `tridb_estimate_intermediate` needs for honest EXPLAIN
output and the eventual v2 cost model.

This was surfaced by an external-research audit (2026-06-28): standard practice in NaviX (PVLDB 18,
2025) and Exqutor (ICDE 2026) is to carry exactly this graph-cardinality statistic on the
catalog/metapage. TriDB already intended it — see the `legstats.c` comment quoted below.

## Current state

Files:
- `src/graph_store/gph_page.h` — on-disk 32 KB page format; defines the `GphMeta` metapage struct.
- `src/graph_store/graph_am.c` — the native graph access method: metapage init, vertex insert, **edge
  insert** (the path that must increment the new counter).
- `src/planner/join_order_legstats.c` — builds `LegStats` from the catalog; currently sets
  `avg_out_degree = 0.0` with a comment specifying this plan's fix.
- `src/planner/join_order_legstats.h` — `LegStats` struct + accessor contract.
- `src/planner/join_order.c` — the FROZEN decision core (do NOT modify — see Scope).

The metapage struct today (`gph_page.h:56-65`) — note there is **no edge count**:

```c
typedef struct GphMeta
{
	uint32		gm_magic;		/* GPH_MAGIC */
	uint32		gm_version;		/* GPH_VERSION */
	uint64		gm_next_vid;	/* next vertex id to assign (dense, monotone) */
	uint32		gm_vertex_count;
	uint32		gm_reserved;
	BlockNumber	gm_first_vertex_blk;	/* head of the vertex-page chain (Invalid if none) */
	BlockNumber	gm_last_vertex_blk;		/* tail of the vertex-page chain (append target) */
} GphMeta;
```

The metapage is initialized in `gph_ensure_meta` (`graph_am.c:152-160`):

```c
	meta = (GphMeta *) GphPageRecordBase(page);
	meta->gm_magic = GPH_MAGIC;
	meta->gm_version = GPH_VERSION;
	meta->gm_next_vid = 0;
	meta->gm_vertex_count = 0;
	meta->gm_reserved = 0;
	meta->gm_first_vertex_blk = InvalidBlockNumber;
	meta->gm_last_vertex_blk = InvalidBlockNumber;
	((PageHeader) page)->pd_lower += MAXALIGN(sizeof(GphMeta));
```

The intended fix is documented verbatim in `src/planner/join_order_legstats.c:89-103`:

> `avg_out_degree: PLACEHOLDER 0.0.` … *"Adding `gm_edge_count` to the metapage (incremented in
> `gph_insert_edge`) and deriving `avg_out_degree = gm_edge_count / NULLIF(gm_vertex_count, 0)` is a
> graph-store-track follow-on (ADR-0011 Stage 0)."* … *"avg_out_degree is NOT an input to
> `tridb_choose_join_order` (FROZEN §10.1 — it is carried only for `tridb_estimate_intermediate`'s
> EXPLAIN graph fan-out)."*

**Load-bearing constraint discovered during planning (read before Step 2).** `gph_insert_edge`
(`graph_am.c:362-479`) **does not currently lock or register the metapage at all** — it locks the
source vertex page (`vbuf`) then the adjacency page (`abuf`). `gph_insert_vertex`
(`graph_am.c:246-360`), by contrast, locks **`metabuf` first, then `vbuf`**. To avoid a buffer-lock
deadlock, the edge path must also acquire `metabuf` **before** `vbuf`. The "append to a tail
adjacency page with room" branch (`graph_am.c:436-442`) currently registers **only** `abuf`
("vertex record unchanged; do not register vbuf") — the metapage increment must be added to **all
three** edge-insert branches, and the metapage buffer must be registered in the `GenericXLogState`
of each so the increment is WAL-logged atomically with the edge.

Conventions to honor (match exactly):
- All graph writes go through `GenericXLogStart` / `GenericXLogRegisterBuffer` / `GenericXLogFinish`
  — the **shared** WAL. **No second WAL, no custom rmgr** (`gph_page.h:8-14`; CLAUDE.md golden rule 2).
- Buffer lock order is metapage → vertex page → adjacency page (the order `gph_insert_vertex` uses).
- Counters that can be derived from `pd_lower` are NOT duplicated (`gph_page.h:108-120`); a
  store-wide edge count is genuinely new state (it spans pages), so it belongs on the metapage.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Python tests (here) | `make test` | pytest passes, exit 0 |
| Lint (here) | `make lint` | ruff clean, exit 0 |
| Engine build (GX10 only) | `scripts/gx10build.sh` then `make graph-test` | graph engine suite passes |
| Graph engine suite (GX10) | `make graph-test` | all `test/*.sql` graph suites pass |

## Scope

**In scope** (the only files you may modify):
- `src/graph_store/gph_page.h` — add the `gm_edge_count` field.
- `src/graph_store/graph_am.c` — init it; increment it in `gph_insert_edge`; expose a read accessor.
- `src/planner/join_order_legstats.c` — populate `avg_out_degree` from the new counter.
- `src/planner/Makefile` — add `join_order_legstats.o` to `OBJS` and the `-I…/graph_store` include so
  the metapage-reading helper actually compiles into the `join_order` extension (GX10 build wiring; see
  Step 5). **This is in scope** — without it, 006 ships an authored-but-unbuildable planner change.
- `src/graph_store/graph_store_am--0.1.0.sql` — only if you add a SQL-visible `graph_store.edge_count()`
  / `avg_out_degree()` debug function (optional, see Step 4).
- `test/` — a new SQL test asserting the counter survives crash/WAL replay (GX10-run).
- `tests/test_join_order.py` — extend the host derivation test (runs here).

**Out of scope** (do NOT touch):
- `src/planner/join_order.c` and the FROZEN functions in it — `avg_out_degree` is explicitly NOT an
  input to `tridb_choose_join_order` (frozen contract, `join_order_heuristic_v0.1.0.md` §10.1).
  Changing the decision function breaks parity with `tests/test_join_order.py` and the Python ref.
- The 10% selectivity threshold, the `<=` boundary, or any decision semantics.
- The vector or relational stores.
- Per-tuple MVCC for the counter (v1 uses txn-level visibility + GenericXLog; see "Maintenance notes"
  for the known abort-accounting caveat — do not try to make the counter MVCC-exact in this plan).

## Git workflow

- Branch: `advisor/006-metapage-degree-stats` (or the repo convention `dustin/dev-NNNN` if a Linear
  issue is assigned).
- Commit per step; message style `type(scope): summary` (e.g. `feat(graph-store): add gm_edge_count
  to metapage`), matching the repo's `git log`.
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Add `gm_edge_count` to the metapage struct

In `src/graph_store/gph_page.h`, add a `uint64 gm_edge_count` field to `GphMeta`. Place it on an
8-byte boundary (after `gm_vertex_count`/`gm_reserved`, which together fill 8 bytes) so alignment is
clean:

```c
typedef struct GphMeta
{
	uint32		gm_magic;
	uint32		gm_version;
	uint64		gm_next_vid;
	uint32		gm_vertex_count;
	uint32		gm_reserved;
	uint64		gm_edge_count;	/* store-wide directed-edge count (FR-6 avg_out_degree source) */
	BlockNumber	gm_first_vertex_blk;
	BlockNumber	gm_last_vertex_blk;
} GphMeta;
```

This changes `sizeof(GphMeta)`. The metapage reserves space via `pd_lower += MAXALIGN(sizeof(GphMeta))`
(`graph_am.c:160`), so the larger struct is accommodated automatically. There is **no on-disk
backward-compatibility requirement** — graph stores are re-created for tests/benchmarks (the v0 store
is a heap; `docs/graph_store_v0_limitations.md`). Do NOT add a migration path.

**Verify** (GX10): `scripts/gx10build.sh` compiles `gph_page.h` without the static-assert failures
at the bottom of the header firing → exit 0.

### Step 2: Initialize and increment the counter

In `gph_ensure_meta` (`graph_am.c:152-160`), initialize it next to `gm_vertex_count`:

```c
	meta->gm_vertex_count = 0;
	meta->gm_reserved = 0;
	meta->gm_edge_count = 0;
```

In `gph_insert_edge` (`graph_am.c:362-479`), increment `gm_edge_count` by 1 on every successful edge
append, WAL-logged atomically with the edge. **Acquire and lock `metabuf` (block `GPH_META_BLKNO`)
FIRST, before `vbuf`** (to match `gph_insert_vertex`'s lock order and avoid deadlock), and register
it in the `GenericXLogState` in **each** of the three branches so the increment is durable with the
edge:
- first-edge branch (`graph_am.c:410-430`),
- append-to-tail-with-room branch (`graph_am.c:436-442`) — this one currently registers only `abuf`;
  you must add `metabuf` here too,
- chain-new-adjacency-page branch (`graph_am.c:444-473`).

Pattern (mirror how `gph_insert_vertex` registers `metabuf` then mutates `meta` through the
registered page, e.g. `graph_am.c:284-301`):

```c
	metapage = GenericXLogRegisterBuffer(state, metabuf, 0);
	meta = (GphMeta *) GphPageRecordBase(metapage);
	/* ... append the edge slot ... */
	meta->gm_edge_count += 1;
```

Only count an edge as inserted when the slot is actually appended. Both endpoints are already
validated to exist before any mutation (`graph_am.c:386-393`); the `ereport(ERROR)` there aborts the
txn, so the increment never persists for a rejected edge — correct by construction.

**Decision to record in the commit message (this is a spike decision):** a store-wide counter on the
metapage means **every** edge insert now takes an exclusive lock on the single metapage buffer →
serialized edge inserts. This is acceptable for v1 because the workload is **bulk-load-then-query**
(tests/benchmarks build the full corpus, then `CREATE INDEX`, then read — see ADR-0007 "build the
full corpus before `CREATE INDEX`"). If a future concurrent-ingest workload makes the metapage a hot
spot, the alternative is a **per-vertex** `vr_out_degree` on `GphVertexRecord` (reusing the `vr_pad`
field), incremented on the already-locked `vbuf` — but that requires registering `vbuf` in the
append-with-room branch and a scan to sum degrees for `avg_out_degree`. Do NOT implement the
per-vertex variant in this plan; note it as the documented escape hatch.

**Verify** (GX10): `make graph-test` → the edge-insert suites still pass (no regression).

### Step 3: Expose a read accessor and populate `avg_out_degree`

Add a small accessor next to `gph_read_meta` (`graph_am.c:167-180`) — or reuse `gph_read_meta`
directly — so the planner helper can read `gm_edge_count` and `gm_vertex_count` under a share lock.

In `src/planner/join_order_legstats.c`, replace the placeholder (`legstats.c:103`, `out->avg_out_degree
= 0.0;`) with the documented derivation. Open the graph store relation, read the metapage, and
compute:

```c
	/* avg_out_degree = gm_edge_count / gm_vertex_count, 0.0 when no vertices (NULLIF guard). */
	out->avg_out_degree = (meta.gm_vertex_count > 0)
		? (float8) meta.gm_edge_count / (float8) meta.gm_vertex_count
		: 0.0;
```

Keep the existing comment's contract: `avg_out_degree` is still **not** an input to the ordering
decision; it now feeds `tridb_estimate_intermediate`'s graph fan-out (the EXPLAIN estimate in
`join_order_heuristic_v0.1.0.md` §5). If wiring the graph-store relation handle into this leaf helper
is not clean (it currently only takes the relational `Relation`), STOP and report — the seam may need
ADR-0011's lowering to pass the graph relation in, which is out of this plan's scope.

**Verify** (GX10): `make graph-test` includes `test/join_order_test.sql` — it must still pass
(`avg_out_degree` is not a decision input, so parity holds).

### Step 4 (optional): SQL debug function

If useful for the crash test, add `graph_store.edge_count()` returning `gm_edge_count` to
`src/graph_store/graph_store_am--0.1.0.sql` and a C wrapper in `graph_am.c`, following the existing
`graph_store.neighbors` / `graph_store.add_edge` registration pattern.

### Step 5 (GX10): Wire `join_order_legstats.c` into the planner build

`src/planner/Makefile` currently has `OBJS = join_order.o` only — so `join_order_legstats.c` (a
pre-existing UNBUILT draft that Step 3 now makes load-bearing, since it reads the graph metapage) is
**not compiled into the extension**. Without this step, 006 produces a planner change that cannot be
built on the GX10. Make it self-consistent:

```make
OBJS        = join_order.o join_order_legstats.o
# legstats.c includes src/graph_store/gph_page.h to read GphMeta:
PG_CPPFLAGS += -I$(srcdir)/../graph_store
```

Keep the existing `CFLAGS := $(filter-out -ffast-math,$(CFLAGS))` line intact (it protects the FROZEN
boundary-case IEEE division — Linus review, DEV-1170). This is **GX10-only** (PGXS resolves against the
fork's `pg_config`); author it here, build on the GX10. If `join_order_legstats.c` turns out NOT to
need the graph relation at all because ADR-0011's lowering passes the metapage stats in as scalars
instead (see Step 3's STOP condition), then this Makefile change is unnecessary — report that rather
than adding a dead include.

**Verify** (GX10): `scripts/gx10build.sh` builds the `join_order` extension with both objects linked;
`make graph-test` (which runs `test/join_order_test.sql`) still passes.

## Test plan

- **Host (runs here, gating):** extend `tests/test_join_order.py` with a unit test for the derivation
  formula — given `gm_edge_count`/`gm_vertex_count` pairs (incl. `vertex_count == 0` → `0.0`,
  and a normal case e.g. 500 edges / 100 vertices → `5.0`), assert the Python reference's
  `avg_out_degree` handling matches. Model it after the existing boundary/edge-case tests in that
  file (`test_doc_section8_acceptance_corpora`).
- **Engine (GX10):** add `test/graph_edge_count_test.sql` (or extend an existing graph suite):
  insert N vertices and M edges, assert `graph_store.edge_count() == M`; then run the
  crash-recovery driver (`scripts/crash_recovery_test.sh`) over a tri-store txn that adds edges and
  assert `gm_edge_count` matches the committed edge count after recovery (the counter is GenericXLog
  REDO-covered, so an aborted txn's edges must NOT be counted post-recovery — see Maintenance notes).
- Verification: `make graph-test` → all graph suites pass, including the new edge-count assertions.

## Done criteria

ALL must hold:

- [ ] `make test` exits 0; the new `tests/test_join_order.py` derivation case passes (runs here).
- [ ] `make lint` exits 0 (runs here).
- [ ] (GX10) `scripts/gx10build.sh` builds; `make graph-test` passes including the new edge-count test.
- [ ] `grep -n gm_edge_count src/graph_store/gph_page.h src/graph_store/graph_am.c src/planner/join_order_legstats.c`
      shows the field defined, initialized, incremented in all three `gph_insert_edge` branches, and
      consumed in the legstats helper.
- [ ] `git diff 8b19cb5..HEAD -- src/planner/join_order.c` is **empty** (the FROZEN core is untouched).
- [ ] `grep -n join_order_legstats.o src/planner/Makefile` shows it in `OBJS` (Step 5 wiring present),
      OR the report states ADR-0011 lowering made it unnecessary (Step 5 STOP path).
- [ ] No files outside the Scope list are modified (`git status`).
- [ ] `advisor-plans/README.md` status row updated.

## STOP conditions

Stop and report (do not improvise) if:
- The "Current state" excerpts don't match the live code (drift since `8b19cb5`).
- `gph_insert_edge` no longer has the three-branch structure described, or already touches `metabuf`
  (someone changed the lock discipline).
- Wiring the graph relation handle into `tridb_build_legstats` requires changing `join_order.c` or the
  frozen `LegStats`/decision contract — that is out of scope (ADR-0011 lowering territory).
- The crash-recovery test shows aborted-txn edges being counted (the counter is leaking across abort)
  — report it; do not paper over it.

## Maintenance notes

- **Abort accounting caveat (by design for v1).** The counter is incremented under GenericXLog, so a
  crashed/aborted txn's increments are rolled back with the page image (full-image WAL). But the v1
  graph store uses **txn-level** visibility (`es_xmin`, abort ⇒ invisible) rather than per-tuple
  delete/`xmax`. There is no edge **delete** path in v1, so the counter only grows; if a delete path
  is added later, `gm_edge_count` must be decremented there, and you must decide whether it counts
  live-visible edges or all-ever-inserted. Document the choice when that lands.
- A reviewer should scrutinize: the lock order (metapage before vertex page) in all three branches,
  and that `metabuf` is registered in every `GenericXLogState` that bumps the counter (an
  un-registered mutation is a silent torn-write / lost-update across crash).
- Deferred out of this plan (do not do here): the 8-bucket degree **histogram** and per-vertex
  `vr_out_degree` (only needed for skew-aware estimation / `tjs_open` hub down-weighting — that's
  plan 007 territory); the `amanalyze` hook; making `avg_out_degree` an actual decision input (would
  require un-freezing the FR-6 contract — a deliberate v2 ADR, not a code change).
