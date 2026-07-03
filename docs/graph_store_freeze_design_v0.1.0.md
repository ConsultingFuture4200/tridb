# Graph store freeze pass — design note (v0.1.0, advisor plan 026)

**Status:** Design only — no code in this note's commit. The interim containment (ACLs +
operator guidance) shipped with plan 026; this note specifies the real fix so it is designed
before anyone runs TriDB long-lived.
**Relates to:** ADR-0003 (graph store v1-core: GenericXLog page store, `xmin` visibility),
ADR-0009 / DEV-1259 (the GenericXLog page-rewrite precedent on the HNSW side),
`SECURITY.md` "Graph store container (`gstore`) hazards".

## Problem

Graph records store raw transaction ids: `GphVertexRecord.vr_xmin` and `GphEdgeSlot.es_xmin`
(`src/graph_store/gph_page.h`), checked by `gph_xmin_visible()` (`src/graph_store/graph_am.c`)
via `TransactionIdIsCurrentTransactionId` / `TransactionIdDidCommit`. There is no freeze path,
so two clocks run against every stored xid:

1. **clog horizon** — once `VACUUM` elsewhere truncates clog past a stored xid,
   `TransactionIdDidCommit` on it raises `could not access status of transaction` (this hits
   *aborted* old xids too — they are consulted and error the same way, not just skipped).
2. **2^31 wraparound** — past ~2 billion xids, circular xid comparison flips and old-committed
   records can become invisible (or aborted ones visible).

Additionally, the container `gstore` is a plain heap relation to PostgreSQL: the forced
anti-wraparound autovacuum **ignores** `autovacuum_enabled = false` and would eventually walk the
non-heap pages as a heap. The engine-level cure for that is the same as for (1)/(2): keep
`age(relfrozenxid)` low by actually freezing, so the forced vacuum never triggers.

## Design

### 1. `gph_freeze(horizon xid)` maintenance function

A SQL-callable maintenance pass, same family as the other `gph_*` entry points:

- **Walk order:** metapage → vertex-page chain (`gm_first_vertex_blk` →
  `gph_next_pageno` links) → each vertex's adjacency-page chain. Every record-bearing page is
  visited exactly once; the traversal-iterator scan machinery (bounded, page-at-a-time) is
  reused, not a heap scan.
- **Rewrite rule, per record/slot** with `xmin` normal and `precedes(xmin, horizon)`:
  - committed → `FrozenTransactionId`
  - aborted → `InvalidTransactionId`
  This is decided while the xids are still resolvable in clog — the entire point of running
  the pass before the horizon clocks expire.
- **No visibility-code change:** `gph_xmin_visible()` already returns true for
  `FrozenTransactionId` (`TransactionIdDidCommit` short-circuits permanent xids to committed
  without touching clog) and false for `InvalidTransactionId`. Freeze is purely a storage
  rewrite; the read path is untouched.
- **Crash safety (ADR-0003 style):** each page is rewritten under
  `GenericXLogStart` / `GenericXLogRegisterBuffer` / `GenericXLogFinish` in the caller's
  transaction — the same single-WAL, single-txn-manager discipline as every other graph-store
  page mutation (and the ADR-0009 / DEV-1259 precedent for retrofitting GenericXLog rewrites).
  A crash mid-pass replays only the completed page diffs; a half-frozen store is merely a store
  where fewer xids are old, and the pass is **idempotent** — rerun it.
- **Horizon validation:** reject any `horizon` that does not precede the cluster's oldest
  running xmin (`GetOldestXmin`-derived). A too-new horizon could freeze an in-progress xmin
  into false visibility; validation makes that unreachable rather than a caller contract.
- **relfrozenxid:** after a full successful pass the function updates `pg_class.relfrozenxid`
  for `gstore` to the horizon (as vacuum would), which is what actually resets
  `age(relfrozenxid)` and disarms the forced anti-wraparound vacuum.

### 2. Metapage field: `gm_frozen_horizon`

Record the last completed horizon in the metapage. `TransactionId` is 32-bit in PG 13, so the
existing `uint32 gm_reserved` slot is repurposed as `gm_frozen_horizon` — **no page-layout
change, no `GPH_VERSION` bump**; existing stores read as `0` = "never frozen". Updated under
the same GenericXLog record as the metapage visit, only after every page has been processed.
Diagnostic + monotonicity guard (a new pass's horizon must not regress it).

### 3. Trigger policy

- **v1: manual.** `SELECT graph_store.gph_freeze(<horizon>)` run by the operator (superuser or
  a granted maintenance role — EXECUTE is REVOKEd from PUBLIC like the mutators), on the
  `age(relfrozenxid)` monitoring signal from `SECURITY.md`.
- **Later:** either an autovacuum hook or the full table-AM handler (so PostgreSQL's own
  freeze/vacuum machinery routes into TriDB code and the forced anti-wraparound vacuum becomes
  *correct* instead of corrupting). The table-AM handler remains the durable end-state; this
  pass is the piece of it that is independently shippable.

### 4. Interim operational guidance (until this ships)

Verbatim from `SECURITY.md`: never VACUUM/ANALYZE/SELECT the container; monitor
`age(relfrozenxid)` for `gstore`; treat approach to `autovacuum_freeze_max_age` as an
operational stop-the-world event (halt writes, dump/rebuild the graph). Benchmark- and
research-lifetime workloads are unaffected.

### 5. What this design does NOT solve

- **2^31 wraparound without running the pass.** Freeze is a maintenance action; a deployment
  that never runs it still hits the horizon clocks. Only the autovacuum-hook/table-AM stage
  makes protection automatic.
- **Concurrent writers during freeze.** Proposed lock level: `ShareUpdateExclusiveLock` on
  `gstore` (vacuum's level — self-exclusive, so two freezes serialize; readers and `gph_*`
  writers proceed). Per-page exclusive buffer locks + GenericXLog keep each page rewrite
  atomic, and records inserted behind the scan carry post-horizon xids, so correctness holds —
  but the interaction is argued, not yet proven under the concurrency probe; the freeze pass
  must land with a `graph_concurrency_probe`-style stress test before it is trusted.
- **The vectordb operator ACLs.** `tjs` / `tjs_open` EXECUTE grants live in the vendored
  extension SQL and ride the MSVBASE patch chain — deferred to a fork-patch plan, out of scope
  for plan 026 (which covered only the repo-side `graph_store_am` extension).

## Acceptance sketch (for the implementing plan)

Freeze a populated store, assert: (a) pre-freeze answers == post-freeze answers for
`gph_neighbors`/`gph_traverse`/counters; (b) aborted-insert records stay invisible; (c) restart
+ WAL replay preserves frozen pages (crash-recovery harness pattern); (d) `pg_class.relfrozenxid`
advanced; (e) rerun is a no-op. The 2^31-scale clock itself is not testable in CI — (a)–(e) plus
the code-level argument above are the evidence.
