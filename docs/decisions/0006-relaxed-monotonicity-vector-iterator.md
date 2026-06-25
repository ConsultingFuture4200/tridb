# ADR-0006: Relaxed-monotonicity vector iterator (caller-driven Open/Next/Close over HNSW)

**Status:** Accepted (2026-06-25)
**Issue:** DEV-1168 (FR-3)
**Related:** DEV-1169 (TJS join operator — the production caller), DEV-1228 / ADR-0004 (vector-index
seam this iterator sits behind), DEV-1167 (SQL/PGQ surface), TR-1 (CLAUDE.md golden rule 1)
**Scope decision:** lift the relaxed-monotonicity stop out of the HNSW AM's `hnsw_gettuple` into a
small TriDB-owned `extern "C"` iterator with a caller-controlled stop bound — NOT a new index, NOT a
rewrite of the HNSW scan, NOT a virtual-dispatch abstraction.

## Context

The TJS join operator (DEV-1169) must drive the HNSW ANN stream as the outer leg of an
early-terminating tri-modal join. The HNSW access method already implements a full
relaxed-monotonicity Open/Next/Close scan with early termination — but in `hnsw_gettuple`
(`vendor/MSVBASE/src/hnswindex.cpp`), welded to Postgres's `IndexScanDesc` plumbing, with the
stop hardcoded (`queueThreshold = 50`, `distanceThreshold = 3`). DEV-1169 needs to drive the same
relaxed-mono stream **without** an `IndexScanDesc`, with a **caller-controlled** stop bound, and
with the per-candidate internal distance surfaced on every `Next()`.

Hard constraint (`docs/fork_findings.md`, restated atop `test/trimodal_early_term.sql`): the fork's
scalar `<->` / `l2_distance()` returns a real value only *inside* an index scan; outside it returns
0. The **only** authoritative per-candidate distance is `hnswlib::QueryResult::GetDistance()`
obtained while draining the scan. Any design that tries to re-rank candidates in SQL or recompute a
scalar distance is therefore wrong by construction.

~60% of the machinery already existed: `hnsw_gettuple` is a complete relaxed-mono AM scan, and
`HNSWIndexScan::BeginScan/GetNet/EndScan` already drive `hnswlib::ResultIterator`, exposing the
internal distance. The gap was a TriDB-owned C iterator the operator can call directly.

## Decision

Add `vendor/MSVBASE/src/tridb_vector_iter.{hpp,cpp}` — an `extern "C"` iterator backed by C++ over
`HNSWIndexScan`:

```c
TridbVectorIter *tridb_vec_open(Relation index, const float *query_vec, int dim, int k);
bool tridb_vec_next(TridbVectorIter *it, TridbVectorCand *out);   // false => exhausted OR stop fired
void tridb_vec_set_kth_bound(TridbVectorIter *it, float kth_best_distance);  // DEV-1169 seam
void tridb_vec_close(TridbVectorIter *it);
```

`tridb_vec_open` derives the index path + distance method from the `Relation` exactly as
`hnsw_begin_scan` does (`DataDir`/`DatabasePath`/`RelationGetRelationName`, `hnsw_ParaGetDistmethod`,
`hnsw_ParaGetDimension`), loads the index via `HNSWIndexScan::LoadIndex`, and begins an
`hnswlib::ResultIterator` scan. `tridb_vec_next` pulls one candidate per call from `GetNet`, decodes
`GetLabel()` into a `heap_tid` with the same `>> 32` / low-32 shift logic as `hnsw_gettuple` and
`HNSWIndexScan::Insert`, and surfaces `GetDistance()` as the candidate distance. It ships behind the
DEV-1228 vector-index seam (ADR-0004): one more consumer of `HNSWIndexScan::BeginScan/GetNet/EndScan`,
no new abstraction.

### D1 — Stopping condition: lift the k-queue stop, parametrize k + inversion tolerance

The stop is lifted from `hnsw_gettuple`'s orderby path but its two magic constants become
parameters. The iterator keeps a max-heap of the best-`k` *surviving* distances; once that heap is
full, its top is the current k-th-best bound. A candidate worse than the active bound is an
*inversion*; the iterator tolerates up to `distanceThreshold` (default 3, overridable) **consecutive**
over-bound candidates before terminating — because relaxed monotonicity is approximately, not
strictly, non-decreasing, so a single inversion must not end the scan. `k` sizes the heap and is a
constructor parameter, not a hardcoded 50.

### D2 — Sufficiency stays upstream via `set_kth_bound`; internal k-queue is the fallback

The real top-k cut belongs to the DEV-1169 operator, which knows the k-th-best *surviving* distance
after graph expansion + relational filtering — a vector candidate that gets filtered out downstream
must not define the stop. So the operator pushes its k-th-best surviving distance via
`tridb_vec_set_kth_bound`; the iterator stops once the relaxed-mono stream provably can't beat it.
When no caller bound is set (e.g. the test probe, or a vector-only query), the internal best-`k`
priority queue provides the fallback bound. The caller bound, when set, takes precedence and tightens
the stop.

### D3 — Internal distance is authoritative; exactness is an empirical ≥99% parity gate

`GetDistance()` is the only real distance, so it is authoritative and surfaced verbatim per `Next()`.
There is no executor recheck and no SQL re-rank (the fork's scalar `<->` returns 0 outside the scan,
so a recheck is impossible *and* unnecessary). "Exactness" is therefore not proven by recompute; it
is an **empirical** property: the stopping scan's top-k must match a full-drain (no-stop) oracle's
top-k by ≥99%. This is enforced by `test/vector_relaxed_mono_test.sql` via the test-only SRF
`tridb_vec_probe(index, query, k, stop)` (which drains the iterator and dumps `(tid, distance,
examined)`; `stop=false` raises the inversion tolerance so the iterator drains the whole stream — the
oracle).

## Consequences

- DEV-1169 gets a clean C entry point with no `IndexScanDesc` dependency and a back-channel
  (`set_kth_bound`) to push its real top-k cut down into the ANN scan.
- TR-1 holds: strict Open/Next/Close, one candidate pulled per `Next`, O(1) stop check, no
  materialization beyond the k-element heap.
- The iterator + its probe compile into the **unconditional** (hnswlib) `vectordb` source list, never
  inside `if(WITH_SPTAG)` — the default lean build (ADR-0004) carries them; zero SPTAG, no ARM/SIMD
  intrinsics added.
- Ships as `scripts/patches/tridb_vector_iter.patch` (vendor/MSVBASE is gitignored + re-cloned),
  wired idempotently into `scripts/lib/msvbase_patches.sh` with a sentinel + `verify_patches`
  assertion, applied after the seam patch it depends on.
- The probe SRF is test-only; production callers use the C API directly.
