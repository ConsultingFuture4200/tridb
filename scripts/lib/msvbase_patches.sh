#!/usr/bin/env bash
#
# msvbase_patches.sh — shared MSVBASE clone/patch/verify logic for TriDB's build scripts.
#
# SOURCE this from x86build.sh and gx10build.sh. It ONLY defines functions and two defaults
# (PIN_COMMIT, FORCE_INC); it runs no clone/build/patch on source and does not set -e. The
# caller must define log() and die() before invoking any of these functions, and must source
# this AFTER its own argument parsing so a --commit override survives the PIN_COMMIT default.
#
# Single source of truth: both build scripts share this so they cannot drift. The only
# target-specific delta is patch_cmake_aarch64 (GX10/ARM only).

# Absolute dir of THIS lib, resolved at source time. Callers cd into the MSVBASE tree before
# invoking these functions, so a call-time relative ${BASH_SOURCE} would not resolve — capture
# it now (read-only path resolution; no other side effects on source).
_MSVBASE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Validated upstream base (plan 002). Honors a prior assignment (e.g. from --commit).
# RE-PIN DISCIPLINE: this pin is the contract the entire fork-patch chain below is diffed against
# (esp. the stacked tjs_operator -> DEV-1236 snapshot -> DEV-1169 termination patches, which target
# the SAME file in sequence). Bumping PIN_COMMIT requires a FULL clean-room rebuild + `make test-all`
# (NOT just smoke) to re-validate every patch applies and the SM-1..SM-5 suite still passes — a
# newer upstream may have touched the executor-lifecycle code these patches assume. (Linus review.)
PIN_COMMIT="${PIN_COMMIT:-1a548db14d7a3f6f64808c99b9bc1aa01a25b71f}"   # MSVBASE "Fix vector constant parsing (#20)"

# Official checksums for build-time downloads (supply-chain integrity, plan 007). Update these
# whenever a version changes or MSVBASE is re-pinned.
#   Boost 1.81.0 source  : boost.org 1.81.0 release, confirmed against the archives.boost.io tarball
#   CMake 3.14.4 x86_64  : Kitware cmake-3.14.4-SHA-256.txt (x86 build)
#   CMake 3.27.9 aarch64 : Kitware cmake-3.27.9-SHA-256.txt (GX10 build, via patch_cmake_aarch64)
BOOST_1_81_0_SHA256="205666dea9f6a7cfed87c7a6dfbeb52a2c1b9de55712c9c1a87735d7181452b6"
CMAKE_3_14_4_X86_64_SHA256="9f414df8e432c4a143c2d6d81e170581badba8d89df1cf8944735b9122765c50"
CMAKE_3_27_9_AARCH64_SHA256="11bf3d30697df465cdf43664a9473a586f010c528376a966fd310a3a22082461"

# Base-image digest pin (advisor plan 021, UP-BUILD-02). `FROM gcc:12.3.0` is a floating tag;
# pinning it by digest makes two builds months apart pull identical base layers. Resolved
# 2026-07-01 via `docker buildx imagetools inspect gcc:12.3.0 --format '{{.Manifest.Digest}}'`.
# REFRESH INTENTIONALLY (like the checksums above) when the base image is deliberately bumped.
GCC_12_3_0_DIGEST="sha256:de5c5093e2ceb5c09901ed541ecab0660d1f01b7fd79587ad0294fa758a1690e"

# Sentinels proving each MSVBASE patch applied. The vendored scripts/patch.sh has no `set -e`
# and ignores `git apply`'s exit code, so a failed patch yields a clean build of the WRONG
# database (stock Postgres, no relaxed monotonicity). Verify the end-state and die on any miss.
#   Postgres.patch -> amcanrelaxedorderbyop (relaxed monotonicity, amapi.h)
#   hnsw.patch     -> ResultIterator       (VBASE iterator, hnswlib)
#   spann.patch    -> MultiIndexScan       (new SPTAG header AnnService/inc/Core/MultiIndexScan.h)
verify_patches() {
  local root="$1"
  grep -rq 'amcanrelaxedorderbyop' "$root/thirdparty/Postgres/src/include/access/" \
    || die "Postgres.patch NOT applied (no amcanrelaxedorderbyop) — relaxed monotonicity missing; upstream drift?"
  grep -rq 'ResultIterator' "$root/thirdparty/hnsw/hnswlib/" \
    || die "hnsw.patch NOT applied (no ResultIterator) — VBASE iterator missing; upstream drift?"
  # SPTAG is opt-in (WITH_SPTAG, DEV-1228); a lean build may skip the submodule entirely, so only
  # verify the spann patch when the SPTAG tree is actually present.
  if [[ -d "$root/thirdparty/SPTAG" ]]; then
    grep -rq 'MultiIndexScan' "$root/thirdparty/SPTAG/" \
      || die "spann.patch NOT applied (no MultiIndexScan) — upstream drift?"
  fi
  grep -q 'TRIDB: real scalar L2 distance' "$root/src/operator.cpp" \
    || die "TriDB l2_distance_scalar.patch NOT applied — scalar distance still broken; drift?"
  grep -q 'WITH_SPTAG' "$root/CMakeLists.txt" \
    || die "TriDB sptag_optional_build.patch NOT applied — WITH_SPTAG gate missing (DEV-1228); drift?"
  grep -q 'TRIDB_ASSERT_VECTOR_BACKEND' "$root/src/hnswindex_scan.cpp" \
    || die "TriDB tridb_vector_index_seam.patch NOT applied — vector-index seam missing (DEV-1228); drift?"
  grep -q 'tridb_vec_open' "$root/src/tridb_vector_iter.cpp" 2>/dev/null \
    || die "TriDB tridb_vector_iter.patch NOT applied — relaxed-mono vector iterator missing (DEV-1168); drift?"
  grep -q 'src/tridb_vector_iter.cpp' "$root/CMakeLists.txt" \
    || die "TriDB tridb_vector_iter.patch NOT wired into CMakeLists vectordb sources (DEV-1168); drift?"
  grep -q 'TRIDB: TJS operator' "$root/src/tjs_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_tjs_operator.patch NOT applied — Traversal-Join-Similarity operator missing (DEV-1169); drift?"
  grep -q 'src/tjs_operator.cpp' "$root/CMakeLists.txt" \
    || die "TriDB tridb_tjs_operator.patch NOT wired into CMakeLists vectordb sources (DEV-1169); drift?"
  grep -q 'DEV-1236' "$root/src/tjs_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_fix_double_scan_snapshot.patch NOT applied — snapshot lifecycle + UAF fix missing (DEV-1236); drift?"
  grep -q 'DEV-1236' "$root/src/topk.cpp" 2>/dev/null \
    || die "TriDB tridb_fix_double_scan_snapshot.patch NOT applied in topk.cpp — snapshot lifecycle fix missing (DEV-1236); drift?"
  grep -q 'DEV-1236' "$root/src/multicol_topk.cpp" 2>/dev/null \
    || die "TriDB tridb_fix_double_scan_snapshot.patch NOT applied in multicol_topk.cpp — snapshot lifecycle fix missing (DEV-1236); drift?"
  grep -q 'hnsw index scan requires an ORDER BY' "$root/src/hnswindex.cpp" 2>/dev/null \
    || die "TriDB tridb_hnsw_scan_no_orderby.patch NOT applied in hnswindex.cpp — no-ORDER-BY scan guard missing (DEV-1236); drift?"
  grep -q 'null-safe teardown' "$root/src/hnswindex_scan.cpp" 2>/dev/null \
    || die "TriDB tridb_hnsw_scan_no_orderby.patch NOT applied in hnswindex_scan.cpp — null-safe EndScan missing (DEV-1236); drift?"
  grep -q 'TRIDB: HNSW rebuild-on-recovery (DEV-1235)' "$root/src/hnswindex_scan.cpp" 2>/dev/null \
    || die "TriDB tridb_hnsw_rebuild_on_recovery.patch NOT applied — heap-rebuild-on-load missing (DEV-1235); drift?"
  grep -q 'rank_score >= kth' "$root/src/tjs_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_tjs_predicate_termination.patch NOT applied — predicate-blind early termination (DEV-1169 scale defect); drift?"
  # Sentinel anchors on the load-bearing marker string (matching tjs_operator's convention), NOT a
  # comment phrase, so a reformat cannot let verify pass on an unapplied patch.
  grep -q 'TRIDB: TJS-OPEN operator' "$root/src/tjs_open_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_tjs_open_operator.patch NOT applied — seedless multi-seed tjs_open operator missing (ADR-0012 B); drift?"
  grep -q 'src/tjs_open_operator.cpp' "$root/CMakeLists.txt" \
    || die "TriDB tridb_tjs_open_operator.patch NOT wired into CMakeLists vectordb sources (ADR-0012 B); drift?"
  grep -q 'tjs_open(table_name' "$root/sql/vectordb.sql" \
    || die "TriDB tridb_tjs_open_operator.patch NOT applied in sql/vectordb.sql — tjs_open() not registered (ADR-0012 B); drift?"
  grep -q 'L2SqrSIMD16ExtNEON' "$root/thirdparty/hnsw/hnswlib/space_l2.h" 2>/dev/null \
    || die "TriDB tridb_neon_l2_distance.patch NOT applied — ARM NEON L2 kernel missing, scalar fallback sandbags latency (DEV-1234); drift?"
  grep -q 'offsetof(hnsw_ParaOptions, ef_construction)' "$root/src/hnswindex.cpp" 2>/dev/null \
    || die "TriDB tridb_hnsw_reloptions.patch NOT applied — HNSW m/ef_construction reloptions missing (DEV-1286); drift?"
  grep -q 'DEV-1248' "$root/src/hnswindex.cpp" 2>/dev/null \
    || die "TriDB tridb_hnsw_costestimate_no_orderby.patch NOT applied — HNSW no-ORDER-BY cost penalty missing (DEV-1248); drift?"
  # advisor plan 019: HNSW AM entry guards. Sentinel on the distinctive marker at the single
  # choke point (util.cpp) AND the bulkdelete site so a partial/failed apply is caught.
  grep -q 'TRIDB: HNSW AM entry guards' "$root/src/util.cpp" 2>/dev/null \
    || die "TriDB tridb_hnsw_am_entry_guards.patch NOT applied in util.cpp — array-content validation missing (advisor plan 019); drift?"
  grep -q 'TRIDB: HNSW AM entry guards' "$root/src/hnswindex_scan.cpp" 2>/dev/null \
    || die "TriDB tridb_hnsw_am_entry_guards.patch NOT applied in hnswindex_scan.cpp — BulkDelete NULL-stats/TID guard missing (advisor plan 019); drift?"
  # advisor plan 018: unused PostgresMain approximate_sum rewriter removed. Assert the sentinel is
  # present AND the rewriter's active marker `approximate_sum(` is GONE from postgres.c, so a
  # partial/failed apply (sentinel absent, or block still present) is caught.
  grep -q 'TRIDB: MSVBASE approximate_sum PostgresMain rewriter removed' "$root/thirdparty/Postgres/src/backend/tcop/postgres.c" 2>/dev/null \
    || die "TriDB tridb_remove_pgmain_rewriter.patch NOT applied — PostgresMain rewriter sentinel missing (advisor plan 018); drift?"
  ! grep -q 'approximate_sum(' "$root/thirdparty/Postgres/src/backend/tcop/postgres.c" 2>/dev/null \
    || die "TriDB tridb_remove_pgmain_rewriter.patch INCOMPLETE — approximate_sum( rewriter still present in postgres.c (advisor plan 018); drift?"
  # advisor plan 022: relaxed-monotonicity executor guard. Two sentinels on LOAD-BEARING code
  # tokens (not comment phrases): the xs_inorder zero-init in genam.c AND the amcanrelaxedorderbyop
  # gate in nodeIndexscan.c — a partial/failed apply misses one and die()s.
  grep -q 'scan->xs_inorder = false' "$root/thirdparty/Postgres/src/backend/access/index/genam.c" 2>/dev/null \
    || die "TriDB tridb_relaxed_order_executor_guard.patch NOT applied — xs_inorder zero-init missing in genam.c (advisor plan 022); drift?"
  grep -q 'amcanrelaxedorderbyop' "$root/thirdparty/Postgres/src/backend/executor/nodeIndexscan.c" 2>/dev/null \
    || die "TriDB tridb_relaxed_order_executor_guard.patch NOT applied — amcanrelaxedorderbyop gate/guard missing in nodeIndexscan.c (advisor plan 022); drift?"
  # DEV-1290: TJS filter-first body. Sentinels on LOAD-BEARING tokens in BOTH touched files: the
  # C body marker + the drain function in tjs_operator.cpp, and the 8-arg registration in
  # sql/vectordb.sql — a partial apply (C without SQL or vice versa) misses one and die()s.
  grep -q 'TRIDB: TJS filter-first body' "$root/src/tjs_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_tjs_filter_first.patch NOT applied — filter-first tjs() body missing (DEV-1290); drift?"
  grep -q 'beginFilterFirstT' "$root/src/tjs_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_tjs_filter_first.patch INCOMPLETE — beginFilterFirstT drain missing in tjs_operator.cpp (DEV-1290); drift?"
  grep -q 'orderby_exp text, join_order text' "$root/sql/vectordb.sql" 2>/dev/null \
    || die "TriDB tridb_tjs_filter_first.patch NOT applied in sql/vectordb.sql — 8-arg tjs()/tjs_last_join_order() not registered (DEV-1290); drift?"
  # advisor plan 024: operator arg + memory-lifecycle hardening. Sentinels on LOAD-BEARING
  # tokens in BOTH operator files: the sentinel-marked arg guards, plus the release-callback
  # registration each file must carry — a partial apply misses one and die()s.
  grep -q 'TRIDB: operator arg hardening' "$root/src/tjs_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_operator_arg_hardening.patch NOT applied — tjs() arg guards missing (advisor plan 024); drift?"
  grep -q 'registerTJSStateRelease' "$root/src/tjs_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_operator_arg_hardening.patch INCOMPLETE — release callback missing in tjs_operator.cpp (advisor plan 024); drift?"
  grep -q 'TRIDB: operator arg hardening' "$root/src/tjs_open_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_operator_arg_hardening.patch NOT applied — tjs_open() arg guards missing (advisor plan 024); drift?"
  grep -q 'registerTJSOpenStateRelease' "$root/src/tjs_open_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_operator_arg_hardening.patch INCOMPLETE — release callback missing in tjs_open_operator.cpp (advisor plan 024); drift?"
  # advisor plan 025 / ADR-0013 Stage A: operators probe the v1 native AM. Sentinels on the
  # LOAD-BEARING SPI text (gph_neighbors_ext) in BOTH operator files, and the v0 probe text
  # must be GONE — a partial apply (one file swapped, one still on v0) misses one and die()s.
  grep -q 'graph_store.gph_neighbors_ext' "$root/src/tjs_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_graph_v1_rewire.patch NOT applied — tjs() still probes the v0 graph ext (ADR-0013 Stage A); drift?"
  grep -q 'graph_store.gph_neighbors_ext' "$root/src/tjs_open_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_graph_v1_rewire.patch NOT applied — tjs_open() still probes the v0 graph ext (ADR-0013 Stage A); drift?"
  ! grep -q 'graph_store\.neighbors' "$root/src/tjs_operator.cpp" "$root/src/tjs_open_operator.cpp" 2>/dev/null \
    || die "TriDB tridb_graph_v1_rewire.patch INCOMPLETE — a graph_store.neighbors probe remains in an operator (ADR-0013 Stage A); drift?"
  log "all MSVBASE + TriDB fork patches verified present"
}

# Apply MSVBASE's submodule patches (spann/hnsw/Postgres — relaxed monotonicity), idempotent
# via a sentinel guard, then verify on BOTH the fresh-apply and already-applied paths.
apply_msvbase_patches() {
  local root="$1"
  if grep -rq 'amcanrelaxedorderbyop' "$root/thirdparty/Postgres/src/include/access/" 2>/dev/null; then
    log "MSVBASE submodule patches already applied"
  else
    log "applying MSVBASE submodule patches (scripts/patch.sh: spann, hnsw, Postgres) — relaxed monotonicity"
    ( cd "$root" && bash scripts/patch.sh )
  fi
  apply_tridb_fork_patches "$root"
  verify_patches "$root"
}

# TriDB's own fork patches, applied on top of MSVBASE's (scripts/patch.sh). These live under
# scripts/patches/ in THIS repo because vendor/MSVBASE/ is gitignored + re-cloned, so a patch
# placed there would be wiped. Idempotent via each patch's sentinel.
#   l2_distance_scalar.patch (plan 005): scalar l2_distance returned 0 for any dim < 16 (static
#     L2Space built with dim=0 -> hnswlib L2SqrSIMD16Ext sums only full 16-float blocks). Fixed
#     to compute the Euclidean distance directly; unblocks SQL exact re-rank / DEV-1168 tests.
#   sptag_optional_build.patch (DEV-1228, ADR-0004): WITH_SPTAG CMake option (default OFF) gating
#     the SPTAG build/link, the sptag/spann sources, the lib.cpp registration, and the SQL DDL.
#     Default build is hnswlib-only (no SPTAG) — unblocks the GX10 ARM port. Opt in: -DWITH_SPTAG=ON.
apply_tridb_fork_patches() {
  local root="$1"
  local patch="${_MSVBASE_LIB_DIR}/../patches/l2_distance_scalar.patch"
  [[ -f "$patch" ]] || die "missing TriDB fork patch: $patch"
  if grep -q 'TRIDB: real scalar L2 distance' "$root/src/operator.cpp" 2>/dev/null; then
    log "TriDB fork patch (scalar l2_distance) already applied"
  else
    log "applying TriDB fork patch: real scalar l2_distance (plan 005)"
    ( cd "$root" && git apply "$patch" ) \
      || die "l2_distance_scalar.patch did not apply — MSVBASE drift? re-generate from src/operator.cpp"
  fi

  local sptag_patch="${_MSVBASE_LIB_DIR}/../patches/sptag_optional_build.patch"
  [[ -f "$sptag_patch" ]] || die "missing TriDB fork patch: $sptag_patch"
  if grep -q 'WITH_SPTAG' "$root/CMakeLists.txt" 2>/dev/null; then
    log "TriDB fork patch (WITH_SPTAG decouple, DEV-1228) already applied"
  else
    log "applying TriDB fork patch: WITH_SPTAG vector-index decouple (DEV-1228 / ADR-0004)"
    ( cd "$root" && git apply "$sptag_patch" ) \
      || die "sptag_optional_build.patch did not apply — MSVBASE drift? re-generate per ADR-0004"
  fi

  local seam_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_vector_index_seam.patch"
  [[ -f "$seam_patch" ]] || die "missing TriDB fork patch: $seam_patch"
  if grep -q 'TRIDB_ASSERT_VECTOR_BACKEND' "$root/src/hnswindex_scan.cpp" 2>/dev/null; then
    log "TriDB fork patch (vector-index seam, DEV-1228) already applied"
  else
    log "applying TriDB fork patch: TriDB-owned vector-index seam (DEV-1228 / ADR-0004)"
    ( cd "$root" && git apply "$seam_patch" ) \
      || die "tridb_vector_index_seam.patch did not apply — MSVBASE drift? re-generate per ADR-0004"
  fi

  #   tridb_vector_iter.patch (DEV-1168, FR-3): the relaxed-monotonicity vector iterator the TJS
  #     operator (DEV-1169) drives WITHOUT an IndexScanDesc. Adds src/tridb_vector_iter.{hpp,cpp}
  #     (extern "C" Open/Next/Close lifting hnsw_gettuple's stop into a caller-controlled bound,
  #     surfacing hnswlib's internal GetDistance() per Next) + src/tridb_vector_probe.cpp (a
  #     test-only SQL SRF), wires both into the UNCONDITIONAL vectordb source list, and declares
  #     tridb_vec_probe() in sql/vectordb.sql. Must apply AFTER the seam patch (it relies on
  #     HNSWIndexScan + the seam contract). hnswlib-only — no SPTAG.
  local iter_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_vector_iter.patch"
  [[ -f "$iter_patch" ]] || die "missing TriDB fork patch: $iter_patch"
  if grep -q 'tridb_vec_open' "$root/src/tridb_vector_iter.cpp" 2>/dev/null; then
    log "TriDB fork patch (relaxed-mono vector iterator, DEV-1168) already applied"
  else
    log "applying TriDB fork patch: relaxed-monotonicity vector iterator (DEV-1168 / FR-3)"
    ( cd "$root" && git apply "$iter_patch" ) \
      || die "tridb_vector_iter.patch did not apply — MSVBASE drift? re-generate per DEV-1168"
  fi

  #   tridb_tjs_operator.patch (DEV-1169, FR-4): the Traversal-Join-Similarity operator — the
  #     tri-modal keystone. A C SRF tjs(...) (registered like multicol_topk) whose body is a
  #     generalized execFagins (execTJS): it drives the HNSW IndexScan via SPI as the SOLE rank
  #     authority (xs_orderbyvals[0]), pushes the relational filter into the leg's WHERE, and tests
  #     a graph-reachability predicate (graph_store.neighbors(src), probed once + cached) per
  #     candidate — all under ONE early-terminating top-k (VBASE consecutive_drops). Adds
  #     src/tjs_operator.cpp, wires it into the UNCONDITIONAL vectordb sources, and registers
  #     tjs()/tjs_candidates_examined() in sql/vectordb.sql. Must apply AFTER tridb_vector_iter.patch
  #     (it appends to the same CMakeLists source list and SQL tail). hnswlib-only — no SPTAG.
  local tjs_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_tjs_operator.patch"
  [[ -f "$tjs_patch" ]] || die "missing TriDB fork patch: $tjs_patch"
  if grep -q 'TRIDB: TJS operator' "$root/src/tjs_operator.cpp" 2>/dev/null; then
    log "TriDB fork patch (Traversal-Join-Similarity operator, DEV-1169) already applied"
  else
    log "applying TriDB fork patch: Traversal-Join-Similarity operator (DEV-1169 / FR-4)"
    ( cd "$root" && git apply "$tjs_patch" ) \
      || die "tridb_tjs_operator.patch did not apply — MSVBASE drift? re-generate per DEV-1169"
  fi

  #   tridb_fix_double_scan_snapshot.patch (DEV-1236, ADR-0010): snapshot lifecycle fix for
  #     topk(), multicol_topk(), and tjs(). A second executor-driven scan in the same plpgsql block
  #     as any of these SRFs can SIGSEGV the backend: each operator built its child IndexScan with
  #     GetActiveSnapshot() (borrowed, not pinned), and a sibling statement pushes/pops the active
  #     snapshot, leaving the child's xs_snapshot dangling. Fix: RegisterSnapshot at first-call,
  #     PushActiveSnapshot/PopActiveSnapshot around each child drive (PG_TRY/PG_CATCH for error
  #     path), UnregisterSnapshot in teardown. Also fixes UAF in topk/multicol_topk EndFaginsState
  #     (free(state) then free(state->qDescs) reads freed memory; new-allocated vectors were
  #     free()'d instead of delete'd). Builds clean. NOTE: this is latent-UB HARDENING (correct per
  #     Postgres snapshot-ownership rules + a real teardown UAF) — but no reproducible crash was
  #     demonstrated for it under controlled stock-vs-patched testing. The REPRODUCIBLE DEV-1236
  #     crash is the HNSW no-ORDER-BY bug fixed by tridb_hnsw_scan_no_orderby.patch (below).
  local snapshot_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_fix_double_scan_snapshot.patch"
  [[ -f "$snapshot_patch" ]] || die "missing TriDB fork patch: $snapshot_patch"
  if grep -q 'DEV-1236' "$root/src/tjs_operator.cpp" 2>/dev/null; then
    log "TriDB fork patch (snapshot lifecycle + UAF fix, DEV-1236) already applied"
  else
    log "applying TriDB fork patch: snapshot lifecycle + teardown UAF fix (DEV-1236 / ADR-0010)"
    ( cd "$root" && git apply "$snapshot_patch" ) \
      || die "tridb_fix_double_scan_snapshot.patch did not apply — MSVBASE drift? re-generate per DEV-1236"
  fi

  #   tridb_hnsw_scan_no_orderby.patch (DEV-1236, the REPRODUCIBLE crash): with enable_seqscan off
  #     the planner picks an Index-Only Scan on the HNSW index for an unordered/aggregate scan
  #     (e.g. count(*)). hnsw_gettuple's no-ORDER-BY/no-key branch returned false WITHOUT creating a
  #     ResultIterator, so hnsw_endscan -> HNSWIndexScan::EndScan -> Close() on a null shared_ptr
  #     SIGSEGV'd the backend (and count(*) silently returned 0). Fix: null-safe EndScan +
  #     ereport(ERROR) on the unordered-scan branch. BUILT AND VERIFIED on the x86 standin (backtrace
  #     + deterministic repro flips crash -> clean error). This is the reproducible DEV-1236 crash;
  #     tridb_fix_double_scan_snapshot.patch above is separate latent-UB hardening.
  local hnsw_scan_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_hnsw_scan_no_orderby.patch"
  [[ -f "$hnsw_scan_patch" ]] || die "missing TriDB fork patch: $hnsw_scan_patch"
  if grep -q 'hnsw index scan requires an ORDER BY' "$root/src/hnswindex.cpp" 2>/dev/null; then
    log "TriDB fork patch (HNSW no-ORDER-BY scan guard, DEV-1236) already applied"
  else
    log "applying TriDB fork patch: HNSW no-ORDER-BY scan guard (DEV-1236)"
    ( cd "$root" && git apply "$hnsw_scan_patch" ) \
      || die "tridb_hnsw_scan_no_orderby.patch did not apply — MSVBASE drift? re-generate per DEV-1236"
  fi

  #   tridb_hnsw_rebuild_on_recovery.patch (DEV-1235, ADR-0009): fixes Defect A — the flat-file
  #     LoadIndex path stale after crash or in any fresh backend that didn't run ambuild. LoadIndex
  #     now rebuilds the in-RAM HierarchicalNSW by scanning the WAL-durable HEAP (SnapshotAny +
  #     HeapTupleSatisfiesVacuum) on cache-miss. The heap is the source of truth. aminsert double-add
  #     is handled by hnswlib::addPoint's built-in label idempotency. Must apply AFTER
  #     tridb_hnsw_scan_no_orderby.patch. BUILT AND VERIFIED on x86 standin: git apply --check exit 0;
  #     oracles A (crash recovery), B (cross-session), C (abort exclusion), D (recall/no-dup) all PASS.
  local rebuild_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_hnsw_rebuild_on_recovery.patch"
  [[ -f "$rebuild_patch" ]] || die "missing TriDB fork patch: $rebuild_patch"
  if grep -q 'TRIDB: HNSW rebuild-on-recovery (DEV-1235)' "$root/src/hnswindex_scan.cpp" 2>/dev/null; then
    log "TriDB fork patch (HNSW rebuild-on-recovery, DEV-1235) already applied"
  else
    log "applying TriDB fork patch: HNSW rebuild-on-recovery — heap as source of truth (DEV-1235 / ADR-0009)"
    ( cd "$root" && git apply "$rebuild_patch" ) \
      || die "tridb_hnsw_rebuild_on_recovery.patch did not apply — MSVBASE drift? re-generate per DEV-1235"
  fi

  #   tridb_tjs_predicate_termination.patch (DEV-1169 scale defect, found on the first live GX10 run):
  #     the TJS early-termination counted EVERY non-inserted candidate — INCLUDING graph/relational
  #     predicate rejections — as a VBASE consecutive_drop, so a selective predicate tripped term_cond
  #     BEFORE the top-k priority queue filled and tjs() returned an EMPTY/partial result. Confirmed at
  #     100k/dim-768: examined==term_cond, SM-4 = 5% (0/12 exact). Invisible at toy scale (2k/dim-32:
  #     qualifying rows sat in the top-50, SM-4 = 100%). Fix: a "drop" now means ONLY past-frontier
  #     (PQ full AND distance >= k-th); predicate rejections and sub-threshold candidates do not advance
  #     the counter, and termination cannot fire before the PQ fills (a selective predicate drains the
  #     ANN stream to exhaustion, which is correct). Restores SM-4 to 100% at term_cond=10000 / SM-3
  #     20.1% (still < 25%, TR-1 preserved). Diffed against the post-DEV-1236 tjs_operator.cpp, so it
  #     MUST apply AFTER tridb_fix_double_scan_snapshot.patch (it does — this is last in the chain).
  # Sentinel anchors on a LOAD-BEARING CODE token (the past-frontier drop test), NOT a comment
  # phrase: a comment reformat must not silently let verify pass on an unapplied patch (Linus review).
  local tjs_term_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_tjs_predicate_termination.patch"
  [[ -f "$tjs_term_patch" ]] || die "missing TriDB fork patch: $tjs_term_patch"
  if grep -q 'rank_score >= kth' "$root/src/tjs_operator.cpp" 2>/dev/null; then
    log "TriDB fork patch (predicate-correct TJS termination, DEV-1169 scale fix) already applied"
  else
    log "applying TriDB fork patch: predicate-correct TJS early termination (DEV-1169 scale fix)"
    ( cd "$root" && git apply "$tjs_term_patch" ) \
      || die "tridb_tjs_predicate_termination.patch did not apply — MSVBASE/DEV-1236 drift? re-generate per DEV-1169"
  fi

  # tridb_neon_l2_distance.patch (DEV-1234): native AArch64 NEON L2-squared kernel in hnswlib's
  #   space_l2.h. On aarch64 the build strips x86 ISA flags (patch_cmake_arm_isa_flags below), so
  #   USE_SSE/AVX are undefined and L2Space falls back to the scalar L2Sqr for EVERY distance — the
  #   hottest loop in ANN search and the TJS re-rank — sandbagging all latency numbers. Adds a NEON
  #   path gated on __ARM_NEON (no build-flag change needed; inert on x86). Validated equal to scalar
  #   within 1e-4 rel err and 3.6x-7.8x faster (dim 32..768) on the GX10 via tools/neon_l2_bench.c.
  #   Applies INSIDE the hnsw submodule (paths a/hnswlib/...), like upstream scripts/patch.sh, and
  #   AFTER hnsw.patch (which only makes L2SqrSIMD16Ext static — disjoint from these hunks).
  local neon_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_neon_l2_distance.patch"
  [[ -f "$neon_patch" ]] || die "missing TriDB fork patch: $neon_patch"
  if grep -q 'L2SqrSIMD16ExtNEON' "$root/thirdparty/hnsw/hnswlib/space_l2.h" 2>/dev/null; then
    log "TriDB fork patch (NEON L2 kernel, DEV-1234) already applied"
  else
    log "applying TriDB fork patch: AArch64 NEON L2 distance kernel (DEV-1234)"
    ( cd "$root/thirdparty/hnsw" && git apply "$neon_patch" ) \
      || die "tridb_neon_l2_distance.patch did not apply — hnswlib drift? re-generate from thirdparty/hnsw/hnswlib/space_l2.h"
  fi

  # tridb_hnsw_reloptions.patch (DEV-1286): expose per-index HNSW build quality as reloptions
  #   WITH (m=..., ef_construction=...) on the vectordb HNSW AM (the relopt table previously exposed
  #   only dimension/distmethod). Default 0 -> hnswlib defaults (M=16 / ef_construction=200), so
  #   existing indexes are unchanged; opt-in per index. Threads the values into BOTH construction
  #   sites: the FRESH-build constructor (hnswindex_builder.cpp ConstructInternalBuilder) AND the
  #   DEV-1235 rebuild-on-recovery path (hnswindex_scan.cpp LoadIndex). A tuned index
  #   WITH (m=32, ef_construction=400) now recovers at the SAME quality it was built with after an
  #   unclean crash, instead of silently falling back to hnswlib defaults until reindexed. Both sites
  #   read the reloptions off the same index catalog relation and share the 0 -> 16/200 fallback.
  #   Unblocked by NEON (DEV-1234): higher build quality is only affordable to build once the
  #   distance kernel is SIMD.
  local relopt_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_hnsw_reloptions.patch"
  [[ -f "$relopt_patch" ]] || die "missing TriDB fork patch: $relopt_patch"
  if grep -q 'offsetof(hnsw_ParaOptions, ef_construction)' "$root/src/hnswindex.cpp" 2>/dev/null; then
    log "TriDB fork patch (HNSW m/ef_construction reloptions, DEV-1286) already applied"
  else
    log "applying TriDB fork patch: HNSW m/ef_construction reloptions (DEV-1286)"
    ( cd "$root" && git apply "$relopt_patch" ) \
      || die "tridb_hnsw_reloptions.patch did not apply — MSVBASE drift? re-generate from src/{hnswindex.hpp,hnswindex.cpp,lib.cpp,hnswindex_builder.cpp,hnswindex_scan.cpp}"
  fi

  #   tridb_hnsw_costestimate_no_orderby.patch (DEV-1248): planner-side companion to the DEV-1236
  #     runtime guard. hnsw_costestimate charges disable_cost when the index path has no
  #     order-by-distance pathkey (path->indexorderbys == NIL), so the planner NEVER picks the HNSW
  #     index for an unordered/aggregate scan (which DEV-1236 makes ereport(ERROR) at scan time).
  #     Ordered ANN scans (indexorderbys != NIL) keep their normal, attractive cost. Two hunks in
  #     hnswindex.cpp; line numbers assume tridb_hnsw_scan_no_orderby + tridb_hnsw_reloptions are
  #     already applied, so it MUST come after both. BUILT AND VERIFIED on x86 standin (git apply
  #     --check exit 0; make graph-test green incl. test/hnsw_costestimate_unordered_test.sql).
  local costest_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_hnsw_costestimate_no_orderby.patch"
  [[ -f "$costest_patch" ]] || die "missing TriDB fork patch: $costest_patch"
  if grep -q 'DEV-1248' "$root/src/hnswindex.cpp" 2>/dev/null; then
    log "TriDB fork patch (HNSW costestimate no-ORDER-BY penalty, DEV-1248) already applied"
  else
    log "applying TriDB fork patch: HNSW costestimate no-ORDER-BY penalty (DEV-1248)"
    ( cd "$root" && git apply "$costest_patch" ) \
      || die "tridb_hnsw_costestimate_no_orderby.patch did not apply — MSVBASE drift? re-generate per DEV-1248"
  fi

  #   tridb_tjs_open_operator.patch (ADR-0012 realization B): the `tjs_open` seedless multi-seed
  #     open-domain retrieval operator — the engine form of the validated host prototype
  #     (bench/tjs_open_ref.py, recall@10 ~0.987 on HotpotQA). Additive: tjs() (single-source) is
  #     UNCHANGED. tjs_open derives m_seeds ANN seeds from the vector leg (no caller src), BFS-expands
  #     the graph hops-deep from ALL seeds via graph_store.neighbors (the bridges), and emits the
  #     top-k vector-ranked with the graph-reachable bridges INJECTED past the vector frontier — a
  #     bridge does NOT reset the consecutive_drops counter, so the VBASE early-termination bound
  #     still holds (TR-1, golden rule 1; only the bounded bridge set is materialized, never the
  #     corpus). Adds src/tjs_open_operator.cpp, wires it into the UNCONDITIONAL vectordb sources
  #     (after src/tjs_operator.cpp), and registers tjs_open()/tjs_open_candidates_examined()/
  #     tjs_open_bridges_injected() in sql/vectordb.sql. Appends to the SAME CMakeLists source list and
  #     SQL tail as the tjs chain, so it MUST apply AFTER tridb_tjs_operator.patch (the anchor lines).
  #     hnswlib-only — no SPTAG. The bridge-distance read relies on l2_distance_scalar.patch (scalar
  #     `<->` correct OUTSIDE an index scan). NOTE: this realizes ADR-0012's ORIGINAL (B) recipe
  #     (vector rank + reachability-set bridge injection + consecutive_drops); the 2026-06-29 addendum
  #     refinement (bounded-push PPR graph score + NRA/FR termination + RRF fusion) modeled in
  #     bench/tjs_open_ref.py is a follow-up, NOT in this patch.
  local tjs_open_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_tjs_open_operator.patch"
  [[ -f "$tjs_open_patch" ]] || die "missing TriDB fork patch: $tjs_open_patch"
  if grep -q 'TRIDB: TJS-OPEN operator' "$root/src/tjs_open_operator.cpp" 2>/dev/null; then
    log "TriDB fork patch (tjs_open multi-seed operator, ADR-0012 B) already applied"
  else
    log "applying TriDB fork patch: tjs_open seedless multi-seed retrieval operator (ADR-0012 B)"
    ( cd "$root" && git apply "$tjs_open_patch" ) \
      || die "tridb_tjs_open_operator.patch did not apply — MSVBASE/tjs-chain drift? re-generate per ADR-0012"
  fi

  #   tridb_hnsw_am_entry_guards.patch (advisor plan 019): defensive validation on the HNSW AM
  #     entry points an unprivileged SQL query reaches. The insert/build paths validate
  #     caller-supplied arrays; the scan/vacuum/range paths did not, so a short/empty/mistyped
  #     vector literal could OOB-read the distance kernel or NULL-deref VACUUM. Four additive
  #     guards: (1) hnsw_gettuple checks the query-vector length against the index dimension in
  #     BOTH the ORDER BY (== dim) and range (== dim+1, non-empty) branches; (2) the single
  #     choke point convert_array_to_vector rejects multi-dim / NULL-containing / non-float8[]
  #     arrays (mirrors convert_array_to_vector_str), defending build/insert/scan uniformly;
  #     (3) HNSWIndexScan::BulkDelete palloc0s a NULL stats (ambulkdelete's first call) and builds
  #     the visibility TID with ItemPointerSet instead of malformed brace-elision; (4)
  #     range_l2_distance / range_inner_product_distance length-check lhs == rhs-1. Touches four
  #     already-TriDB-patched files, so it MUST apply LAST (diffed against the full chain above).
  #     The endscan NULL guard the plan also called for is ALREADY present (added by
  #     tridb_hnsw_scan_no_orderby.patch, DEV-1236), so it is not duplicated here.
  local am_guards_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_hnsw_am_entry_guards.patch"
  [[ -f "$am_guards_patch" ]] || die "missing TriDB fork patch: $am_guards_patch"
  if grep -q 'TRIDB: HNSW AM entry guards' "$root/src/util.cpp" 2>/dev/null; then
    log "TriDB fork patch (HNSW AM entry guards, advisor plan 019) already applied"
  else
    log "applying TriDB fork patch: HNSW AM entry guards (dim/array validation, bulkdelete, range dist) (advisor plan 019)"
    ( cd "$root" && git apply "$am_guards_patch" ) \
      || die "tridb_hnsw_am_entry_guards.patch did not apply — MSVBASE/fork-chain drift? re-generate per advisor plan 019"
  fi

  #   tridb_remove_pgmain_rewriter.patch (advisor plan 018): MSVBASE's Postgres.patch injects a
  #     hand-written string rewriter into PostgresMain's simple-query handler that intercepts any
  #     statement containing approximate_sum(...) and rewrites it to a topk(...) call. TriDB NEVER
  #     uses this path (its canonical query lowers directly to tjs()/tjs_open(); grep -rn
  #     approximate_sum src/ test/ tools/ bench/ is empty), yet inherits the whole liability: a
  #     char* order[100] stack overflow, an unbounded strcat past palloc(strlen*2), '-unescaped SQL
  #     injection, a pfree(NULL) crash on a WHERE-less query, plus an always-on per-query heap leak
  #     (lowercase()+palloc freed only inside the taken branch) and an ereport(LOG) that logs the
  #     full text of EVERY query. Removes the entire rewrite block from PostgresMain (restoring the
  #     plain query_string/exec_simple_query flow, leaving a one-line sentinel) and its dependent
  #     pfree(result). Edits thirdparty/Postgres/src/backend/tcop/postgres.c, which ONLY the base
  #     Postgres.patch touches, so ordering vs the other fork patches is immaterial; it MUST apply
  #     AFTER the base patch (scripts/patch.sh) that introduced the block. The executor hunks of
  #     Postgres.patch (nodeSort/nodeIndexscan relaxed monotonicity) are LEFT intact — plan 022.
  #     Applies inside the Postgres submodule (paths a/src/backend/tcop/...), like upstream patch.sh.
  local pgmain_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_remove_pgmain_rewriter.patch"
  [[ -f "$pgmain_patch" ]] || die "missing TriDB fork patch: $pgmain_patch"
  if grep -q 'TRIDB: MSVBASE approximate_sum PostgresMain rewriter removed' "$root/thirdparty/Postgres/src/backend/tcop/postgres.c" 2>/dev/null; then
    log "TriDB fork patch (remove PostgresMain approximate_sum rewriter, advisor plan 018) already applied"
  else
    log "applying TriDB fork patch: remove unused PostgresMain approximate_sum query rewriter (advisor plan 018)"
    ( cd "$root/thirdparty/Postgres" && git apply "$pgmain_patch" ) \
      || die "tridb_remove_pgmain_rewriter.patch did not apply — MSVBASE/Postgres.patch drift? re-generate per advisor plan 018"
  fi

  #   tridb_relaxed_order_executor_guard.patch (advisor plan 022): harden the VBASE relaxed-
  #     monotonicity executor path that TR-1 early termination rides on. Three additive changes:
  #     (1) genam.c RelationGetIndexScan zero-inits scan->xs_inorder (a new, un-zeroed
  #     IndexScanDescData field the base Postgres.patch added) so a bounded Sort over an ORDINARY
  #     index can no longer read garbage and truncate its top-N; (2) nodeIndexscan.c gates the
  #     is_index_inorder EState flag (which arms nodeSort's bounded early-stop) on the driving
  #     AM's amcanrelaxedorderbyop, so the early-stop fires ONLY for a genuinely-relaxed (HNSW)
  #     scan; (3) nodeIndexscan.c restores the base patch's removed `index returned tuples in
  #     wrong order` ERROR, guarded to the NON-relaxed case (relaxed scans legitimately violate
  #     strict order). Also documents the fixed hnswindex.cpp emission-window heuristics
  #     (range=86 / distanceThreshold=3 / queueThreshold=50, upstream issue #22) as approximate,
  #     term_cond-not-window being the real recall knob (ADR-0007 addendum). Spans BOTH the
  #     Postgres submodule (executor/genam) AND src/hnswindex.cpp, so it applies from $root (NOT
  #     inside thirdparty/Postgres like the pgmain patch) and MUST come LAST — after the
  #     hnswindex.cpp fork chain (reloptions/costestimate/scan_no_orderby/am_entry_guards) whose
  #     line offsets it is diffed against. The executor hunks (genam/nodeIndexscan) are untouched
  #     by any other fork patch, so their offsets are stable. Interim hardening only; the real fix
  #     is the ADR-0007 CustomScan migration (relaxed order as a path, no global executor bool).
  local relaxed_guard_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_relaxed_order_executor_guard.patch"
  [[ -f "$relaxed_guard_patch" ]] || die "missing TriDB fork patch: $relaxed_guard_patch"
  if grep -q 'scan->xs_inorder = false' "$root/thirdparty/Postgres/src/backend/access/index/genam.c" 2>/dev/null; then
    log "TriDB fork patch (relaxed-order executor guard, advisor plan 022) already applied"
  else
    log "applying TriDB fork patch: relaxed-monotonicity executor guard (xs_inorder zero-init + amcanrelaxedorderbyop gate + wrong-order ERROR restore) (advisor plan 022)"
    ( cd "$root" && git apply "$relaxed_guard_patch" ) \
      || die "tridb_relaxed_order_executor_guard.patch did not apply — MSVBASE/fork-chain drift? re-generate per advisor plan 022"
  fi

  #   tridb_tjs_filter_first.patch (ADR-0011 Stage 3/4 / DEV-1290): the SECOND tjs() physical
  #     body. Adds an OPTIONAL 8th `join_order` argument ('vector_first'|'filter_first'; 7-arg
  #     callers byte-identical), the filter-first body (drain reachable(src) ∩ relational filter
  #     through ONE bounded-batch SPI cursor — the id set travels as an int8[] parameter, never
  #     interpolated SQL — and rank EXACTLY by squared L2 in C into the SAME bounded top-k PQ;
  #     peak memory O(batch + k), per-batch detoast scratch reset), and the operator-level
  #     tjs_last_join_order() observability companion (Option B's EXPLAIN-visibility tax). The
  #     vector-first merge (execTJS) is UNTOUCHED — filter-first pre-completes the merge and
  #     execTJS just serves the pops. Motivated by the measured 1M regime where vector-first
  #     loses 2x (docs/benchmark_sm2_1m_v0.1.0.md). Diffed against the post-termination-patch
  #     tjs_operator.cpp AND the post-tjs_open sql/vectordb.sql tail, so it MUST apply after
  #     BOTH tridb_tjs_predicate_termination.patch and tridb_tjs_open_operator.patch.
  local ff_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_tjs_filter_first.patch"
  [[ -f "$ff_patch" ]] || die "missing TriDB fork patch: $ff_patch"
  if grep -q 'TRIDB: TJS filter-first body' "$root/src/tjs_operator.cpp" 2>/dev/null; then
    log "TriDB fork patch (TJS filter-first body + join_order arg, DEV-1290) already applied"
  else
    log "applying TriDB fork patch: TJS filter-first physical body + join_order arg (DEV-1290)"
    ( cd "$root" && git apply "$ff_patch" ) \
      || die "tridb_tjs_filter_first.patch did not apply — tjs-chain drift? re-generate per DEV-1290"
  fi

  #   tridb_operator_arg_hardening.patch (advisor plan 024): hardens the tjs()/tjs_open()
  #     entry points and memory lifecycle. (1) arg range checks (tjs k 1..10000; tjs_open
  #     k/m_seeds 1..10000, hops 1..8) — k=0 hit top()/pop() on an EMPTY priority queue (UB,
  #     SQL-reachable backend crash) and a negative k arrived via PG_GETARG_UINT32 as ~4.29e9;
  #     (2) heap_freetuple on every bounded-PQ eviction (evicted copies leaked into the SRF's
  #     multi-call context until end of query); (3) a MemoryContextCallback releases the
  #     malloc'd state + new'd C++ containers on error paths (ereport longjmps past the End*
  #     teardown, leaking them permanently); (4) snprintf truncation guards at all 4
  #     previously-unchecked compose sites. Diffed against the post-filter-first tree, so it
  #     MUST apply after tridb_tjs_filter_first.patch (last in the tjs chain).
  local ah_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_operator_arg_hardening.patch"
  [[ -f "$ah_patch" ]] || die "missing TriDB fork patch: $ah_patch"
  if grep -q 'TRIDB: operator arg hardening' "$root/src/tjs_operator.cpp" 2>/dev/null; then
    log "TriDB fork patch (operator arg hardening, advisor plan 024) already applied"
  else
    log "applying TriDB fork patch: tjs/tjs_open arg + memory-lifecycle hardening (advisor plan 024)"
    ( cd "$root" && git apply "$ah_patch" ) \
      || die "tridb_operator_arg_hardening.patch did not apply — tjs-chain drift? re-generate per advisor plan 024"
  fi

  #   tridb_graph_v1_rewire.patch (advisor plan 025 / ADR-0013 Stage A): both operators'
  #     graph probe moves off the v0 heap-backed extension onto the v1 NATIVE access method:
  #     graphReachableT and expandMultiSeedO now SPI-call graph_store.gph_neighbors_ext
  #     (external-id traversal over the gph_upsert_vertex id map hosted in
  #     src/graph_store/graph_store_am--0.1.0.sql). Probe shape/caching/SPI nesting unchanged.
  #     Diffed against the post-arg-hardening tree, so it MUST apply after
  #     tridb_operator_arg_hardening.patch (last in the tjs chain).
  local v1rw_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_graph_v1_rewire.patch"
  [[ -f "$v1rw_patch" ]] || die "missing TriDB fork patch: $v1rw_patch"
  if grep -q 'TRIDB: graph v1 rewire' "$root/src/tjs_operator.cpp" 2>/dev/null; then
    log "TriDB fork patch (graph v1 rewire, advisor plan 025) already applied"
  else
    log "applying TriDB fork patch: operator graph probe -> v1 native AM (advisor plan 025 / ADR-0013 Stage A)"
    ( cd "$root" && git apply "$v1rw_patch" ) \
      || die "tridb_graph_v1_rewire.patch did not apply — tjs-chain drift? re-generate per advisor plan 025"
  fi

  # ----------------------------------------------------------------------------
  # SUPERSEDED / DO NOT ENABLE (DEV-1235 / ADR-0009): original GenericXLog draft.
  # ----------------------------------------------------------------------------
  #   hnsw_wal_durability.patch was the DRAFT GenericXLog approach for HNSW durability.
  #   by routing every index mutation through the SAME Postgres WAL the native
  #   graph store uses (GenericXLog) — see docs/decisions/0009-hnsw-wal-durability.md
  #   and docs/hnsw_wal_durability_bug_analysis_v0.1.0.md. The patch is a SPIKE
  #   DRAFT against vendored C++ that has NOT been compiled or run; its
  #   GenericXLog page bodies are TODO(GX10) stubs. It MUST be implemented and
  #   BUILT on the GX10 (Docker), and the crash/abort tests in
  #   docs/hnsw_wal_durability_bug_analysis_v0.1.0.md / test/crash_recovery_assert.sql
  #   must pass, BEFORE it is moved into the active apply path above.
  #
  #   When graduating it (GX10 Phase B), follow the existing convention exactly:
  #   sentinel-guarded idempotent apply + a verify_patches grep for the sentinel
  #   "TRIDB: HNSW WAL durability (DEV-1235)". The activation sketch:
  #
  #     local wal_patch="${_MSVBASE_LIB_DIR}/../patches/hnsw_wal_durability.patch"
  #     [[ -f "$wal_patch" ]] || die "missing TriDB fork patch: $wal_patch"
  #     if grep -q 'TRIDB: HNSW WAL durability (DEV-1235)' "$root/src/hnswindex_scan.cpp" 2>/dev/null; then
  #       log "TriDB fork patch (HNSW WAL durability, DEV-1235) already applied"
  #     else
  #       log "applying TriDB fork patch: HNSW WAL durability (DEV-1235 / ADR-0009)"
  #       ( cd "$root" && git apply "$wal_patch" ) \
  #         || die "hnsw_wal_durability.patch did not apply — MSVBASE drift? re-generate per DEV-1235"
  #     fi
  #     # and add to verify_patches():
  #     #   grep -q 'TRIDB: HNSW WAL durability (DEV-1235)' "$root/src/hnswindex_scan.cpp" \
  #     #     || die "TriDB hnsw_wal_durability.patch NOT applied — vector-index WAL durability missing (DEV-1235); drift?"
  #     #   grep -q 'src/tridb_hnsw_wal.cpp' "$root/CMakeLists.txt" \
  #     #     || die "TriDB hnsw_wal_durability.patch NOT wired into CMakeLists vectordb sources (DEV-1235); drift?"
  # ----------------------------------------------------------------------------
}

# Patch known-dead upstream URLs / build-breakers in the MSVBASE Dockerfile. Arch-independent
# bit-rot — applies equally to the x86 and GX10 builds. Idempotent.
patch_upstream_dockerfile() {
  local df="$1"
  [[ -f "$df" ]] || return 0
  # Digest-pin the floating base image for reproducibility (advisor plan 021 / UP-BUILD-02).
  # Guard on the exact pristine line ('FROM gcc:12.3.0' with nothing after) so a re-run over an
  # already-pinned Dockerfile ('FROM gcc:12.3.0@sha256:...') is a no-op.
  if grep -q '^FROM gcc:12.3.0$' "$df"; then
    log "digest-pinning base image (FROM gcc:12.3.0 -> gcc:12.3.0@${GCC_12_3_0_DIGEST}) for reproducibility"
    sed -i "s|^FROM gcc:12.3.0\$|FROM gcc:12.3.0@${GCC_12_3_0_DIGEST}|" "$df"
  fi
  if grep -q 'boostorg.jfrog.io' "$df"; then
    log "patching dead Boost URL (boostorg.jfrog.io left JFrog in 2024) -> archives.boost.io"
    sed -i 's#https://boostorg.jfrog.io/artifactory/main/release/1.81.0/source/boost_1_81_0.tar.gz#https://archives.boost.io/release/1.81.0/source/boost_1_81_0.tar.gz#g' "$df"
  fi
  # Hardcoded GID/UID 999 collides with a pre-existing group in current gcc:12.3.0 base.
  # Add -o (allow non-unique id) so the postgres group/user reuses 999 without erroring.
  if grep -q 'groupadd -r postgres --gid=' "$df"; then
    log "patching postgres GID/UID 999 collision (add -o for non-unique id)"
    sed -i 's/groupadd -r postgres --gid=/groupadd -r -o postgres --gid=/g' "$df"
    sed -i 's/useradd -m -r -g postgres --uid=/useradd -m -r -o -g postgres --uid=/g' "$df"
  fi
  # PG 13.4 plpython.h #include "eval.h" — removed in Python 3.11+, so --with-python fails
  # against the base image's modern Python. PL/Python is not used by TriDB (all C), so drop it.
  if grep -q -- '--with-python' "$df"; then
    log "dropping --with-python from PG configure (eval.h gone in Python 3.11+; PL/Python unused by TriDB v1)"
    sed -i '/--with-python/d' "$df"
  fi
  harden_dockerfile_downloads "$df"
}

# Supply-chain integrity (plan 007): the upstream Dockerfile streams Boost + CMake tarballs
# (`wget -O - | tar`) with `--no-check-certificate` and no hash check — a tampered mirror could
# inject code into the image undetected. Restore TLS verification and rewrite each download into
# download-to-file -> `sha256sum -c` -> extract. Idempotent (the streaming form is gone after the
# first pass). CMake here is the x86_64 3.14.4 tarball; patch_cmake_aarch64 swaps URL+hash on ARM.
harden_dockerfile_downloads() {
  local df="$1"
  [[ -f "$df" ]] || return 0
  # Remove the global git TLS-off setting (advisor plan 021 / UP-BUILD-01). Upstream bakes
  # `RUN git config --global http.sslverify false` into the image, disabling cert verification for
  # ALL git traffic at build AND runtime — the MITM path the tarball hardening below closed,
  # reopened for git. Delete the whole RUN line (git defaults to sslverify=true). Idempotent.
  if grep -q 'git config --global http.sslverify false' "$df"; then
    log "removing global git TLS-off (RUN git config --global http.sslverify false) — restores cert verification for git"
    sed -i '/git config --global http.sslverify false/d' "$df"
  fi
  if grep -q -- '--no-check-certificate' "$df"; then
    log "restoring TLS verification on Dockerfile downloads (drop --no-check-certificate)"
    sed -i 's/ --no-check-certificate//g' "$df"
  fi
  if grep -q 'boost_1_81_0.tar.gz" -q -O -' "$df"; then
    log "hardening Boost download (sha256sum -c before extract)"
    sed -i 's#boost_1_81_0.tar.gz" -q -O - \\#boost_1_81_0.tar.gz" -q -O boost.tgz \&\& \\#' "$df"
    sed -i 's#| tar -xz && \\#echo "'"$BOOST_1_81_0_SHA256"'  boost.tgz" | sha256sum -c - \&\& tar -xzf boost.tgz \&\& rm -f boost.tgz \&\& \\#' "$df"
  fi
  if grep -q 'cmake-3.14.4-Linux-x86_64.tar.gz" -q -O -' "$df"; then
    log "hardening CMake download (sha256sum -c before extract)"
    sed -i 's#cmake-3.14.4-Linux-x86_64.tar.gz" -q -O - \\#cmake-3.14.4-Linux-x86_64.tar.gz" -q -O cmake.tgz \&\& \\#' "$df"
    sed -i 's#| tar -xz --strip-components=1 -C /usr/local#echo "'"$CMAKE_3_14_4_X86_64_SHA256"'  cmake.tgz" | sha256sum -c - \&\& tar -xzf cmake.tgz --strip-components=1 -C /usr/local \&\& rm -f cmake.tgz#' "$df"
  fi
  # post-condition (plan 015): the hardening MUST have landed — a silent sed no-op here would
  # ship an image with unverified downloads (the exact hole plan 007 closed).
  grep -q -- '--no-check-certificate' "$df" && \
    die "harden_dockerfile_downloads: --no-check-certificate still present (upstream drift?) — inspect $df"
  if grep -q 'boost_1_81_0.tar.gz' "$df" && ! grep -q 'sha256sum -c' "$df"; then
    die "harden_dockerfile_downloads: Boost/CMake download present without sha256sum -c (upstream drift?) — inspect $df"
  fi
}

# Modern GCC (12/13 in the gcc:12.3.0 base) no longer transitively includes <mutex>,
# <cstdint>, etc. — old SPTAG/vectordb code assumed it did. Force-include the dropped
# headers via SPTAG's own CXX flags (SPTAG resets CMAKE_CXX_FLAGS, so a global -D won't reach
# it). Idempotent. Arch-independent; same fix needed on the GX10.
FORCE_INC='-include cstdint -include mutex -include shared_mutex -include memory -include cstring -include limits -include functional'
patch_modern_gcc_includes() {
  local root="$1"
  local sptag_cm="$root/thirdparty/SPTAG/CMakeLists.txt"
  # SPTAG only: it sets CXX-only flags, so force-includes never leak onto the C compiler.
  # (Do NOT touch the top vectordb CMakeLists — it derives CMAKE_C_FLAGS from CMAKE_CXX_FLAGS,
  # so a C++-header force-include there breaks cmake's OpenMP_C probe. Use a CXX-only genexp
  # if vectordb sources ever need it.)
  if [[ -f "$sptag_cm" ]] && grep -q -- '-std=c++14 -fopenmp"' "$sptag_cm" && ! grep -q 'include cstdint' "$sptag_cm"; then
    log "force-including dropped std headers into SPTAG build (modern GCC transitive-include fix)"
    sed -i "s/-std=c++14 -fopenmp\"/-std=c++14 -fopenmp ${FORCE_INC}\"/" "$sptag_cm"
  fi
}

# GX10/ARM-only delta: the upstream Dockerfile hardcodes an x86_64 CMake tarball. The pinned
# version (3.14.4) predates Kitware's aarch64 Linux binaries, so swap BOTH arch and version to
# 3.27.9 — the first-class aarch64 release the prior ensure_cmake already used. MSVBASE only
# needs CMake >= 3.14, and the install uses `tar --strip-components=1 -C /usr/local`, so the
# tarball's internal directory name is irrelevant. Idempotent, grep-guarded. Call only on ARM.
patch_cmake_aarch64() {
  local df="$1"
  [[ -f "$df" ]] || return 0
  if grep -q 'cmake-3.14.4-Linux-x86_64.tar.gz' "$df"; then
    log "patching Dockerfile CMake download for aarch64 (3.14.4 x86_64 -> 3.27.9 aarch64; GX10 delta)"
    sed -i 's#https://github.com/Kitware/CMake/releases/download/v3.14.4/cmake-3.14.4-Linux-x86_64.tar.gz#https://github.com/Kitware/CMake/releases/download/v3.27.9/cmake-3.27.9-linux-aarch64.tar.gz#g' "$df"
    # If harden_dockerfile_downloads already injected the x86_64 checksum, swap it to the
    # aarch64 one too (no-op if hardening has not run). Keeps URL and hash consistent on ARM.
    sed -i "s#${CMAKE_3_14_4_X86_64_SHA256}#${CMAKE_3_27_9_AARCH64_SHA256}#g" "$df"
  fi
}

# GX10/ARM-only delta #2: MSVBASE's CMakeLists.txt hardcodes x86-only ISA flags
# (-msse4.2 -maes -mavx2 -mmwaitx) into the global CMAKE_C/CXX_FLAGS and two
# target_compile_options(-mavx2). On aarch64 GCC these flags are UNRECOGNIZED, so
# every cmake compile probe fails — the first visible casualty is the OpenMP probe
# ("Could NOT find OpenMP_C (missing: OpenMP_C_FLAGS OpenMP_C_LIB_NAMES)"), NOT
# OpenMP itself. Strip them on ARM. hnswlib's SIMD kernels are all gated on
# __SSE__/__AVX__ (hnswlib.h: `#ifdef __SSE__` -> `#define USE_SSE`), which GCC
# only predefines under -msse/-mavx; with the flags gone they compile via the
# scalar L2Sqr/InnerProduct fallback (fstdistfunc_ = L2Sqr). Idempotent
# (grep-guarded), with a post-condition assert that no x86 ISA flag survives.
# Call ONLY on ARM, after patch_cmake_aarch64. (Performance tuning — Neoverse
# -mcpu=native — is deferred; this is correctness-first to clear the ARM build.)
patch_cmake_arm_isa_flags() {
  local root="$1"
  local top="$root/CMakeLists.txt"
  local tp="$root/thirdparty/CMakeLists.txt"
  [[ -f "$top" ]] || die "patch_cmake_arm_isa_flags: missing $top"
  if grep -qE 'msse4\.2|maes|mavx2|mmwaitx' "$top" "$tp" 2>/dev/null; then
    log "stripping hardcoded x86 ISA flags (-msse4.2 -maes -mavx2 -mmwaitx) for aarch64 (GX10 delta #2; hnswlib -> scalar path)"
    sed -i -E -e 's/ -msse4\.2 -maes -mavx2//g' -e 's/ -mmwaitx//g' -e 's/ -mavx2//g' "$top"
    [[ -f "$tp" ]] && sed -i -E -e 's/ -mavx2//g' "$tp"
  fi
  # post-condition: NO x86 ISA flag may remain on ARM (else the cmake probes re-fail)
  if grep -qE 'msse4\.2|maes|mavx2|mmwaitx' "$top" "$tp" 2>/dev/null; then
    die "patch_cmake_arm_isa_flags: x86 ISA flag still present after patch (upstream drift?) — inspect $top and $tp"
  fi
}
