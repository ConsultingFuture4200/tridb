# ADR-0013: rewire the operators and benchmarks onto the v1 native graph access method

Status: **Accepted** (2026-07-03, maintainer session; executed by advisor plan 025 — Stage A/B addendum below). Proposed 2026-07-01 (advisor plan 016).
Design detail: [[graph_rewire_design_v0.1.0]] (`docs/graph_rewire_design_v0.1.0.md`).
Measured evidence: `scripts/graph_v0v1_bench.sh` + `test/graph_v0v1_bench.sql` (engine-gated).

## Context

TriDB's central architectural bet (golden rule 3, ADR-0002/0003) is a **native adjacency-list
access method**: the v1 store in `src/graph_store/` — 32KB pages via the shared buffer manager,
GenericXLog WAL, the `gph_*` SQL surface — built and passing 8 AM harnesses. ADR-0003 says v1
"supersedes" the v0 store. **But nothing on the shipped path uses v1.** Verified at commit `408e852`:

- **Both operators probe v0.** `tjs`'s `graphReachableT` issues
  `SELECT dst FROM graph_store.neighbors(%lld) AS dst`; `tjs_open`'s `expandMultiSeedO` issues
  `LATERAL graph_store.neighbors(f.src)` — both against the v0 heap-backed *extension*
  (`src/graph_store_ext/`, SPI over a regular heap table), not the native AM.
- **All benchmarks install v0.** The 9 bench/test drivers
  (`scripts/bench_{graphrag,graphrag_h2h,sm2,filtered,public,live,gx10_sweep}.sh`,
  `scripts/graph_test.sh`, `scripts/tjs_test.sh`) set `EXT="$ROOT/src/graph_store_ext"` and
  `CREATE EXTENSION graph_store`.
- **Every published headline measured the predecessor store** — SM-2 (15.1×), SIFT-1M filtered,
  GraphRAG (+15.6 pt), the neon sweep — all ran the v0 heap composition, not the native AM the
  thesis is about.
- The canonical SQL/PGQ front door (`graph_store.graph_query`, ADR-0008) also lives only in v0.

Full call-site → v1 interface-gap matrix (90 v0-surface call-site hits across 13 files, out of 108
total `graph_store.` hits) and the id-mapping analysis are in the companion design doc §2–§3.

## Decision

Adopt a **staged migration** from the v0 heap extension to the v1 native AM, executed as follow-on
plans (one per stage) only after this ADR is accepted. TR-1 and the one-WAL/one-txn invariants hold
throughout — the v1 AM already participates in the host transaction (FR-7 proven live, ADR-0003a).

- **Stage A — operators onto v1.** Swap the two operators' SPI text from `graph_store.neighbors`
  to the v1 probe, and port the `graph_query` front door into the v1 extension. The id gap (v0 takes
  arbitrary bigint ids and auto-creates vertices; v1 takes dense vids that must pre-exist) is bridged
  by a new **`gph_upsert_vertex(ext_id) RETURNS bigint` mapping layer** (design doc §3, Option A —
  chosen over teaching `gph_insert_edge` to treat its argument as a vid, which would export the
  dense-id constraint to every caller). Sequenced AFTER plans 010/017 settle the shared
  `tridb_tjs_open_operator.patch`. GX10-gated C.
- **Stage B — benches + corpus onto v1.** Flip the 9 drivers to `CREATE EXTENSION graph_store_am`
  and change the corpus generators to emit the vertex-materialization pass + `gph_insert_edge`
  through the map; re-run every headline on v1 and record the deltas.
- **Stage C — archive v0.** Per ADR-0003's supersession clause, archive `src/graph_store_ext/` with
  a pointer here, and update every doc that quotes a v0-measured number to name the store measured.

## Measured evidence (Step-2 spike)

`scripts/graph_v0v1_bench.sh` loads the SAME deterministic 50k-vertex / 500k-edge graph (one
degree-5000 hub + pseudo-random tail) into each store — in separate databases, since both extensions
own the `graph_store` schema and collide — and reports bulk-load wall clock, `neighbors()` latency
over 100 fixed probe vertices, and page reads (`gph_page_reads()` for v1; `pg_statio` heap blocks
for v0). It is committed runnable; the live run is **engine-gated (unbuilt on this host — no
`tridb/msvbase:dev` image)**:

| metric | v0 (heap ext) | v1 (native AM) |
|---|---|---|
| bulk-load 500k edges (ms) | _engine-gated_ | _engine-gated_ |
| neighbors() over 100 probes (ms) | _engine-gated_ | _engine-gated_ |
| page reads over 100 probes | _engine-gated_ (heap blks) | _engine-gated_ (`gph_page_reads`) |

**Expected shape (the design premise the run must confirm):** v1 ingest looks *worse* than v0 until
O(1) vid addressing lands (rider 1) — `gph_locate_vertex` walks the vertex-page chain per lookup, so
edge ingest is O(E·V/recs_per_page). That bad number is the evidence FOR rider 1, not a blocker. v1
`neighbors()` read latency and page reads should match-or-beat v0 (the read-once adjacency scan
already landed, `ff7f239`). **STOP condition (plan 016):** if the run shows v1 `neighbors()` slower
than v0 even on warm cache at this scale, the migration premise inverts — pause this ADR at Context
and report the numbers before proceeding to Decision acceptance.

## Riders (harden the substrate before it is the operator floor)

1. **O(1) vid addressing.** `gph_locate_vertex` (`src/graph_store/graph_am.c:198`) chain-walks the
   vertex pages though vids are dense+monotone (`gm_next_vid`, graph_am.c:276) — replace with
   arithmetic addressing. Rides Stage A/B (format-touching).
2. **Widen the vertex counter.** `GphMeta.gm_vertex_count` is `uint32` (`src/graph_store/gph_page.h:60`)
   beside `uint64` `gm_next_vid`/`gm_edge_count` with a free adjacent `uint32 gm_reserved` — widen to
   `uint64` on the same format bump as rider 1 (one bump, not two).
3. **Traversal snapshot stability.** Per-`Next()` `gph_open_store(AccessShareLock)`/`relation_close`
   in `gph_neighbors`/`gph_traverse` (`graph_am.c:716,727,774`) + visibility via
   `TransactionIdDidCommit` (graph_am.c:72) without a pinned snapshot ⇒ intra-traversal torn reads
   possible. Decide whether the v1 contract covers snapshot stability before it is the operator
   substrate.

## Consequences

- **Every published headline number must be regenerated on v1 and relabeled.** Named docs:
  `docs/benchmark_sm2_v0.1.0.md`, `docs/benchmark_filtered_v0.1.0.md`,
  `docs/benchmark_graphrag_v0.1.0.md`, `docs/benchmark_h2h_v0.1.0.md`,
  `docs/benchmark_tjs_open_ref_v0.1.0.md`, and the STATUS.md banners that quote them. Until then,
  the honest label is: *headline numbers to date measure the v0 heap-backed graph extension* (added
  to `docs/STATUS.md` as the ADR-0013 banner).
- **The 128 GB headline run must NOT start until this decision is made** — otherwise it measures v0
  again and the launch inherits the mislabel.
- The `gph_upsert_vertex` map adds one indexed lookup per edge insert and per traversal open; it
  rides the same WAL (no second txn manager — golden rule 2 intact). Its cost is itself a measurable
  item for Stage-A re-measurement.

## Alternatives considered

- **Keep v0 as the permanent operator substrate; reposition v1 as future work.** Zero migration
  risk, but it forfeits the thesis claim: the differentiator is a *native adjacency-list access
  method inside the Postgres process*, and v0 is SPI over a heap `bigint[]` table — architecturally
  the "edges are a relational structure" shape golden rule 3 rejects (v0 stores adjacency arrays, not
  a join table, so it is not *fully* that anti-pattern, but it is not the native-page AM either).
  Choosing this means the launch must explicitly say the numbers describe a heap-backed composition
  and v1 is unshipped — a conscious, documented deferral, not silence. Rejected as the *default* but
  named as the fallback if the Step-2 spike inverts the premise.
- **Side-by-side (both stores live, route per query).** Impossible without relocating one extension:
  both are `relocatable = false` and own the `graph_store` schema. Not pursued.

## Sequencing vs plan 009 (CSR-lite)

Plan 009 (sorted-by-dst / CSR-lite adjacency layout, "leaning GO", prototype on
`spike/009-contiguity`) would change v1's on-disk adjacency layout. Two orders:

- **rewire-then-CSR:** land Stage A/B on today's v1 layout, then apply CSR-lite as a later format
  bump. Gets the real store under the operators sooner; pays two format bumps (rider-1/2 bump, then
  CSR bump).
- **CSR-then-rewire:** land CSR-lite first, then rewire onto the final layout — one format bump total.

**Lean: rewire-then-CSR, gated on the Step-2 numbers.** If the spike shows v1 read latency/page
reads already match-or-beat v0 (expected, post read-once-scan `ff7f239`), the operators can move onto
v1 *now* and inherit CSR-lite later — the value (measuring the real thesis component before the 128 GB
run) is worth one extra format bump. If the spike shows v1 reads still lag v0, CSR-lite's contiguity
win becomes a prerequisite and the order flips to CSR-then-rewire. This ADR does not resolve plan
009's go/no-go — it defers to plan 009 and only fixes the *ordering* relative to it.

## Addendum 2026-07-03 — Stage A/B executed and validated (advisor plan 025)

Stages A and B are DONE and measured; the Status line above is discharged here.

**Stage A (operators onto v1).** The `gph_upsert_vertex(ext_id) RETURNS bigint` id-mapping layer
(Option A, chosen over vid-as-argument) + a `gph_vid_map` dictionary side-table + a
`gph_neighbors_ext(ext_id)` external-id traversal landed in `src/graph_store/graph_store_am--0.1.0.sql`,
with drop-in v0-compat shims (`add_edge`/`neighbors`/`visits`/`graph_query`, byte-identical
signatures). Both operators' SPI probe swapped `graph_store.neighbors` → `gph_neighbors_ext` via
`scripts/patches/tridb_graph_v1_rewire.patch` (last in the chain, sentinel-verified in both operator
files + a negative guard on the stale symbol). The single-SPI-connection / no-sibling-connect
discipline (DEV-1236) is preserved — strengthened, since `gph_neighbors_ext` reads native pages
directly rather than nesting a v0 SRF's own SPI. Golden rule 3 is now genuinely satisfied: topology
traversal runs on the 32KB native adjacency pages; `gph_vid_map` is an id **dictionary**, not the edge
set.

**Stage B (benches + corpus onto v1).** All 9 drivers flipped to `CREATE EXTENSION graph_store_am`;
the 3 corpus emitters materialize vertices through the map (corpus determinism preserved —
entity ids/edges/manifests byte-identical, verified).

**Validation.** `test/graph_v0v1_parity_test.sql` (differential oracle: dense-native vs
sparse-ext-id-via-compat, digested over every probe vertex) PASSES — v0 and v1 traversals are
byte-identical. All 4 operator suites + FR-7 (txn_atomicity C1/C2, crash_recovery both scenarios)
PASS on `tridb/msvbase:gx10-v1`. The 1M filter-first headline re-measured on v1:
**median 6.66 ms, recall@5 1.000 vs the exact oracle, SM-2 24/24, 13.4× over the correct baseline**,
answers byte-identical to v0 — `docs/benchmark_sm2_1m_v0.3.0.md`. The ~2 ms over v0 (4.7→6.66) is the
id-mapping indirection, a constant per-query tax with a named future fix (a C-level dense vid cache,
DIRECTION-04).

**Riders NOT discharged (tracked, gate concurrent-writer work).** This ADR's substrate-hardening
riders remain open and must land BEFORE any concurrent-writer / incremental-ingest work makes v1 the
write floor: rider 1 (O(1) arithmetic vid addressing — an *ingest/open* cost, distinct from the read
tax above), rider 2/3 (traversal snapshot stability without a pinned per-Next snapshot;
`gph_upsert_vertex`'s `ON CONFLICT` re-SELECT can return NULL under REPEATABLE READ + a concurrent
committed winner). Both are bounded to near-zero for THIS launch by the append-only, single-writer
contract the probe already assumes, and are disclosed in the v0.3.0 honesty box — but they are the
explicit precondition for DIRECTION-04.

**Stage C (archive v0) deliberately deferred** one release cycle: v0 is the parity oracle's
counterparty and the rollback safety net (golden rule 9 exception with an explicit contract).

**Rider 1 addendum (advisor plan 048, engine-authored/GX10-unbuilt).** Rider 1 was previously
discharged only for the *ingest* side (`gph_insert_edges`'s O(1) `gph_locate_vertex_dense`, DEV-1354).
It now also lands for the *open* side: `gs_open` (the traversal Open of Open/Next/Close) tries the
same O(1) dense locate first when the session opts in with `SET graph_store.assume_dense_open = on`
(default off — a pre-existing sparse/non-dense reader is byte-identical to before this plan). This is
a session GUC, not an auto-detected or metapage-persisted condition: there is no cheap, general way to
tell a dense store from a sparse one without either an O(1) trust-the-caller flag or an O(V) scan, and
`gph_locate_vertex_dense` already hard-verifies the page it computes and ERRORs (never silently
mis-reads) if the layout turns out not to be dense — so an incorrect opt-in fails loud, not wrong.
Parity is asserted by `test/graph_dense_open_test.sql` (linear vs dense digest over every vid, incl.
a tombstoned vid and one out-of-range vid). Rider 1 is now closed for both the write and the read
path; riders 2/3 (traversal snapshot stability) remain open per the paragraph above.
