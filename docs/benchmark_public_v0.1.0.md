# TriDB Public-Dataset Benchmark (GTM make-or-break) v0.1.0

**TL;DR.** The canonical TriDB `tjs()` query, run on the LIVE forked-MSVBASE
engine over a topical graph synthesized on a **recognized public ANN dataset**
(default **gist-960-euclidean**: real GIST descriptors, **dim 960**, **L2**), with
**recall@k graded against an exact numpy top-k oracle** and a **one-command repro**
(`make fetch-dataset && make bench-public`). This is the launch artifact
[[gtm_opensource_v0.1.0]] names as make-or-break — the move that retires the
"synthetic corpus" attack. The recall oracle is computed host-side on the real
embeddings, so **correctness is measurable today**; the **live engine run + its
latency are GX10/stack-gated**, and the **dataset download is network-gated**.
Nothing in this doc claims a live number that was not produced.

## Why this dataset (gist-960-euclidean)

The GTM headline asks for **real embeddings, dim 768+**, and the TriDB canonical
query ranks by **L2** (`<->`, engine `distmethod=l2_distance`). One recognized
ann-benchmarks set satisfies BOTH at once:

| requirement | gist-960-euclidean | why it matters |
|---|---|---|
| recognized | GIST1M is a standard ANN benchmark corpus; ann-benchmarks ships it as a named HDF5 | survives the hostile "is this a real dataset?" question |
| dim 768+ | **960** | the headline target; real high-dim descriptors, not toy vectors |
| L2 distance | **euclidean** | matches the canonical `<->` ordering AND the exact numpy oracle. An *angular* set (e.g. glove-100) would rank by cosine and **disagree** with the L2 oracle — so we deliberately do NOT default to one |

A smaller, also-recognized, also-L2 set — **sift-128-euclidean** (dim 128) — is
pinned too, for a fast pipeline smoke. It is **below** the 768+ headline target;
use it only to exercise the plumbing, and say so. The headline run uses GIST.

Both are pinned (URL + SHA256) in `tools/fetch_dataset.py`, mirroring the
pinned-download discipline the build uses for Boost/CMake
(`scripts/lib/msvbase_patches.sh`). See "Pinning / first fetch" below for the
honest state of the checksum constants.

## The tuned baseline (and "beat it")

The comparison baseline is the real multi-store stack people actually run —
**Milvus + Neo4j + Postgres**, merged app-side (`baseline/`) — and it is **tuned,
with the configs committed** (`baseline/TUNING.md`): IVF_FLAT `nlist=128` /
`nprobe=64` (a deliberately high-recall operating point), a `k*32` ANN over-fetch
(the intrinsic multi-store penalty, set generously so the baseline does not lose
on under-fetch), an indexed Neo4j 1-hop, and a Postgres B-tree on the timestamp.
Every value is a constant in `baseline/sm2.py` — no hidden config. **If you can
tune it faster, the configs are in the repo: change them, re-run, send the diff.**
That committed-and-contestable config is the counter to the "strawman baseline"
attack.

## One-command repro

```bash
make fetch-dataset      # download + SHA256-verify the pinned public dataset (network-gated)
make bench-public       # run the canonical query LIVE over it, grade recall@k (engine-gated)
```

`make fetch-dataset` downloads the pinned dataset into `data/public/` (gitignored)
and verifies its checksum. `make bench-public` then:

1. guards that the dataset is present (else it tells you to `make fetch-dataset`),
2. guards on the engine image (the live run is GX10/stack-gated; off-target it
   raises a clear "engine-gated" message instead of fabricating a number),
3. generates the canonical `#BENCH` SQL + an **exact numpy oracle** over the real
   embeddings (`tools/real_corpus.py` — the SAME emitter the synthetic path uses,
   so the engine sees an identical surface),
4. runs it on the live engine in one container (build `graph_store_ext`, load the
   corpus, run the canonical query capturing `#BENCH`/EXPLAIN lines), and
5. grades the live `tjs()` answer set against the oracle
   (`real_corpus.report_recall`), writing `bench/results/bench_public_*`.

Knobs (env, defaults are a bounded smoke slice; raise for the headline):

```bash
# Headline (GTM gate — run on a quiet GX10): 100k real GIST vectors, dim 960.
PUBLIC_LIMIT=100000 make bench-public

# Fast pipeline smoke on the smaller L2 set:
PUBLIC_DATASET=sift-128-euclidean make fetch-dataset
PUBLIC_DATASET=sift-128-euclidean PUBLIC_LIMIT=20000 make bench-public
```

`PUBLIC_LIMIT` takes the first N rows of the (1M-row) public train matrix so a
shared-box run is bounded; `PUBLIC_LIMIT=0` uses the whole set. Deterministic via
`PUBLIC_SEED` (default 42) — the real vectors are fixed by the file, the graph /
queries are seeded.

## What is measured today vs gated

| Aspect | State | Where |
|---|---|---|
| Exact recall **oracle** over real public embeddings | **Measurable today, no engine** (numpy top-k) | `tools/real_corpus.py: exact_oracle`, unit-tested |
| recall@k **grading** of a result set vs the oracle | **Measurable today, no engine** | `report_recall`, unit-tested |
| Canonical `#BENCH` SQL emission (drop-in) | **Measurable today** | `tools/real_corpus.py: emit` |
| Dataset **download** + checksum verify | **NETWORK-gated** (never run by tests/CI) | `tools/fetch_dataset.py` |
| **Live** `tjs()` answer set + recall@k on the engine | **GX10/stack-gated** | `scripts/bench_public.sh` (image-guarded) |
| **Latency** (EXPLAIN ANALYZE Execution Time) | **GX10/stack-gated** | live transcript `bench/results/bench_public_raw.txt` |
| Fair multi-store **SM-2** head-to-head on this corpus | **GX10/stack-gated** | `make sm2` + `make baseline-up` + `baseline/TUNING.md` |
| The **at-scale** 100k/dim-960 headline curve | **GX10-gated** | `PUBLIC_LIMIT=100000 make bench-public` |

The honest line, matching [[gtm_opensource_v0.1.0]]: the *mechanism + the
correctness oracle on a recognized real dataset* are real and reproducible here;
the *live latency at the operating point* and the *fair multi-store head-to-head*
are the gated headline items, run on the GX10 with the stack up. Report recall as a
curve across `term_cond` (per [[benchmark_neon_sweep_v0.1.0]] / R1), never a bare
peak — and never quote a latency or recall figure this scaffold did not produce.

## Pinning / first fetch (honest note)

The SHA256 constants in `tools/fetch_dataset.py` are **sentinels (`_PENDING`)**
until a first real fetch records them — this scaffold was authored offline, and
fabricating a hash would be worse than admitting it is unset. The first fetch on a
networked box must resolve the pin:

```bash
# print the observed digest, then paste it into REGISTRY[...]:
python3 -m tools.fetch_dataset --dataset gist-960-euclidean --pin
```

After the constant is pinned and committed, every subsequent fetch verifies
against it with no escape (a mismatch is fatal) — the same "pin once, verify
forever" contract `msvbase_patches.sh` uses for build downloads. A one-time
`--allow-unpinned` escape exists for an emergency fetch but warns loudly and
skips verification; prefer `--pin`.

## Files

- `tools/fetch_dataset.py` — pinned-dataset registry + verified download (network-gated).
- `tools/real_corpus.py` — ann-benchmarks `.hdf5` loader, topical-graph synthesis,
  exact oracle, canonical `#BENCH` SQL, recall grading (`--limit` for a bounded slice).
- `scripts/bench_public.sh` / `make bench-public` — the live repro (dataset + engine guarded).
- `make fetch-dataset` — the download step.
- `baseline/TUNING.md` — committed tuned baseline config + "beat it".
- `tests/test_fetch_dataset.py` — offline unit tests (registry, checksum, .hdf5
  loader, oracle, recall grading; h5py is optional via `importorskip`, no network).
