# Plan 003: Thread HNSW reloptions into the crash-recovery rebuild

> **Executor instructions**: This is **GX10-gated** — the C compiles only inside the MSVBASE fork on the
> GX10 (aarch64) / via the Docker image, NOT on an x86 dev box. Author + self-review the patch here; build
> and verify on the GX10. Follow steps in order; STOP and report on any STOP condition. Update this plan's
> row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat 7bf3dca..HEAD -- scripts/patches/tridb_hnsw_reloptions.patch scripts/lib/msvbase_patches.sh vendor/MSVBASE/src/hnswindex_scan.cpp vendor/MSVBASE/src/hnswindex_builder.cpp`

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (extends the reloptions patch from PR #21)
- **Category**: bug (correctness asymmetry)
- **Planned at**: commit `7bf3dca`, 2026-06-26

## Why this matters

PR #21 (DEV-1286) added HNSW `m` / `ef_construction` reloptions and threads them into the **fresh** index
build (`hnswindex_builder.cpp`). But the **crash-recovery rebuild** path (`hnswindex_scan.cpp` `LoadIndex`,
the DEV-1235 heap-as-source-of-truth rebuild) still constructs the in-RAM index at hnswlib defaults
(M=16, ef_construction=200). So after an unclean crash, a tuned index (`WITH (m=32, ef_construction=400)`)
silently rebuilds at **lower quality** and serves degraded recall until a manual `REINDEX` — a correctness
asymmetry between the two construction sites. It is already flagged as a documented follow-up in the patch
comment; this plan closes it.

## Current state

- Fresh-build site — the pattern to mirror, `vendor/MSVBASE/src/hnswindex_builder.cpp` (forward-decls near
  the top of the file, construction in `ConstructInternalBuilder`):
  ```cpp
  // forward decls (C++ linkage; defined in hnswindex.cpp)
  int hnsw_ParaGetM(Relation index);
  int hnsw_ParaGetEfConstruction(Relation index);
  ...
  int relM = hnsw_ParaGetM(m_index);
  int relEfc = hnsw_ParaGetEfConstruction(m_index);
  size_t hnsw_M  = (relM  > 0) ? (size_t)relM  : 16;
  size_t hnsw_efc = (relEfc > 0) ? (size_t)relEfc : 200;
  vector_index = std::make_shared<hnswlib::HierarchicalNSW<float>>(
      distance.get(), m_indtuples * 10, hnsw_M, hnsw_efc);
  ```
- Recovery-rebuild site — `vendor/MSVBASE/src/hnswindex_scan.cpp:144` (inside `LoadIndex`, which has the
  `index` `Relation` in scope; capacity already derived from `reltuples`):
  ```cpp
  auto vector_index =
      std::make_shared<hnswlib::HierarchicalNSW<float>>(distanceFunction.get(), capacity);
  ```
- The getters `hnsw_ParaGetM` / `hnsw_ParaGetEfConstruction` are defined in
  `vendor/MSVBASE/src/hnswindex.cpp` (C++ linkage), return 0 when the reloption is unset.
- Source of truth: `scripts/patches/tridb_hnsw_reloptions.patch` (applied at build by
  `scripts/lib/msvbase_patches.sh`). `vendor/MSVBASE` is gitignored + re-cloned, so the **patch file is
  what must change**, not just the working tree. The patch's apply step + `verify_patches` sentinel
  (`offsetof(hnsw_ParaOptions, ef_construction)`) live in `scripts/lib/msvbase_patches.sh` around the
  `tridb_hnsw_reloptions.patch` block, which currently has a comment:
  `"NOTE: the DEV-1235 rebuild-on-recovery path ... still rebuilds at hnswlib defaults ... documented follow-up, not wired here."`

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Re-verify patch applies (after editing the working tree) | `cd vendor/MSVBASE && git apply --check <new patch>` | exit 0 |
| Build (GX10 / Docker) | `scripts/gx10build.sh` (GX10) or `scripts/x86build.sh --docker` | image builds, `verify_patches` passes |
| Crash-recovery suite | `bash scripts/crash_recovery_test.sh <image>` | both scenarios PASS |

## Scope

**In scope:**
- `vendor/MSVBASE/src/hnswindex_scan.cpp` (edit working tree to author the change), then regenerate
  `scripts/patches/tridb_hnsw_reloptions.patch` to include it.
- `scripts/lib/msvbase_patches.sh` (update the stale "rebuild ... still defaults" comment).
- A new GX10 test (SQL) asserting recovery preserves index quality.

**Out of scope:** the fresh-build site (already correct); the relopt registration/struct (already done);
anything outside the reloptions plumbing; the `live_report`/Python layer.

## Steps

### Step 1: Thread M/ef into `LoadIndex`

In `vendor/MSVBASE/src/hnswindex_scan.cpp`, add the two getter forward-declarations near the top (mirror
the builder; they are C++ linkage, defined in `hnswindex.cpp`), then at line ~144 read the reloptions from
`index` and pass them:
```cpp
int relM = hnsw_ParaGetM(index);
int relEfc = hnsw_ParaGetEfConstruction(index);
size_t hnsw_M  = (relM  > 0) ? (size_t)relM  : 16;
size_t hnsw_efc = (relEfc > 0) ? (size_t)relEfc : 200;
auto vector_index =
    std::make_shared<hnswlib::HierarchicalNSW<float>>(distanceFunction.get(), capacity, hnsw_M, hnsw_efc);
```
Confirm the `Relation index` parameter name in `LoadIndex`'s signature and use it (it is already used for
`RelationGetRelid(index)` nearby).

### Step 2: Regenerate the patch + update the comment

Regenerate `scripts/patches/tridb_hnsw_reloptions.patch` so it includes the new `hnswindex_scan.cpp` hunk
(the patch is the source of truth; the working-tree edit alone is wiped on re-clone). In
`scripts/lib/msvbase_patches.sh`, update the `tridb_hnsw_reloptions.patch` block comment to state that BOTH
the fresh build and the recovery rebuild now honor the reloptions (remove the "documented follow-up" caveat).

**Verify**: `cd vendor/MSVBASE && git apply --check ../../scripts/patches/tridb_hnsw_reloptions.patch` → exit 0
against a freshly re-cloned/patched-up-to-this-point tree (or round-trip verify like the original patch did).

### Step 3: GX10 verification test

Add a SQL test (model on `test/crash_recovery_assert.sql` / `scripts/crash_recovery_test.sh`) that:
1. creates `entities_hnsw WITH (m=32, ef_construction=400)`,
2. crashes (`pg_ctl -m immediate`) and recovers,
3. asserts the recovered index's search behaves like a freshly-built `m=32/ef=400` index, not the
   `m=16/ef=200` default — e.g. compare `tjs_candidates_examined()` / recall at a fixed `term_cond`
   against a control cluster built fresh with the same reloptions (equal within tolerance), AND distinct
   from a control built at defaults.

**Verify (GX10 only)**: build the image, run the new test → PASS; `scripts/crash_recovery_test.sh` still PASS.

## Done criteria

- [ ] `scripts/patches/tridb_hnsw_reloptions.patch` contains an `hnswindex_scan.cpp` hunk passing the M/ef
      args to the `LoadIndex` `HierarchicalNSW` constructor.
- [ ] `git apply --check` of the regenerated patch exits 0.
- [ ] `scripts/lib/msvbase_patches.sh` comment no longer claims the recovery path defaults.
- [ ] (GX10) image builds, `verify_patches` passes, the new recovery-quality test PASSES, existing
      `crash_recovery_test.sh` PASSES.
- [ ] `advisor-plans/README.md` row updated.

## STOP conditions

- The getters are not linkable from `hnswindex_scan.cpp`'s translation unit (linker error on the GX10) —
  the builder's forward-decl approach should work identically; if an include cycle forces a different
  wiring, report rather than restructuring headers ad hoc.
- The `HierarchicalNSW` constructor signature in the patched hnswlib does not accept `(space, capacity, M,
  ef_construction)` positionally (the relaxed-mono `hnsw.patch` could have reordered it) — report.
- The recovery-quality assertion can't distinguish m=32 from m=16 behavior at the chosen corpus/term_cond
  (too small to matter) — scale the test corpus up until it does, or report that the asymmetry is
  unobservable at testable scale.

## Maintenance notes

- Keep the two construction sites' default-fallback logic identical (both `>0 ? : 16/200`). If a third
  construction site appears, thread the reloptions there too.
- Reviewer: confirm the recovery path reads reloptions from the SAME `index` relation the catalog stores
  them on (not the heap), so a tuned index recovers tuned.
