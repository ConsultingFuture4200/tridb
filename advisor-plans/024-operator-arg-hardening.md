# Plan 024: Harden the tjs/tjs_open operator entry points and memory lifecycle (crash class + leak class + lowering regression)

> **Executor instructions**: Follow step by step. Run every verification command and confirm the
> expected result before moving on. On any STOP condition, stop and report — do not improvise.
> Update your row in `advisor-plans/README.md` when done (unless a reviewer told you they maintain it).
>
> **Drift check (run first)**: `git diff --stat e345998..HEAD -- scripts/patches scripts/lib/msvbase_patches.sh src/graph_store_ext test/ Makefile`
> If any in-scope file changed since e345998, compare the "Current state" excerpts against live code;
> on a mismatch, STOP.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED (touches the operator C via a new fork patch; vector-first semantics must not change)
- **Depends on**: none
- **Category**: bug / security
- **Planned at**: commit `e345998`, 2026-07-03

## Why this matters

Three SQL-reachable defects in the engine operators: (1) `tjs(k=0)` (and `tjs_open`) call
`std::priority_queue::top()/pop()` on an EMPTY queue — undefined behavior, likely backend SIGSEGV,
reachable by any connected user; a negative k passes `PG_GETARG_UINT32` and becomes ~4.29e9, making
the "bounded" heap unbounded. (2) Every top-k eviction leaks the evicted `heap_copytuple`'d tuple
(~6KB at dim-768) into the SRF's multi-call memory context until end of query — so the
"peak memory O(batch + k)" comment in the filter-first body is currently false — and any
`ereport(ERROR)` after state init leaks the `malloc`'d state + `new`'d C++ containers PERMANENTLY
(longjmp skips destructors and `free`). (3) A lowering regression: the canonical scope guard accepts
`src.id = -1`, and since Stage-4 binding a selective window makes FR-6 pick `filter_first`, which
rejects negative src with an ERROR — a query shape that previously "worked" (silently graph-disabled)
now errors depending on ANALYZE state. Also: the `snprintf` truncation guard exists only in
`tjs_open`; the other two bodies can silently truncate composed SQL (worst case dropping a trailing
`and (<filter>)` — wrong rows).

## Current state

Engine C lives as fork patches in `scripts/patches/*.patch`, applied in order by
`scripts/lib/msvbase_patches.sh` to the vendored MSVBASE tree. **On this box, `vendor/MSVBASE/` has
the full chain already applied** — the validated workflow for adding engine changes (used by
`tridb_tjs_filter_first.patch`, commit `f2c93be`) is: snapshot the target files, edit the vendor
tree, build+test incrementally in the `tridb/msvbase:dev` image, then `diff -u snapshot current`
becomes a NEW patch applied LAST in the chain. Do NOT edit existing patch files (that forces
regenerating every downstream patch).

Relevant code (all in `vendor/MSVBASE/src/` post-chain; the same text appears inside the patches):

- `src/tjs_operator.cpp` — `Datum tjs(...)`: `uint32 k = PG_GETARG_UINT32(argc++);` has NO range
  check. `execTJS` insert branch:
  ```c
  if ( graph_ok && kth > rank_score ){
      if ( pq_full )
          proc_pq->pop();                     // <-- evicted tuple never heap_freetuple'd
      MemoryContextSwitchTo(estate->result_cxt);
      proc_pq->push(std::make_pair(rank_score, copyItemT(slot)));
  ```
  With k=0: `pq_full = (proc_pq->size() == k)` is true on an empty queue and
  `kth = proc_pq->top().first` in the pre-termination-patch shape — in the CURRENT post-chain shape
  the k=0 hazard is: `bool pq_full = ( proc_pq->size() == k );` → true when both are 0 →
  `float kth = pq_full ? proc_pq->top().first : ...` → **top() on empty queue, UB**.
  `beginFilterFirstT` already guards `k > 0` in its insert condition — copy that discipline.
  `TJSState* state = (TJSState*)malloc(sizeof(TJSState));` + 6 `new`'d containers; freed only in
  `EndTJSState` on the normal SRF-final path.
- `src/tjs_open_operator.cpp` — same k hazard in its bounded-push sites (`boundedPushO` /
  `admitCandidateO`); same malloc/new lifecycle; it DOES have the snprintf guard pattern to copy:
  ```c
  int __n = snprintf(sourceText, sizeof(sourceText), ...);
  if (__n < 0 || (size_t) __n >= sizeof(sourceText))
      ereport(ERROR, (errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED), errmsg("tjs_open: generated ... exceeds %zu bytes ...", sizeof(sourceText))));
  ```
- `src/tjs_operator.cpp` vector-first compose (2 branches, `snprintf(sourceText, sizeof(sourceText), "select %s from %s ...")`)
  and `beginFilterFirstT` compose (2 branches) — return value ignored at all 4 sites.
- `src/graph_store_ext/graph_store--0.1.0.sql:109` (repo, NOT vendor) — scope guard captures
  `'WHERE\s+src\.id\s*=\s*(-?\d+)\s+AND\s+...'` — the `-?` admits negative ids the guard should reject
  (real entity ids are non-negative; the graph-disabled `src=-1` parity case remains available via
  DIRECT `tjs()` calls, which is where the tests use it).
- Conventions: every fork patch is registered in `scripts/lib/msvbase_patches.sh` with (a) an apply
  block guarded by a sentinel grep on a LOAD-BEARING token and (b) verify_patches entries — copy the
  `tridb_tjs_filter_first.patch` block (search "DEV-1290") as the exemplar. New engine tests go in
  `test/*.sql` and are wired into `ENGINE_TESTS` in the `Makefile` (see `test/tjs_filter_first_test.sql`
  as the structural pattern — DO-block ASSERTs, `psql -v ON_ERROR_STOP=1`).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Incremental engine compile | `docker run --rm --user root --entrypoint bash -v /home/bob/code/tridb/vendor/MSVBASE/src/tjs_operator.cpp:/tmp/t1.cpp:ro -v /home/bob/code/tridb/vendor/MSVBASE/src/tjs_open_operator.cpp:/tmp/t2.cpp:ro tridb/msvbase:dev -c 'cp /tmp/t1.cpp /tmp/vectordb/src/tjs_operator.cpp; cp /tmp/t2.cpp /tmp/vectordb/src/tjs_open_operator.cpp; cd /tmp/vectordb/build && make vectordb -j8 2>&1 \| tail -5'` | `Built target vectordb`, no errors |
| Full image rebuild | `bash scripts/x86build.sh --docker` | `image built: tridb/msvbase:dev` |
| Engine suite | `make graph-test` | all suites pass (crash_recovery may flake — rerun it alone once: known DEV-1331) |
| One engine test | `bash scripts/graph_test.sh tridb/msvbase:dev test/<file>.sql` | ALL PASS banner |
| Python layer | `make test && make lint` | 205+ passed; lint clean |

## Scope

**In scope:**
- `vendor/MSVBASE/src/tjs_operator.cpp`, `vendor/MSVBASE/src/tjs_open_operator.cpp` (edits → new patch)
- `scripts/patches/tridb_operator_arg_hardening.patch` (create, from the vendor diff)
- `scripts/lib/msvbase_patches.sh` (register + verify)
- `src/graph_store_ext/graph_store--0.1.0.sql` (regex fix only)
- `test/tjs_arg_guards_test.sql` (create), `test/join_order_lowering_test.sql` (extend for regex), `Makefile` (wire test)

**Out of scope:** any edit to EXISTING `scripts/patches/*.patch` files; the vector-first merge
LOGIC (only add the k-guard + eviction free — the insert/drop conditions are validated and frozen);
`multicol_topk.cpp`/`topk.cpp` (upstream twins — tracked separately); ACL/REVOKE changes (plan 026).

## Git workflow

Branch `advisor/024-operator-arg-hardening`; commits `fix(engine): ...` style; do NOT push.

## Steps

### Step 1: Snapshot the two vendor files
`cp vendor/MSVBASE/src/tjs_operator.cpp /tmp/024_tjs.orig && cp vendor/MSVBASE/src/tjs_open_operator.cpp /tmp/024_tjso.orig`
**Verify**: `git -C vendor/MSVBASE apply --check -R ../../scripts/patches/tridb_tjs_filter_first.patch` → clean (proves the tree is at full-chain state).

### Step 2: Argument validation
In `tjs()` first-call, immediately after reading `k` and `termCond`: `if (k < 1 || k > 10000) ereport(ERROR, ERRCODE_INVALID_PARAMETER_VALUE, "tjs: k must be 1..10000 (got %u)")` — note a negative SQL int arrives here as a huge uint32, so this single check covers both. Same for `tjs_open` (`k` 1..10000; `m_seeds` 1..10000; `hops` 1..8; read its arg order from the source). Include a code comment marker `TRIDB: operator arg hardening` (the patch sentinel).

**Verify**: incremental compile → `Built target vectordb`.

### Step 3: Eviction frees
At every bounded-PQ eviction across `execTJS`, `beginFilterFirstT`, and the tjs_open push sites:
```c
if ( pq_full ) { HeapTuple evicted = proc_pq->top().second; proc_pq->pop(); heap_freetuple(evicted); }
```
Do NOT free tuples during the final unspool into `result_stack` (those are the results).
**Verify**: incremental compile clean; then `bash scripts/graph_test.sh tridb/msvbase:dev test/tjs_filter_first_test.sql` (after Step 6's image rebuild it must still be ALL PASS — eviction ordering must not change answers).

### Step 4: Error-path memory release
Add a `MemoryContextCallback` registered on the SRF's `multi_call_memory_ctx` that releases ONLY the
malloc/new memory (containers + the state struct) if not already released: add `bool released;` +
`MemoryContextCallback cb;` to both state structs; factor the container-deletes + `free(state)` out
of `EndTJSState`/its tjs_open twin into `releaseStateMemory(state)` guarded by `released`; the
callback calls it too. The callback must NOT touch executor/SPI/relation teardown (abort cleanup
owns those via resource owners). Register with `MemoryContextRegisterResetCallback` right after the
state is fully initialized.
**Verify**: incremental compile clean.

### Step 5: snprintf guards
Mirror the tjs_open `__n` guard (excerpt above, adjust the errmsg prefix to `tjs:`/`tjs filter_first:`)
at all 4 unchecked compose sites.
**Verify**: incremental compile clean.

### Step 6: Generate + register the patch, rebuild, full suite
`diff -u /tmp/024_tjs.orig vendor/MSVBASE/src/tjs_operator.cpp` (fix headers to `--- a/src/tjs_operator.cpp` / `+++ b/...`) + same for tjs_open → concatenate into `scripts/patches/tridb_operator_arg_hardening.patch`. Register in `scripts/lib/msvbase_patches.sh` AFTER the `tridb_tjs_filter_first.patch` block, sentinel-grep on `TRIDB: operator arg hardening` in BOTH files, plus verify_patches entries for both. Then `bash scripts/x86build.sh --docker`.
**Verify**: build log shows `already applied` for all prior patches and `applying ... operator arg hardening` is ABSENT (tree already edited) BUT `git -C vendor/MSVBASE apply --check -R scripts/patches/tridb_operator_arg_hardening.patch` → clean; then `make graph-test` → green.

### Step 7: Lowering regex + tests
In `src/graph_store_ext/graph_store--0.1.0.sql:109` change `(-?\d+)` → `(\d+)`. Create
`test/tjs_arg_guards_test.sql` (pattern: `test/tjs_filter_first_test.sql`): assert (a) `tjs('entities',0,...)` and k=-1 and k=20000 each RAISE invalid_parameter_value (all three bodies where applicable), backend stays alive; (b) same-class guards for `tjs_open(m_seeds=0, hops=9)`; (c) an oversized filter_exp (generate >110KB via repeat()) RAISEs program_limit_exceeded, not silent truncation; (d) repeated dim-mismatch errors (10×) leave the backend alive and a subsequent good query correct (the leak-fix smoke). Extend `test/join_order_lowering_test.sql` with: a canonical query with `src.id = -1` is REJECTED by the scope guard (off-template RAISE). Wire the new file into `ENGINE_TESTS` in `Makefile`.
**Verify**: `bash scripts/graph_test.sh tridb/msvbase:dev test/tjs_arg_guards_test.sql` → ALL PASS; `bash scripts/join_order_lowering_test.sh tridb/msvbase:dev` → ALL PASS.

## Test plan
Covered by Step 7 (new adversarial suite + lowering extension) plus full `make graph-test` and `make test && make lint`.

## Done criteria
- [ ] `make graph-test` green (crash_recovery flake rerun allowed once)
- [ ] `make test && make lint` green
- [ ] `test/tjs_arg_guards_test.sql` in ENGINE_TESTS and passing
- [ ] `git apply --check -R` of the new patch is clean against the vendor tree
- [ ] `grep -c 'heap_freetuple' vendor/MSVBASE/src/tjs_operator.cpp` ≥ 2 and `vendor/MSVBASE/src/tjs_open_operator.cpp` ≥ 1
- [ ] No existing patch file modified (`git diff --name-only -- scripts/patches` shows ONLY the new file)
- [ ] README status row updated

## STOP conditions
- The vendor tree fails the Step-1 reverse-apply check (chain not at expected state).
- The MemoryContextCallback approach causes ANY crash_recovery/txn_atomicity instability after two attempts — fall back to wrapping only the filter-first drain in PG_TRY/PG_CATCH (free + re-throw), report the narrowed scope.
- Any pre-existing ENGINE/AM test changes its ANSWERS (not just timing) after your edits.

## Maintenance notes
The eviction-free makes the O(batch+k) comment true — reviewers should check no result tuple is freed. Future operators must copy the arg-guard + release-callback pattern; note it in the patch header. The `src=-1` direct-call parity case is intentionally still legal at the OPERATOR level for vector_first.
