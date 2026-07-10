# Fusion head-to-head — TriDB `tjs_open` vs a tuned multi-store (v0.1.0)

> **Status: EXECUTED. First speed win attributable to TriDB's actual differentiator** — the
> in-process fused operator (no cross-system round-trips), not HNSW-vs-HNSW. At N=200k, matched
> recall, TriDB's `tjs_open` beats an app-side Milvus→Neo4j→pgvector pipeline at **every** hop
> depth (1.29×–11.5×), **even on loopback** (the multi-store's best case). But the advantage
> **shrinks** as hops deepen — it comes from eliminating fixed cross-system overhead, so it is
> largest on cheap queries, NOT "grows with hops." Compute-regime (dim-384, RAM-resident); this
> is not the I/O-bound thesis (which is structurally unsupported — see the co-location audit).
>
> **Credible-scale (N=1,000,000) update:** the 1M fused head-to-head was attempted and is **BLOCKED** —
> TriDB's vector leg / `tjs_open` does not function at 1M (hang, `examined=0` at a 600 s ceiling; 0/2
> fresh HNSW builds healthy). The batched edge loader (Wall 3) DID validate at 1M (38.99M edges in
> ~35 s, reconciled). No 1M latency number is emitted. See "N=1,000,000 (credible scale)" below.

## TL;DR

Matched recall@10 (~0.997 TriDB vs 1.000 baseline), p50 latency, N=200,000, loopback baseline:

| hop | TriDB `tjs_open` p50 (p95) | Multi-store p50 (p95) | **TriDB speedup** | graph reach / bridges | baseline bytes shipped | samples |
|---|---|---|---|---|---|---|
| 1 | **3.69 ms** (5.05) | 42.57 ms (49.9) | **11.5×** | ~515 | 9.9 KB | 240 (80q×3) |
| 2 | **96.6 ms** (149) | 314.9 ms (508) | **3.26×** | ~21,000 | 324 KB | 240 (80q×3) |
| 3 | **1,729 ms** | 2,227 ms | **1.29×** | ~123,000 | 1.97 MB | 30 (lean) |

hop-1/2 confirmed on a tightened run (80 queries × 3 runs = 240 samples/config, recall matched ~0.995
both sides); the lean 30-query run gave the same magnitudes (11.5× / 3.4×), so the wins are robust.
hop-3 remains a single lean sample (a large-reach stress case, see caveats).

- **TriDB wins at every hop.** This is the first benchmark win in this project attributable to the
  *fused operator* — TriDB avoiding the vector→graph→filter round-trips the multi-store must pay —
  rather than raw ANN (the earlier 2.1× vector-leg win was just HNSW-vs-HNSW).
- **The advantage shrinks with hop depth (11.5× → 3.4× → 1.3×), the OPPOSITE of "grows with hops."**
  Mechanistically clear: TriDB's edge is eliminating *fixed* per-query cross-system overhead
  (3 round-trips + serialization). At hop=1 the query is cheap, so that fixed overhead is nearly the
  whole cost → 11.5×. At hop=3 the intrinsic work (processing ~123k reached/bridge nodes) dominates
  *both* systems, so the fixed saving is a smaller fraction → 1.3×. TriDB still wins by avoiding the
  1.97 MB of cross-store shipping, but the gap narrows.

## N=1,000,000 (credible scale) — attempted, BLOCKED on the vector leg (honest)

**Verdict: the 1M fused head-to-head did NOT execute. TriDB's fused `tjs_open` (and even a plain
ANN top-10) does not function at N=1,000,000 on the current engine build — the vector leg hangs
before the first candidate.** No TriDB operating point exists at 1M, so there is nothing to compare
the multi-store against at matched recall. Per the honesty gate, **no 1M latency number is emitted,
and none is fabricated.** The 200k win above (11.5× / 3.26×) is unaffected and remains the credible
operating point.

### What DID validate at 1M — Wall 3 (the batched edge loader)

The reason we could even attempt 1M is the batched `gph_insert_edges` path, and it **works exactly as
designed at full 1M scale**, on *both* engine images tried:

| stage | `gx10-v1-batchedge` | `gx10-v1-hnswcap` |
|---|---:|---:|
| COPY 1,000,000 articles | 62.8 s | 62.8 s |
| HNSW index build (single-threaded pole) | 691 s (11:31) | 791 s (13:11) |
| dense vertex materialize (1M) | 12.8 s | 13.1 s |
| COPY 38,991,320 edges | 5.5 s | 5.6 s |
| **batched `gph_insert_edges` (all edges)** | **35.1 s** | **34.1 s** |
| reconcile | `gph_edge_count=38,991,320`, `gph_vertex_count=1,000,000` ✓ | same ✓ |

38.99M edges loaded in ~35 s and reconciled byte-for-byte against the manifest-induced count — the
Wall-3 payoff is real. The graph and relational legs load cleanly at 1M; **only the vector leg is
broken.**

### The blocker (measured, reproducible)

- **`tjs_open('articles', 10, 64, 16, 1, …)` at 1M: `examined=0` after a 600 s ceiling** (canceled by
  statement timeout). It never reaches the first candidate — this is a hang, not a slow-but-finishing
  cold `LoadIndex`. The engine's own load-time sample `tjs_open` hung identically (killed after
  3–11 min) on both images.
- **A plain ANN top-10** — `SELECT id FROM articles ORDER BY embedding <-> '{v}' LIMIT 10`, the exact
  form the vector-leg h2h uses — **also blocks** (15 s → timeout). `EXPLAIN` shows the planner puts a
  **blocking `Sort` over ~1,000,000 rows** on top of the `articles_hnsw` index scan: at 1M it no longer
  trusts the HNSW ordering, so the LIMIT can't push into the beam and the scan is forced to full-corpus.
- **2 of 2 fresh HNSW builds** (batchedge, then hnswcap) produced an unusable vector leg. This is the
  **standing `publication_gate` BLOCKER** carried in `bench/wiki_h2h.py` verbatim: *"the HNSW build is
  RANDOMIZED and was observed to hang (examined=0, statement-timeout) on 4 of 5 fresh builds … the
  examined-cap does NOT fix this — the hang is upstream of the first examined++. Root-cause the HNSW
  relaxed-monotonicity / opclass binding in the fork's vector iterator before quoting any TriDB
  latency/recall."* The one healthy 1M vector-leg result on record (`wiki_h2h_vecleg_1m.json`,
  p50≈1.03 ms) came from a *lucky* build; quoting a fusion headline off such a build is exactly the
  cherry-pick the gate forbids (it demands ≥3 healthy of 3 fresh builds). We got 0 of 2 here.

### Does the fusion win hold / grow / shrink at 10× scale?

**Undetermined — the experiment is blocked upstream of the comparison.** The mechanism (eliminating
3 cross-system round-trips + MB of shipped intermediates) has no reason to reverse at 1M, and the
baseline's per-hop shipped bytes only grow with corpus size, so the *expectation* is the advantage at
least holds. But that is a hypothesis, not a measurement, and this document does not claim it. The
credible, executed number remains the **200k** result.

### Honest caveats specific to this attempt

- **Baseline left at 200k.** With no working TriDB operating point at 1M, reloading the isolated
  `tridb-wiki-*` stores (Milvus/Neo4j/pgvector) to 1M would have been pointless memory-heavy work; it
  was skipped. SM-2 baseline untouched.
- **The cold `LoadIndex` "~96 s at 1M" expectation was wrong.** At 200k it is ~95 s; at 1M the
  `tjs_open` vector iterator does not complete a cold load at all (still `examined=0` at 10 min) — it
  is a hang, not a longer-but-finite warm-up.
- **Compute-regime caveat unchanged.** Even were the vector leg healthy, dim-384 f32 at 1M (~1.5 GB) is
  still RAM-resident — the compute regime, not the spec's I/O-bound thesis.

### Path to unblock (future work, not attempted here)

Fix the fork's HNSW vector iterator so the index (a) is reproducibly monotonic at 1M (planner keeps the
index ordering, no full-corpus `Sort`) and (b) `tjs_open`'s beam returns candidates at 1M. Then the
matched-recall harness (`bench/wiki_fusion.py`) runs unchanged at `--n 1000000`. Gate: ≥3 healthy of 3
fresh builds before any 1M headline. Raw evidence: `bench/results/wf1m_blocked.json`.

## Mechanism (what TriDB avoids)

The multi-store pipeline is: Milvus ANN (seed) → **ship seed ids** → Neo4j `*1..h` traversal →
**ship reached ids** → pgvector filter/rank → app-side merge. Instrumented per query:

| hop | round-trips | bytes shipped across store boundaries | reached nodes |
|---|---|---|---|
| 1 | 3 | 9.9 KB | 512 |
| 2 | 3 | 337 KB | 20,982 |
| 3 | 3 | 1.97 MB | 122,995 |

TriDB's `tjs_open` does the same fusion in ONE in-process call over libpq: ANN seeds from the
`vectordb` HNSW leg → native `graph_store` BFS from all seeds → vector-ranked merge with bridge
injection + early termination — **0 bytes shipped between systems, 1 round-trip**. That eliminated
per-query overhead is the entire source of the win.

## Real-network split — the loopback numbers UNDERSTATE the win (measured)

Loopback (all systems co-located) is the multi-store's best case. We re-ran with the **harness on a
separate x86 box, and the TriDB engine + all three stores on the Spark box** — so every call
(TriDB's single `tjs_open` *and* each of the baseline's 3 store round-trips) crosses the same LAN at
equal distance. Same 200k data, same matched-recall protocol (80q×3 runs):

| hop | loopback speedup | **real-network speedup** | TriDB p50 (loop → net) | baseline p50 (loop → net) |
|---|---|---|---|---|
| 1 | 11.5× | **16.7×** | 3.69 → 3.55 ms (flat) | 42.6 → **59.1 ms** |
| 2 | 3.26× | **10.6×** | 96.6 → 90.8 ms (flat) | 314.9 → **960.8 ms** (3×) |

TriDB's latency is **unchanged** — its single call barely notices one network hop. The baseline
**balloons** — most at hop-2, where it ships ~324 KB of reached ids across 3 real round-trips (on
loopback that shipping was ~free). So the advantage widens from 3.3–11.5× to **10.6–16.7×**. Since
real multi-store deployments *are* distributed (the three engines rarely share a host at scale),
the real-network numbers are arguably the more representative ones — and a higher-RTT network
(cross-rack / cross-region / cloud) would widen the gap further still. (`wf200k_realnet.json`.)

## Method

- **Matched scale:** N=200,000 articles, 8,208,179 induced edges, loaded identically into the engine
  (`tridb/msvbase:gx10-v1-hnswcap`, port 5447) and the isolated baseline (Milvus :19531 dim-384
  HNSW/COSINE, Neo4j :7688 200k nodes / 8.2M rels, pgvector :5434 200k rows). Counts reconcile.
- **Matched recall:** for each hop, both sides swept (TriDB m_seeds/term_cond grid; baseline
  seeds×ef grid) and compared ONLY at equal recall@10 vs a per-query exact fused oracle
  (Milvus-equivalent ANN ∪ exact h-hop graph reach). recall_matched=true each hop.
- **Timer parity:** both client-timed over TCP (psycopg to the engine; pymilvus/neo4j/psycopg to the
  stores). Warm: the TriDB HNSW index was warmed once (cold `LoadIndex` rebuild = **~96 s** at 200k,
  disclosed — the multi-store has no equivalent cold load).
- Harness: `bench/wiki_fusion.py`; raw results `bench/results/wf200k_lean.json`.

## Honest caveats

- **Sample size:** hop-1/2 are confirmed at 240 samples/config (80q×3 runs, `wf200k_tight.json`) and
  match the initial 30-query lean run (`wf200k_lean.json`) — the wins are robust. hop-3 is still a
  single 30-query lean sample (the `*1..3` baseline traversal makes a high-stat hop-3 sweep cost
  hours). Note: a full 8-config × 200q × 5-run sweep was attempted but SIGKILL'd twice mid-baseline —
  transient memory spikes (Milvus `col.load()` colliding with the 13.4 GB reader under an aggressive
  OOM policy on the shared box); the reduced-grid tightened run above completed cleanly. A dedicated,
  reader-free box would allow the full high-stat sweep.
- **Loopback favors the baseline — now measured.** All three stores on localhost = the multi-store's
  best case; the headline table uses that (conservative) setup. The real-network split (section above)
  confirms the prediction: TriDB's advantage *grows* to 10.6–16.7× when the baseline pays real
  round-trips + wire-shipping. Both are reported; loopback is the conservative floor. NOTE the
  real-network test used a single fast LAN — higher-RTT networks would widen it further.
- **Compute regime, dim-384, RAM-resident.** Not the spec's I/O-bound thesis (which the co-location
  audit found structurally unsupported). This measures the *fusion* mechanism, not page locality.
- **hop-3 is a large-intermediate stress case** (reach ~123k ≈ 61% of the corpus). Realistic 2–3-hop
  wiki QA rarely reaches that far; the hop-1/hop-2 numbers are the more representative operating points
  (and where TriDB's advantage is largest anyway).
- **Cold-start asymmetry:** TriDB pays a ~96 s one-time `LoadIndex` per cold backend (DEV-1235); the
  multi-store does not. Warm steady-state is compared above; the cold cost is disclosed, not hidden.

## Verdict

The fusion thesis gets its **first real support**: TriDB's in-process fused retrieval beats a tuned
multi-store at matched recall across all hop depths, even on the baseline's best case, and the
mechanism (eliminated cross-system round-trips / MB of shipped intermediates) is directly measured.
This is a genuine, differentiator-attributable win — distinct from the earlier HNSW-vs-HNSW result.

The nuance to carry honestly: the win is **largest on cheap/shallow queries and narrows as the
intrinsic work grows** — TriDB removes fixed overhead, it does not make deep graph traversal cheap.
So the pitch is "fused single-engine retrieval is materially faster than orchestrating three stores,
especially at interactive query sizes" — plus the standing ADR-0017 value (one-WAL consistency +
operational simplicity), which is independent of and complementary to this latency result.
