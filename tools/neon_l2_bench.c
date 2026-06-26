// tools/neon_l2_bench.c — standalone ARM64 NEON L2-squared kernel validation + microbenchmark (DEV-1234).
//
// WHY THIS EXISTS
// On the GX10/DGX-Spark (aarch64) the MSVBASE fork build strips MSVBASE's hardcoded x86 ISA flags
// (-msse4.2 -mavx2 ...), which aarch64 GCC rejects (see scripts/lib/msvbase_patches.sh
// patch_cmake_arm_isa_flags). Consequence: hnswlib's USE_SSE/USE_AVX SIMD L2 paths are all dead on
// ARM and the index falls back to the scalar L2Sqr loop (vendor/.../hnswlib/space_l2.h:6-20). Every
// distance computation — the hottest loop in ANN search and in the TJS operator's re-rank — runs one
// float at a time. That sandbags EVERY latency number on the real target hardware.
//
// This program is the de-risking gate for the NEON kernel BEFORE wiring it into the engine: it
// reproduces the EXACT scalar fallback and the NEW NEON kernel (identical to the production patch to
// space_l2.h), proves they agree to floating-point tolerance across representative dimensions
// (including non-multiples of 16, which exercise the residual path), and measures the speedup on the
// real ISA. It compiles standalone with gcc -O2 -lm — no Postgres, no MSVBASE build — so the kernel
// is validated on aarch64 in seconds instead of waiting on a multi-hour engine rebuild.
//
// BUILD + RUN (on an aarch64 host, e.g. `ssh spark`):
//   gcc -O2 -o /tmp/neon_l2_bench tools/neon_l2_bench.c -lm && /tmp/neon_l2_bench
// On x86 it still compiles and runs the correctness check (NEON path falls back to scalar) but the
// speedup is only meaningful on aarch64.

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

// ----------------------------------------------------------------------------
// SCALAR — byte-for-byte the current ARM fallback (hnswlib space_l2.h L2Sqr).
// ----------------------------------------------------------------------------
static float L2Sqr_scalar(const void *pVect1v, const void *pVect2v, const void *qty_ptr) {
    float *pVect1 = (float *)pVect1v;
    float *pVect2 = (float *)pVect2v;
    size_t qty = *((size_t *)qty_ptr);
    float res = 0;
    for (size_t i = 0; i < qty; i++) {
        float t = *pVect1 - *pVect2;
        pVect1++;
        pVect2++;
        res += t * t;
    }
    return res;
}

// ----------------------------------------------------------------------------
// NEON — identical to the production patch to space_l2.h (gated on __ARM_NEON).
// 16 floats per loop iteration via 4 independent 128-bit accumulators (4 lanes
// each) to hide FMA latency, then a horizontal add. Residual (qty % 16) handled
// by the scalar tail, mirroring hnswlib's L2SqrSIMD16ExtResiduals structure.
// ----------------------------------------------------------------------------
#if defined(__ARM_NEON)
#include <arm_neon.h>

static float L2SqrSIMD16ExtNEON(const void *pVect1v, const void *pVect2v, const void *qty_ptr) {
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
        float32x4_t d0 = vsubq_f32(vld1q_f32(pVect1), vld1q_f32(pVect2));
        float32x4_t d1 = vsubq_f32(vld1q_f32(pVect1 + 4), vld1q_f32(pVect2 + 4));
        float32x4_t d2 = vsubq_f32(vld1q_f32(pVect1 + 8), vld1q_f32(pVect2 + 8));
        float32x4_t d3 = vsubq_f32(vld1q_f32(pVect1 + 12), vld1q_f32(pVect2 + 12));
        sum0 = vfmaq_f32(sum0, d0, d0);
        sum1 = vfmaq_f32(sum1, d1, d1);
        sum2 = vfmaq_f32(sum2, d2, d2);
        sum3 = vfmaq_f32(sum3, d3, d3);
        pVect1 += 16;
        pVect2 += 16;
    }
    float32x4_t sum = vaddq_f32(vaddq_f32(sum0, sum1), vaddq_f32(sum2, sum3));
    return vaddvq_f32(sum);  // AArch64 horizontal add across the 4 lanes
}

// dim % 16 == 0  -> pure 16-ext; otherwise 16-ext over the aligned prefix + scalar tail.
static float L2SqrNEON(const void *pVect1v, const void *pVect2v, const void *qty_ptr) {
    size_t qty = *((size_t *)qty_ptr);
    size_t qty16 = (qty >> 4) << 4;
    float res = L2SqrSIMD16ExtNEON(pVect1v, pVect2v, &qty16);
    if (qty16 == qty) return res;
    size_t qty_left = qty - qty16;
    float *p1 = (float *)pVect1v + qty16;
    float *p2 = (float *)pVect2v + qty16;
    return res + L2Sqr_scalar(p1, p2, &qty_left);
}
#else
// Non-ARM: keep the program buildable; NEON name aliases scalar (no SIMD speedup expected).
static float L2SqrNEON(const void *a, const void *b, const void *q) { return L2Sqr_scalar(a, b, q); }
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
    printf("# neon_l2_bench  ISA: %s\n", isa);

    // ---- correctness: scalar vs NEON must agree to fp tolerance across dims ----
    // Includes non-multiples of 16 (31, 100, 768 is a multiple of 16; 100 and 31 exercise the tail).
    const int cdims[] = {1, 3, 4, 15, 16, 31, 32, 96, 100, 128, 384, 768};
    int nfail = 0;
    printf("\n## correctness (scalar vs NEON)\n");
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
            float s = L2Sqr_scalar(a, b, &dim);
            float n = L2SqrNEON(a, b, &dim);
            float denom = fabsf(s) > 1e-6f ? fabsf(s) : 1.0f;
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
        // ~256 MB of vectors worth of calls, sized so total work ~ const across dims.
        long calls = (long)(2.0e8 / (double)dim);
        if (calls < 100000) calls = 100000;

        // distinct random vectors so the prefetcher/branch predictor see realistic data.
        const int NV = 256;
        float *va = malloc(sizeof(float) * dim * NV);
        float *vb = malloc(sizeof(float) * dim * NV);
        for (size_t i = 0; i < dim * NV; i++) {
            va[i] = frand() * 10.0f;
            vb[i] = frand() * 10.0f;
        }

        volatile float sink = 0.0f;  // defeat dead-code elimination

        // scalar
        double t0 = now_sec();
        for (long c = 0; c < calls; c++) {
            float *a = va + (size_t)(c & (NV - 1)) * dim;
            float *b = vb + (size_t)(c & (NV - 1)) * dim;
            sink += L2Sqr_scalar(a, b, &dim);
        }
        double t_scalar = now_sec() - t0;

        // neon
        t0 = now_sec();
        for (long c = 0; c < calls; c++) {
            float *a = va + (size_t)(c & (NV - 1)) * dim;
            float *b = vb + (size_t)(c & (NV - 1)) * dim;
            sink += L2SqrNEON(a, b, &dim);
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
