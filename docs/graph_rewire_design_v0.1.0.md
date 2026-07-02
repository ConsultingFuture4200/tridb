# v0 → v1 graph-store rewire — migration design (v0.1.0)

Companion to **ADR-0013**. This doc holds the mechanical detail the ADR references: the exact
call-site → v1 interface-gap matrix, the id-mapping design decision, and the staged migration
mechanics. The ADR holds the decision, riders, consequences, and sequencing.

Planned at commit `408e852`, 2026-07-01 (advisor plan 016). DESIGN only — no code is migrated here.

## 1. The two surfaces (verified)

| | v0 (shipped path) | v1 (native AM, target) |
|---|---|---|
| Extension | `graph_store` (`src/graph_store_ext/`) | `graph_store_am` (`src/graph_store/`) |
| Storage | heap table `graph_store.adjacency (vid PK, nbrs bigint[])`, SPI over a regular heap | 32KB native pages (metapage / vertex / adjacency) via shared buffer manager + GenericXLog WAL |
| Schema | creates `CREATE SCHEMA graph_store` | `control: schema = graph_store` (fixed; `relocatable = false`) |
| Insert edge | `add_edge(src bigint, dst bigint)` — arbitrary bigint ids, upsert-append | `gph_insert_edge(bigint src_vid, bigint dst_vid)` — **dense vids only** |
| Create vertex | implicit (first `add_edge` upserts the row) | **explicit** `gph_insert_vertex() RETURNS bigint` (assigns the next dense vid) |
| Traverse | `neighbors(src) RETURNS SETOF bigint` (C SRF, Open/Next/Close) | `gph_neighbors(bigint) RETURNS SETOF bigint`; also `gph_traverse(bigint, OUT src, OUT dst)` |
| Instrumentation | `visits()` | `gph_visits()`, `gph_page_reads()`, `gph_vertex_count()`, `gph_edge_count()` |
| Canonical front door | `graph_query(canonical_sql text)` → lowers to one `tjs()` call | none — lives only in v0 |

**Schema collision:** both extensions are `relocatable = false` and both own the `graph_store`
schema, so they cannot coexist in one database. Any migration is a *replacement*, not a
side-by-side; the Step-2 spike loads them into two separate databases for exactly this reason.

## 2. Interface-gap matrix — every consumer of the v0 surface

Grep basis: `grep -rn "graph_store\." scripts/ tools/ bench/ test/ src/graph_store_ext/` →
**108 hits**, partitioned exactly as:

| token | hits | kind |
|---|---:|---|
| `graph_store.add_edge` | 44 | v0 call site |
| `graph_store.neighbors` | 32 | v0 call site |
| `graph_store.visits` | 7 | v0 call site |
| `graph_store.graph_query` | 7 | v0 call site |
| `graph_store.adjacency` | 8 | v0 heap table (direct reads in tests) |
| `graph_store.gph_*` | 5 | v1 AM already probed in the `graph_store` schema (concurrency tests) |
| `graph_store.c` | 4 | source-filename mentions in comments |
| `graph_store.o` | 1 | Makefile `OBJS` |
| **total** | **108** | |

Per-function consumer → v1 mapping (the migration surface: 44+32+7+7 = 90 call-site hits across
the files below; `adjacency` table reads, `gph_*`, and filename/Makefile tokens are not v0-surface
calls and need no remap):

### `add_edge(src, dst)` → `gph_insert_edge(vid, vid)` (+ vertex pre-creation, + id map)
Consumers: `tools/sweep_corpus.py`, `tools/bench_sm2_corpus.py`, `tools/bench_corpus.py`,
`bench/v2a_open.py`, `bench/tjs_open_live.py`, `bench/h2h_report.py`, `test/tjs_open_smoke.sql`,
`test/trimodal_early_term.sql`, `test/graph_store_test.sql`, `test/canonical_e2e_test.sql`,
`test/trimodal_compose.sql`, `test/_fork_bug_tjs_double_scan.sql`, `test/parse_canonical.sql`
(+ the definition in `src/graph_store_ext/graph_store--0.1.0.sql`).
**Gap:** v1 has no auto-vertex-create and no arbitrary-id insert. Every one of these sites passes
arbitrary bigint entity ids (`add_edge(1, 10)`, `add_edge(300, g)`, corpus generators emitting
`add_edge(s, d)`). They require the id-mapping layer (§3) and a vertex-materialization pass.

### `neighbors(src)` → `gph_neighbors(src_vid)`
Consumers: `scripts/lib/msvbase_patches.sh` (comment), `scripts/patches/tridb_tjs_operator.patch`
(operator `graphReachableT`: `SELECT dst FROM graph_store.neighbors(%lld) AS dst`),
`scripts/patches/tridb_tjs_open_operator.patch` (operator `expandMultiSeedO`:
`LATERAL graph_store.neighbors(f.src)`), `tools/bench_corpus.py`, `bench/v2a_open.py`,
`bench/live_report.py`, `test/graph_store_test.sql`, `test/trimodal_compose.sql`,
`test/trimodal_early_term.sql` (+ definition).
**Gap:** signature is compatible (`bigint → SETOF bigint`), so this is a name swap **iff** the
argument is already a v1 vid. Because the operators receive the candidate id from the vector/heap
leg, the id passed to `neighbors` is the same external id used at `add_edge` time — so this swap is
only correct once the id map makes external id == vid (or the operators translate). These two patch
call sites are the load-bearing ones: they are the shipped operator substrate.

### `visits()` → `gph_visits()`
Consumers: both operator patches (early-termination assertions), `test/graph_store_test.sql`
(+ `src/graph_store_ext/graph_store.c` self-comment, + definition). Pure name swap; identical
semantics (per-backend traversal-step counter).

### `graph_query(canonical_sql)` → **no v1 equivalent (must be ported)**
Consumers: `test/parse_canonical.sql` (+ definition + comment). The canonical SQL/PGQ front door
(ADR-0008) lives ONLY in v0's SQL and lowers to `tjs()`. It does not touch the adjacency store
directly (it validates + lowers), so the port is a straight copy into the v1 extension's SQL — but
it MUST be carried, or the canonical query loses its front door. This is the one v0 asset with no
v1 counterpart at all.

## 3. The id-strategy decision (THE design choice)

v0 `add_edge` accepts arbitrary bigint ids and lazily creates the vertex row on first use. v1
`gph_insert_edge` accepts only dense vids `[0, gm_next_vid)` that must already exist via
`gph_insert_vertex()`. Every consumer above passes *entity ids from the relational/vector store*
(the `entities.id` space), which is arbitrary, sparse, and caller-chosen. Bridging the two is
mandatory. Two options were weighed:

**Option A — an external `gph_upsert_vertex(ext_id bigint) RETURNS bigint` mapping layer.**
A new v1 SQL function backed by a small persistent `ext_id → vid` map (a heap side-table or a
metapage-adjacent structure). `add_edge(s, d)` sites become
`gph_insert_edge(gph_upsert_vertex(s), gph_upsert_vertex(d))`; `neighbors(x)` sites become
`gph_neighbors(gph_upsert_vertex(x))` (lookup-only on the read path). The operators translate the
candidate id through the same map before probing.

- Pros: keeps the native AM's dense-vid invariant pure (the whole point of dense vids — O(1)
  arithmetic addressing, rider 1 — survives); the entity-id space stays external where it belongs;
  no format change to the AM.
- Cons: adds a lookup (one indexed probe) on every edge insert and every traversal open; the map is
  a second structure to keep transactionally consistent (it rides the same WAL, so this is
  bookkeeping, not a second txn manager — golden rule 2 intact).

**Option B — teach `gph_insert_edge` to auto-create by treating the argument as the vid.**
Drop the external-id concept: make callers responsible for assigning dense ids, i.e. the corpus
generator emits `gph_insert_vertex()` for `max(id)+1` vertices up front and uses the entity id
*as* the vid (identity map, requires entity ids to be dense `[0, N)`).

- Pros: zero lookup overhead; simplest read path; the spike (§ Step 2) uses exactly this.
- Cons: forces the *entire relational entity-id space* to be dense and gap-free forever, couples the
  graph store's addressing to the relational PK allocator, and breaks the moment an entity is
  deleted or ids are sparse (real corpora: HotpotQA paragraph ids, SIFT ids are dense today only by
  construction). It exports the id problem to every caller instead of solving it once.

**Decision: Option A (the `gph_upsert_vertex` mapping layer).** It preserves the native AM's dense
vid invariant (which rider 1's O(1) addressing depends on) while giving callers the arbitrary-id
`add_edge` ergonomics they already rely on. Option B's identity map is retained ONLY inside the
Step-2 spike (where the synthetic id space is chosen dense on purpose) to isolate storage cost from
the id question — it is not the production strategy. The mapping-layer cost is itself a measurable
item and should be included when the migration's Stage-A recall/latency is re-measured.

## 4. Staged migration mechanics (detail for ADR-0013's Decision)

- **Stage A — operators.** Add `gph_upsert_vertex`/lookup to the v1 SQL; change the two operator
  patches' SPI text (`graphReachableT`, `expandMultiSeedO`) from `graph_store.neighbors(...)` to
  the v1 probe through the map. Port `graph_query` (ADR-0008 front door) into the v1 extension.
  Sequenced AFTER plans 010/017 settle `tridb_tjs_open_operator.patch` (same file). GX10-gated C.
- **Stage B — benches + corpus.** Flip every `EXT="$ROOT/src/graph_store_ext"` /
  `CREATE EXTENSION graph_store` in the 9 bench/test scripts to `graph_store_am`, and change the
  corpus generators (`tools/*_corpus.py`, `bench/*`) to emit the vertex-materialization pass +
  `gph_insert_edge` (via the map). Re-run every headline (SM-2, SIFT-1M filtered, GraphRAG, neon
  sweep) on v1 and record the deltas.
- **Stage C — archive v0.** Per ADR-0003's supersession clause: move `src/graph_store_ext/` to an
  archive path with an ADR pointer, and update every doc that quotes a v0-measured number to name
  the store. Riders 1–2 (O(1) vid addressing, `uint32 gm_vertex_count → uint64`) ride the format
  bump inside Stage A/B (they change on-disk layout — one bump, not two).

## 5. Riders (hardening the substrate before it becomes the operator floor)

1. `gph_locate_vertex` (`src/graph_store/graph_am.c:198`) walks the whole vertex-page chain per
   lookup though vids are dense+monotone (`gm_next_vid`, graph_am.c:276). O(1) arithmetic addressing
   is possible; today edge ingest is O(E·V/recs_per_page) and every scan-open pays O(V). This is the
   number the Step-2 spike exposes.
2. `GphMeta.gm_vertex_count` is `uint32` (`src/graph_store/gph_page.h:60`) beside `uint64
   gm_next_vid`/`gm_edge_count`, with an adjacent `uint32 gm_reserved` free — widen to `uint64`
   during any format-touching migration.
3. Per-`Next()` `gph_open_store(AccessShareLock)`/`relation_close` in `gph_neighbors`/`gph_traverse`
   (`src/graph_store/graph_am.c:716,727,774`) and visibility via `TransactionIdDidCommit`
   (graph_am.c:72) without a pinned snapshot — intra-traversal torn reads are possible. Decide
   whether the v1 contract covers snapshot stability before it becomes the operator substrate.
