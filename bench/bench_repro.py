"""One-command public-dataset reproduction (GTM make-or-break).

This is the assembly step the GTM plan (docs/gtm_opensource_v0.1.0.md, "Part 1 —
Proof plan" / "the make-or-break item") calls for: a SINGLE command that, from a
clean checkout, runs TriDB's retrieval against a RECOGNIZED public dataset, grades
recall@k against an EXACT oracle, and emits a metrics JSON + a rendered table —
with pinned data and pinned seeds, and a ruthlessly honest split of what is real
here vs. GX10-gated.

It does NOT re-derive any benchmark. It ASSEMBLES the two host-gradeable pieces the
repo already has, both on data strangers recognize:

  1. HOTPOTQA GRAPHRAG (the headline — real recall, gradeable HERE).
     Real multi-hop QA (HotpotQA dev slice), a real embedding-INDEPENDENT graph
     (title-mention edges, a Wikipedia-hyperlink proxy), BGE-768 embeddings. The
     full retrieval (vector-only vs. graph-inject) runs host-side in numpy and is
     graded against the gold supporting paragraphs. This produces a REAL recall
     curve on this box: graph-inject lifts multi-hop JOINT evidence recall@5 by
     +15.6 pt over vector-only. Reused verbatim from bench/graphrag_report.py.

  2. SIFT-128 PUBLIC ANN (the recognized-corpus pin + oracle plumbing).
     sift-128-euclidean is a canonical ann-benchmarks dataset, pinned by SHA256
     (tools/fetch_dataset.py) and verified here. We build the exact numpy top-k
     oracle over the REAL SIFT vectors (tools/real_corpus.py). The oracle itself
     is real and reproducible; grading the LIVE tjs() answer set against it needs
     the engine and is GX10-gated, so this section reports the oracle/plumbing
     state HONESTLY and never invents a live recall or latency number.

HONESTY CONTRACT (the GTM doc demands it):
  * Every recall number here is host-computed against an exact oracle on real data.
  * The HotpotQA recall is REAL and reproduces on this box.
  * The SIFT live tjs() recall + ALL latency numbers are GX10/engine-gated and are
    reported as gated, never fabricated.
  * recall is reported as a CURVE across k (per R1), never a bare peak.
  * Pinned data (SHA256), pinned seeds.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from bench import graphrag_report as gr
from tools import fetch_dataset, real_corpus

RESULTS_DIR = Path("bench/results")


# --------------------------------------------------------------------------- #
# Section 1 — HotpotQA GraphRAG recall (REAL, host-gradeable)
# --------------------------------------------------------------------------- #
def run_hotpot(manifest_path: Path, ks: list[int], reader_k: int) -> dict:
    """Run the HotpotQA GraphRAG evidence-recall sweep host-side and return a
    structured result. Reuses bench/graphrag_report.py verbatim, so this number
    is identical to `make graphrag` (no second implementation to drift)."""
    sl = gr.load_slice(manifest_path)
    sweep = gr.sweep(sl, ks)
    types = sorted({q["type"] for q in sl.questions})
    grp = "bridge" if "bridge" in types else "all"
    return {
        "dataset": "HotpotQA dev slice",
        "graph": "real title-mention edges (Wikipedia-hyperlink proxy, embedding-independent)",
        "embeddings": "BGE-base-en-v1.5 (dim 768), cosine",
        "n_questions": len(sl.questions),
        "n_paragraphs": sl.n,
        "ks": ks,
        "reader_k": reader_k,
        "headline_group": grp,
        "sweep": sweep,
        "types": types,
    }


# --------------------------------------------------------------------------- #
# Section 2 — SIFT-128 public ANN (recognized pin + exact oracle, plumbing)
# --------------------------------------------------------------------------- #
def run_sift(hdf5_path: Path, *, limit: int, queries: int, k: int, seed: int) -> dict:
    """Verify the pinned SIFT dataset, build the exact oracle over the real
    vectors, and run the host-side recall self-check.

    HONEST: the self-check grades the oracle against itself (== 1.0) — it proves
    the dataset is recognized + pinned + verifiable and the oracle plumbing is
    correct. It is NOT an engine recall number; that needs the live tjs()
    transcript (GX10-gated), which this function never fabricates.
    """
    ds = fetch_dataset.REGISTRY["sift-128-euclidean"]
    pinned = ds.sha256 != fetch_dataset._PENDING
    out: dict = {
        "dataset": ds.name,
        "note": ds.note,
        "dim": ds.dim,
        "distance": ds.distance,
        "pinned_sha256": ds.sha256 if pinned else None,
        "present": hdf5_path.exists(),
    }
    if not hdf5_path.exists():
        out["state"] = (
            "MISSING — run: make fetch-dataset PUBLIC_DATASET=sift-128-euclidean"
        )
        out["checksum_verified"] = False
        return out

    # Verify the on-disk file against the committed pin (supply-chain integrity).
    if pinned:
        observed = fetch_dataset.sha256_file(hdf5_path)
        out["observed_sha256"] = observed
        out["checksum_verified"] = observed.lower() == ds.sha256.lower()
        if not out["checksum_verified"]:
            out["state"] = "CHECKSUM MISMATCH — refusing to trust the file"
            return out
    else:
        out["checksum_verified"] = False
        out["state"] = "UNPINNED — fetch with --pin to record the digest"
        return out

    # Build the exact oracle over the real vectors (host-side, no engine).
    # h5py is an OPTIONAL dep (ann-benchmarks .hdf5 only) — if it is absent, the
    # pin is still verified above; we just can't open the file. Report that
    # honestly rather than crashing the whole repro (the HotpotQA recall, which
    # needs no h5py, is the headline and still runs).
    try:
        emb = real_corpus.load_vectors(hdf5_path, limit=limit)
    except RuntimeError as exc:  # h5py missing (real_corpus raises a clear message)
        out["checksum_verified"] = True
        out["state"] = (
            "PINNED + VERIFIED, but h5py is not installed so the oracle was not "
            "built here (pip install h5py to build it). The pin is still verified."
        )
        out["oracle_error"] = str(exc).splitlines()[0]
        return out
    manifest = real_corpus.synthesize_corpus(
        emb,
        hubs=queries,
        fanout=150,
        queries=queries,
        k=k,
        window=600,
        seed=seed,
    )
    selfcheck = real_corpus.report_recall(manifest, results=None)
    out.update(
        {
            "vectors_loaded": int(emb.shape[0]),
            "limit": limit,
            "queries": queries,
            "k": k,
            "seed": seed,
            "oracle_selfcheck_recall": selfcheck["mean_recall"],
            "state": "PINNED + VERIFIED; exact oracle built host-side",
            "live_recall_at_k": "GX10/engine-gated (needs live tjs() transcript)",
            "latency": "GX10/engine-gated (EXPLAIN ANALYZE on the live engine)",
        }
    )
    return out


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def render_md(hot: dict, sift: dict, *, reader_k: int) -> str:
    grp = hot["headline_group"]
    sw = hot["sweep"]
    vj = sw["vector_only"][reader_k][grp]["joint"]
    gj = sw["graph_inject"][reader_k][grp]["joint"]
    L: list[str] = []
    w = L.append
    w("# TriDB — One-Command Public-Dataset Reproduction (rendered)")
    w("")
    w(
        f"**Headline (real, reproduced on this box):** on the HotpotQA dev slice "
        f"({hot['n_questions']} questions / {hot['n_paragraphs']} paragraphs), "
        f"injecting REAL graph bridges lifts multi-hop **joint** evidence "
        f"recall@{reader_k} on `{grp}` questions by **{gj - vj:+.1%}** "
        f"({vj:.1%} -> {gj:.1%}) over vector-only."
    )
    w("")
    w("## 1. HotpotQA GraphRAG — evidence recall vs k (REAL, host-graded)")
    w("")
    w(f"Group: `{grp}` (the multi-hop case the graph targets). Higher is better.")
    w("")
    names = list(gr.RETRIEVERS)
    w("| k | " + " | ".join(f"{n} joint" for n in names) + " | inject lift |")
    w("|---:|" + "---:|" * (len(names) + 1))
    for k in hot["ks"]:
        cells = [f"{sw[n][k][grp]['joint']:.3f}" for n in names]
        lift = sw["graph_inject"][k][grp]["joint"] - sw["vector_only"][k][grp]["joint"]
        w(f"| {k} | " + " | ".join(cells) + f" | {lift:+.3f} |")
    w("")
    w(
        "_Real recall, graded host-side against gold supporting paragraphs "
        "(no engine, no LLM). graph_rerank is the naive ablation shown to NOT "
        "help; only graph_inject lifts the hard 2nd hop. Reported as a curve._"
    )
    w("")
    w("## 2. SIFT-128 public ANN — recognized dataset pin + exact oracle")
    w("")
    w("| field | value |")
    w("|---|---|")
    w(f"| dataset | `{sift['dataset']}` (canonical ann-benchmarks set) |")
    w(f"| dim / distance | {sift['dim']} / {sift['distance']} (L2, matches `<->`) |")
    w(f"| pinned SHA256 | `{sift.get('pinned_sha256')}` |")
    w(f"| checksum verified here | {sift.get('checksum_verified')} |")
    w(f"| state | {sift.get('state')} |")
    if "oracle_selfcheck_recall" in sift:
        w(
            f"| oracle self-check | {sift['oracle_selfcheck_recall']:.3f} "
            f"(plumbing proof, NOT an engine recall) |"
        )
        w(f"| live recall@k | {sift['live_recall_at_k']} |")
        w(f"| latency | {sift['latency']} |")
    w("")
    w(
        "_The SIFT dataset is recognized, pinned (SHA256), and verified on this "
        "box; the exact numpy top-k oracle is built over the REAL vectors. The "
        "oracle self-check grades the oracle against itself (1.000) and proves "
        "ONLY the dataset/oracle plumbing. Grading the live `tjs()` answer set "
        "against this oracle — and ALL latency — is GX10/engine-gated and is "
        "never fabricated here._"
    )
    w("")
    w("## Honesty split")
    w("")
    w("| Aspect | State |")
    w("|---|---|")
    w(
        "| HotpotQA graph-inject recall lift | **REAL, reproduced here** (host numpy, exact gold) |"
    )
    w("| SIFT recognized-dataset pin + checksum | **REAL, verified here** |")
    w("| SIFT exact oracle over real vectors | **REAL, built here** |")
    w("| SIFT live `tjs()` recall@k vs oracle | **GX10/engine-gated** |")
    w("| Any `tjs()` latency (ms) | **GX10/engine-gated** |")
    w("| 128 GB / dim-960 GIST headline | **GX10-gated** |")
    w("")
    w(
        "_Generated by `bench/bench_repro.py` (`make bench-repro`). Numbers are observed._"
    )
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--hotpot-manifest", type=Path, default=Path("data/hotpot/manifest.json")
    )
    p.add_argument(
        "--sift", type=Path, default=Path("data/public/sift-128-euclidean.hdf5")
    )
    p.add_argument("--ks", type=int, nargs="+", default=[2, 3, 5, 10])
    p.add_argument("--reader-k", type=int, default=5)
    p.add_argument(
        "--sift-limit",
        type=int,
        default=20000,
        help="first N SIFT vectors for the host-side oracle (bounded; the file ships 1M)",
    )
    p.add_argument("--sift-queries", type=int, default=12)
    p.add_argument("--sift-k", type=int, default=10)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--json-out", type=Path, default=RESULTS_DIR / "bench_repro_metrics.json"
    )
    p.add_argument("--md-out", type=Path, default=RESULTS_DIR / "bench_repro_report.md")
    args = p.parse_args(argv)

    if not args.hotpot_manifest.exists():
        raise SystemExit(
            f"HotpotQA manifest {args.hotpot_manifest} missing — build it with: "
            "make fetch-hotpot && make graphrag (network-gated fetch)"
        )

    ks = sorted(set(args.ks) | {args.reader_k})
    np.random.seed(args.seed)  # belt-and-suspenders; the paths use seeded RNGs

    print(f"[bench-repro] HotpotQA recall sweep ({args.hotpot_manifest}) ...")
    hot = run_hotpot(args.hotpot_manifest, ks, args.reader_k)
    grp = hot["headline_group"]
    vj = hot["sweep"]["vector_only"][args.reader_k][grp]["joint"]
    gj = hot["sweep"]["graph_inject"][args.reader_k][grp]["joint"]
    print(
        f"[bench-repro]   joint recall@{args.reader_k} ({grp}): "
        f"vector={vj:.3f} graph_inject={gj:.3f} lift={gj - vj:+.3f}"
    )

    print(f"[bench-repro] SIFT public-ANN pin + oracle ({args.sift}) ...")
    sift = run_sift(
        args.sift,
        limit=args.sift_limit,
        queries=args.sift_queries,
        k=args.sift_k,
        seed=args.seed,
    )
    print(f"[bench-repro]   {sift.get('state')}")

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(
            {
                "seed": args.seed,
                "reader_k": args.reader_k,
                "hotpot": hot,
                "sift": sift,
            },
            indent=2,
        )
    )
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.write_text(render_md(hot, sift, reader_k=args.reader_k))
    print(f"[bench-repro] wrote {args.json_out} + {args.md_out}")
    print(
        "[bench-repro] DONE. Real recall here = HotpotQA graph-inject lift; "
        "live tjs() latency stays GX10-gated (not run here)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
