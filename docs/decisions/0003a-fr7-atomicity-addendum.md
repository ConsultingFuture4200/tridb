# ADR-0003a: FR-7 verified as ATOMICITY across all three stores — addendum to ADR-0003

**Status:** Accepted (2026-06-25)
**Issue:** DEV-1166 ("Verify single shared transaction manager across all three stores (FR-7)")
**Addends:** ADR-0003 (graph store v1-core access method). Does not supersede it.
**Scope:** TEST-ONLY. The DEV-1166 concurrency audit found ZERO required changes to
`src/graph_store/graph_am.c`. This addendum records what was verified, the lock-order analysis,
and three KNOWN-LIMITATIONS (two of them in the vendored vector index) filed as the follow-on.

## TL;DR

FR-7 (success metric SM-5, "100% transaction atomicity") is verified on the **v1 native graph AM**
(`graph_store_am` / `gph_insert_*`), not just on the v0 heap-backed extension. All three stores —
relational heap, HNSW-indexed vector, native adjacency-list graph — commit or abort as ONE unit
because they share ONE transaction manager and ONE WAL (`GenericXLog`) inside ONE Postgres process.
FR-7 is **atomicity, not cross-session snapshot isolation**; the snapshot gap is the explicit
follow-on. New tests: `test/txn_atomicity_test.sql`, `test/crash_recovery_*`,
`test/graph_concurrency_test.sh`.

## Why this addendum exists

The pre-existing "PASS FR-7" in `test/graph_store_test.sql` exercised the **v0** heap-backed
`graph_store_ext` — a plain heap, which is atomic for free. It did NOT exercise the **v1** native
access method (`gph_insert_vertex` / `gph_insert_edge`), where atomicity is a deliberately
engineered property: PostgreSQL has no undo, so an aborted INSERT leaves its bytes on the page and
`gph_xmin_visible()` MUST filter them out. FR-7 was therefore unproven on the keystone the graph
store actually ships. DEV-1166 closes that gap with tests, and finds no code defect.

## What FR-7 IS, and what it is NOT (the load-bearing distinction)

- **FR-7 = ATOMICITY.** A transaction's writes to all three stores become visible together on
  COMMIT and vanish together on ABORT/crash. This is what the new tests assert.
- **FR-7 is NOT isolation.** `gph_xmin_visible()` checks only
  `TransactionIdIsCurrentTransactionId(xmin) || TransactionIdDidCommit(xmin)` — there is **no
  snapshot check**. A graph record committed by another session becomes visible to an already-open
  reader immediately (read-committed-ish at best, not snapshot-stable). ADR-0003 defers per-tuple
  `xmin`/`xmax` + the snapshot machinery; this addendum re-files that as the **DEV-1166 follow-on**.
  The tests therefore assert commit/abort/crash visibility ONLY and never assert snapshot stability.

## Lock-order analysis (no deadlock cycle)

`gph_insert_vertex` / `gph_insert_edge` acquire locks in a single consistent order:

1. `RowExclusiveLock` on the container relation (`relation_open`),
2. the **relation-extension lock** (`LockRelationForExtension`), taken *only* across a `P_NEW`
   extend and released immediately — nested INSIDE,
3. the **metapage / vertex-page / adjacency-page buffer EXCLUSIVE locks**.

The extension lock is always released before the next page is locked (it never wraps a second
extension), and buffers are locked low-block→high-block within a single call. No call path takes a
page buffer lock and *then* waits on the extension lock it does not already hold, so there is no
lock-order cycle and no self-deadlock. `gph_insert_edge` re-reads `vr_adj_tail` UNDER the source
vertex page's EXCLUSIVE buffer lock (the cached value from the lock-free `gph_locate_vertex` scan
can be stale), which is the correct pattern for the single-writer contract.

## Verified properties (all GREEN on the x86 standin, tridb/msvbase:dev)

- **Atomic COMMIT** (`txn_atomicity_test.sql` Test A): one BEGIN…COMMIT writes relational + HNSW +
  graph; after COMMIT all three are visible, including the **HNSW index** returning the new row as
  nearest (index path, `enable_seqscan=off`), and `gph_vertex_count()`/`gph_neighbors`.
- **Atomic ROLLBACK** (Test B, the keystone): same writes in BEGIN…ROLLBACK; self-visible before
  rollback; after rollback ZERO partial state in all three (relational count 0, HNSW nearest ≠
  doomed vector, `gph_vertex_count` back to pre-txn, aborted edge absent from `gph_neighbors`).
  Graph visibility read via the **MVCC-aware** `gph_vertex_count()`/`gph_neighbors()`, never the raw
  metapage counter (`gm_vertex_count` is not abort-aware).
- **vid non-reuse** (Test B-C3): after the rollback a fresh `gph_insert_vertex()` returns a vid
  GREATER than the rolled-back one — `gm_next_vid` is monotonic-with-gaps, visibility stays correct.
- **SM-5 randomized** (Test C1): a single-session 200-iter loop randomly COMMIT/ROLLBACK over the
  relational heap + native graph; the visible state EXACTLY equals the expected-committed set (zero
  divergence). Vector leg under randomized abort is covered by the bounded Test C2 (see below).
- **Crash recovery / WAL redo** (`crash_recovery_test.sh`): CHECKPOINT baseline → committed
  tri-store txn → `pg_ctl stop -m immediate` (SIGQUIT, no shutdown checkpoint) → restart forces
  GenericXLog generic-REDO. The committed relational + vector-heap + native-graph state is present
  after recovery; an UNCOMMITTED tri-store txn that was open at crash is invisible to all three
  after recovery (the crash-aborted xid fails `TransactionIdDidCommit`).
- **Concurrency** (`graph_concurrency_test.sh`): (a) T1's open uncommitted vertex is invisible to a
  separate T2; (b) after T1 COMMIT a NEW T2 statement sees it (no pre-snapshot-stability assertion);
  (c) an aborted vertex is invisible to a third party. Deterministic `pg_advisory_lock` sync, no
  sleep races.

## KNOWN-LIMITATIONS (recorded, not fixed in DEV-1166)

1. **Snapshot isolation gap (TriDB graph store, the planned follow-on).** `gph_xmin_visible` has no
   snapshot check; cross-session reads are not snapshot-stable. Deferred by ADR-0003; this is the
   DEV-1166 follow-on (per-tuple `xmin`/`xmax` + snapshot visibility). Atomicity does not depend on
   it, so FR-7/SM-5 is satisfied without it.
2. **Same-vertex concurrent first-edge lost update (TriDB graph store, v1 single-writer contract).**
   `graph_concurrency_test.sh` probe (d): two sessions adding the FIRST edge to the SAME source
   vertex can race — both observe `vr_adj_tail == Invalid`, both allocate a first adjacency page,
   and one vertex-record update overwrites the other, losing an edge (observed `count=1`). This is
   exactly the CONCURRENCY CONTRACT comment in `graph_am.c` ("concurrent writers appending to the
   SAME vertex can still lose an adjacency update"); the v1 logical-single-writer seed loader never
   hits it. The probe RECORDS the behavior; it does not assert a fix. Out of scope for DEV-1166.
3. **Vendored HNSW index is not abort/crash-durable for INCREMENTAL inserts (vectordb/MSVBASE).**
   Reproduced with the graph store entirely absent, so it is a vendor property, not a TriDB defect:
   - Many cumulative transaction **aborts** of HNSW incremental inserts crash the backend after
     ~25–50 aborted inserts (top-level OR subtransaction). The randomized SM-5 loop therefore drives
     its 200-iter abort stress over the relational heap + native graph (both abort-durable at any
     scale) and exercises the HNSW leg under a **bounded** randomized batch (Test C2, ≤ the safe
     abort budget). Single-txn HNSW commit/rollback atomicity is fully covered by Tests A and B.
   - After an immediate-stop **crash**, the HNSW heap row redoes from WAL but the index's in-memory
     graph is NOT reconstructed (it still answers the pre-crash nearest, even after a later
     CHECKPOINT). The crash-recovery test therefore asserts the vector STORE's durable **heap**
     backing redoes (seqscan path), and labels the index-redo gap a vendor KNOWN-LIMITATION.
   Both vector-index limitations are vendor follow-ons, tracked alongside the DEV-1166 follow-on.

## Consequences

- No change to `src/graph_store/graph_am.c`. The native graph store's atomicity, WAL redo, and
  abort-durability are correct as shipped in ADR-0003.
- The TriDB-owned stores (relational heap, native graph) satisfy FR-7 / SM-5 fully on the standin.
- The vector leg satisfies FR-7 for the single-transaction commit/abort case and for committed-crash
  durability of its heap backing; its index-level abort/crash-redo gaps are vendored and tracked.
- GX10: the tests are hardware-independent and pass on the x86 standin; ARM64 sign-off is GX10-gated
  and unchanged by this addendum.
