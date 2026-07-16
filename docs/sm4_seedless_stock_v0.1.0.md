# SM-4 on stock PG — the seedless recall surface of the re-homed operator (1M, measured)

**What this is.** The (term_cond × hnsw.max_scan_tuples) recall surface for `tjs_pg`'s
vector-first path (ADR-0019) at N=1,002,331 — the budget-shaped-recall measurement ADR-0015
E3.3 said must replace the fork's term_cond-only curve. Filtered ANN (typed P31 filter,
moderate selectivity), 50 seeded queries, exact oracle, client-clocked over TCP, stock PG 17 +
pgvector on the DGX Spark. Raw: `bench/results/wd_1m_sm4_seedless.json`.

| term_cond | budget 1k | 5k | 20k | 80k | examined (med) | latency @20k |
|---|---|---|---|---|---|---|
| 16  | 0.750 (14% capped) | 0.800 | 0.830 (4% capped) | 0.830 | 46 | 0.8 ms |
| 64  | 0.752 (18% capped) | 0.808 | 0.840 (6% capped) | 0.840 | 140 | 1.5 ms |
| 256 | 0.752 (42% capped) | 0.814 | 0.850 (18% capped) | 0.850 | 868–1014 | 11.2 ms |

**Honest findings.**

1. **Recall is budget-shaped at the low end, exactly as E3.3 predicted** — every term_cond row
   rises with budget and saturates at 20k tuples; the capped fraction (disclosed per point)
   explains the low-budget loss.
2. **Past saturation the ceiling is the relaxed-order stream itself, not the budget**: 80k
   buys nothing over 20k. The seedless plateau on this workload is **~0.83–0.85 recall@10** —
   the 20k-corpus ADR-0015 probe's 0.965 does not survive at 1M with typed filters.
3. **term_cond has steep diminishing returns**: 16→256 buys +0.02 recall for ~14× the examined
   work and ~14× the latency. The operating sweet spot is term_cond 16–64 at budget ≥20k:
   0.83–0.84 recall@10 at **0.8–1.5 ms** (vs the multi-store baseline's ~3.3 ms at its best).
4. **Positioning consequence:** filter-first remains the headline physical path (recall 0.992
   at 0.14 ms, Gate B); seedless on stock PG is *serviceable* (sub-ms to low-ms, ~0.85
   ceiling) and its limits are now measured and public, not hidden. Raising the ceiling means
   pgvector-side work (ef_search interplay, quantized rescoring) or the ADR-0012 stretch
   (PPR-graded bridges) — future work, listed not promised.

Fork phase/bridge parity note: the seedless semantics measured here include the fork's
guaranteed-bridge injection (ADR-0012 recipe B, commit 81b8023) with m_seeds=0 (pure filtered
ANN — the E3 probe shape). Bridge-mode benchmarks belong to the GraphRAG workload suite.
