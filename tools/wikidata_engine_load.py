"""Load an ingested+embedded Wikidata slice into the TriDB engine container (plan 060).

The engine half of the two ADR-0018 loaders (the other is tools/wikidata_baseline_load).
Consumes a tools/wikidata_ingest manifest directory + the id-aligned embeddings
(emb/dense_id_aligned.npy) and drives ONE psql session inside the persistent engine
container (docker exec -i <container> psql -f -, the bench/wiki_h2h.py convention) that:

  (a) creates the relational table (WCfg.engine_table, default "entities"):
        id bigint PRIMARY KEY   -- the DENSE id == graph vid == emission ordinal
        qid bigint              -- the sparse Wikidata Q-number (ext id)
        P31 int[]               -- DENSE type ids (P31 targets remapped through the map)
        embedding float8[dim]   -- normalize-at-write (ADR-0017 B4-interim), dim 384
                                -- (--dialect stock: pgvector vector(dim) instead)
      and COPY-streams the rows (embedding inline, the wiki_engine_load shape);
  (b) upserts graph vertices via graph_store.gph_upsert_vertex(qid) in EMISSION ORDER
      and ASSERTS each returned vid == the dense ordinal — the shared-id invariant
      (ADR-0013 / ADR-0018 (c)) fails LOUDLY, never silently drifts;
  (c) registers one typed-edge dictionary id per distinct property via
      graph_store.register_edge_type('P<m>') (ADR-0016) and inserts every in-slice edge
      with graph_store.gph_insert_edge(src_vid, dst_vid, type_id) — src/dst ARE the dense
      vids verified in (b), ORDER BY src for adjacency-chain locality;
  (d) builds the HNSW index — fork dialect exactly as tools/wiki_engine_load
      (USING hnsw(embedding) WITH (dimension=D, distmethod=l2_distance)); stock dialect
      via pgvector (USING hnsw (embedding vector_l2_ops) WITH (m=16, ef_construction=64),
      the ADR-0015 E3 probe parameters) — plus a top-k health probe either way;
  (e) prints final counts and writes a load-manifest JSON (--out) with counts +
      durations, including the WD_ENGINE_EDGES gate value (the WH_ENGINE_EDGES
      analogue bench/wikidata_h2h.publication_gate needs).

EDGE-COUNT PARITY DEFINITION (shared with the baseline loader): an edge counts iff BOTH
endpoints are in-slice (present in the dense map); duplicates in the shards are preserved.
This mirrors bench/wikidata_h2h.load_typed_adj, so oracle graph == engine graph == Neo4j
graph when the gate's engine_edges == neo4j_edges parity holds.

IDENTITY MODE IS NOT FLIPPED: ext ids here are sparse Q-numbers, so ext_id != vid and
gph_set_identity_mode(true) would (correctly) refuse. The dense-id contract lives in the
vid == table PK equality asserted in (b), not in the identity fast-path.

GX10-GATED EXECUTION: the load RUNS only where the engine container exists. All pure
logic (shard iteration, dense remap, SQL text generation) is importable and host-tested
(tests/test_wikidata_engine_load.py). --emit-sql writes the full load script to a file
without touching docker, for inspection or manual replay on the GX10.

Idempotent re-run: --force drops + recreates the table AND the graph store extension
(DROP EXTENSION graph_store_am CASCADE resets the native pages, the id map and the
edge-type dictionary). Without --force an existing table fails the load loudly.

CLI:
    python -m tools.wikidata_engine_load --slice data/wikidata_slice \\
        [--container tridb-wikidata] [--table entities] [--dim 384] [--force] \\
        [--out load_manifest.json] [--emit-sql /tmp/wd_load.sql]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

# Defaults mirror bench/wikidata_h2h.WCfg — the harness must find what we load.
DEFAULT_CONTAINER = "tridb-wikidata"
DEFAULT_DB = "postgres"
DEFAULT_TABLE = "entities"
DEFAULT_DIM = 384
PROBE_K = 10


# ======================================================================================
# Pure slice logic (host-testable, no docker/numpy beyond the emb array itself)
# ======================================================================================
def load_slice_manifest(slice_dir: Path) -> dict:
    return json.loads((slice_dir / "manifest.json").read_text())


def shard_paths(manifest: dict, kind: str) -> list[str]:
    """Order-preserving path dedup — mirrors bench/wikidata_h2h._shard_paths."""
    return list(dict.fromkeys(s["path"] for s in manifest["shards"][kind]["files"]))


def build_dense_map(
    slice_dir: Path, manifest: dict
) -> tuple[dict[int, int], list[int]]:
    """Q-id -> dense id and dense -> Q-id, in EMISSION ORDER, first occurrence wins.

    MUST stay byte-identical in behaviour to bench/wikidata_h2h.load_dense_map (the
    harness's dense-id contract); tests/test_wikidata_engine_load.py pins the parity.
    Dense id == the 0-based ordinal across the entities-*.jsonl shards == the vid
    gph_upsert_vertex assigns when the loader upserts in this order.
    """
    qid_to_dense: dict[int, int] = {}
    dense_to_qid: list[int] = []
    for path in shard_paths(manifest, "entities"):
        for line in (slice_dir / path).read_text().splitlines():
            if not line.strip():
                continue
            q = json.loads(line)["id"]
            if q not in qid_to_dense:
                qid_to_dense[q] = len(dense_to_qid)
                dense_to_qid.append(q)
    return qid_to_dense, dense_to_qid


def load_p31_dense(
    slice_dir: Path, manifest: dict, qmap: dict[int, int]
) -> dict[int, list[int]]:
    """dense id -> sorted DENSE P31 type ids (types outside the slice dropped).

    Mirrors bench/wikidata_h2h.load_types' remap-and-drop rule; sorted for a
    deterministic int[] literal. First claims row per entity wins (duplicate
    shard rows carry identical content, matching the dense map's first-wins).
    """
    out: dict[int, list[int]] = {}
    for path in shard_paths(manifest, "claims"):
        spath = slice_dir / path
        if not spath.exists():
            continue
        for line in spath.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            e = qmap.get(row["id"])
            if e is None or e in out:
                continue
            out[e] = sorted({qmap[t] for t in row.get("P31", []) if t in qmap})
    return out


def iter_kept_edges(
    slice_dir: Path, manifest: dict, qmap: dict[int, int], stats: dict | None = None
) -> Iterator[tuple[int, int, int]]:
    """Yield (src_dense, p_id, dst_dense) for every edge row with BOTH endpoints in-slice.

    THE parity definition: an edge counts iff src AND dst are in the dense map; dangling
    rows are dropped (counted in stats["dropped"]); duplicates preserved. p_id stays the
    sparse P-number — properties map through register_edge_type at load, not the entity
    dense map. Mirrors bench/wikidata_h2h.load_typed_adj (same shard order, same drop rule).
    """
    for path in shard_paths(manifest, "edges"):
        spath = slice_dir / path
        if not spath.exists():
            continue
        with spath.open() as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                s = qmap.get(int(parts[0]))
                d = qmap.get(int(parts[2]))
                if s is None or d is None:
                    if stats is not None:
                        stats["dropped"] = stats.get("dropped", 0) + 1
                    continue
                if stats is not None:
                    stats["kept"] = stats.get("kept", 0) + 1
                yield s, int(parts[1]), d


def norm_vec(v: Iterable[float]) -> list[float]:
    """L2-normalize at write (ADR-0017 B4-interim): <-> l2 order == cosine order."""
    vals = [float(x) for x in v]
    n = math.sqrt(sum(x * x for x in vals)) or 1.0
    return [x / n for x in vals]


def vec_literal(v: Iterable[float], dialect: str = "fork") -> str:
    # fork: Postgres float8[] literal (matches tools/wiki_engine_load._vec_literal).
    # stock: pgvector vector literal (square brackets) — D2 phase 2.2 / Gate B.
    body = ",".join(repr(float(x)) for x in v)
    return f"[{body}]" if dialect == "stock" else "{" + body + "}"


def int_array_literal(vals: Iterable[int]) -> str:
    return "{" + ",".join(str(int(x)) for x in vals) + "}"


# ======================================================================================
# SQL text generation (pure; streamed to psql stdin, COPY data inline pg_dump-style)
# ======================================================================================
def sql_prologue(table: str, dim: int, force: bool, dialect: str = "fork") -> str:
    L = [
        "-- AUTO-GENERATED by tools/wikidata_engine_load.py — Wikidata tri-modal load",
        "-- (plan 060 / ADR-0018). Dense id == graph vid == table PK.",
        "\\set ON_ERROR_STOP on",
        "\\pset pager off",
        # fork: MSVBASE vectordb (float8[] + its hnsw AM). stock: pgvector (vector type
        # + its hnsw AM) on stock PG 17 — the D2 un-fork vector leg (ADR-0015 Option B/C).
        "CREATE EXTENSION IF NOT EXISTS vector;"
        if dialect == "stock"
        else "CREATE EXTENSION IF NOT EXISTS vectordb;",
    ]
    if force:
        L += [
            f"DROP TABLE IF EXISTS {table};",
            "-- reset the native store: gstore pages, gph_vid_map and the edge_type",
            "-- dictionary are extension members, so CASCADE recreates them empty.",
            "DROP EXTENSION IF EXISTS graph_store_am CASCADE;",
            "CREATE EXTENSION graph_store_am;",
        ]
    else:
        L += ["CREATE EXTENSION IF NOT EXISTS graph_store_am;"]
    emb_col = f"vector({dim})" if dialect == "stock" else f"float8[{dim}]"
    L += [
        "\\echo #WDL TABLE_CREATE",
        f"CREATE TABLE {table} (",
        "    id        bigint PRIMARY KEY,  -- dense vid == emission ordinal",
        "    qid       bigint NOT NULL,     -- sparse Wikidata Q-number (ext id)",
        "    P31       int[] NOT NULL DEFAULT '{}',  -- DENSE type ids",
        f"    embedding {emb_col}",
        ");",
        "\\echo #WDL COPY_ENTITIES_START",
        f"COPY {table} (id, qid, P31, embedding) FROM stdin;",
    ]
    return "\n".join(L) + "\n"


def entity_copy_row(
    dense_id: int, qid: int, p31: list[int], vec: list[float], dialect: str = "fork"
) -> str:
    return f"{dense_id}\t{qid}\t{int_array_literal(p31)}\t{vec_literal(vec, dialect)}\n"


def sql_vertex_verify(table: str, n: int) -> str:
    """Upsert vertices in emission order (ORDER BY id == ordinal order) and assert
    every returned vid equals the dense id — the ADR-0013/ADR-0018 (c) contract."""
    return f"""\\.
\\echo #WDL COPY_ENTITIES_DONE
DO $$ BEGIN
  IF (SELECT count(*) FROM {table}) <> {n} THEN
    RAISE EXCEPTION '{table} count % != expected {n}', (SELECT count(*) FROM {table});
  END IF;
END $$;
\\echo #WDL VERTEX_UPSERT_START
DO $$
DECLARE r RECORD; v bigint; expect bigint := 0;
BEGIN
  FOR r IN SELECT id, qid FROM {table} ORDER BY id LOOP
    IF r.id <> expect THEN
      RAISE EXCEPTION 'entity ids not dense: id % at ordinal %', r.id, expect;
    END IF;
    v := graph_store.gph_upsert_vertex(r.qid);
    IF v <> r.id THEN
      RAISE EXCEPTION 'DENSE-VID CONTRACT BROKEN: gph_upsert_vertex(Q%) returned vid % '
        'but the dense id is % — engine vid != table PK (ADR-0013/ADR-0018 (c)); aborting',
        r.qid, v, r.id;
    END IF;
    expect := expect + 1;
  END LOOP;
  RAISE NOTICE '#WDL VERTEX_UPSERT verified=%', expect;
END $$;
\\echo #WDL VERTEX_UPSERT_DONE
"""


def sql_etype_prologue() -> str:
    return (
        "CREATE TEMP TABLE etype_stage (pid int PRIMARY KEY);\n"
        "COPY etype_stage (pid) FROM stdin;\n"
    )


def sql_etype_register() -> str:
    """register_edge_type('P<m>') per distinct property (ADR-0016 dictionary)."""
    return """\\.
CREATE TEMP TABLE etype_map AS
  SELECT pid, graph_store.register_edge_type('P' || pid) AS type_id
  FROM etype_stage ORDER BY pid;
CREATE TEMP TABLE edge_stage (src bigint, pid int, dst bigint);
\\echo #WDL COPY_EDGES_START
COPY edge_stage (src, pid, dst) FROM stdin;
"""


def edge_copy_row(src: int, pid: int, dst: int) -> str:
    return f"{src}\t{pid}\t{dst}\n"


def sql_edge_insert(n_edges: int, n_vertices: int) -> str:
    """Typed edge insert by verified dense vid, ORDER BY src for chain locality."""
    return f"""\\.
\\echo #WDL COPY_EDGES_DONE
\\echo #WDL EDGE_INSERT_START
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM (
    SELECT graph_store.gph_insert_edge(e.src, e.dst, m.type_id)
    FROM edge_stage e JOIN etype_map m USING (pid)
    ORDER BY e.src
  ) _;
  IF n <> {n_edges} THEN
    RAISE EXCEPTION 'edge insert count % != staged {n_edges}', n;
  END IF;
END $$;
\\echo #WDL EDGE_INSERT_DONE
DROP TABLE edge_stage;
DO $$
DECLARE ec bigint; vc bigint;
BEGIN
  SELECT graph_store.gph_edge_count() INTO ec;
  SELECT graph_store.gph_vertex_count() INTO vc;
  IF ec <> {n_edges} THEN
    RAISE EXCEPTION 'gph_edge_count % != expected {n_edges}', ec;
  END IF;
  IF vc <> {n_vertices} THEN
    RAISE EXCEPTION 'gph_vertex_count % != expected {n_vertices}', vc;
  END IF;
  RAISE NOTICE '#WDL ASSERT edges=% vertices=% OK', ec, vc;
END $$;
"""


def sql_hnsw_and_health(
    table: str, dim: int, probe_vec: list[float], k: int, dialect: str = "fork"
) -> str:
    """HNSW build + health probe (top-k on a loaded row's own vector must return k rows).

    fork: MSVBASE hnsw AM (dimension/distmethod reloptions), exactly as
    tools/wiki_engine_load._build_load_sql. stock: pgvector hnsw AM with pinned
    m=16 / ef_construction=64 (the ADR-0015 E3 probe's parameters, disclosed)."""
    if dialect == "stock":
        index_sql = (
            # disclosed build resources: pgvector's HNSW build spills without enough
            # maintenance memory at 1M x 384; parallelism is pgvector-native
            "SET maintenance_work_mem = '8GB';\n"
            "SET max_parallel_maintenance_workers = 8;\n"
            f"CREATE INDEX {table}_hnsw ON {table} USING hnsw "
            f"(embedding vector_l2_ops) WITH (m = 16, ef_construction = 64);"
        )
    else:
        index_sql = (
            f"CREATE INDEX {table}_hnsw ON {table} USING hnsw(embedding)\n"
            f"    WITH (dimension = {dim}, distmethod = l2_distance);"
        )
    return f"""\\echo #WDL HNSW_BUILD_START
{index_sql}
\\echo #WDL HNSW_BUILD_DONE
SET enable_seqscan = off;  -- force the ANN scan for the probe
DO $$
DECLARE got int;
BEGIN
  SELECT count(*) INTO got FROM (
    SELECT id FROM {table} ORDER BY embedding <-> '{vec_literal(probe_vec, dialect)}' LIMIT {k}
  ) q;
  IF got <> {k} THEN
    RAISE EXCEPTION 'HNSW health probe returned % rows, expected {k} (unhealthy build)', got;
  END IF;
  RAISE NOTICE '#WDL HNSW_HEALTH rows=% OK', got;
END $$;
"""


def sql_epilogue(table: str) -> str:
    return f"""SELECT '#WDL FINAL entities=' || (SELECT count(*) FROM {table})
    || ' edges=' || graph_store.gph_edge_count()
    || ' vertices=' || graph_store.gph_vertex_count() AS line;
SELECT '#WDL ETYPE P' || pid || '=' || type_id AS line FROM etype_map ORDER BY pid;
\\echo #WDL LOAD_COMPLETE
"""


def iter_load_sql(
    slice_dir: Path,
    manifest: dict,
    emb,
    *,
    table: str = DEFAULT_TABLE,
    dim: int = DEFAULT_DIM,
    force: bool = False,
    stats: dict | None = None,
    dialect: str = "fork",
) -> Iterator[str]:
    """Yield the full load script (COPY data inline). Pure: no docker, host-testable.

    `emb` is the id-aligned array (row i == dense entity i); rows are L2-normalized at
    write. `stats` (optional dict) gains entities / edges_kept / edges_dropped /
    distinct_properties as the stream is consumed.
    """
    qmap, dense_to_qid = build_dense_map(slice_dir, manifest)
    n = len(dense_to_qid)
    if n == 0:
        raise SystemExit("empty slice: no entities in the manifest shards")
    if emb.shape[0] < n:
        raise SystemExit(
            f"embeddings have {emb.shape[0]} rows < N={n}; cannot load the slice"
        )
    if emb.shape[1] != dim:
        raise SystemExit(f"embeddings dim {emb.shape[1]} != --dim {dim}")
    p31 = load_p31_dense(slice_dir, manifest, qmap)

    # pass 1 over edge shards: distinct properties + the kept/dropped counts
    pre = {"kept": 0, "dropped": 0}
    pids: set[int] = set()
    for _s, pid, _d in iter_kept_edges(slice_dir, manifest, qmap, pre):
        pids.add(pid)
    if stats is not None:
        stats.update(
            entities=n,
            edges_kept=pre["kept"],
            edges_dropped_dangling=pre["dropped"],
            distinct_properties=len(pids),
        )

    yield sql_prologue(table, dim, force, dialect)
    probe_vec: list[float] | None = None
    for dense_id, qid in enumerate(dense_to_qid):
        vec = norm_vec(emb[dense_id])
        if probe_vec is None:
            probe_vec = vec
        yield entity_copy_row(dense_id, qid, p31.get(dense_id, []), vec, dialect)
    yield sql_vertex_verify(table, n)
    yield sql_etype_prologue()
    for pid in sorted(pids):
        yield f"{pid}\n"
    yield sql_etype_register()
    # pass 2: stream the kept edges into the stage table (same drop rule as pass 1)
    for src, pid, dst in iter_kept_edges(slice_dir, manifest, qmap):
        yield edge_copy_row(src, pid, dst)
    yield sql_edge_insert(pre["kept"], n)
    yield sql_hnsw_and_health(table, dim, probe_vec, min(PROBE_K, n), dialect)
    yield sql_epilogue(table)


# ======================================================================================
# Transcript parsing (pure)
# ======================================================================================
_FINAL_RE = re.compile(r"#WDL FINAL entities=(\d+) edges=(\d+) vertices=(\d+)")
_ETYPE_RE = re.compile(r"#WDL ETYPE P(\d+)=(\d+)")
_HEALTH_RE = re.compile(r"#WDL HNSW_HEALTH rows=(\d+) OK")


def parse_transcript(text: str) -> dict:
    """Harvest the engine-reported counts + edge-type map from the psql transcript."""
    out: dict = {"hnsw_healthy": bool(_HEALTH_RE.search(text)), "edge_type_map": {}}
    m = _FINAL_RE.search(text)
    if m:
        out["entities"] = int(m.group(1))
        out["edges"] = int(m.group(2))
        out["vertices"] = int(m.group(3))
    for pid, tid in _ETYPE_RE.findall(text):
        out["edge_type_map"][f"P{pid}"] = int(tid)
    return out


# ======================================================================================
# Docker runner (GX10-gated: needs the live engine container)
# ======================================================================================
def container_running(container: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def run_load(container: str, db: str, sql_iter: Iterator[str]) -> tuple[int, str]:
    """Stream the script into `docker exec -i <container> psql -f -` (wiki_h2h shape).

    stdout+stderr are drained on a thread while stdin streams, so a large COPY payload
    cannot deadlock against a chatty transcript.
    """
    proc = subprocess.Popen(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-U",
            "postgres",
            "-d",
            db,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: list[str] = []

    def _drain():
        for line in proc.stdout:
            lines.append(line)
            print(line, end="")

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    try:
        for chunk in sql_iter:
            proc.stdin.write(chunk)
    finally:
        proc.stdin.close()
    t.join()
    rc = proc.wait()
    return rc, "".join(lines)


# ======================================================================================
# CLI
# ======================================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Load a Wikidata slice into the TriDB engine container."
    )
    ap.add_argument(
        "--slice", type=Path, required=True, help="wikidata_ingest manifest dir"
    )
    ap.add_argument(
        "--emb",
        type=Path,
        default=None,
        help="id-aligned embeddings .npy (default <slice>/emb/dense_id_aligned.npy)",
    )
    ap.add_argument("--container", default=DEFAULT_CONTAINER)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--table", default=DEFAULT_TABLE)
    ap.add_argument("--dim", type=int, default=DEFAULT_DIM)
    ap.add_argument(
        "--force", action="store_true", help="drop + recreate table AND graph store"
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="load-manifest JSON (default <slice>/engine_load_manifest.json)",
    )
    ap.add_argument(
        "--emit-sql",
        type=Path,
        default=None,
        help="write the load script to FILE instead of executing (no docker needed)",
    )
    ap.add_argument(
        "--dialect",
        choices=("fork", "stock"),
        default="fork",
        help="fork = MSVBASE vectordb (float8[] + fork hnsw); stock = pgvector on "
        "stock PG 17 (vector type + pgvector hnsw) — D2 un-fork / Gate B",
    )
    args = ap.parse_args(argv)

    import numpy as np  # local: keep module import light for the pure-logic tests

    manifest = load_slice_manifest(args.slice)
    emb_path = args.emb or (args.slice / "emb" / "dense_id_aligned.npy")
    emb = np.load(emb_path, mmap_mode="r")
    stats: dict = {}
    sql_iter = iter_load_sql(
        args.slice,
        manifest,
        emb,
        table=args.table,
        dim=args.dim,
        force=args.force,
        stats=stats,
        dialect=args.dialect,
    )

    t0 = time.time()
    if args.emit_sql:
        with args.emit_sql.open("w") as f:
            for chunk in sql_iter:
                f.write(chunk)
        engine: dict = {"executed": False}
        rc = 0
        print(
            f"[wikidata_engine_load] EMITTED (not executed): {args.emit_sql} — "
            f"run on the GX10: docker exec -i {args.container} psql -U postgres "
            f"-d {args.db} -v ON_ERROR_STOP=1 -f - < {args.emit_sql}"
        )
    else:
        if not container_running(args.container):
            raise SystemExit(
                f"engine container '{args.container}' is not running — this load is "
                "GX10-gated (see CLAUDE.md hardware reality). Use --emit-sql to "
                "generate the script without executing, or --container to point at "
                "the live engine."
            )
        rc, transcript = run_load(args.container, args.db, sql_iter)
        engine = {"executed": True, **parse_transcript(transcript)}
        if rc != 0:
            print(f"[wikidata_engine_load] LOAD FAILED (psql rc={rc})", file=sys.stderr)
    secs = time.time() - t0

    out = args.out or (args.slice / "engine_load_manifest.json")
    load_manifest = {
        "tool": "tools/wikidata_engine_load.py",
        "created": datetime.now(timezone.utc).isoformat(),
        "slice_dir": str(args.slice),
        "emb_path": str(emb_path),
        "container": args.container,
        "db": args.db,
        "table": args.table,
        "dim": args.dim,
        "force": args.force,
        "counts": stats,
        "engine": engine,
        "durations": {"total_secs": round(secs, 2)},
        # the WH_ENGINE_EDGES analogue for bench/wikidata_h2h's publication gate
        "gate_env": {"WD_ENGINE_EDGES": engine.get("edges", stats.get("edges_kept"))},
    }
    out.write_text(json.dumps(load_manifest, indent=2))
    print(
        f"[wikidata_engine_load] entities={stats.get('entities')} "
        f"edges kept (both endpoints in-slice)={stats.get('edges_kept')} "
        f"(dropped dangling={stats.get('edges_dropped_dangling')}, "
        f"properties={stats.get('distinct_properties')}) -> {out}"
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
