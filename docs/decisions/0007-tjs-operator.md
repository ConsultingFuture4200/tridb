# ADR-0007: The Traversal-Join-Similarity (TJS) operator — SRF now, CustomScan later

**Status:** Accepted (2026-06-25)
**Issue:** DEV-1169 (FR-4 — tri-modal composition in one plan)
**Related:** DEV-1167 (SQL/PGQ surface), DEV-1168 / ADR-0006 (relaxed-monotonicity vector iterator),
DEV-1170 (join order), ADR-0001 (architecture overview), ADR-0002 (adjacency-list graph store)
**Scope decision:** ship the operator as a forked, generalized `execFagins` SRF — reusing the
validated `multicol_topk.cpp` IndexScan-driving merge — not a new executor node and not SQL nesting.

## Context

TriDB's thesis is that all three modalities — **graph traversal, relational filter, vector
similarity** — compose in **ONE** Postgres plan with a **single global, early-terminating top-k**
(CLAUDE.md TR-1 / golden rules 1 & 4; spec §5). DEV-1169 is the keystone: the operator that makes
that true.

Two hard facts from the fork shape the design:

1. **The vector distance is only real inside an index scan.** MSVBASE's scalar `<->` / `l2_distance`
   returns 0 outside an HNSW index scan (recorded in `test/trimodal_early_term.sql`, ADR-0006). The
   ONLY authoritative per-candidate distance is `node->iss_ScanDesc->xs_orderbyvals[0]`, read while
   draining the index scan. Any design that re-ranks survivors in SQL is therefore wrong.

2. **A validated executor-driving merge already exists.** `topk.cpp` / `multicol_topk.cpp` build a
   child `IndexScan` via SPI (`enable_seqscan=off`, `extractIndexScanNode`), extract the live
   `IndexScanState`, drive it with `ExecProcNode` in a hand-rolled Fagin merge (`execFagins`) with a
   bounded priority queue and early termination on `consecutive_drops >= term_cond`. This is the
   battle-tested path; reinventing it is pure risk.

## Decision

### 1. SRF now, CustomScan later — sharing one pure `execTJS()`

Ship `tjs(...)` as a **C set-returning function** registered in `sql/vectordb.sql` exactly like
`multicol_topk`. A CustomScan node would couple to the **unfinished** SQL/PGQ parser (DEV-1167) and
buys nothing for v1. The merge body is a pure `TupleTableSlot* execTJS(PlanState*)` — the SRF is a
thin driver around it — so a future CustomScan reuses `execTJS` verbatim with no rewrite.

We explicitly reject **SQL nesting** (wrapping the legs in nested subqueries): that yields only
pipeline-level early termination (a `LIMIT` stopping a nested-loop after the index scan emits enough
rows), which is the issue's stated anti-requirement. The single global top-k must live *inside* the
operator, governing the ANN beam directly.

### 2. The vector leg is the SOLE rank authority; graph + relational are predicates on it

There is exactly **one ordered stream** — the HNSW IndexScan, ranked by `xs_orderbyvals[0]`. The
other two legs are **predicates** evaluated per candidate, not additional ordered streams to merge:

- **Relational** — the filter is pushed into the vector leg's SQL `WHERE` (`multicol_topk` already
  builds `select <attr> from <t> where <filter> order by <orderby>`), so the index scan never even
  emits filtered-out rows.
- **Graph** — a reachability predicate: is candidate `dst` reachable `(src)-[:related_to]->(dst)`?
  We probe the native graph store's own iterator, `graph_store.neighbors(src)`, **once** at operator
  Open, cache the reachable-id set, and test O(1) membership per candidate. This honors golden rule
  3 (graph is native traversal, NOT a relational join) and golden rule 2 (one process, one txn — the
  probe is in-process SPI). We resolve the set once rather than interleaving a graph SPI cursor
  *inside* the SPI-driven IndexScan loop: nesting a second SPI cursor under the executor-driven scan
  risks SPI-stack confusion, and the reachable set is finite, so caching it is **exact, not an
  approximation**.

Treating graph + relational as predicates on the single vector stream sidesteps any order-merge and
keeps `consecutive_drops` correct: a candidate that fails the graph or relational predicate is
simply not inserted into the result priority queue — accounted exactly like a candidate that loses
on distance.

### 3. Reuse VBASE's `consecutive_drops` stop — do not invent a new one

Early termination (TR-1) reuses the existing bound verbatim: a bounded priority queue of size `k`,
and a stop once `term_cond` consecutive candidates fail to improve the top-k. We do **not** invent a
new stopping condition. Because the ANN stream is relaxed-monotone (ADR-0006), this bound is the
correct, already-validated way to know the stream can no longer beat the current k-th best; firing
it then `Close()`s the child (`ExecutorFinish/End`), propagating the stop into the HNSW beam.

### 4. Correctness target: approximate top-k, proven by ≥99% parity

Per ADR-0006 / the fork constraint, exact top-k cannot be produced by SQL over-fetch + re-rank. TJS
inherits the relaxed-monotonicity ANN stream's approximate ranking. Correctness is asserted by
**set parity** against the nested-SQL oracle (`test/trimodal_compose.sql` → `{20,10}`) and, on a
larger corpus, by `tjs_candidates_examined() << corpus` (SM-3) — i.e. the result set matches and the
scan provably early-terminates rather than blocking.

## Consequences

- **Single plan (FR-4) lands** without touching the parser: the canonical query
  graph(1)→filter(ts<500)→vector(<->19)→LIMIT runs as ONE `tjs(...)` call returning `{20,10}`, the
  same answer as the nested-SQL oracle, with no app-layer merge.
- **The CustomScan upgrade is cheap** — `execTJS(PlanState*)` is the reusable seam; only a node
  wrapper + planner hook are added later, no merge logic rewrite.
- **The graph predicate is precomputed**, so a *very* high-out-degree `src` materializes a large
  reachable set at Open. For v1's adjacency-list graph store this is bounded by one vertex's
  out-neighbors and is acceptable; a streaming graph predicate is a v-next concern if degree grows
  unbounded.
- **`tjs(...)` is `STABLE`, not `IMMUTABLE`/`PARALLEL SAFE`** (unlike `multicol_topk`): it drives the
  executor via SPI and reads live graph state.
- **BM25 / a fourth leg** remains a future predicate-or-stream decision; the predicates-on-one-stream
  pattern generalizes to any additional *filter* leg without an order-merge.
- **Inherited fork SPI limitation (known issue — verified pre-existing).** `tjs()` forks the
  `topk`/`multicol_topk` SPI-driven-executor lifecycle, and inherits a PRE-EXISTING MSVBASE fork bug:
  issuing another query against the operator's own target table in the **same plpgsql block** as the
  operator call (e.g. `SELECT count(*) FROM entities;` alongside `tjs('entities', …)`) segfaults the
  backend. Top-level (non-plpgsql) calls and back-to-back calls are unaffected — only a sibling scan
  of the same table within one plpgsql block.

  **Attribution proof (Linus review #9, verified 2026-06-25 on `tridb/msvbase:dev`).** Reproduced
  with **UNMODIFIED `multicol_topk` alone** — no `tjs()`, no `graph_store` extension, no graph leg:

  ```sql
  -- vectordb only; see test/_fork_bug_multicol_double_scan.sql (NOT in CI — it crashes the backend)
  DO $$ DECLARE got bigint[]; corpus bigint;
  BEGIN
      SELECT count(*) INTO corpus FROM entities;                 -- sibling scan of the same table
      SELECT array_agg(id) INTO got FROM (
          SELECT t.id FROM multicol_topk('entities',5,0,'id','','',
                         'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint, d float8)) q;
  END $$;
  ```

  Server log: `server process (PID 34) was terminated by signal 11: Segmentation fault`, failed
  process = the DO block above. **Verdict: genuinely the fork's bug, not TJS's** — TJS only inherits
  it by forking the same `execFagins` lifecycle (the mandated v1 architecture). The fix belongs to
  the fork's executor-driving lifecycle (a separate hardening task, GX10-adjacent); the canonical
  e2e test sidesteps it by not co-issuing a second `entities` scan in the early-termination block.

  > **CORRECTION (2026-06-29, supersedes the bullet above) — RESOLVED, root cause re-diagnosed.**
  > Under controlled stock-vs-patched testing (ADR-0010 correction banner), the sibling-scan plpgsql
  > shape **survives with AND without** the snapshot patch — the snapshot issue is *latent UB, not a
  > reliable crash*. The crash actually filed under DEV-1236 was the **HNSW no-ORDER-BY bug**
  > (`HNSWIndexScan::EndScan` on a null `ResultIterator` for a `count(*)` Index-Only Scan under
  > `enable_seqscan=off`), now **fixed by `scripts/patches/tridb_hnsw_scan_no_orderby.patch`**;
  > `tridb_fix_double_scan_snapshot.patch` ships as additional latent-UB + teardown-use-after-free
  > hardening. See `docs/decisions/0010-fork-operator-snapshot-lifecycle.md` and
  > `docs/fork_segfault_double_scan.md` for the corrected evidence. Treat the "segfaults the backend"
  > wording above as historical, not current behavior.
- **HNSW incremental-insert limitation (known fork issue) — STALE, see correction.** This bullet
  originally claimed inserting into an already-indexed table crashes the fork's HNSW AM.
  > **CORRECTION (2026-06-29):** `docs/STATUS.md` (FR-7 churn verification, scenario C2) proves the
  > engine **does** take incremental HNSW-vector inserts inside a transaction with zero cross-store
  > divergence — an update is **not** forced to be a rebuild on the native path. The build-before-
  > `CREATE INDEX` pattern in the tests is a convenience, not a hard limitation. Treat the original
  > claim as stale.
- **`term_cond` = consecutive PAST-FRONTIER candidates (corrected — DEV-1169 scale fix).**
  Originally the bound counted EVERY non-improving candidate, *including graph-rejected ones*, and
  this ADR documented that as "correct". It was a **scale defect**: with a selective predicate the
  qualifying rows rank deeper than `term_cond`, so `consecutive_drops` hit the bound before the
  top-k priority queue ever filled and `tjs()` returned **empty** results (measured SM-4 = 5% at
  100k/dim-768; invisible at the 2k/dim-32 standin where the rows sat in the top-50). Fixed in
  `tridb_tjs_predicate_termination.patch`: a "drop" now means **only past-frontier** — the PQ is
  full (a valid k-th distance exists) AND the candidate's distance ≥ that k-th. Predicate rejections
  do NOT advance the counter, and termination cannot fire before the PQ fills (a selective predicate
  drains the ANN stream instead, which is correct). `term_cond` remains the recall/effort knob; see
  the operating-point curve in `docs/STATUS.md`. (The live `tjs(...)` SQL `COMMENT` still carries the
  original wording — correcting it in the catalog is a follow-up folded into the termination patch.)
- **SQL-fragment injection surface (known v1 limitation, mitigated).** `attr_exp` / `filter_exp` /
  `orderby_exp` are raw SQL fragments interpolated into the vector-leg query (the same design as
  `topk`/`multicol_topk` — they are expressions, so they cannot be parameter-bound or quoted). They
  are therefore a SQL-injection surface IF fed untrusted input. **Mitigated** because v1 feeds them
  exclusively from the controlled DEV-1167 lowering of the *single canonical query*, not from end
  users. The `table_name` argument is NOT part of this surface: it is resolved via the catalog
  (`RangeVarGetRelid`), not string-interpolated (Linus review #1 — this also removed an SPI
  connection leak on the resolver's error paths). A future multi-query surface must validate/bind
  these fragments before exposing TJS to untrusted callers.

## Alternatives rejected

| Alternative | Why rejected |
|---|---|
| CustomScan node now | Couples to the unfinished SQL/PGQ parser (DEV-1167); no v1 benefit. `execTJS` already factored for later reuse. |
| SQL nesting of the three legs | Only pipeline-level early termination — the issue's explicit anti-requirement. No single global top-k inside the operator. |
| Edge join table for the graph leg | Violates golden rule 3 (graph is native traversal). The native `graph_store.neighbors` iterator is the contract. |
| Re-rank survivors with scalar `<->` | Returns 0 outside an index scan (ADR-0006) — would silently produce garbage rankings. |
| Interleave a graph SPI probe inside the IndexScan drive loop | Nests a second SPI cursor under the executor-driven scan (SPI-stack risk); unnecessary since the reachable set is finite and cacheable. |
| Invent a new early-termination bound | VBASE's `consecutive_drops` is already validated against the relaxed-monotone stream; a new stop adds risk for no gain. |

## Addendum (advisor plan 022, 2026-07-01): hardening the relaxed-monotonicity executor path

This ADR's relaxed-order approach rides on the inherited MSVBASE executor hunk
(`patch/Postgres.patch`, files `nodeSort.c` / `nodeIndexscan.c` / `execnodes.h` / `relscan.h`).
Advisor plan 022 hardens that hunk in place (interim; the eventual fix is the CustomScan migration
recorded above, which expresses relaxed order as a proper path and drops the global executor bool).
Shipped as `scripts/patches/tridb_relaxed_order_executor_guard.patch` (last in the fork chain).

**Guard decisions (what the patch changes and why):**

1. **`xs_inorder` is now zero-initialized** in `RelationGetIndexScan` (`genam.c`). The base patch
   added `bool xs_inorder;` to `IndexScanDescData` but never cleared it, so a stock (non-relaxed)
   AM held garbage. A bounded `Sort` (LIMIT n) over an ORDINARY btree index scan could read that
   garbage, arm the `nodeSort.c` early-break (`if (is_index_inorder && tuplesort_heapfull(...))
   break;`), and truncate its input — wrong top-N for queries unrelated to vector search.
2. **The Sort early-stop is now gated on `amcanrelaxedorderbyop`.** `nodeIndexscan.c` sets
   `estate->is_index_inorder = scandesc->xs_inorder && scandesc->indexRelation->rd_indam->
   amcanrelaxedorderbyop;`, so the early-break can arm ONLY for a genuinely-relaxed (HNSW) scan.
3. **The wrong-order safety ERROR is restored for NON-relaxed AMs.** The base patch commented out
   `if (cmp < 0) elog(ERROR, "index returned tuples in wrong order")` for ALL order-by-op scans;
   it is re-enabled, guarded to fire only when the AM does NOT advertise `amcanrelaxedorderbyop`
   (relaxed scans legitimately emit approximately-ordered tuples).

**Emission-window constants — DECISION: document, do not parameterize (plan 022 option (b)).**
`src/hnswindex.cpp` sets the VBASE ORDER BY emission window `scanState->range = 86` and the
range-scan `distanceThreshold = 3` / `queueThreshold = 50` as literals (all upstream
`//TODO(Qianxi): set parameter`). Deriving them from `k`/`ef`/reloptions is a HIGH-risk,
recall-sensitive library change deferred to the CustomScan migration; plan 022 instead adds a code
comment at the constants (and this note) making the semantics explicit, so no future reader mistakes
the 86-window for a tuned bound:

- **Emission order is only APPROXIMATELY monotone.** The `xs_inorder` flip is a heuristic ("the
  86-deep distance window's top is now below the current candidate"), NOT a proof of global order.
  For `k` larger than the window, or in dense regions, the flip can fire early — matching upstream
  issue #22 (VBase iterative-search maintains no result heap / no median-window terminator; it emits
  in raw frontier-pop order).
- **`term_cond` is the real correctness knob, never this window.** Per the TJS termination fix
  (DEV-1169 scale defect, corrected in this ADR above and `tridb_tjs_predicate_termination.patch`),
  the operator's `consecutive_drops`/`term_cond` past-frontier bound decides sufficiency. SM-4 must
  be reported as a **curve over `term_cond`**, not a bare headline — the 86-window is a fixed
  heuristic underneath it, not the tuning surface.

**Verification / residual risk.** The guard is validated by `test/relaxed_order_guard.sql` (wired
into `ENGINE_TESTS`): (a) a bounded Sort over a plain btree index returns the exact top-N; (b) an
HNSW `ORDER BY <-> LIMIT k` still returns the near set. STOP conditions for the executor: if gating
the early-stop measurably raises HNSW latency, or HNSW recall drops at the fixed `term_cond`
operating point, report the numbers — do not revert the correctness gate or tweak the window to
chase them. The `tuplesort_heapfull` private-`Tuplesortstate`-field coupling
(`memtupcount >= bound`) is unchanged but now reached only on genuinely-relaxed scans; it remains
the load-bearing reason a PG re-pin needs full re-validation.
