// tools/neon_ip_bench.c — standalone ARM64 NEON inner-product kernel validation + microbenchmark (DEV-1343).
//
// WHY THIS EXISTS
// Sibling of tools/neon_l2_bench.c (DEV-1234), for the INNER-PRODUCT metric. inner_product is the
// DEFAULT distance (hnsw_ParaGetDistmethod returns hnsw_Inner_Product when distmethod is unset —
// vendor/MSVBASE/src/hnswindex.cpp), yet vendor/.../hnswlib/space_ip.h shipped with NO NEON path:
// only x86 SSE/AVX/AVX512, all dead on aarch64 (the MSVBASE fork build strips MSVBASE's hardcoded
// x86 ISA flags — see scripts/lib/msvbase_patches.sh patch_cmake_arm_isa_flags). Consequence on the
// GX10/DGX-Spark: any cosine/angular index built without distmethod=l2_distance falls back to the
// scalar InnerProduct loop, one float at a time — sandbagging EVERY latency number for the default
// metric, exactly the bug DEV-1234 fixed for L2.
//
// This program is the de-risking gate for the NEON IP kernel BEFORE wiring it into the engine: it
// reproduces the EXACT scalar fallback and the NEW NEON kernel (identical to the production patch to
// space_ip.h — including the fork's `1 - dot` distance convention), proves they agree to
// floating-point tolerance across representative dimensions (including non-multiples of 16, which
// exercise the residual path), and measures the speedup on the real ISA. It compiles standalone with
// gcc -O2 -lm — no Postgres, no MSVBASE build — so the kernel is validated on aarch64 in seconds.
//
// BUILD + RUN (on an aarch64 host, e.g. `ssh spark`):
//   gcc -O2 -o /tmp/neon_ip_bench tools/neon_ip_bench.c -lm && /tmp/neon_ip_bench
// On x86 it still compiles and runs the correctness check (NEON path falls back to scalar) but the
// speedup is only meaningful on aarch64.

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

// ----------------------------------------------------------------------------
// SCALAR — byte-for-byte the current ARM fallback (hnswlib space_ip.h
// InnerProduct + InnerProductDistance; the ctor selects InnerProductDistance).
// ----------------------------------------------------------------------------
static float InnerProduct_scalar(const void *pVect1v, const void *pVect2v, const void *qty_ptr) {
    float *pVect1 = (float *)pVect1v;
    float *pVect2 = (float *)pVect2v;
    size_t qty = *((size_t *)qty_ptr);
    float res = 0;
    for (size_t i = 0; i < qty; i++) {
        res += pVect1[i] * pVect2[i];
    }
    return res;
}

static float InnerProductDistance_scalar(const void *a, const void *b, const void *q) {
    return 1.0f - InnerProduct_scalar(a, b, q);
}

// ----------------------------------------------------------------------------
// NEON — identical to the production patch to space_ip.h (gated on __ARM_NEON).
// 16 floats per loop iteration via 4 independent 128-bit FMA accumulators (4
// lanes each) to hide FMA latency, then a horizontal add. Returns the raw dot;
// the Distance wrappers apply the fork's `1 - dot`. Residual (qty % 16) handled
// by the scalar tail, mirroring InnerProductDistanceSIMD16ExtResiduals.
// ----------------------------------------------------------------------------
#if defined(__ARM_NEON)
#include <arm_neon.h>

static float InnerProductSIMD16ExtNEON(const void *pVect1v, const void *pVect2v, const void *qty_ptr) {
    float *pVect1 = (float *)pVect1v;
    float *pVect2 = (float *)pVect2v;
    size_t qty = *((size_t *)qty_ptr);
    size_t qty16 = qty >> 4;
    const float *pEnd1 = pVect1 + (qty16 << 4);

    float32x4_t sum0 = vdupq_n_f32(0.0f);
    float32x4_t sum1 = vdupq_n_f32(0.0f);
    float32x4_t sum2 = vdupq_n_f32(0.0f);
    float32x4_t sum3 = vdupq_n_f32(0.0f);

    while (pVect1 < pEnd1) {
        sum0 = vfmaq_f32(sum0, vld1q_f32(pVect1), vld1q_f32(pVect2));
        sum1 = vfmaq_f32(sum1, vld1q_f32(pVect1 + 4), vld1q_f32(pVect2 + 4));
        sum2 = vfmaq_f32(sum2, vld1q_f32(pVect1 + 8), vld1q_f32(pVect2 + 8));
        sum3 = vfmaq_f32(sum3, vld1q_f32(pVect1 + 12), vld1q_f32(pVect2 + 12));
        pVect1 += 16;
        pVect2 += 16;
    }
    float32x4_t sum = vaddq_f32(vaddq_f32(sum0, sum1), vaddq_f32(sum2, sum3));
    return vaddvq_f32(sum);  // AArch64 horizontal add across the 4 lanes
}

static float InnerProductDistanceSIMD16ExtNEON(const void *a, const void *b, const void *q) {
    return 1.0f - InnerProductSIMD16ExtNEON(a, b, q);
}

// dim % 16 == 0 -> pure 16-ext; otherwise 16-ext over the aligned prefix + scalar tail, 1 - (...) once.
static float InnerProductDistanceNEON(const void *pVect1v, const void *pVect2v, const void *qty_ptr) {
    size_t qty = *((size_t *)qty_ptr);
    size_t qty16 = (qty >> 4) << 4;
    float res = InnerProductSIMD16ExtNEON(pVect1v, pVect2v, &qty16);
    size_t qty_left = qty - qty16;
    float *p1 = (float *)pVect1v + qty16;
    float *p2 = (float *)pVect2v + qty16;
    return 1.0f - (res + InnerProduct_scalar(p1, p2, &qty_left));
}

// Dispatch mirroring the InnerProductSpace ctor: dim%16==0 -> 16-ext, dim>16 -> residual, else scalar.
static float InnerProductDistanceNEON_dispatch(const void *a, const void *b, const void *q) {
    size_t dim = *((size_t *)q);
    if (dim % 16 == 0) return InnerProductDistanceSIMD16ExtNEON(a, b, q);
    if (dim > 16) return InnerProductDistanceNEON(a, b, q);
    return InnerProductDistance_scalar(a, b, q);
}
#else
// Non-ARM: keep the program buildable; NEON path aliases scalar (no SIMD speedup expected).
static float InnerProductDistanceNEON_dispatch(const void *a, const void *b, const void *q) {
    return InnerProductDistance_scalar(a, b, q);
}
#endif

// ----------------------------------------------------------------------------
// Harness
// ----------------------------------------------------------------------------
static double now_sec(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static float frand(void) { return (float)rand() / (float)RAND_MAX - 0.5f; }

int main(void) {
#if defined(__ARM_NEON)
    const char *isa = "aarch64 + NEON";
#else
    const char *isa = "non-ARM (NEON path == scalar; speedup N/A)";
#endif
    printf("# neon_ip_bench  ISA: %s\n", isa);

    // ---- correctness: scalar vs NEON distance must agree to fp tolerance across dims ----
    // Includes non-multiples of 16 (31, 100) that exercise the residual tail, plus dim<=16
    // non-multiples (3, 15) that fall back to scalar in the dispatch (must be exact).
    const int cdims[] = {1, 3, 4, 15, 16, 31, 32, 96, 100, 128, 384, 768};
    int nfail = 0;
    printf("\n## correctness (scalar vs NEON, distance = 1 - dot)\n");
    srand(12345);
    for (size_t di = 0; di < sizeof(cdims) / sizeof(cdims[0]); di++) {
        size_t dim = (size_t)cdims[di];
        double worst_rel = 0.0;
        for (int trial = 0; trial < 2000; trial++) {
            float a[1024], b[1024];
            for (size_t i = 0; i < dim; i++) {
                a[i] = frand() * 10.0f;
                b[i] = frand() * 10.0f;
            }
            float s = InnerProductDistance_scalar(a, b, &dim);
            float n = InnerProductDistanceNEON_dispatch(a, b, &dim);
            // Distance = 1 - dot; dot can be ~0 so 1 - dot ~ 1. Use rel err vs the raw dot magnitude
            // (the quantity the SIMD reorders) with a 1.0 floor, matching neon_l2_bench's envelope.
            float dot_mag = fabsf(1.0f - s);
            float denom = dot_mag > 1e-6f ? dot_mag : 1.0f;
            double rel = fabsf(s - n) / denom;
            if (rel > worst_rel) worst_rel = rel;
        }
        // f32 accumulation order differs (4 lanes vs sequential); 1e-4 rel is the expected envelope.
        const char *verdict = worst_rel < 1e-4 ? "OK" : "FAIL";
        if (worst_rel >= 1e-4) nfail++;
        printf("  dim=%-4zu worst_rel_err=%.3e  %s\n", dim, worst_rel, verdict);
    }
    if (nfail) {
        printf("\nCORRECTNESS FAILED for %d dim(s)\n", nfail);
        return 1;
    }
    printf("  -> all dims agree within 1e-4 relative error\n");

    // ---- throughput: representative ANN dims ----
    // dim=32 (current x86-standin bench), dim=128 (SIFT), dim=768 (spec embedding dim).
    const int bdims[] = {32, 128, 768};
    printf("\n## throughput (lower ns/call is better)\n");
    printf("  %-6s %14s %14s %10s\n", "dim", "scalar ns/call", "neon ns/call", "speedup");
    for (size_t di = 0; di < sizeof(bdims) / sizeof(bdims[0]); di++) {
        size_t dim = (size_t)bdims[di];
        long calls = (long)(2.0e8 / (double)dim);
        if (calls < 100000) calls = 100000;

        const int NV = 256;
        float *va = malloc(sizeof(float) * dim * NV);
        float *vb = malloc(sizeof(float) * dim * NV);
        for (size_t i = 0; i < dim * NV; i++) {
            va[i] = frand() * 10.0f;
            vb[i] = frand() * 10.0f;
        }

        volatile float sink = 0.0f;  // defeat dead-code elimination

        double t0 = now_sec();
        for (long c = 0; c < calls; c++) {
            float *a = va + (size_t)(c & (NV - 1)) * dim;
            float *b = vb + (size_t)(c & (NV - 1)) * dim;
            sink += InnerProductDistance_scalar(a, b, &dim);
        }
        double t_scalar = now_sec() - t0;

        t0 = now_sec();
        for (long c = 0; c < calls; c++) {
            float *a = va + (size_t)(c & (NV - 1)) * dim;
            float *b = vb + (size_t)(c & (NV - 1)) * dim;
            sink += InnerProductDistanceNEON_dispatch(a, b, &dim);
        }
        double t_neon = now_sec() - t0;

        double ns_scalar = t_scalar / (double)calls * 1e9;
        double ns_neon = t_neon / (double)calls * 1e9;
        printf("  %-6zu %14.2f %14.2f %9.2fx  (sink=%.1f)\n", dim, ns_scalar, ns_neon,
               ns_scalar / ns_neon, (double)sink);
        free(va);
        free(vb);
    }
    printf("\n# done\n");
    return 0;
}
