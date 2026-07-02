# Plan 019: Add defensive guards to the MSVBASE HNSW access-method entry points (query-vector dimension/array validation, endscan NULL guard, VACUUM BulkDelete safety)

> **Executor instructions**: Follow step by step; run every verification command. Stop and report
> on any "STOP condition". Update `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `cd vendor/MSVBASE && git show HEAD:src/hnswindex.cpp | grep -n "BeginScan((char \*)scanState->workSpace->array.data()" ; git show HEAD:src/hnswindex_scan.cpp | grep -n "ItemPointerData tid = {blkno, offset}\|stats->tuples_removed"`
> If these lines are gone, the pin changed — STOP.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW (adds validation to entry points; engine-gated verify)
- **Depends on**: none
- **Category**: security / bug
- **Planned at**: commit `408e852`, 2026-07-01
- **Upstream**: microsoft/MSVBASE `src/hnswindex.cpp`, `src/hnswindex_scan.cpp`, `src/operator.cpp` (frozen fork)

## Why this matters

The MSVBASE HNSW access method is the vector leg every TriDB operator (`tjs`, `tjs_open`, `topk`)
drives. Its entry points trust caller-supplied data the insert/build paths distrust, so an
unprivileged SQL query can crash the backend or read out of bounds:

- **Query-vector length is never validated against the index dimension.** In `hnsw_gettuple`'s
  ORDER BY branch, `convert_array_to_vector(value)` is copied straight into
  `HNSWIndexScan::BeginScan((char *)array.data(), path)` with no check that `array.size() == dim`
  (`src/hnswindex.cpp` ORDER BY branch, ~line 297-302); the range branch indexes `array[0]` and
  `array.data()+1` with no size check (~287-292). hnswlib then reads exactly `dim` floats — a
  short/empty vector → **out-of-bounds heap read** inside the distance kernel. The insert path
  (`hnswindex_scan.cpp` Insert) and build callbacks DO validate `dim == array.size()`. (UPCORE-01)
- **`convert_array_to_vector` ignores the array's NULL bitmap, element type, and dimensionality**
  (`src/util.cpp:28-35`) — a `float4[]`/`int[]` or NULL-containing literal is reinterpreted as a
  packed `float8[]` → OOB read. Its sibling `convert_array_to_vector_str` already does these checks.
  (UPCORE-07)
- **`hnsw_endscan` dereferences `workSpace` without the NULL guard `hnsw_rescan` has** — a scan
  opened but never fetched from (`LIMIT 0`, empty nested-loop outer) hits
  `scanState->workSpace->resultIterator` on a NULL `workSpace` → **segfault**
  (`src/hnswindex.cpp` `hnsw_endscan`, verified still unguarded in the patched tree; `hnsw_rescan`
  guards `if (scanState->workSpace != nullptr)`). Distinct from DEV-1236/1248 (those fix the
  *gettuple* no-ORDER-BY path, not teardown). (UPCORE-03)
- **`HNSWIndexScan::BulkDelete` crashes / corrupts on VACUUM**: `stats->tuples_removed++`
  (`src/hnswindex_scan.cpp:283`) with no `if (stats == NULL) stats = palloc0(...)` guard —
  `ambulkdelete` passes NULL `stats` on the first call — and the visibility TID is built with
  brace-elision `ItemPointerData tid = {blkno, offset};` (`:279`) leaving `ip_posid = 0` (malformed
  TID), where `hnsw_gettuple` correctly uses `ItemPointerSet`. VACUUM of any HNSW-indexed table can
  NULL-deref crash or delete index entries for the wrong rows. (UP-CORE-03)
- **`range_l2_distance` / `range_inner_product_distance` skip the length check** their non-range
  siblings do (`src/operator.cpp` ~63-73, ~105-115) — a mismatched range argument reads past `rhs`.
  (UP-CORE-10)

These live in pristine (or already-TriDB-patched) upstream files; the fix is one additive TriDB
fork patch adding validation, wired into the patch chain with a sentinel.

## Current state

- Which files are pristine vs already TriDB-patched (from `git diff HEAD` in `vendor/MSVBASE`):
  `src/hnswindex.cpp` and `src/operator.cpp`... note `operator.cpp` IS already TriDB-patched
  (l2_distance_scalar) and `hnswindex_scan.cpp`/`hnswindex.hpp` are TriDB-patched (vector seam,
  scan-no-orderby). Your new patch STACKS on those — generate its hunks against the
  **post-all-existing-patches** state (apply the full chain first, then diff), and order it LAST in
  `apply_tridb_fork_patches`.
- Exemplars for the checks to mirror:
  - dim check on insert: `src/hnswindex_scan.cpp` Insert (`dim == array.size()` → `ereport(ERROR)`).
  - NULL/type checks: `src/util.cpp` `convert_array_to_vector_str` (has `ARR_HASNULL` / elem-type).
  - endscan guard shape: `src/hnswindex.cpp` `hnsw_rescan` (`if (scanState->workSpace != nullptr)`).
  - correct TID + NULL-stats: standard PG `ambulkdelete` contract
    (`if (stats == NULL) stats = (IndexBulkDeleteResult*)palloc0(sizeof(*stats));`,
    `ItemPointerSet(&tid, blkno, offset)`).
- Patch-chain wiring pattern: `scripts/lib/msvbase_patches.sh` `apply_tridb_fork_patches` (sentinel
  guard + `git apply` + `die`), and a `verify_patches` grep. `bash scripts/ci_check_patches.sh` is
  the fast (no-compile) gate.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Patch chain applies + verify | `bash scripts/ci_check_patches.sh` | exit 0 |
| Python layer | `make test && make lint` | exit 0 |
| Engine build+suite (gated) | `scripts/x86build.sh --docker && make graph-test` | PASS |

## Scope

**In scope**:
- `scripts/patches/tridb_hnsw_am_entry_guards.patch` (create)
- `scripts/lib/msvbase_patches.sh` (apply block + sentinel)
- `test/hnsw_am_guards.sql` (create — negative-path asserts) + wire into a suite
- `advisor-plans/README.md` (status row)

**Out of scope**:
- SPTAG/PASE siblings of these bugs (`sptag_endscan`, `index.cpp` OOB) — SPTAG is default-OFF
  (DEV-1228); note them in your report but do not patch (they don't compile in the default build).
- The relaxed-monotonicity termination constants (86/3/50) — plan 022.
- The stale index-map cache — plan 023.

## Git workflow

- Branch: `advisor/019-hnsw-am-entry-guards` from `origin/master`
- Commit: `security(fork): guard HNSW AM entry points (dim/array validation, endscan NULL, bulkdelete) (advisor plan 019)`
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Query-vector validation in hnsw_gettuple

In the patch, in `hnsw_gettuple` after each `convert_array_to_vector(value)`: ORDER BY branch —
`ereport(ERROR, ...)` unless `array.size() == dim`; range branch — require `!array.empty()` and
`array.size() == dim + 1` before touching `array[0]`/`array.data()+1`. Read `dim` from the scan's
index reloption/opaque the same way Insert does.

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0.

### Step 2: Array-content validation in convert_array_to_vector

Add to `src/util.cpp` `convert_array_to_vector`: reject `ARR_HASNULL`, assert
`ARR_ELEMTYPE == FLOAT8OID`, assert `ARR_NDIM <= 1` — matching `convert_array_to_vector_str`. (This
is the single choke point behind Step 1's OOB, so it defends build/insert/scan uniformly.)

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0.

### Step 3: endscan NULL guard

Wrap `hnsw_endscan`'s body in `if (scanState->workSpace != nullptr) { ... }`, mirroring
`hnsw_rescan`. Keep the `pfree(scanState); scan->opaque = NULL;` outside the guard.

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0.

### Step 4: BulkDelete safety

In `HNSWIndexScan::BulkDelete`: at entry, `if (stats == NULL) stats = (IndexBulkDeleteResult*)
palloc0(sizeof(IndexBulkDeleteResult));`; replace `ItemPointerData tid = {blkno, offset};` with
`ItemPointerData tid; ItemPointerSet(&tid, blkno, offset);`.

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0.

### Step 5: range distance length check

In `range_l2_distance` / `range_inner_product_distance` (`src/operator.cpp`), validate
`lhs.size() == rhs.size() - 1` before computing, mirroring `inner_product_distance`.

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0.

### Step 6: Wire + sentinel + test

Add the apply block + `verify_patches` sentinel (grep a distinctive comment marker you put in the
patch, e.g. `TRIDB: HNSW AM entry guards`). Create `test/hnsw_am_guards.sql`: a wrong-dimension
ORDER BY query errors cleanly (not crash); a `LIMIT 0` vector scan returns 0 rows without crashing;
a `VACUUM` on an HNSW-indexed table completes. Wire into `AM_TESTS` or `ENGINE_TESTS`.

**Verify**: `make -n graph-test | grep hnsw_am_guards` → present; engine-gated live run if image
exists, else "engine-gated: unbuilt here".

## Test plan

- `test/hnsw_am_guards.sql`: wrong-dim error, LIMIT-0 no-crash, VACUUM no-crash — the three
  regression classes.
- `bash scripts/ci_check_patches.sh` proves apply + verify against the pinned clone.
- `make test && make lint` unchanged.

## Done criteria

- [ ] `scripts/patches/tridb_hnsw_am_entry_guards.patch` applies clean against a fully base+TriDB-
      patched MSVBASE clone (via `ci_check_patches.sh`)
- [ ] All five guards present (grep the patch: dim check, `ARR_HASNULL`, endscan `!= nullptr`,
      `ItemPointerSet`, range-distance size check)
- [ ] Sentinel added to `verify_patches`; `bash scripts/ci_check_patches.sh` exits 0
- [ ] `test/hnsw_am_guards.sql` wired; engine run PASS or "engine-gated: unbuilt here"
- [ ] `make test && make lint` exit 0; `git status` clean outside scope
- [ ] `advisor-plans/README.md` row updated

## STOP conditions

- `dim` is not reachable in `hnsw_gettuple`'s scope the way Insert reads it — report how Insert gets
  it and where the mismatch is, rather than guessing.
- Any guard changes a PASSING existing engine test (e.g. a test that relied on a lax array type) —
  report which; the test may encode the bug.
- The BulkDelete brace-init turns out to already be `ItemPointerSet` in the patched tree (drift) —
  skip Step 4 and note it.

## Maintenance notes

- All hunks are additive validation; a re-pin re-validates the whole chain (`msvbase_patches.sh:19-24`).
- Reviewer: confirm the dim/array checks don't reject the legitimate corpus vectors used by the
  bench suites (run `make graph-test` fully).
- The SPTAG/PASE siblings (`sptag_endscan`, `index.cpp`/`pase_hnswindex.cpp` OOB, `LoadIndex`
  unchecked-return) remain unpatched by design (SPTAG default-OFF) — revisit only if
  `-DWITH_SPTAG=ON` is ever shipped.
