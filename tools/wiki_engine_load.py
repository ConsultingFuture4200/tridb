"""Full-Wikipedia AT-SCALE loader for the three TriDB legs (DEV-1354, Phase 2).

Implements the load contracts in `docs/wiki_scale_load_design_v0.1.0.md` against the
REAL engine surface shipped in the gx10 images (`graph_store_am` + `vectordb`). This is
the scale twin of `tools/bench_sm2_corpus.py`'s loader: it consumes a `tools/wiki_extract`
manifest and stages a tri-modal load whose shape is intended to be identical at 7M and at the
bounded slice we actually run.

VALIDATION STATUS: validated on the shard-0 / 100k slice only. Full-scale (unbounded, `--limit 0`)
is BLOCKED pending manifest reconciliation — the enwiki extract has duplicate + truncated shards
(76 shard descriptors for 72 distinct files; on-disk articles ~7.14M < manifest count 7.19M). The
loader now dedupes shard paths and, in the unbounded case, ABORTS if the loaded slice does not
reconcile against the ground-truth manifest counts rather than emitting a silently-truncated corpus.

Design realities this tool encodes (probed on tridb/msvbase:gx10-v1, 2026-07-06):

  * The batched C entry point `gph_insert_edges(src, dst[])` that design §2 calls "New engine
    work to specify (GX10)" is NOT in the shipped SQL. The available bulk lever is therefore:
    materialize N dense vertices in id order (vid == article id), then bulk-insert edges by
    calling `gph_insert_edge(src, dst)` DIRECTLY by vid from a COPY-staged relation
    (`ORDER BY src` for adjacency-chain locality). This bypasses the per-edge id-map tax that
    `add_edge()` pays (two `gph_upsert_vertex` map descents per edge) — the exact lever
    design §2 names ("edge endpoints need NO map lookup"). Only after the verified dense
    in-order load do we flip `gph_set_identity_mode(true)` so the tjs_open read path skips
    the map too.

  * Load is COPY-staged from server-side files (PERF-11), NOT inlined INSERT/STDIN: at 232M
    edges you cannot inline the data in a SQL script, so the loader writes `*.copy` files and
    the driver `COPY ... FROM '/data/..'` them. This is the shape that scales to full enwiki.

Vector source (design §3) is pluggable so the same tool runs at every readiness level:
  --emb FILE.npy   reuse Phase-1 persisted id-aligned embeddings (float32[N,dim], row i = art i)
  --embed          embed a slice on the box with fastembed (CPU here; GPU when a Blackwell
                   onnxruntime CUDA wheel exists — currently unavailable, see STATUS.md)
  --synthetic      deterministic per-id pseudo-random unit vectors (dim configurable). Proves
                   the LOADER + HNSW build + tjs_open FUSION end-to-end without paying the
                   embed cost; graph topology in the proof stays REAL. Clearly labelled as
                   non-semantic in the emitted SQL + report.

Subcommand `prepare` writes an out dir the engine runner (`scripts/wiki_engine_load.sh`)
mounts as /data:  articles.copy, edges.copy, load.sql, prep.json. Host-independent to WRITE;
GX10/Spark to RUN.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time


# --------------------------------------------------------------------------------------
# COPY FORMAT text escaping (design §1: MediaWiki titles/categories can contain backslashes
# per $wgLegalTitleChars, so raw TSV would corrupt them).
# --------------------------------------------------------------------------------------
def _copy_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _vec_literal(v) -> str:
    # Postgres float8[] literal '{a,b,...}' (matches tools/bench_corpus_shared.vec_literal).
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def _synthetic_vec(art_id: int, dim: int) -> list[float]:
    """Deterministic unit vector keyed by article id (dim-agnostic). Reproducible across
    prepare runs so the emitted query vector (a loaded row's own vector) is guaranteed to
    match its stored row. Non-semantic by construction (labelled as such)."""
    rng = random.Random(art_id)
    vals = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    n = math.sqrt(sum(x * x for x in vals)) or 1.0
    return [x / n for x in vals]


# --------------------------------------------------------------------------------------
# Manifest access
# --------------------------------------------------------------------------------------
def _load_manifest(manifest_dir: str) -> dict:
    with open(os.path.join(manifest_dir, "manifest.json")) as f:
        return json.load(f)


def _article_shards(manifest: dict) -> list[str]:
    # Dedup order-preserving: the real enwiki manifest lists the same shard path more than once
    # (the extractor reopens a shard in 'w'/truncate mode and appends a fresh descriptor each time
    # the non-monotonic id stream revisits it), so the raw 'files' list has duplicate paths. Reading
    # a path twice would emit the same article ids twice -> duplicate-key violation on the 'id bigint
    # PRIMARY KEY' COPY. dict.fromkeys keeps first-seen order.
    return list(
        dict.fromkeys(s["path"] for s in manifest["shards"]["articles"]["files"])
    )


def _edge_shards(manifest: dict) -> list[str]:
    # edges shards are index-aligned to article shards; the extractor names them edges-NNNNN.tsv.
    # Dedup order-preserving for the same reason as _article_shards (index-aligned duplicate paths).
    files = manifest["shards"].get("edges", {}).get("files")
    if files:
        return list(dict.fromkeys(s["path"] for s in files))
    # fall back to the naming convention when the manifest omits an edges block.
    n = len(_article_shards(manifest))
    return [f"edges-{i:05d}.tsv" for i in range(n)]


# --------------------------------------------------------------------------------------
# prepare: write articles.copy, edges.copy, load.sql
# --------------------------------------------------------------------------------------
def prepare(args) -> int:
    manifest = _load_manifest(args.manifest)
    total_articles = manifest["counts"]["articles"]
    total_edges = manifest["counts"].get("edges")
    unbounded = not (args.limit and args.limit > 0)
    N = args.limit if args.limit and args.limit > 0 else total_articles
    N = min(N, total_articles)
    dim = args.dim

    os.makedirs(args.out, exist_ok=True)
    articles_copy = os.path.join(args.out, "articles.copy")
    edges_copy = os.path.join(args.out, "edges.copy")
    sql_path = os.path.join(args.out, "load.sql")

    # ---- optional persisted embeddings (numpy, id-aligned) ---------------------------
    emb = None
    if args.emb:
        import numpy as np  # local import: only the persisted-vector path needs numpy

        emb = np.load(args.emb, mmap_mode="r")
        if emb.shape[0] < N:
            print(
                f"[prepare] --emb has {emb.shape[0]} rows < N={N}; capping N",
                file=sys.stderr,
            )
            N = emb.shape[0]
        dim = int(emb.shape[1])

    # ---- optional fastembed slice ----------------------------------------------------
    embedder = None
    if args.embed:
        from fastembed import TextEmbedding

        embedder = TextEmbedding(model_name=args.embed_model, threads=args.threads)
        dim = 384 if "small" in args.embed_model else 768

    # ---- ARTICLES + VECTORS: stream shards, write COPY rows --------------------------
    t0 = time.time()
    seed_vec = (
        None  # the query vector for the sample tjs_open (a real loaded row's vector)
    )
    written = 0
    max_id = -1  # highest article id actually loaded -> drives dense vertex materialization (identity)
    pending_texts: list[
        tuple[int, str, str, str]
    ] = []  # (id,title,ts,text) for --embed batch

    def _emit_row(fout, art_id, title, ts, vec):
        nonlocal seed_vec
        ts_field = _copy_escape(ts) if ts else "\\N"
        fout.write(
            f"{art_id}\t{_copy_escape(title)}\t{ts_field}\t{_vec_literal(vec)}\n"
        )
        if art_id == args.seed_id:
            seed_vec = vec

    with open(articles_copy, "w") as fout:
        for shard in _article_shards(manifest):
            if written >= N:
                break
            with open(os.path.join(args.manifest, shard)) as fin:
                for line in fin:
                    if written >= N:
                        break
                    o = json.loads(line)
                    aid = o["id"]
                    if aid >= N:
                        continue
                    if aid > max_id:
                        max_id = aid
                    title = o.get("title") or ""
                    ts = o.get("ts") or ""
                    if embedder is not None:
                        pending_texts.append((aid, title, ts, o.get("text") or ""))
                        if len(pending_texts) >= args.embed_batch:
                            _flush_embed(fout, pending_texts, embedder, _emit_row)
                            pending_texts = []
                    elif emb is not None:
                        _emit_row(fout, aid, title, ts, [float(x) for x in emb[aid]])
                    else:
                        _emit_row(fout, aid, title, ts, _synthetic_vec(aid, dim))
                    written += 1
        if embedder is not None and pending_texts:
            _flush_embed(fout, pending_texts, embedder, _emit_row)
    art_secs = time.time() - t0
    if unbounded and written != total_articles:
        # Full-scale runs MUST reconcile against ground-truth manifest counts, not the self-derived N:
        # the enwiki extract has truncated/duplicate shards on disk, so an unbounded pass silently
        # loads FEWER than 'all 7M' (and 'aid >= N' drops any sparse high-id article). Abort loudly
        # rather than emit a silently-truncated corpus whose self-referential asserts would still pass.
        raise SystemExit(
            f"[prepare] ABORT (unbounded): loaded {written} articles but manifest counts "
            f"{total_articles} (delta {total_articles - written}). Full-scale load is BLOCKED "
            f"pending manifest reconciliation (truncated/duplicate shards). Refusing to emit a "
            f"silently-truncated corpus."
        )
    if written != N:
        print(
            f"[prepare] WARNING: wrote {written} article rows, expected N={N}",
            file=sys.stderr,
        )
        N = written
    if seed_vec is None:
        seed_vec = (
            [float(x) for x in emb[args.seed_id]]
            if emb is not None
            else _synthetic_vec(args.seed_id, dim)
        )

    # ---- EDGES: induced subgraph (both endpoints < N), streamed to COPY file ----------
    t1 = time.time()
    edge_count = 0
    seed_out_neighbor = (
        None  # a real induced out-neighbor of seed_id, for bridge-injection proof
    )
    seed_neighbors: list[
        int
    ] = []  # up to SEED_NEIGH_CAP induced out-neighbors of seed_id
    SEED_NEIGH_CAP = 200
    # Edge shards are src-partitioned + index-aligned to the article shards (design §0): shard i
    # holds only edges whose src is in article shard i (ids i*shard_size..). So for a bounded slice
    # of N articles, only shards 0..floor((N-1)/shard_size) can contain an induced (src<N) edge —
    # reading the rest would scan the whole 232M-edge corpus to produce a small slice.
    shard_size = (
        manifest.get("shard_size") or manifest["shards"]["articles"]["files"][0]["rows"]
    )
    max_edge_shard = (N - 1) // shard_size
    with open(edges_copy, "w") as fout:
        for idx, shard in enumerate(_edge_shards(manifest)):
            if idx > max_edge_shard:
                break
            spath = os.path.join(args.manifest, shard)
            if not os.path.exists(spath):
                continue
            with open(spath) as fin:
                for line in fin:
                    tab = line.find("\t")
                    if tab < 0:
                        continue
                    src = int(line[:tab])
                    if src >= N:
                        # article shards are src-partitioned; once src >= N in a later shard
                        # there is nothing induced left, but shards may interleave — keep scanning
                        # this shard's remaining lines cheaply (src<N filter below).
                        continue
                    dst = int(line[tab + 1 :])
                    if dst >= N:
                        continue
                    fout.write(f"{src}\t{dst}\n")
                    edge_count += 1
                    if src == args.seed_id:
                        if seed_out_neighbor is None:
                            seed_out_neighbor = dst
                        if len(seed_neighbors) < SEED_NEIGH_CAP:
                            seed_neighbors.append(dst)
    edge_secs = time.time() - t1
    if unbounded and total_edges is not None and edge_count != total_edges:
        # Same ground-truth reconciliation as articles: an unbounded pass must materialize every
        # induced edge in the manifest, not a self-derived subset of a truncated on-disk corpus.
        raise SystemExit(
            f"[prepare] ABORT (unbounded): induced {edge_count} edges but manifest counts "
            f"{total_edges} (delta {total_edges - edge_count}). Full-scale load is BLOCKED "
            f"pending manifest reconciliation (truncated/duplicate shards)."
        )

    # Native identity mode (vid == ext_id) requires a DENSE vertex range [0, max_id]; article ids are
    # sparse, so we materialize up to the highest loaded id (not N-1, which fabricates phantom vertices
    # for gap ids and drops real high-id articles). n_vertices == 0 iff no articles were loaded.
    n_vertices = max_id + 1

    # ---- LOAD.SQL driver -------------------------------------------------------------
    sql = _build_load_sql(
        N=N,
        n_vertices=n_vertices,
        dim=dim,
        edge_count=edge_count,
        seed_id=args.seed_id,
        seed_vec=seed_vec,
        seed_out_neighbor=seed_out_neighbor,
        seed_neighbors=seed_neighbors,
        seed_neigh_complete=(len(seed_neighbors) < SEED_NEIGH_CAP),
        k=args.k,
        term_cond=args.term_cond,
        m_seeds=args.m_seeds,
        hops=args.hops,
        vector_source=(
            "emb" if emb is not None else "embed" if embedder else "synthetic"
        ),
    )
    with open(sql_path, "w") as f:
        f.write(sql)

    prep = {
        "manifest": os.path.abspath(args.manifest),
        "articles_loaded": N,
        "articles_total": total_articles,
        "graph_vertices": n_vertices,
        "induced_edges": edge_count,
        "dim": dim,
        "vector_source": (
            "emb" if emb is not None else "embed" if embedder else "synthetic"
        ),
        "seed_id": args.seed_id,
        "seed_out_neighbor": seed_out_neighbor,
        "prepare_articles_secs": round(art_secs, 2),
        "prepare_edges_secs": round(edge_secs, 2),
        "k": args.k,
        "term_cond": args.term_cond,
        "m_seeds": args.m_seeds,
        "hops": args.hops,
    }
    with open(os.path.join(args.out, "prep.json"), "w") as f:
        json.dump(prep, f, indent=2)

    print(
        f"[prepare] N={N} articles ({art_secs:.1f}s), induced_edges={edge_count} "
        f"({edge_secs:.1f}s), dim={dim}, vectors={prep['vector_source']}"
    )
    print(f"[prepare] out={args.out} (articles.copy, edges.copy, load.sql, prep.json)")
    return 0


def _flush_embed(fout, batch, embedder, emit):
    texts = [(t + ". " + body[:512]) for (_a, t, _ts, body) in batch]
    vecs = list(embedder.embed(texts, batch_size=64))
    for (aid, title, ts, _body), v in zip(batch, vecs):
        emit(fout, aid, title, ts, [float(x) for x in v])


def _build_load_sql(
    *,
    N,
    n_vertices,
    dim,
    edge_count,
    seed_id,
    seed_vec,
    seed_out_neighbor,
    seed_neighbors,
    seed_neigh_complete,
    k,
    term_cond,
    m_seeds,
    hops,
    vector_source,
) -> str:
    L: list[str] = []
    w = L.append
    qvec = _vec_literal(seed_vec)
    w(
        "-- AUTO-GENERATED by tools/wiki_engine_load.py — TriDB Phase-2 AT-SCALE tri-modal load."
    )
    w(f"-- N={N} induced_edges={edge_count} dim={dim} vectors={vector_source}")
    w(
        "-- Load path: COPY articles (PERF-11) + dense vertices in id order + COPY-staged"
    )
    w("-- direct-by-vid gph_insert_edge (identity lever, design §2) + HNSW build.")
    w("\\set ON_ERROR_STOP on")
    w("\\timing on")
    w("CREATE EXTENSION vectordb;")
    w("CREATE EXTENSION graph_store_am;")
    w("")
    w("-- ===== §1 relational + vector payload (COPY bulk load) =====")
    w("CREATE TABLE articles (")
    w("    id        bigint PRIMARY KEY,")
    w("    title     text NOT NULL,")
    w("    ts        text,")
    w(f"    embedding float8[{dim}]")
    w(");")
    w("\\echo #WL COPY_ARTICLES_START")
    w("\\copy articles (id,title,ts,embedding) FROM '/data/articles.copy'")
    w("\\echo #WL COPY_ARTICLES_DONE")
    w("-- assert: relational row count == prepared slice")
    w("DO $$ BEGIN")
    w(f"  IF (SELECT count(*) FROM articles) <> {N} THEN")
    w(
        f"    RAISE EXCEPTION 'articles count % != expected {N}', (SELECT count(*) FROM articles);"
    )
    w("  END IF;")
    w("  RAISE NOTICE '#WL ASSERT articles=% OK', (SELECT count(*) FROM articles);")
    w("END $$;")
    w("")
    w(
        "-- ===== §3 vector leg: HNSW build (CPU hnswlib now; GPU CAGRA per Phase-2 PERF-08) ====="
    )
    w("\\echo #WL HNSW_BUILD_START")
    w("CREATE INDEX articles_hnsw ON articles USING hnsw(embedding)")
    w(f"    WITH (dimension = {dim}, distmethod = l2_distance);")
    w("\\echo #WL HNSW_BUILD_DONE")
    w("SET enable_seqscan = off;  -- force the HNSW ANN scan for the vector leg")
    w("")
    w("-- ===== §2 native graph: dense vertices in id order -> vid == article id =====")
    w("\\echo #WL VERTEX_MATERIALIZE_START")
    w(
        "-- gph_upsert_vertex(0..max_id) in ascending order: native vids are dense/monotone from 0, so"
    )
    w(
        "-- ext_id == vid for every vertex (the identity precondition, design §2 step 1). Article ids are"
    )
    w(
        "-- SPARSE, so the range runs to the highest LOADED id (not N-1) — a dense range is what identity"
    )
    w(
        "-- mode requires; gap ids get an isolated vertex, real high-id articles are never dropped."
    )
    w("SELECT count(*) FROM (")
    w(
        f"  SELECT graph_store.gph_upsert_vertex(g) FROM (SELECT g FROM generate_series(0,{n_vertices - 1}) g ORDER BY g) s"
    )
    w(") _;")
    w("\\echo #WL VERTEX_MATERIALIZE_DONE")
    w("")
    w(
        "-- COPY-stage the induced edges, then ONE set-oriented BATCHED insert: group by src (each"
    )
    w(
        "-- source's whole adjacency run in one gph_insert_edges call) ORDER BY src for chain locality."
    )
    w(
        "-- gph_insert_edges is O(1)-per-edge (dense src locate + metapage dst bounds) vs the O(E*V) the"
    )
    w(
        "-- per-edge gph_insert_edge cost at scale; src/dst ARE vids (design §2 step 3, dense load)."
    )
    w("CREATE TEMP TABLE edge_stage (src bigint, dst bigint);")
    w("\\echo #WL COPY_EDGES_START")
    w("\\copy edge_stage (src,dst) FROM '/data/edges.copy'")
    w("\\echo #WL COPY_EDGES_DONE")
    w("\\echo #WL EDGE_INSERT_START")
    w("SELECT sum(graph_store.gph_insert_edges(src, dst_arr)) FROM (")
    w(
        "  SELECT src, array_agg(dst) AS dst_arr FROM edge_stage GROUP BY src ORDER BY src"
    )
    w(") g;")
    w("\\echo #WL EDGE_INSERT_DONE")
    w("DROP TABLE edge_stage;")
    w(
        "-- dense in-order load verified above -> flip identity mode (tjs_open read path skips the map)"
    )
    w("SELECT graph_store.gph_set_identity_mode(true);")
    w("")
    w("-- assert: native edge/vertex counts == prepared slice")
    w("DO $$")
    w("DECLARE ec bigint; vc bigint;")
    w("BEGIN")
    w("  SELECT graph_store.gph_edge_count() INTO ec;")
    w("  SELECT graph_store.gph_vertex_count() INTO vc;")
    w(f"  IF ec <> {edge_count} THEN")
    w(f"    RAISE EXCEPTION 'gph_edge_count % != expected {edge_count}', ec; END IF;")
    w(f"  IF vc <> {n_vertices} THEN")
    w(f"    RAISE EXCEPTION 'gph_vertex_count % != expected {n_vertices}', vc; END IF;")
    w("  RAISE NOTICE '#WL ASSERT edges=% vertices=% OK', ec, vc;")
    w("END $$;")
    w("")
    w("-- ===== sample tjs_open: verify tri-modal fusion returns sane top-k =====")
    w(
        f"\\echo #WL TJS_OPEN k={k} term_cond={term_cond} m_seeds={m_seeds} hops={hops} seed={seed_id}"
    )
    w("-- (1) top-k count + early termination (TR-1: examined << N)")
    w("DO $$")
    w("DECLARE got bigint[]; ex bigint;")
    w("BEGIN")
    w("  SELECT array_agg(id) INTO got FROM (")
    w(
        f"    SELECT t.id FROM tjs_open('articles', {k}, {term_cond}, {m_seeds}, {hops}, 'id', '',"
    )
    w(f"      'embedding <-> ''{qvec}''') AS t(id bigint)")
    w("  ) q;")
    w("  ex := tjs_open_candidates_examined();")
    w(f"  RAISE NOTICE '#WL TJS_OPEN topk=% examined=% (corpus {N})', got, ex;")
    w(f"  IF array_length(got,1) IS DISTINCT FROM {k} THEN")
    w(
        f"    RAISE EXCEPTION 'tjs_open returned % rows, expected k={k}', array_length(got,1); END IF;"
    )
    w(f"  IF ex >= {N} THEN")
    w(
        "    RAISE EXCEPTION 'tjs_open NOT early-terminating: examined % >= corpus (blocking!)', ex; END IF;"
    )
    w(f"  RAISE NOTICE '#WL PASS top-k + early termination (examined % << {N})', ex;")
    w("END $$;")
    if seed_neighbors:
        neigh_lit = "ARRAY[" + ",".join(str(n) for n in seed_neighbors) + "]::bigint[]"
        bridge_k = max(k, 60)
        w("")
        w(
            "-- (1b) native graph-read diagnostic (isolate graph-leg health from tjs fusion, and"
        )
        w(
            "--      positively rule out the DEV-1352 identity-mode read corruption for THIS load):"
        )
        w(
            f"--      gph_neighbors_ext({seed_id}) under identity mode must return real induced neighbors."
        )
        w("DO $$")
        w("DECLARE ns bigint[];")
        w("BEGIN")
        w(
            f"  SELECT array_agg(n) INTO ns FROM (SELECT graph_store.gph_neighbors_ext({seed_id}) n LIMIT 8) s;"
        )
        w(
            "  RAISE NOTICE '#WL GRAPH_READ gph_neighbors_ext(%) first8 = %', "
            + str(seed_id)
            + ", ns;"
        )
        w("  IF ns IS NULL OR array_length(ns,1) IS NULL THEN")
        w(
            f"    RAISE EXCEPTION 'graph read DEAD: gph_neighbors_ext({seed_id}) empty under identity mode (DEV-1352?)';"
        )
        w("  END IF;")
        if seed_neigh_complete:
            # we captured the seed's COMPLETE induced-neighbor set, so every returned id must be in it.
            w(f"  IF NOT (ns <@ {neigh_lit}) THEN")
            w(
                f"    RAISE EXCEPTION 'graph read CORRUPT: gph_neighbors_ext({seed_id}) returned ids outside the induced out-neighbor set (identity-mode bug): %', ns;"
            )
            w("  END IF;")
        else:
            # partial neighbor set captured: assert the read at least overlaps the known induced neighbors.
            w(f"  IF NOT (ns && {neigh_lit}) THEN")
            w(
                f"    RAISE EXCEPTION 'graph read SUSPECT: gph_neighbors_ext({seed_id}) shares no id with the known induced out-neighbors: %', ns;"
            )
            w("  END IF;")
        w(
            "  RAISE NOTICE '#WL PASS graph read: native neighbors match the loaded induced topology';"
        )
        w("END $$;")
        w("")
        w(
            "-- (2) bridge injection over REAL topology: the seed's induced out-neighbors are (almost"
        )
        w(
            "--     surely) far in vector space, so any that appear in the top-k arrived via the GRAPH"
        )
        w(
            "--     leg, not the vector leg. m_seeds=1 pins the seed set to {seed_id}; assert the top-k"
        )
        w(
            "--     OVERLAPS the seed's induced-neighbor set (robust to the k/2 bridge cap, which keeps"
        )
        w(
            "--     a subset of a high-degree seed's neighbors) AND that the operator injected bridges."
        )
        w("DO $$")
        w("DECLARE got bigint[]; bi bigint;")
        w("BEGIN")
        w("  SELECT array_agg(id) INTO got FROM (")
        w(
            f"    SELECT t.id FROM tjs_open('articles', {bridge_k}, 0, 1, {max(hops, 1)}, 'id', '',"
        )
        w(f"      'embedding <-> ''{qvec}''') AS t(id bigint)")
        w("  ) q;")
        w("  bi := tjs_open_bridges_injected();")
        w(f"  RAISE NOTICE '#WL BRIDGE top-{bridge_k}=% bridges_injected=%', got, bi;")
        w(f"  IF NOT (got && {neigh_lit}) THEN")
        w(
            f"    RAISE EXCEPTION 'bridge injection FAILED: top-{bridge_k} shares NO id with seed {seed_id}''s induced out-neighbors (graph leg dead?): %', got;"
        )
        w("  END IF;")
        w("  IF bi < 1 THEN")
        w(
            "    RAISE EXCEPTION 'bridge injection FAILED: tjs_open_bridges_injected()=% (graph leg did not fire)', bi;"
        )
        w("  END IF;")
        w(
            f"  RAISE NOTICE '#WL PASS bridge injection: real induced neighbors of seed {seed_id} admitted via the graph leg (bridges_injected=%)', bi;"
        )
        w("END $$;")
    w("")
    w("\\echo #WL LOAD_COMPLETE")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser(
        "prepare",
        help="write articles.copy + edges.copy + load.sql from a manifest slice",
    )
    pp.add_argument("--manifest", required=True, help="tools/wiki_extract manifest DIR")
    pp.add_argument(
        "--out", required=True, help="output dir (mounted as /data by the runner)"
    )
    pp.add_argument(
        "--limit",
        type=int,
        default=0,
        help="load first N articles (0 = all; BLOCKED at full scale — see module docstring VALIDATION STATUS)",
    )
    pp.add_argument(
        "--dim",
        type=int,
        default=64,
        help="synthetic-vector dim (ignored for --emb/--embed)",
    )
    pp.add_argument(
        "--emb", help="persisted id-aligned embeddings .npy (float32[N,dim])"
    )
    pp.add_argument(
        "--embed", action="store_true", help="embed a slice with fastembed (CPU here)"
    )
    pp.add_argument("--embed-model", default="BAAI/bge-small-en-v1.5")
    pp.add_argument("--embed-batch", type=int, default=1024)
    pp.add_argument(
        "--threads", type=int, default=0, help="fastembed threads (0 = library default)"
    )
    pp.add_argument(
        "--synthetic",
        action="store_true",
        help="deterministic per-id vectors (default when no --emb/--embed)",
    )
    pp.add_argument(
        "--seed-id",
        type=int,
        default=0,
        help="article id used as the sample tjs_open query vector",
    )
    pp.add_argument("--k", type=int, default=10)
    pp.add_argument(
        "--term-cond", type=int, default=64, help="tjs_open early-termination depth"
    )
    pp.add_argument("--m-seeds", type=int, default=8)
    pp.add_argument("--hops", type=int, default=1)
    args = p.parse_args(argv)

    if args.cmd == "prepare":
        if args.threads == 0:
            args.threads = None
        return prepare(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
