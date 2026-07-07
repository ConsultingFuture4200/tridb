# Hybrid GPU+CPU wiki embedding with a shared unified-memory checkpoint — v0.1.0

**Date:** 2026-07-06
**Host:** DGX Spark — NVIDIA GB10 (Grace 20-core ARM64 + Blackwell GPU), 128 GB
coherent unified memory. `ssh spark`.
**Tool:** `tools/wiki_embed_hybrid.py`
**Issue:** DEV-1354 (offline-wiki). **Corpus:** `data/wiki/enwiki` (7,189,653 articles).
**Model:** `BAAI/bge-small-en-v1.5` (384-dim), title + leading text[:512], L2-normalized.

## TL;DR

A prior CPU-only embed of all 7.19M enwiki articles took >12h with **zero visibility**
(block-buffered stdout to a file) and **no checkpoint** — it could be neither observed
nor resumed. This redoes it right:

- **Hybrid** — 1 GPU worker (torch cuda) + N CPU workers (torch cpu), same model, pulling
  shards off one shared work queue.
- **Explicit shared checkpoint on unified memory** — all workers `mmap` the *same*
  `vectors.f32` file; on the GB10's coherent pool those pages are physically shared, so
  worker writes are zero-copy into one array. This is the unified-memory primitive, used
  for real.
- **Resumable + crash-safe** — per-shard `shards.done` markers; kill and re-run continues
  where it left off (proven below).
- **Full visibility** — a flushed heartbeat every ~10s + a `progress.json` rewritten every
  tick, pollable from off-box via `ssh spark cat .../progress.json`.

## Architecture

```
                         shared work queue (undone shard indices)
                                    |
        +---------------------------+--------------------------+
        |                    |                |                |
   GPU worker           CPU worker 1     CPU worker 2  ...  CPU worker N
   torch cuda           torch cpu        torch cpu          torch cpu
        |                    |                |                |
        +----------- all mmap the SAME files (r+) ------------+
                                    |
        vectors.f32 (N,dim) f32  |  ids.i64.npy (N,)  |  shards.done (n_shards,)
                                    |
             monitor (main proc) --> progress.json + stdout heartbeat
```

- **Shard = unit of work.** N rows are split into `ceil(N/shard_size)` contiguous shards.
  Global row order is the concatenation of the manifest's `articles-*.jsonl` files, so
  row `i` is deterministic and pinned. A worker reads its shard's rows directly
  (`islice` into the covering file(s), spanning boundaries), embeds them, writes the
  slice, and marks the shard done.
- **One GPU worker owns the only CUDA context.** Start method is **`spawn`** (never fork
  after CUDA init). CPU workers are separate processes (torch cpu).
- **Greedy pull, no static partition.** Each worker loops `queue.get_nowait()` until empty.
  The fast GPU naturally drains the majority of shards; the slow CPU workers each finish
  whatever they grabbed. No coordinator, no rebalancing.
- **CPU thread budget.** N torch CPU processes must not each grab all 20 cores — unbounded,
  they oversubscribe and stall (measured: 4 unbounded CPU workers made *zero* progress in
  7 min against a contended run queue). Each CPU worker calls
  `torch.set_num_threads((cores-2)//cpu_workers)`; 2 cores are reserved for the GPU
  worker's host-side tokenization + the monitor.

## Checkpoint format (in `--out`, default `data/wiki/enwiki/emb/`)

| File | Type | Meaning |
|---|---|---|
| `vectors.f32` | raw `np.memmap` float32 `(N, dim)` | the shared vector array; row `i` = article `ids[i]`. Not a `.npy` — headerless, so the offset math is trivial and every process maps it identically. |
| `ids.i64.npy` | `.npy` int64 `(N,)` | row → article id. Initialized to `-1` (unfilled sentinel); a worker writes its slice when it embeds the shard. |
| `shards.done` | raw `np.memmap` uint8 `(n_shards,)` | `1` = shard fully embedded **and flushed**. The atomic per-shard completion marker and the sole source of truth for resume. |
| `meta.json` | json | `{model, dim, N, shard_size, n_shards, normalized:true, status, created[, completed]}`. `status` is `running` during the run, `complete` after verify passes. |
| `progress.json` | json | rewritten every tick: `{total, done_shards, n_shards, done_rows, rate_rows_s, gpu_rows, cpu_rows, eta_seconds, updated_at}`. The external visibility handle. |

`gpu_rows`/`cpu_rows` are **this-session** counters (rows embedded since the current
process started); on a resume they start at 0 while `done_rows`/`done_shards` reflect the
full on-disk state. `rate_rows_s` and `eta_seconds` are computed from this session's
progress against wall time.

## Resume protocol

1. On startup, if `--resume` (default) and an existing checkpoint's `meta.json` matches
   `(N, dim, shard_size)`, reopen the files in place; else allocate fresh.
2. Read `shards.done`; enqueue only shards with `done[i] == 0`.
3. Workers embed only queued shards. Each shard, in order: write `vectors[s:e]`, write
   `ids[s:e]`, `flush()` both, **then** set `done[idx]=1` and `flush()`. Marking done
   *last* guarantees a done shard's data is durable — a crash re-does at most the in-flight
   shards, and re-doing is idempotent (a shard overwrites its own slice).
4. `--no-resume` ignores an existing checkpoint and starts over.

## Why a shared mmap is the right primitive on the GB10 (and how it maps to `tjs_open`)

On a **discrete** GPU, "GPU worker + CPU workers writing one array" would mean either a
host staging buffer with device→host copies, or per-worker device buffers gathered at the
end — extra copies and coordination. The **GB10 is coherent unified memory**: the CPU and
the Blackwell GPU share one physical pool and one address space. So a single file `mmap`ed
`r+` by every process is literally one array that both device kinds write into, with **no
host↔device copy** and no gather step. The checkpoint *is* the shared buffer; durability
(the file) and sharing (the mapping) are the same object.

This is the same structural bet **ADR-0017** ("CPU/GPU heterogeneous `tjs_open`") makes for
the query path. `tjs_open` welds a dense, data-parallel **vector leg** (GPU-shaped) to a
branchy **graph walk** + serial **FR termination brain** (CPU-shaped). On a discrete GPU a
hybrid operator pays a host↔device copy every frontier round, which erases the benefit; the
GB10's single coherent address space is what removes that copy and makes a fused operator
worth evaluating. This embed tool is a **working, load-bearing instance of that same
pattern at the batch/offline scale**: heterogeneous CPU+GPU workers cooperating over one
shared unified-memory buffer with zero copies. The offline embed proves the memory model
that the online fused operator depends on.

## Measured (Spark, 2026-07-06)

Two bottlenecks were found and fixed while bringing this up; both are load-bearing for the
throughput and are baked into the tool:

1. **Corpus read, not embedding, was the first wall.** Reading a shard with `islice` from
   the file start re-scans every preceding line on every shard — a shard at local offset
   95k read at **3,087 rows/s** vs **21,172 rows/s** at offset 0. Fix: each worker caches
   the covering jsonl file (parse once, slice many); reads stop being the bottleneck.
2. **fp32 GPU forward was the second wall.** bge-small on real enwiki text (title +
   text[:512], ~220 tokens after tokenization — tokenization itself is cheap at ~4,500/s
   and is *not* the limit) runs at only **180 art/s fp32** on the GB10 GPU. **fp16
   (`model.half()`) → 463 art/s, a 2.6x win** at negligible cost (L2-normalized vectors,
   fp16 rounding ~1e-3, immaterial for cosine ANN). fp16 is on by default (`--no-fp16` to
   disable). Note the older `spark_gpu_path_findings` 1,301 art/s figure was `all-MiniLM`,
   a different/faster model — bge-small on full-length text is genuinely slower.

**CPU embedding is slow on this Grace part** — even with a bounded thread budget each CPU
worker sustains only ~10–15 art/s (bge fp32 forward on CPU). CPU is a *minor* contributor,
not co-equal, and adding many CPU workers *hurts*: 6 CPU workers starved the GPU worker's
host-side pipeline and dropped net throughput. **The full run therefore uses `--cpu-workers 2`**
— enough to exercise + prove the hybrid shared-write, not enough to rob the GPU.

3. **Process hygiene mattered more than expected.** Multiprocessing **spawn** children do
   NOT carry the module name in their cmdline, so `pkill -f wiki_embed_hybrid` kills only
   the launcher and leaves the workers as orphans (reparented to init) that keep running
   against the same checkpoint. Several stale generations silently accumulated during
   bring-up and starved every throughput measurement (an early "fp16 = 463 art/s" was
   taken under load-average 90+). Fix: each worker checks `os.getppid() == 1` at the top of
   its loop and exits on reparent, so `kill`-ing the launcher stops the pool within one
   shard. On a *clean* box the real numbers are far higher (below).

Steady-state on the full 7.19M run (`--shard-size 5000 --cpu-workers 2`, fp16, clean box):
**~1,500 rows/s (GPU) → ETA ≈ 80 min.** Both devices confirmed live (`gpu_rows` and
`cpu_rows` both > 0). This is ~9x the prior CPU-only run's effective ~166 art/s (>12h) and,
unlike it, fully observable and resumable. CPU adds a low-single-digit % (each worker
~30–60 art/s clean) — its role is to *exercise and prove* the unified-memory shared-write,
not to move the needle on wall time.

Bounded hybrid run (`--limit 80000 --shard-size 4000 --cpu-workers 4`):

```
workers done. gpu_rows=64,000  cpu_rows=16,000     # 80% GPU / 20% CPU, both > 0
verify OK: 80,000 rows finite + unit-norm, ids unique, all shards done
meta.status = complete
```

Resume proof (`--limit 80000 --shard-size 4000 --cpu-workers 2`, killed with `kill -9`
mid-run, then the identical command re-run):

```
# before kill:   shards.done sum = 9/20 persisted on disk
# on restart:
[emb] resuming checkpoint at /tmp/emb_resume
[emb] N=80,000 ... n_shards=20 undone=11 cpu_workers=2 gpu=1
[emb] 9/20 shards  36,000/80,000 rows (45.0%)  gpu=0 cpu=0     # session counters 0 ==
                                                               # done shards NOT re-embedded
# then ran to 20/20, verify OK, status=complete
```

**GPU-idle tail caveat.** Because a CPU worker holds a shard for minutes, the last
CPU-held shards finish *after* the GPU has drained the queue — the GPU sits idle for up to
one CPU-shard-time at the end. Keep `--shard-size` modest so that tail is bounded (a 4,000-
row CPU shard ≈ a few minutes; a 20,000-row CPU shard would be a ~20–30 min idle tail).
This is why the full run uses a smaller shard size than a GPU-only run would want.

## How to run / resume / monitor

Full 7.19M run (background, live-tailable):

```bash
ssh spark 'cd ~/code/tridb && nohup .venv/bin/python -u -m tools.wiki_embed_hybrid \
  --corpus data/wiki/enwiki --out data/wiki/enwiki/emb \
  --shard-size 5000 --cpu-workers 2 \
  > /tmp/emb_hybrid.log 2>&1 & echo $! > /tmp/emb_hybrid.pid'
# fp16 GPU is on by default (2.6x); --cpu-workers stays low (2) so the CPU
# workers prove the hybrid shared-write without starving the GPU pipeline.
```

Monitor (from anywhere):

```bash
ssh spark cat ~/code/tridb/data/wiki/enwiki/emb/progress.json   # structured, pollable
ssh spark tail -f /tmp/emb_hybrid.log                           # live heartbeat
```

Resume after any interruption — **re-run the identical command**. It reads `shards.done`,
skips completed shards, and continues. `--no-resume` forces a fresh start.

Reuse the artifact (cheap link-prediction overlap, no re-embed):

```bash
.venv/bin/python -m tools.wiki_linkpredict --emb-in data/wiki/enwiki/emb --corpus data/wiki/enwiki
```

## Correctness guarantees (asserted by the tool at the end)

- every shard marked done (`shards.done.sum() == n_shards`),
- no unfilled ids (`-1`) and `len(unique(ids)) == N` (full, once-only coverage),
- vectors finite and unit-norm (chunked check over all N rows),
- `meta.status = "complete"` only after all of the above pass.
```
