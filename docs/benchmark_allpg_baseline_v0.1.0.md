# All-Postgres baseline: pgvector + relational `links` + plain SQL vs the fused TriDB statement (v0.1.0)

**Date:** 2026-07-20 · **Box:** DGX Spark (GB10, aarch64, 128 GB) · **Engine:** stock PostgreSQL 17.10,
container `tridb-wikidata-pg17` (`tridb/pg17-unfork:dev`, the Gate B corpus, up 4+ days) ·
**Harness:** `bench/wd_allpg_baseline.py` · **Artifacts:** `bench/results/wd_1m_allpg_{metrics.json,report.md}`

## The question

The hostile launch question: *"TriDB is just three extensions on stock Postgres — so why not plain
pgvector + a relational links table + plain SQL in ONE Postgres, with no TriDB extension in the
query path?"* This bench answers it head-on: the same logical query, the same database, the same
box, the same session boundary — one side through TriDB's native surfaces, the other through
nothing but stock Postgres + pgvector.

## TL;DR

Pinned-oracle KBQA leg (the Gate B query class: 2-hop typed reach, median 22 vertices, P31 filter,
exact vector rank, k=10; 50 pinned queries, 25 runs each, warm, client-clocked psycopg over TCP,
one backend for both contenders):

| Contender (same DB, same backend) | recall@10 | median | p95 |
|---|---:|---:|---:|
| **A — fused** (native `gph_traverse_bfs` → P31 → pgvector rank) | 0.986 | **0.049 ms** | 0.310 ms |
| **B — all-PG SQL** (recursive CTE over `links` → P31 → pgvector rank) | 0.986 | **0.065 ms** | 0.353 ms |
| **C — multi-store** (Milvus + Neo4j + pg, app-side) | 0.986 | **3.34 ms** | — (cited from Gate B, not re-run) |

- **A beats B by only ~16 µs/query (1.33×)** — paired per-query: A faster on 48/50, median B−A =
  +15.5 µs. Server-side (`EXPLAIN ANALYZE`): exec 0.034 vs 0.053 ms, planning 0.021 vs 0.051 ms —
  about half of B's deficit is *planning* the recursive CTE, which prepared statements would reclaim.
- **Both beat the multi-store by ~50–70×.** The 23.68× Gate B headline is a *single-system vs
  three-systems* result, and this bench shows most of that win is available to plain SQL in one
  Postgres — the fused operator adds a real but small constant on top at this query class.
- **Reached sets and returned ids are byte-identical on all 50 queries** (per-query `EXCEPT`
  verification both ways) — recall is identical by construction, so the latency comparison is at
  exactly matched semantics.
- **Seedless (filtered-ANN) leg: plain pgvector matches or beats `tjs_open`** at every matched-recall
  point measured, with far better tails (below). This undercuts the seedless operator story today
  and is consistent with the open ADR-0015 E3 work.

## What ran

- Corpus: the EXACT Gate B slice — 1,002,331 entities, **7,422,959** native-AM edges, pgvector
  0.8.5 HNSW (m=16, ef_construction=64), extensions `graph_store_am 0.1.0` / `tjs_pg 0.1.0`.
  The container pre-dates the PPR default flip and the writer-lock work — fine for this bench
  (read-only queries; no PPR in either contender), stated for reproducibility.
- Queries/oracle: the 50 pinned queries + committed exact oracle
  (`bench/results/wd_1m_oracle.json`, k=10, hops=2), same as Gate B.
- `links` materialization: **additive only** — `links(src bigint, dst bigint, type_id int)` COPY-loaded
  from the same slice shards by the same kept-edge rule the engine loader used
  (`tools/wikidata_engine_load.iter_kept_edges`: both endpoints in-slice, duplicates preserved,
  `type_id` through the engine's own edge-type dictionary). **Edge-parity hard gate:**
  `count(links)` == `graph_store.gph_edge_count()` == 7,422,959, or the harness aborts.
- Boundary: both contenders through the SAME psycopg connection (one backend), client
  `perf_counter` around execute+fetchall, warm cache, runs interleaved A/B with alternating order,
  median of 25 runs/query; medians + p95 across the 50 queries. A secondary in-server channel
  (`EXPLAIN (ANALYZE, TIMING OFF)`, the gbrain bench's convention) is reported alongside.

## Exact SQL

**A — the Gate B fused filter-first statement, verbatim** (session: `SET enable_seqscan = off;
SET graph_store.assume_dense_open = on` — the Gate B pair, disclosed there):

```sql
SELECT e.id
FROM graph_store.gph_traverse_bfs(:x, 2, :type_id) AS t(dst)
JOIN entities e ON e.id = t.dst
WHERE e.P31 @> ARRAY[:t] AND e.id <> :x
ORDER BY e.embedding <-> ':qv', e.id LIMIT 10;
```

**B — no TriDB extension in the query path** (`:qv` is the same inlined pgvector literal):

```sql
WITH RECURSIVE reach(dst, depth) AS (
    SELECT l.dst, 1 FROM links l WHERE l.src = :x AND l.type_id = :type_id
  UNION
    SELECT l.dst, r.depth + 1
    FROM reach r JOIN links l ON l.src = r.dst AND l.type_id = :type_id
    WHERE r.depth < 2
)
SELECT e.id
FROM entities e
WHERE e.id IN (SELECT dst FROM reach WHERE dst <> :x)
  AND e.P31 @> ARRAY[:t]
ORDER BY e.embedding <-> ':qv'::vector, e.id LIMIT 10;
```

### B's tuning (the fairness bar of the gbrain bench)

- Covering btree `links_src_type_dst ON links (src, type_id, dst)` → every traversal step is an
  **Index Only Scan, Heap Fetches: 0** (`VACUUM ANALYZE` after load keeps the visibility map set).
- Plan shape (from the captured `EXPLAIN (ANALYZE, BUFFERS)`): Recursive Union of index-only scans
  → HashAggregate dedup of the reached set → Nested Loop into `entities_pkey` with the `p31 @>`
  filter → top-N heapsort on `<->`. All memory-resident (`shared hit`, zero reads warm).
- Planner defaults beat forced plans: `enable_seqscan = off` changes nothing (verified — B's plan is
  byte-identical under the session-A settings, which is why one shared backend is fair), `work_mem`
  is irrelevant at reach ≤ 74. No parallelism at this row count.
- Iterated live until we believe a skilled Postgres user could not readily beat it at this shape:
  the remaining B deficit is ~20 µs of executor overhead (CTE + HashAggregate machinery) and
  ~30 µs of extra planning for the recursive CTE — the latter reclaimable with PREPARE, which
  would apply equally to A.

## Seedless (filtered-ANN) leg

The pinned leg never exercises pgvector's ANN scan (filter-first ranks a tiny exact set), so the
one leg where pgvector's index actually runs is the SM-4 seedless shape
(`bench/wikidata_sm4_seedless`): type-filtered ANN over all 1M, live exact oracle, same seeded 50
queries (seed 1354), median of 7 runs, same single-connection boundary:

| Point | recall@10 | median | p95 |
|---|---:|---:|---:|
| `tjs_open` tc=16 budget=80k | 0.822 | 0.967 ms | 69.8 ms |
| `tjs_open` tc=64 budget=20k | 0.832 | 1.705 ms | 119.3 ms |
| `tjs_open` tc=256 budget=20k | 0.842 | 12.264 ms | 219.7 ms |
| pgvector iterative ef=100 | 0.820 | **0.762 ms** | **26.5 ms** |
| pgvector iterative ef=200 | 0.834 | **1.395 ms** | **28.1 ms** |
| pgvector iterative ef=400 | 0.858 | 2.607 ms | 30.3 ms |
| pgvector iterative ef=800 | 0.872 | 5.103 ms | 29.4 ms |

The plain-SQL side is one statement (`WHERE p31 @> ARRAY[:t] AND id <> :x ORDER BY embedding <->
(SELECT embedding FROM entities WHERE id = :x) LIMIT 10` under `hnsw.iterative_scan =
relaxed_order`). **At matched recall (0.82 / 0.83), plain pgvector is ~1.2× faster at the median
and 3–4× better at p95, and its curve extends to recall levels (0.858 / 0.872) the swept `tjs_open`
points do not reach.** Honest verdict: at this query class the seedless `tjs_open` currently adds
no value over pgvector's own iterative filtered scan — the E3 gaps (per-candidate distance
exposure, budget-shaped termination) are not closed by the current operator, and the tail behavior
(p95 70–220 ms vs pgvector's stable ~26–30 ms) is a defect signature worth its own issue.

## Honest findings

1. **The fused operator's edge over strong single-Postgres SQL is ~16 µs at this query class — a
   real, reproducible, paired win (48/50 queries), but NOT the headline-grade gap.** The
   launch-question answer that survives this measurement: TriDB's 23.68× Gate B number is
   single-system fusion vs *multi-system* assembly. Against a well-tuned all-PG composition the
   honest pitch at reach-~50 is (a) a modest constant win, (b) one native graph store instead of a
   hand-maintained `links` mirror + its btree (storage/locality: the gbrain bench measured 28×
   fewer pages touched per hub for the native AM), and (c) the same one-WAL consistency argument —
   which plain SQL in one Postgres *also* gets, so consistency alone no longer differentiates
   against THIS baseline (it differentiates against the multi-store).
2. **Recall is identical by construction** — reach parity verified per query, ids byte-identical.
   Graded recall vs the committed oracle is 0.986 today for BOTH contenders (Gate B's transcript
   graded the same statement 0.992; Δ = 0.006 = 3/500 ids, within the harness's 0.02 epsilon —
   near-tie rank flips at float32 literal precision; it does not affect the A-vs-B comparison,
   which is exact-set-vs-exact-set).
3. **Where B would win instead:** the gbrain bench already showed the set-based relational join
   BEATS per-node native expansion at 2-hop/100k-scale hub traversals (30.6 vs 88.9 ms; its
   2-pages-vs-~2000-index-entries locality finding is the counter-asset). This bench's reach ≤ 74
   never enters that regime — do not extrapolate either side's number to hub/deep traversals.
4. **The seedless leg undercuts the operator today** (table above). Report it, fix it, or scope the
   operator claims to filter-first.

## Measurement honesty box

- **One box, one core-class caveat:** the GB10 has heterogeneous cores (10× Cortex-X925 + 10×
  Cortex-A725). Two separate connections land on different core classes and the ~2–3× class gap
  swamps a 16 µs difference — early split-connection passes flipped the verdict 0.24×–2.4× pass to
  pass. The published protocol pins BOTH contenders to ONE backend (one connection), interleaves
  runs A/B with alternating order, and cross-checks with the in-server channel. Absolute medians
  therefore reflect a single (fast-class) backend; Gate B's 0.14 ms for the same statement A was
  psql-in-container with no core control — treat 0.049 vs 0.14 ms as boundary/core variance, and
  the Gate B number as corroborating A's magnitude, not as this bench's comparison row.
- Warm cache throughout (shared hit only in captured plans); container settings as found
  (`shared_buffers=128MB`, `work_mem=4MB`) — untouched, additive-only discipline (new `links`
  table + index; nothing dropped, nothing restarted).
- Client p95s in the pinned leg (0.31 / 0.35 ms) are scheduling-noise tails on a shared box, not
  query-shape tails — the min-of-runs floor is A 0.045 / B 0.058 ms and the server-side exec p95s
  are 0.079 / 0.102 ms.
- NOT tested: deep traversals and hub fan-outs (see finding 3), cold cache, concurrency, writes
  (the `links` mirror's update/consistency cost is the architectural argument, not measured here),
  BM25, and the fork/GX10 path (this is the stock-PG17 un-fork container).
- The 3.34 ms multi-store row is cited from `docs/gate_b_spike_v0.1.0.md` (2026-07-15, same
  container corpus, same 50 queries) and was NOT re-run.

## Repro

```bash
# on the Spark (container tridb-wikidata-pg17 at 172.17.0.5, slice at ~/data/wikidata_slice_1m)
PYTHONPATH=~/code/tridb python bench/wd_allpg_baseline.py load-links --host 172.17.0.5 \
    --slice ~/data/wikidata_slice_1m \
    --engine-manifest bench/results/wd_1m_pg17_engine_load_manifest.json
PYTHONPATH=~/code/tridb python bench/wd_allpg_baseline.py run --host 172.17.0.5 \
    --oracle bench/results/wd_1m_oracle.json \
    --engine-manifest bench/results/wd_1m_pg17_engine_load_manifest.json \
    --emb ~/data/wikidata_slice_1m/emb/dense_id_aligned.npy --runs 25 \
    --out bench/results/wd_1m_allpg_metrics.json
PYTHONPATH=~/code/tridb python bench/wd_allpg_baseline.py seedless --host 172.17.0.5 \
    --runs 7 --out bench/results/wd_1m_allpg_metrics.json
python -m bench.wd_allpg_baseline report --metrics bench/results/wd_1m_allpg_metrics.json \
    --out bench/results/wd_1m_allpg_report.md
```

## Addendum A1 (2026-07-20) — issue #30 seedless-tail fix: post-fix table (plan 102)

**What changed:** plan 102 diagnosed the seedless p95 defect (per-query traces, Spark, this
container read-only + a new one): the outliers are very-selective-filter queries whose
filter-passing candidates stop appearing in the stream — the ADR-0007 drop rule counts only
passers (DEV-1169, deliberately), so those queries had NO operator-side bound and drained to
pgvector's internal stream end (~21k tuples) at ~10 µs/tuple (~4–5 µs pgvector deep-drain cost,
superlinear in depth and ef_search-independent; ~5 µs/candidate SPI filter probe — plan cached,
the cost is per-execution SPI machinery). Fix: **`tjs.vector_scan_budget`** (default 0 =
disabled, byte-identical) — an operator-owned examined-candidates cap, disclosed via
`tjs_open_termination_reason() = 'scan_budget'` / `tjs_open_budget_capped() = true`. The tail
is bounded by BOTH knobs together: `hnsw.max_scan_tuples` bounds pgvector's internal iteration
work (emitting tuple ~2000 under a high `max_scan_tuples` can still cost ~50 ms of re-walk);
the vector-scan budget bounds and DISCLOSES the operator's stream.

**Setup:** same 50 seeded queries (seed 1354), live exact oracle, median of 7, same
single-connection boundary, same box, same day. (a) OLD = this doc's container
`tridb-wikidata-pg17` (extensions 0.1.0), re-run; (b) NEW = `tridb-issue30-pg17` (port 5460,
extensions 0.2.0 + the plan-102 fix), same slice reloaded with the committed loader
(edge-parity gate 7,422,959 == native AM count, PASSED); (c) plain pgvector iterative in the
NEW container. NB the NEW container's HNSW is an independent (parallel) build with a slightly
weaker recall curve — pgvector's own ef=800 point grades 0.814 there vs 0.874 on OLD — so
matched-recall comparisons are WITHIN a container, never across. Artifacts:
`bench/results/wd_1m_issue30_{old_seedless,new_seedless,new_lowmst}.json`.

| Point (same-day) | recall@10 | median | p95 |
|---|---:|---:|---:|
| OLD `tjs_open` tc=16 mst=80k | 0.824 | 0.865 ms | 70.4 ms |
| OLD `tjs_open` tc=64 mst=20k | 0.834 | 1.483 ms | 125.9 ms |
| OLD `tjs_open` tc=256 mst=20k | 0.844 | 11.798 ms | 217.4 ms |
| OLD pgvector ef=100 | 0.822 | 0.787 ms | 25.8 ms |
| NEW `tjs_open` tc=16 mst=80k vsb=0 | 0.796 | 0.786 ms | 70.1 ms |
| NEW `tjs_open` tc=64 mst=20k vsb=0 | 0.804 | 1.463 ms | 115.1 ms |
| NEW `tjs_open` tc=256 mst=20k vsb=0 | 0.812 | 6.620 ms | 226.5 ms |
| NEW `tjs_open` tc=64 mst=20k vsb=2000 | 0.792 | 1.481 ms | 60.7 ms |
| NEW `tjs_open` tc=256 mst=20k vsb=10000 | 0.812 | 6.414 ms | 135.7 ms |
| NEW `tjs_open` tc=16 mst=5000 vsb=2000 | 0.764 | 0.621 ms | 13.2 ms |
| NEW `tjs_open` tc=64 mst=5000 vsb=2000 | 0.772 | 1.593 ms | 15.5 ms |
| NEW `tjs_open` tc=256 mst=2000 vsb=2000 | 0.752 | 3.329 ms | 11.5 ms |
| NEW pgvector ef=40 | 0.782 | 0.345 ms | 28.9 ms |
| NEW pgvector ef=100 | 0.802 | 0.640 ms | 26.3 ms |
| NEW pgvector ef=200 | 0.800 | 0.987 ms | 28.0 ms |
| NEW pgvector ef=800 | 0.814 | 4.568 ms | 32.1 ms |

**Verdict vs the #30 targets (median ≤ ~1.1×, p95 ≤ 2× pgvector at matched recall) — NOT met
at matched recall; reported straight.** Nearest matched pairs in the NEW container: tc=256
mst=20k vsb=10000 (0.812) vs pgv ef=800 (0.814) = median 1.40×, p95 4.2×; tc=64 mst=20k
vsb=2000 (0.792) vs pgv ef=100 (0.802) = median 2.31×, p95 2.31×. What the fix DOES deliver:
(1) the runaway tail is gone as a defect signature — with both knobs set the tjs p95 is FLAT
and BELOW pgvector's own curve (11.5–15.5 ms vs 26–32 ms) at a disclosed ~3–6 pt recall cost,
where the pre-fix operator sat at 70–226 ms with NO cap and NO disclosure; (2) every capped
ending is now honest (`scan_budget`/true — closing ADR-0015 E3.3's budget-shaped-termination
gap). The remaining gap to the numeric targets is the **per-candidate constant**: at equal
drain depth tjs pays ~5 µs/candidate of SPI probe machinery (plan already cached) vs
pgvector's native executor qual (~sub-µs) — at the ~21k-tuple drains recall parity requires,
that is ~+105 ms of p95 neither budget knob can remove. Flagged follow-up (NOT implemented,
per plan-102 scope): a guarded ExprState fast path evaluating simple single-table filters
directly against the already-fetched heap slot, SPI fallback for the general fragment surface.
The honest guidance of this doc stands: for pure filtered-ANN with no graph leg, use
pgvector's iterative scan directly; `tjs_open`'s value is the fused graph+filter+vector
composition — the budget makes its seedless tail bounded and honest, not free.
