# CSR-lite gate-(b) — real disk-pressure seek measurement (DGX Spark, 2026-07-16)

**Verdict: gate-(b) PASS.** Under genuine disk pressure the contiguous sorted-extent layout shows a
MATERIAL wall-clock win (~2.9-3.6x on full-hub / mega-hub scans) over the scattered page-chain
baseline. This is the one number docs/graph_store_csr_lite_v0.1.0.md §8.1 left unmeasured (a warm
x86 cache could not show the seek component). It moves CSR-lite from STILL-INCONCLUSIVE-leaning-GO
to **GO** — with one quantified cost (below).

## Regime (real, not warm)

- Corpus: V=5400, 5000 hubs x deg-65408 (64 pages each) + 2 mega hubs x deg-511000 (500 pages) +
  398-vertex low-degree band = 328,068,368 edges, loaded round-robin so the page-chain baseline
  scatters each hub across ~315,000 blocks while the sorted layout keeps each extent consecutive.
- Cold cache via posix_fadvise(DONTNEED) over the whole data dir before each round; measurement
  container cgroup-bounded (initially 2GB; 2GB OOM-killed the sorted scan, so re-run at **4GB** —
  still >=10x smaller than the sorted 40GB / baseline 10GB working set, so the seek regime holds).
- cgroup io.stat confirms REAL reads: each cold round adds ~620MB rbytes / ~19,000 read-ios of
  genuine NVMe traffic. The final WARM round (fadvise skipped) adds ZERO rbytes/rios (fully cached,
  k5 drops to 0.045ms) — proving the cold rounds were genuinely cold.

## Results (median of 3 cold A/B rounds; ms and page-reads)

| Metric (cold) | page-chain baseline | sorted-extent (contiguous) | wall-clock win | page-reads |
|---|---|---|---|---|
| Full-hub scan (deg-65408), ms/scan | ~46.8 | ~16.0 | **2.9x** | 65,472 -> 65 (~1000x) |
| Mega-hub scan (deg-511000), ms | ~450 | ~124 | **3.6x** | 511,500 -> 501 (~1000x) |
| EXISTS present probe, ms | ~24.6 | ~14.7 | **1.7x** | 13,094,400 -> 13,000 (~1000x) |
| k=5 early-term traversal, ms | ~0.32 | ~0.50 | 0.6x (baseline wins) | 5 -> 2 |
| Contiguity (gph_adj_blocks) | span 315,000 `contiguous=f` | span 63/499 `contiguous=t` | — | layout proof |

- **Full-hub / mega scans: the seek win is real and large** (~3x wall-clock) under cold cache —
  the exact signal the warm x86 run could not produce. It comes from BOTH ~1000x fewer page reads
  (read-once-per-page vs re-read-per-neighbor) AND sequential vs scattered block order.
- **Contiguity holds at scale under interleaved load**: sorted extents are consecutive block runs
  (`contiguous=t`, span == npages-1); the baseline scatters across the whole relation
  (`contiguous=f`, span 315,000). Neighbor order is sorted (`nondecreasing=t`) vs insertion (`=f`).
- **k5 early-termination is the one regime the baseline wins** (0.50 vs 0.32ms): scans so tiny
  (2-5 pages) that the sorted layout per-Next overhead dominates the negligible seek. Not the
  target regime — the design targets large-degree hub scans, where it wins ~3x.

## The cost (quantified, honest)

- **~4x on-disk footprint**: sorted data dir = ~40 GB vs baseline ~10.6 GB for the same 328M edges.
  This is the migrate-on-grow relayout write-amplification + orphaned old-extent pages that
  §8.1(e) / §7.2 flagged. The seek win is real, but a production CSR-lite MUST ship the orphan-page
  compaction pass (already named as required) — this bench gives it a size: 4x reclaimable.
- 2GB OOM on the sorted scan (fixed by 4GB) is a harness memory note, not a layout defect: the
  read-once scan buffers a full extent; a deg-511000 mega hub = 500 x 32KB pages held per scan.

## Gate-(b) checklist (docs/graph_store_csr_lite_v0.1.0.md §8.1)

- (a) physical contiguity under interleaved load — DELIVERED (prior run) + reconfirmed at 328M scale.
- (b) real disk pressure where the seek penalty is real — **DELIVERED HERE** (cold cache, working
  set >>bound, io.stat-confirmed reads): ~2.9-3.6x wall-clock win on hub/mega scans. **THE GATE.**
- (c) read-once-per-page scan — DELIVERED (65,472 -> 65 page reads).
- (d) bulk-load within noise + FR-7 — prior run.
- (e) crash-atomicity of merge/relayout + orphan compaction — STILL a production to-do; this run
  quantifies the orphan cost at ~4x footprint.

## Recommendation

Gate (b) is met: **GO** on CSR-lite for the large-degree-hub regime, contingent on the §8.1(e)
orphan-page compaction + relayout crash-atomicity work (now scoped by the 4x footprint number).
Raw logs: ~/csrbench/logs/rounds4g.log (+ rounds4g_summary.txt); harness ~/csrbench/harness/.
