# Plan 032: NEON inner-product / cosine distance kernel for hnswlib (DEV-1343 / PERF-01)

> **Executor instructions**: Follow step by step; every perf claim needs a before/after number, not
> vibes. On any STOP condition, stop and report. Update your row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat 876a696..HEAD -- scripts/patches vendor/MSVBASE/thirdparty/hnsw`
> This plan mirrors the shipped L2 patch into the inner-product space — re-read the live L2 patch before
> editing, in case its shape moved.

## Status

- **Priority**: P1 (latent correctness/credibility bug, not just perf — the default metric is affected)
- **Effort**: S
- **Risk**: LOW (distance kernel only; bit-equal to scalar within ~1e-4; ordering preserved)
- **Depends on**: none. Sibling of DEV-1288 (the L2 kernel this copies).
- **Category**: perf / correctness
- **Planned at**: commit `876a696`, 2026-07-04
- **Linear**: DEV-1343

## Why this matters

DEV-1288 added a NEON kernel for **L2** distance and un-sandbagged every ARM latency number. But
inner-product is the **default** metric — `hnsw_ParaGetDistmethod` returns `hnsw_Inner_Product` when
unset (`vendor/MSVBASE/src/hnswindex.cpp:52`) — and `space_ip.h` has **no NEON path at all** (only x86
SSE/AVX/AVX512, all dead on aarch64). So any index built without `distmethod=l2_distance`, i.e. any
real cosine/angular embedding (BGE-768, GloVe, most RAG encoders), silently runs the **scalar**
`InnerProductDistance` on the GX10. This is the exact "every ARM latency number wrong-low" bug DEV-1288
fixed, still live for the default metric. Every current benchmark pins `l2_distance`
(`tools/bench_sm2_corpus.py:94`, `tools/sweep_corpus.py:183`, `tools/bench_corpus.py:206`,
`tools/filtered_corpus.py:105`) so the measured numbers are on the fast path — which means the gap is
invisible until someone runs a cosine workload and quotes a wrong-low latency. Close it before any
cosine claim ships.

## Current state

- `vendor/MSVBASE/thirdparty/hnsw/hnswlib/space_ip.h` — scalar `InnerProductDistance` + x86
  `InnerProductDistanceSIMD{4,16}Ext{SSE,AVX,AVX512}`; `grep -c ARM_NEON space_ip.h` = **0**.
- `vendor/MSVBASE/thirdparty/hnsw/hnswlib/space_l2.h:220-260` — the shipped NEON `L2SqrSIMD16ExtNEON` /
  `L2SqrNEON` (from `scripts/patches/tridb_neon_l2_distance.patch`), `__ARM_NEON`-gated, selected in the
  `L2Space` ctor (`space_l2.h:291-299`). This is the exact template to mirror.
- `vendor/MSVBASE/src/hnswindex.cpp:52` — the default-metric decision that makes this load-bearing.
- Patch-chain conventions + `verify_patches` sentinels: identical to DEV-1288 / plan 024 workflow
  (vendor edit → new last patch → register + sentinel).
- Kernel validation precedent: `tools/neon_l2_bench.c` (per-call kernel micro-bench + rel-err vs scalar).

## Commands you will need

Plan 024's table (incremental compile, `scripts/x86build.sh --docker`, `make graph-test`, single-file
test, `make test`/`make lint`), plus:

| Purpose | Command | Expected |
|---|---|---|
| Kernel micro-bench (adapt for IP) | build a `tools/neon_ip_bench.c` off `neon_l2_bench.c` | rel-err ≤ 1e-4 vs scalar; per-call speedup at dim 32/128/768 |
| Default-metric index smoke | a scratch `.sql`: `CREATE INDEX ... USING hnsw (v)` with NO distmethod, top-k query | correct neighbors; runs the NEON IP path |

## Scope

**In scope:** a NEON inner-product kernel in `space_ip.h` (`vfmaq_f32`-accumulated, `__ARM_NEON`-gated,
inert on x86), selected in the `InnerProductSpace` ctor mirroring `L2Space`; a new last patch
`scripts/patches/tridb_neon_ip_distance.patch` + registration + a sentinel on a load-bearing token
(the NEON IP function name); a `tools/neon_ip_bench.c` validator; README row.

**Out of scope:** the `uint8` `L2SqrI` / integer-IP scalar paths (note as a follow-on); any change to
the default-metric decision itself; GPU/quantization (PERF-08/10); the L2 kernel (already shipped).

## Git workflow
Branch `advisor/032-neon-ip-kernel`; `perf(vector):` commit with the rel-err + per-call speedup numbers
in the body; do NOT push.

## Steps

### Step 1: Author the NEON IP kernel
Snapshot `space_ip.h`. Add `InnerProductDistanceSIMD16ExtNEON` (and a residual-handling variant for
`dim % 16 != 0`, mirroring the L2 patch's dim-31/100 handling) using `float32x4_t` + `vfmaq_f32`;
return `1 - dot` where the scalar path does (match the existing `InnerProductDistance` return exactly —
confirm whether the fork's IP returns `dot` or `1-dot` and preserve it). `__ARM_NEON`-gated; leave the
x86 SIMD blocks untouched. Select it in the `InnerProductSpace` ctor for `dim%4==0`/`dim>4`, else scalar.
**Verify**: incremental compile clean on the fork; kernel returns within 1e-4 of scalar across dims
16/32/100/128/768 (incl. a non-multiple-of-16 residual) via `tools/neon_ip_bench.c`.

### Step 2: Prove it runs on the default path
Scratch `.sql`: build an HNSW index with **no** `distmethod` set and run a top-k query; confirm answers
match a scalar/exact oracle. This proves the default (IP) metric now takes the NEON path.
**Verify**: neighbors correct; if you can capture a per-query timing, record the IP index build-time
drop (expect the ~L2-scale improvement DEV-1288 showed, since distance dominates HNSW build).

### Step 3: Patch generation + full validation
Generate `tridb_neon_ip_distance.patch` from the snapshot diff; register (after the L2 patch; sentinel
on the NEON IP function name); `bash scripts/x86build.sh --docker` (confirms the patch applies + the
x86 build is unaffected — the NEON block is inert there); `make graph-test`; `make test && make lint`.
**Verify**: all green; reverse-apply check clean. The x86 build proves the patch is chain-safe; the
**per-call speedup + build-time drop are GX10-gated** — author here, measure on the Spark.

## Test plan
Correctness = rel-err ≤ 1e-4 vs scalar (micro-bench) + default-metric index answers match an exact
oracle. Perf evidence = per-call speedup + IP-index build-time before/after, recorded on the GX10.

## Done criteria
- [ ] NEON IP kernel added, `__ARM_NEON`-gated, selected in `InnerProductSpace` ctor; x86 untouched
- [ ] rel-err ≤ 1e-4 vs scalar across dims incl. a residual case; default-metric index answers correct
- [ ] New patch registered + sentinel + reverse-apply clean; `x86build --docker` + `make graph-test` green
- [ ] `make test && make lint` green; README row updated
- [ ] GX10: per-call speedup + IP-index build-time drop recorded (target ~6–8×, matching L2)

## STOP conditions
- The IP return convention (`dot` vs `1-dot`) differs from what you assumed and answers change — stop,
  re-read `InnerProductDistance`, do not re-pin any test.
- Any default-metric index answer diverges from the exact oracle at test scale.
- Patch-chain reverse-apply breaks (the L2 patch shape moved) — rebase the vendor edit, regenerate, note it.

## Maintenance notes
This closes the last scalar hot loop in the float distance paths on ARM. The `uint8` integer distance
kernels (`L2SqrI`, integer IP) remain scalar everywhere — a natural follow-on if/when quantized storage
(PERF-10) lands and needs fast integer distance. Reviewer focus: the IP return convention and the
residual-tail handling for non-multiple-of-16 dims.
