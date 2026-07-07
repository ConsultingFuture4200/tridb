"""Hybrid GPU+CPU embedding of the offline-wiki corpus with a shared, resumable
checkpoint on unified memory (DEV-1354).

WHY THIS EXISTS
---------------
A prior CPU-only run embedded all 7.19M enwiki articles in >12h with ZERO
visibility (block-buffered stdout) and NO checkpoint, so it could neither be
observed nor resumed. This tool redoes it RIGHT on the DGX Spark (GB10):

  * HYBRID execution — one GPU worker (torch cuda) + N CPU workers (torch cpu),
    same model, pulling shards from a shared work queue. The GPU naturally does
    the majority; the CPU workers contribute in parallel.
  * EXPLICIT SHARED CHECKPOINT — vectors live in a single np.memmap file. On the
    GB10's *coherent unified memory* (CPU + Blackwell GPU share one physical
    pool), every worker process maps the SAME file, so the mmap pages are
    physically shared and writes are zero-copy. This is the "explicit shared
    checkpoint on unified memory" primitive.
  * RESUMABLE + CRASH-SAFE — per-shard `shards.done` markers. Kill the process
    and re-run; it skips completed shards and continues. A shard is marked done
    only AFTER its vectors+ids are flushed, so a crash re-does at most the
    in-flight shards (idempotent — they overwrite their own slice).
  * FULL VISIBILITY — a line-buffered heartbeat every ~10s to stdout AND a
    rewritten progress.json (pollable via `ssh spark cat .../progress.json`).

CHECKPOINT FORMAT (in --out, default data/wiki/enwiki/emb/)
  vectors.f32   raw np.memmap float32 (N, dim)   — the shared array
  ids.i64.npy   int64 (N,) .npy                  — row i -> article id (-1 = unfilled)
  shards.done   np.memmap uint8 (n_shards,)      — 1 = shard embedded+flushed
  meta.json     {model, dim, N, shard_size, normalized, status, created}
  progress.json rewritten every few seconds      — external visibility handle

SCOPE / HARDWARE
  Developed and measured ON THE SPARK (GB10, aarch64+CUDA, 128 GB unified). The
  GPU path is torch's default PyPI aarch64 wheel (== cu130); onnxruntime has no
  aarch64 GPU wheel there, so torch is used on BOTH devices (identical model =>
  consistent vectors). See docs/spark_gpu_path_findings_v0.1.0.md and
  docs/wiki_embed_hybrid_design_v0.1.0.md.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import queue
import time
from itertools import islice
from pathlib import Path

import numpy as np

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384
TEXT_CHARS = 512  # leading body chars fed to the encoder (matches wiki_linkpredict)


# --------------------------------------------------------------------------- #
# Corpus layout: global row order == concatenation of manifest article shards.
# --------------------------------------------------------------------------- #
def shard_file_layout(corpus: Path) -> tuple[list[tuple[str, int, int]], int]:
    """Return ([(path, global_start, nrows), ...], total_rows) in manifest order."""
    manifest = json.loads((corpus / "manifest.json").read_text())
    files = manifest["shards"]["articles"]["files"]
    layout: list[tuple[str, int, int]] = []
    pos = 0
    for f in files:
        layout.append((f["path"], pos, int(f["rows"])))
        pos += int(f["rows"])
    return layout, pos


def _embed_text(title: str, body: str) -> str:
    """Title anchors the entity the graph links on; leading body adds context."""
    return f"{title}. {body[:TEXT_CHARS]}".strip()


def read_rows(
    corpus: Path,
    layout: list[tuple[str, int, int]],
    start: int,
    end: int,
    cache: dict[str, list[tuple[int, str]]] | None = None,
) -> tuple[list[int], list[str]]:
    """Read global rows [start, end) -> (ids, embed_texts), spanning files.

    With `cache` (a dict a worker keeps across shards), the covering jsonl file is
    parsed ONCE into (id, text) rows and sliced — successive shards in the same
    file hit the cache. Without it, the file is scanned with islice (fine for the
    one-off reads in tests). The cache is essential at scale: islice re-scans from
    the file start on every shard, so a shard at local offset 95k reads at ~3k
    rows/s vs ~21k rows/s cached — the difference between a ~5h and a ~2h run."""
    ids: list[int] = []
    texts: list[str] = []
    for path, fstart, nrows in layout:
        fend = fstart + nrows
        if fend <= start or fstart >= end:
            continue
        lo = max(start, fstart) - fstart
        hi = min(end, fend) - fstart
        if cache is not None:
            if path not in cache:
                cache.clear()  # keep only one file resident (~150 MB)
                rows: list[tuple[int, str]] = []
                with (corpus / path).open() as fh:
                    for line in fh:
                        obj = json.loads(line)
                        rows.append(
                            (
                                int(obj["id"]),
                                _embed_text(obj["title"], obj.get("text", "")),
                            )
                        )
                cache[path] = rows
            for i, t in cache[path][lo:hi]:
                ids.append(i)
                texts.append(t)
        else:
            with (corpus / path).open() as fh:
                for line in islice(fh, lo, hi):
                    obj = json.loads(line)
                    ids.append(int(obj["id"]))
                    texts.append(_embed_text(obj["title"], obj.get("text", "")))
    return ids, texts


# --------------------------------------------------------------------------- #
# Checkpoint open/create
# --------------------------------------------------------------------------- #
def _paths(out: Path) -> dict[str, Path]:
    return {
        "vectors": out / "vectors.f32",
        "ids": out / "ids.i64.npy",
        "done": out / "shards.done",
        "meta": out / "meta.json",
        "progress": out / "progress.json",
    }


def create_checkpoint(out: Path, n: int, dim: int, shard_size: int, model: str) -> None:
    """Allocate the shared checkpoint files (fresh run)."""
    out.mkdir(parents=True, exist_ok=True)
    p = _paths(out)
    n_shards = math.ceil(n / shard_size)
    # vectors: raw memmap, allocated (sparse) full size.
    np.memmap(p["vectors"], dtype=np.float32, mode="w+", shape=(n, dim)).flush()
    # ids: proper .npy memmap, init to -1 (unfilled sentinel).
    ids = np.lib.format.open_memmap(p["ids"], mode="w+", dtype=np.int64, shape=(n,))
    ids[:] = -1
    ids.flush()
    del ids
    # shards.done: uint8 markers, init 0.
    done = np.memmap(p["done"], dtype=np.uint8, mode="w+", shape=(n_shards,))
    done[:] = 0
    done.flush()
    del done
    p["meta"].write_text(
        json.dumps(
            {
                "model": model,
                "dim": dim,
                "N": n,
                "shard_size": shard_size,
                "n_shards": n_shards,
                "normalized": True,
                "status": "running",
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            indent=2,
        )
    )


def checkpoint_matches(out: Path, n: int, dim: int, shard_size: int) -> bool:
    p = _paths(out)
    if not all(p[k].exists() for k in ("vectors", "ids", "done", "meta")):
        return False
    m = json.loads(p["meta"].read_text())
    return m.get("N") == n and m.get("dim") == dim and m.get("shard_size") == shard_size


# --------------------------------------------------------------------------- #
# Worker (spawned; owns its own model + memmap handles)
# --------------------------------------------------------------------------- #
def _worker(cfg: dict, device: str, work_q, gpu_rows, cpu_rows) -> None:
    import numpy as _np  # local import keeps the spawn payload tiny
    import torch
    from sentence_transformers import SentenceTransformer

    # CPU workers must NOT each grab every core: N torch processes on 20 cores
    # oversubscribe and each runs ~Nx slower (measured: 4 unbounded CPU workers
    # made zero progress in 7 min). Bound each CPU worker's intra-op threads so
    # the pool sums to the reserved core budget. The GPU worker keeps 1 (it is
    # bound by the device, not CPU BLAS).
    if device == "cpu":
        torch.set_num_threads(max(1, int(cfg["cpu_threads"])))

    corpus = Path(cfg["corpus"])
    out = Path(cfg["out"])
    dim = cfg["dim"]
    n = cfg["n"]
    shard_size = cfg["shard_size"]
    batch = cfg["batch"]
    layout = cfg["layout"]
    p = _paths(out)

    model = SentenceTransformer(cfg["model"], device=device)
    # fp16 on the GPU: bge-small forward on ~220-token real-wiki sequences is
    # fp32-forward-bound (measured 180 art/s fp32 -> 463 art/s fp16, 2.6x) — the
    # single biggest lever here. Embedding vectors are L2-normalized and used for
    # cosine ANN; the fp16 rounding is ~1e-3, immaterial for recall. CPU stays
    # fp32 (CPU fp16 is unsupported/slow), so GPU- and CPU-embedded rows differ at
    # the fp16 level — a documented, negligible inconsistency.
    if device == "cuda" and cfg["fp16"]:
        model.half()
    vectors = _np.memmap(p["vectors"], dtype=_np.float32, mode="r+", shape=(n, dim))
    ids_mm = _np.lib.format.open_memmap(p["ids"], mode="r+")
    done = _np.memmap(p["done"], dtype=_np.uint8, mode="r+")
    counter = gpu_rows if device == "cuda" else cpu_rows
    file_cache: dict[str, list[tuple[int, str]]] = {}  # one jsonl file resident

    while True:
        # If the launcher was killed, spawn workers get reparented to init (ppid 1)
        # and would otherwise keep running as orphans (their cmdline is the generic
        # multiprocessing spawn stub, so `pkill -f wiki_embed_hybrid` misses them).
        # Exit on reparent so `kill`-ing the main process cleanly stops the pool
        # within one shard. Resume then picks up any shard left in-flight.
        if os.getppid() == 1:
            break
        try:
            idx = work_q.get_nowait()
        except queue.Empty:
            break
        start = idx * shard_size
        end = min(start + shard_size, n)
        shard_ids, texts = read_rows(corpus, layout, start, end, file_cache)
        vecs = model.encode(
            texts,
            batch_size=batch,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).astype(_np.float32)
        vectors[start:end] = vecs
        ids_mm[start:end] = _np.asarray(shard_ids, dtype=_np.int64)
        vectors.flush()
        ids_mm.flush()
        done[idx] = 1  # mark done LAST, after data is durable
        done.flush()
        with counter.get_lock():
            counter.value += end - start


# --------------------------------------------------------------------------- #
# Monitor (main process): heartbeat + progress.json
# --------------------------------------------------------------------------- #
def _shard_rows(idx: int, shard_size: int, n: int) -> int:
    return min((idx + 1) * shard_size, n) - idx * shard_size


def _done_rows(done: np.ndarray, shard_size: int, n: int) -> int:
    return sum(_shard_rows(i, shard_size, n) for i in np.nonzero(done)[0].tolist())


def monitor(
    out: Path, n: int, shard_size: int, procs, gpu_rows, cpu_rows, interval: float
) -> None:
    p = _paths(out)
    done = np.memmap(p["done"], dtype=np.uint8, mode="r")
    n_shards = done.shape[0]
    start_rows = _done_rows(done, shard_size, n)
    t0 = time.time()
    while True:
        alive = any(pr.is_alive() for pr in procs)
        dr = _done_rows(done, shard_size, n)
        ds = int(done.sum())
        elapsed = time.time() - t0
        rate = (dr - start_rows) / elapsed if elapsed > 0 else 0.0
        eta = (n - dr) / rate if rate > 0 else None
        prog = {
            "total": n,
            "done_shards": ds,
            "n_shards": n_shards,
            "done_rows": dr,
            "rate_rows_s": round(rate, 1),
            "gpu_rows": gpu_rows.value,
            "cpu_rows": cpu_rows.value,
            "eta_seconds": round(eta, 1) if eta is not None else None,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        # atomic write: an external `cat progress.json` must never see a truncated
        # file mid-rewrite (this is THE pollable visibility handle).
        tmp = p["progress"].with_suffix(".json.tmp")
        tmp.write_text(json.dumps(prog, indent=2))
        os.replace(tmp, p["progress"])
        eta_s = f"{eta / 60:.1f}m" if eta is not None else "?"
        print(
            f"[emb] {ds}/{n_shards} shards  {dr:,}/{n:,} rows "
            f"({100 * dr / n:.1f}%)  {rate:,.0f} rows/s  "
            f"gpu={gpu_rows.value:,} cpu={cpu_rows.value:,}  eta={eta_s}",
            flush=True,
        )
        if not alive and dr >= n:
            break
        if not alive:
            # workers exited but not everything done (queue drained by a crash?)
            break
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def verify(out: Path, n: int, dim: int, shard_size: int) -> None:
    p = _paths(out)
    done = np.memmap(p["done"], dtype=np.uint8, mode="r")
    assert int(done.sum()) == done.shape[0], (
        f"incomplete: {int(done.sum())}/{done.shape[0]} shards done"
    )
    ids = np.lib.format.open_memmap(p["ids"], mode="r")
    assert not (ids < 0).any(), "some ids rows unfilled (-1)"
    assert len(np.unique(ids)) == n, "ids not unique / coverage != N"
    vectors = np.memmap(p["vectors"], dtype=np.float32, mode="r", shape=(n, dim))
    # chunked finite + unit-norm check (avoid materializing 11 GB at once)
    step = 100_000
    for s in range(0, n, step):
        chunk = np.asarray(vectors[s : s + step])
        assert np.isfinite(chunk).all(), f"non-finite vectors near row {s}"
        norms = np.linalg.norm(chunk, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-3), f"non-unit norm near row {s}"
    print(
        f"[emb] verify OK: {n:,} rows finite + unit-norm, ids unique, all shards done"
    )


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus)
    out = Path(args.out)
    layout, total = shard_file_layout(corpus)
    n = min(args.limit, total) if args.limit else total
    dim = args.dim
    shard_size = args.shard_size
    n_shards = math.ceil(n / shard_size)

    resuming = args.resume and checkpoint_matches(out, n, dim, shard_size)
    if resuming:
        print(f"[emb] resuming checkpoint at {out}", flush=True)
        m = json.loads(_paths(out)["meta"].read_text())
        m["status"] = "running"
        _paths(out)["meta"].write_text(json.dumps(m, indent=2))
    else:
        print(f"[emb] creating fresh checkpoint at {out}", flush=True)
        create_checkpoint(out, n, dim, shard_size, args.model)

    done = np.memmap(_paths(out)["done"], dtype=np.uint8, mode="r")
    undone = [i for i in range(n_shards) if not done[i]]
    print(
        f"[emb] N={n:,} dim={dim} shard_size={shard_size:,} "
        f"n_shards={n_shards} undone={len(undone)} "
        f"cpu_workers={args.cpu_workers} gpu=1",
        flush=True,
    )
    if not undone:
        print("[emb] nothing to do — all shards already done", flush=True)
        verify(out, n, dim, shard_size)
        _finalize_meta(out)
        return 0

    ctx = mp.get_context("spawn")
    work_q = ctx.Queue()
    for i in undone:
        work_q.put(i)
    gpu_rows = ctx.Value("q", 0)
    cpu_rows = ctx.Value("q", 0)
    # Divide the core budget across CPU workers (reserve 2 cores for the GPU
    # worker's host-side work + the monitor). Avoids torch BLAS oversubscription.
    cores = os.cpu_count() or 4
    cpu_threads = max(1, (cores - 2) // max(1, args.cpu_workers))
    cfg = {
        "corpus": str(corpus),
        "out": str(out),
        "dim": dim,
        "n": n,
        "shard_size": shard_size,
        "batch": args.batch,
        "model": args.model,
        "layout": layout,
        "cpu_threads": cpu_threads,
        "fp16": args.fp16,
    }
    print(f"[emb] cpu_threads/worker={cpu_threads} (cores={cores})", flush=True)

    procs = []
    if not args.cpu_only:
        procs.append(
            ctx.Process(target=_worker, args=(cfg, "cuda", work_q, gpu_rows, cpu_rows))
        )
    for _ in range(args.cpu_workers):
        procs.append(
            ctx.Process(target=_worker, args=(cfg, "cpu", work_q, gpu_rows, cpu_rows))
        )
    for pr in procs:
        pr.start()

    monitor(out, n, shard_size, procs, gpu_rows, cpu_rows, args.heartbeat)
    for pr in procs:
        pr.join()

    print(
        f"[emb] workers done. gpu_rows={gpu_rows.value:,} cpu_rows={cpu_rows.value:,}",
        flush=True,
    )
    verify(out, n, dim, shard_size)
    _finalize_meta(out)
    print(f"[emb] COMPLETE — checkpoint at {out}", flush=True)
    return 0


def _finalize_meta(out: Path) -> None:
    p = _paths(out)
    m = json.loads(p["meta"].read_text())
    m["status"] = "complete"
    m["completed"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    p["meta"].write_text(json.dumps(m, indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", default="data/wiki/enwiki")
    ap.add_argument("--out", default="data/wiki/enwiki/emb")
    ap.add_argument("--shard-size", type=int, default=20000)
    ap.add_argument("--cpu-workers", type=int, default=4)
    ap.add_argument(
        "--cpu-only", action="store_true", help="no GPU worker (testing/off-Spark)"
    )
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dim", type=int, default=DEFAULT_DIM)
    ap.add_argument(
        "--limit", type=int, default=0, help="cap N to first LIMIT rows (0 => all)"
    )
    ap.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="ignore an existing checkpoint and start fresh",
    )
    ap.add_argument(
        "--no-fp16",
        dest="fp16",
        action="store_false",
        help="disable fp16 on the GPU worker (fp16 is ~2.6x faster, default on)",
    )
    ap.add_argument("--heartbeat", type=float, default=10.0)
    args = ap.parse_args(argv)

    if not (Path(args.corpus) / "manifest.json").exists():
        ap.error(f"no manifest.json under {args.corpus}")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
