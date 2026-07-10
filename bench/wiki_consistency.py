"""One-WAL cross-modal CONSISTENCY demonstrator for TriDB (DEV-1354, ADR-0017).

The other half of the wiki value story. `docs/benchmark_wiki_fusion_v0.1.0.md` measured the
FUSION SPEED win (one fused `tjs_open` round-trip vs a 3-store app-side pipeline). This harness
measures the FUSION CONSISTENCY win: the transactional guarantee TriDB gives across the vector +
graph + relational modalities *because they live in one Postgres process under one WAL*, that a
3-separate-store stack (Milvus + Neo4j + pgvector) structurally cannot give.

A "multi-modal update" on entity E changes the three mutually-dependent modalities at once:
  (a) E's embedding      (the VECTOR leg — engine `embedding` column / Milvus)
  (b) E's graph out-edge (the GRAPH  leg — engine native `graph_store` / Neo4j)
  (c) E's relational attr (the REL    leg — engine table column / pgvector-side Postgres)
Each modality carries an integer version `v`; an entity is CONSISTENT iff all three legs agree on
`v`, and TORN iff they disagree. Every leg is written to version `v` by a multi-modal update.

Three scenarios, each run head-to-head (TriDB in ONE transaction vs the app-side multi-store):

  1. ATOMICITY under injected failure. TriDB: all three writes in one txn; on the injected ones we
     roll back before COMMIT. Multi-store: write Milvus -> Neo4j -> pgvector sequentially; on the
     injected ones we stop after store 1. TriDB rolls back atomically (0 torn); the multi-store's
     partial write PERSISTS (Milvus has v=1, the others v=0) -> a torn entity nothing reconciles.

  2. CRASH consistency. TriDB: an uncommitted multi-modal txn is in flight when the engine is
     crashed (`pg_ctl -m immediate` = SIGQUIT, no checkpoint) and restarted -> WAL crash recovery
     leaves E fully-old (atomic) while a committed sibling is fully-new (durable). Multi-store: a
     partial write (Milvus only) is durably persisted with NO cross-store log to recover it -> the
     torn entity survives as an orphan.

  3. READ ISOLATION (torn reads). A concurrent reader during a live multi-modal update. TriDB reads
     all three legs in ONE statement (one MVCC snapshot) -> always all-old or all-new. The multi-
     store reader hits the three stores at three instants -> catches Milvus-after-write /
     Neo4j-before-write = a torn read. We report the observed torn-read rate.

HONESTY (this is a value claim, not marketing):
  * The multi-store inconsistency is INHERENT to having no cross-system transaction — it is NOT a
    Milvus/Neo4j/pgvector bug. Each store is internally consistent (we read Milvus at STRONG
    consistency so we never sandbag it); the tear is strictly CROSS-store.
  * It IS mitigable app-side — 2PC, sagas, an outbox, or a reconciliation job — but each adds real
    code, latency, and a new failure surface. TriDB gives cross-modal ACID for FREE (one txn mgr,
    one WAL); the multi-store can only APPROXIMATE it with significant engineering. Different
    tradeoff, not "broken".
  * Nothing here is fabricated. Counts are observed; the crash is a real unclean shutdown + WAL
    recovery. Scale is deliberately small — consistency is about correctness, not throughput.

RUN LOCATION: the Spark, where the isolated `tridb-wiki-*` baseline stores live and where a
throwaway engine container (`tridb-consistency`, image gx10-v1-batchedge) can be crashed freely
without touching the 200k wiki load, the running reader, or the SM-2 baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import threading
import time
from pathlib import Path

import psycopg
from neo4j import GraphDatabase
from pymilvus import DataType, MilvusClient

# ======================================================================================
# Config (env-overridable; defaults match the live Spark layout, 2026-07-08)
# ======================================================================================
EDIM = int(os.environ.get("WC_EDIM", "8"))
PGBIN = "/u01/app/postgres/product/13.4/bin"

ENGINE_CONTAINER = os.environ.get("WC_ENGINE_CONTAINER", "tridb-consistency")
ENGINE_HOST = os.environ.get("WC_ENGINE_HOST", "127.0.0.1")
ENGINE_PORT = os.environ.get("WC_ENGINE_PORT", "5455")
ENGINE_DB = os.environ.get("WC_ENGINE_DB", "cons")

MILVUS_URI = os.environ.get("WC_MILVUS_URI", "http://127.0.0.1:19531")
MILVUS_COLL = os.environ.get("WC_MILVUS_COLL", "cons_vec")
NEO4J_URI = os.environ.get("WC_NEO4J_URI", "bolt://127.0.0.1:7688")
NEO4J_USER = os.environ.get("WC_NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("WC_NEO4J_PASS", "wikipassword")
PG_HOST = os.environ.get("WC_PGHOST", "127.0.0.1")
PG_PORT = os.environ.get("WC_PGPORT", "5434")
PG_DB = os.environ.get("WC_PGDB", "tridb_wiki")
PG_USER = os.environ.get("WC_PGUSER", "postgres")
PG_PASS = os.environ.get("WC_PGPASS", "postgres")
PG_TABLE = os.environ.get("WC_PGTABLE", "cons_rel")


def vec(v: int) -> list[float]:
    """version-encoding embedding: leg (a). First component == version; rest 0."""
    return [float(v)] + [0.0] * (EDIM - 1)


# ======================================================================================
# TriDB engine (one process / one WAL): vector + graph + relational in one txn.
#   entity i : table id == graph vid == i          (ids 0..M-1)
#   targets  : graph-only vertices  M+2i (v=0), M+2i+1 (v=1)   -> dense range [0, 3M)
# ======================================================================================


def engine_connect(db: str = ENGINE_DB, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(
        host=ENGINE_HOST,
        port=ENGINE_PORT,
        dbname=db,
        user="postgres",
        autocommit=autocommit,
        connect_timeout=10,
    )


def engine_setup(m: int) -> None:
    """Fresh `cons` DB: M entities at v=0 across all three legs (one store, one WAL)."""
    adm = engine_connect(db="postgres", autocommit=True)
    adm.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname=%s AND pid<>pg_backend_pid()",
        (ENGINE_DB,),
    )
    adm.execute(f"DROP DATABASE IF EXISTS {ENGINE_DB}")
    adm.execute(f"CREATE DATABASE {ENGINE_DB}")
    adm.close()
    c = engine_connect(autocommit=True)
    c.execute("CREATE EXTENSION vectordb")
    c.execute("CREATE EXTENSION graph_store_am")
    c.execute("CREATE TABLE cons (id int PRIMARY KEY, attr int, embedding float8[])")
    with c.cursor() as cur:
        for i in range(m):
            cur.execute(
                "INSERT INTO cons(id,attr,embedding) VALUES (%s,0,%s)", (i, vec(0))
            )
    # dense vertices 0..3M-1 in id order -> identity mode (vid == ext_id)
    c.execute(
        "SELECT count(*) FROM (SELECT graph_store.gph_upsert_vertex(g) "
        f"FROM (SELECT g FROM generate_series(0,{3 * m - 1}) g ORDER BY g) s) _"
    )
    with c.cursor() as cur:
        for i in range(m):
            cur.execute(
                "SELECT graph_store.gph_insert_edge(%s,%s)", (i, m + 2 * i)
            )  # v=0
    c.execute("SELECT graph_store.gph_set_identity_mode(true)")
    c.close()


def engine_write(cur, m: int, i: int, new_v: int, old_v: int) -> None:
    """One multi-modal write inside the caller's transaction: (a) vector + (c) rel in the
    row UPDATE, (b) graph out-edge flipped old->new. Caller COMMITs or ROLLBACKs the lot."""
    cur.execute(
        "UPDATE cons SET attr=%s, embedding=%s WHERE id=%s", (new_v, vec(new_v), i)
    )
    base = m + 2 * i
    cur.execute("SELECT graph_store.gph_tombstone_edge(%s,%s)", (i, base + old_v))
    cur.execute("SELECT graph_store.gph_insert_edge(%s,%s)", (i, base + new_v))


def engine_read(cur, m: int, i: int) -> tuple[int, int, int]:
    """Read all three legs of entity i in ONE statement == one MVCC snapshot."""
    cur.execute(
        "SELECT attr, embedding[1], "
        "(SELECT array_agg(n) FROM graph_store.gph_neighbors_ext(id) n) "
        "FROM cons WHERE id=%s",
        (i,),
    )
    attr, emb0, neigh = cur.fetchone()
    base = m + 2 * i
    gv = next((int(n) - base for n in (neigh or []) if int(n) - base in (0, 1)), -1)
    return int(round(emb0)), gv, int(attr)  # (vector, graph, relational)


def engine_crash_restart(log: list[str]) -> None:
    """Real unclean shutdown (SIGQUIT, no checkpoint) + restart -> WAL crash recovery."""

    def d(*a):
        return subprocess.run(
            ["docker", "exec", ENGINE_CONTAINER, *a], capture_output=True, text=True
        )

    d(f"{PGBIN}/pg_ctl", "-D", "/tmp/pg", "-m", "immediate", "stop")
    log.append("  [crash] pg_ctl -m immediate stop (SIGQUIT; no shutdown checkpoint)")
    r = subprocess.run(
        [
            "docker",
            "exec",
            ENGINE_CONTAINER,
            "bash",
            "-lc",
            f"{PGBIN}/pg_ctl -D /tmp/pg -l /tmp/pg.log "
            f'-o "-p 5432 -c listen_addresses=* -c statement_timeout=0" -w start',
        ],
        capture_output=True,
        text=True,
    )
    log.append(
        f"  [restart] {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()}"
    )
    rl = subprocess.run(
        [
            "docker",
            "exec",
            ENGINE_CONTAINER,
            "bash",
            "-lc",
            "grep -iE 'not been properly|not properly shut down|redo starts|redo done' /tmp/pg.log | tail -3",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    for line in rl.splitlines():
        log.append(f"  [recovery] {line.split('UTC')[-1].strip() or line.strip()}")
    time.sleep(1)


# ======================================================================================
# Multi-store (three separate systems, no cross-system transaction): Milvus / Neo4j / pg.
#   entity i node  :CN{id:i};  target nodes :CT{id:2i} (v=0), :CT{id:2i+1} (v=1)
# ======================================================================================


class MultiStore:
    def __init__(self):
        self.mc = MilvusClient(uri=MILVUS_URI)
        self.neo = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
        self.pg = psycopg.connect(
            host=PG_HOST,
            port=PG_PORT,
            dbname=PG_DB,
            user=PG_USER,
            password=PG_PASS,
            autocommit=True,
        )

    def setup(self, m: int) -> None:
        if self.mc.has_collection(MILVUS_COLL):
            self.mc.drop_collection(MILVUS_COLL)
        s = self.mc.create_schema(auto_id=False, enable_dynamic_field=False)
        s.add_field("id", DataType.INT64, is_primary=True)
        s.add_field("ver", DataType.INT64)
        s.add_field("vector", DataType.FLOAT_VECTOR, dim=EDIM)
        ip = self.mc.prepare_index_params()
        ip.add_index(field_name="vector", index_type="FLAT", metric_type="L2")
        self.mc.create_collection(MILVUS_COLL, schema=s, index_params=ip)
        self.mc.load_collection(MILVUS_COLL)
        self.mc.upsert(
            MILVUS_COLL, [{"id": i, "ver": 0, "vector": vec(0)} for i in range(m)]
        )
        with self.neo.session() as ss:
            ss.run("MATCH (n:CN) DETACH DELETE n")
            ss.run("MATCH (n:CT) DETACH DELETE n")
            ss.run(
                "UNWIND range(0,$m-1) AS i "
                "CREATE (:CN {id:i}) "
                "WITH i CREATE (:CT {id:2*i}) CREATE (:CT {id:2*i+1})",
                m=m,
            )
            ss.run(
                "MATCH (a:CN),(t:CT) WHERE t.id = 2*a.id CREATE (a)-[:OUT]->(t)"  # v=0
            )
        self.pg.execute(f"DROP TABLE IF EXISTS {PG_TABLE}")
        self.pg.execute(f"CREATE TABLE {PG_TABLE} (id int PRIMARY KEY, ver int)")
        with self.pg.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {PG_TABLE}(id,ver) VALUES (%s,0)",
                [(i,) for i in range(m)],
            )

    # --- individual store writes (deliberately NOT wrapped in any cross-store txn) ---
    def w_milvus(self, i, v):
        self.mc.upsert(MILVUS_COLL, [{"id": i, "ver": v, "vector": vec(v)}])

    def w_neo4j(self, i, v):
        with self.neo.session() as ss:
            ss.run(
                "MATCH (a:CN {id:$i})-[r:OUT]->() DELETE r "
                "WITH a MATCH (t:CT {id:2*$i+$v}) CREATE (a)-[:OUT]->(t)",
                i=i,
                v=v,
            )

    def w_pg(self, i, v):
        self.pg.execute(f"UPDATE {PG_TABLE} SET ver=%s WHERE id=%s", (v, i))

    def write(self, i, v, inject_after: int | None = None, gap: float = 0.0):
        """Sequential multi-modal write. If inject_after is set, STOP after that store index
        (1=after Milvus, 2=after Neo4j) simulating a failure with no rollback path."""
        self.w_milvus(i, v)
        if inject_after == 1:
            return
        if gap:
            time.sleep(gap)
        self.w_neo4j(i, v)
        if inject_after == 2:
            return
        if gap:
            time.sleep(gap)
        self.w_pg(i, v)

    # --- reads (three separate round-trips at three instants) ---
    def r_milvus(self, i) -> int:
        r = self.mc.query(
            MILVUS_COLL,
            filter=f"id == {i}",
            output_fields=["ver"],
            consistency_level="Strong",
        )
        return int(r[0]["ver"]) if r else -1

    def r_neo4j(self, i) -> int:
        with self.neo.session() as ss:
            rec = ss.run(
                "MATCH (a:CN {id:$i})-[:OUT]->(t) RETURN t.id AS tid", i=i
            ).single()
        return (int(rec["tid"]) - 2 * i) if rec else -1

    def r_pg(self, i) -> int:
        with self.pg.cursor() as cur:
            cur.execute(f"SELECT ver FROM {PG_TABLE} WHERE id=%s", (i,))
            row = cur.fetchone()
        return int(row[0]) if row else -1

    def read(self, i) -> tuple[int, int, int]:
        return self.r_milvus(i), self.r_neo4j(i), self.r_pg(i)

    def close(self):
        self.neo.close()
        self.pg.close()


def torn(legs: tuple[int, int, int]) -> bool:
    return len(set(legs)) != 1


# ======================================================================================
# SCENARIO 1 — atomicity under injected failure
# ======================================================================================


def scenario1(m: int, fail_rate: float, seed: int) -> dict:
    print(
        f"\n=== SCENARIO 1: atomicity under injected failure (M={m}, fail_rate={fail_rate}) ==="
    )
    rng = random.Random(seed)
    injected = {i for i in range(m) if rng.random() < fail_rate}
    n_inj = len(injected)

    # ---- TriDB: three writes in ONE txn; injected ones roll back before COMMIT ----
    engine_setup(m)
    conn = engine_connect(autocommit=False)
    for i in range(m):
        with conn.cursor() as cur:
            engine_write(cur, m, i, new_v=1, old_v=0)
        if i in injected:
            conn.rollback()  # injected failure before COMMIT -> atomic discard
        else:
            conn.commit()
    conn.close()
    tri_torn = tri_bad = 0
    rc = engine_connect(autocommit=True)
    with rc.cursor() as cur:
        for i in range(m):
            legs = engine_read(cur, m, i)
            if torn(legs):
                tri_torn += 1
            want = 0 if i in injected else 1
            if legs != (want, want, want):
                tri_bad += 1
    rc.close()

    # ---- Multi-store: Milvus -> Neo4j -> pg; injected ones stop after Milvus ----
    ms = MultiStore()
    ms.setup(m)
    for i in range(m):
        ms.write(i, 1, inject_after=1 if i in injected else None)
    ms_torn = 0
    ms_examples = []
    for i in range(m):
        legs = ms.read(i)
        if torn(legs):
            ms_torn += 1
            if len(ms_examples) < 4:
                ms_examples.append(
                    {"id": i, "milvus": legs[0], "neo4j": legs[1], "pg": legs[2]}
                )
    ms.close()

    print(f"  injected failures: {n_inj}/{m}")
    print(
        f"  TriDB (one txn/one WAL): torn={tri_torn}, wrong-state={tri_bad}  -> all injected atomically rolled back"
    )
    print(
        f"  Multi-store (3 stores):  torn={ms_torn}  -> every injected op left a partial write nothing reconciles"
    )
    print(f"  example torn entities (multi-store): {ms_examples}")
    return {
        "M": m,
        "fail_rate": fail_rate,
        "n_injected": n_inj,
        "tridb_inconsistency_count": tri_torn,
        "tridb_wrong_state_count": tri_bad,
        "multistore_inconsistency_count": ms_torn,
        "tridb_inconsistency_rate": tri_torn / m,
        "multistore_inconsistency_rate": ms_torn / m,
        "multistore_torn_examples": ms_examples,
    }


# ======================================================================================
# SCENARIO 2 — crash consistency
# ======================================================================================


def scenario2(m: int) -> dict:
    print(
        "\n=== SCENARIO 2: crash consistency (real unclean shutdown + WAL recovery) ==="
    )
    log: list[str] = []
    engine_setup(m)
    A, B = 0, 1  # A: uncommitted at crash (must roll back); B: committed (must survive)

    cB = engine_connect(autocommit=False)
    with cB.cursor() as cur:
        engine_write(cur, m, B, new_v=1, old_v=0)
    cB.commit()
    cB.close()
    log.append(f"  entity {B}: multi-modal update to v=1 COMMITTED")

    cA = engine_connect(autocommit=False)
    with cA.cursor() as cur:
        engine_write(cur, m, A, new_v=1, old_v=0)  # left UNCOMMITTED, in flight
    log.append(
        f"  entity {A}: multi-modal update to v=1 written but NOT committed (txn in flight)"
    )

    engine_crash_restart(log)
    try:
        cA.close()
    except Exception:
        pass

    rc = engine_connect(autocommit=True)
    with rc.cursor() as cur:
        legsA = engine_read(cur, m, A)
        legsB = engine_read(cur, m, B)
    rc.close()
    a_ok = legsA == (0, 0, 0)  # uncommitted -> fully old, atomic
    b_ok = legsB == (1, 1, 1)  # committed   -> fully new, durable
    log.append(
        f"  post-recovery entity {A} (vector,graph,rel) = {legsA}  -> {'PASS all-old (atomic rollback)' if a_ok else 'FAIL'}"
    )
    log.append(
        f"  post-recovery entity {B} (vector,graph,rel) = {legsB}  -> {'PASS all-new (durable)' if b_ok else 'FAIL'}"
    )
    tridb_pass = a_ok and b_ok and not torn(legsA) and not torn(legsB)

    # ---- Multi-store: partial write, "crash" (stop before Neo4j/pg), orphan persists ----
    ms = MultiStore()
    ms.setup(m)
    C = 0
    ms.write(C, 1, inject_after=1)  # Milvus flushed v=1; process dies before Neo4j/pg
    legsC = ms.read(C)
    ms.close()
    ms_torn = torn(legsC)
    log.append(
        f"  multi-store entity {C}: crash after Milvus write -> (milvus,neo4j,pg)={legsC} "
        f"-> {'TORN orphan persists (no cross-store WAL to recover it)' if ms_torn else 'unexpectedly consistent'}"
    )
    for line in log:
        print(line)
    return {
        "M": m,
        "tridb_uncommitted_legs": list(legsA),
        "tridb_committed_legs": list(legsB),
        "tridb_crash_recovery_pass": bool(tridb_pass),
        "multistore_partial_legs": list(legsC),
        "multistore_torn_after_crash": bool(ms_torn),
        "transcript": log,
    }


# ======================================================================================
# SCENARIO 3 — read isolation (torn reads under concurrency)
# ======================================================================================


def scenario3(n_reads: int, gap_ms: float) -> dict:
    print(
        f"\n=== SCENARIO 3: torn reads under concurrency (n_reads={n_reads}, writer inter-store gap={gap_ms}ms) ==="
    )
    gap = gap_ms / 1000.0
    m = 4
    H = 0  # the hot entity being flipped

    # ---- TriDB: writer flips H in committed txns; reader reads all 3 legs in ONE stmt ----
    engine_setup(m)
    stop = threading.Event()

    def tri_writer():
        c = engine_connect(autocommit=False)
        v = 0
        while not stop.is_set():
            nv = 1 - v
            with c.cursor() as cur:
                engine_write(cur, m, H, new_v=nv, old_v=v)
            c.commit()
            v = nv
        c.close()

    tw = threading.Thread(target=tri_writer)
    tw.start()
    tri_torn = 0  # any leg disagrees
    tri_heap_torn = 0  # the two heap legs (vector,relational) disagree — should be 0
    tri_examples = []
    rc = engine_connect(autocommit=True)
    for _ in range(n_reads):
        with rc.cursor() as cur:
            legs = engine_read(cur, m, H)  # (vector, graph, relational)
        if torn(legs):
            tri_torn += 1
            if len(tri_examples) < 4:
                tri_examples.append(
                    {"vector": legs[0], "graph": legs[1], "relational": legs[2]}
                )
        if (
            legs[0] != legs[2]
        ):  # vector vs relational (both heap-resident, one snapshot)
            tri_heap_torn += 1
    rc.close()
    stop.set()
    tw.join()

    # ---- Multi-store: writer flips H across 3 stores; reader reads 3 stores sequentially ----
    ms = MultiStore()
    ms.setup(m)
    stop2 = threading.Event()

    def ms_writer():
        w = MultiStore()  # own connections (thread safety)
        v = 0
        while not stop2.is_set():
            nv = 1 - v
            w.write(H, nv, gap=gap)
            v = nv
        w.close()

    mw = threading.Thread(target=ms_writer)
    mw.start()
    ms_torn = 0
    ms_examples = []
    for _ in range(n_reads):
        legs = ms.read(H)
        if torn(legs):
            ms_torn += 1
            if len(ms_examples) < 4:
                ms_examples.append({"milvus": legs[0], "neo4j": legs[1], "pg": legs[2]})
    stop2.set()
    mw.join()
    ms.close()

    print(
        f"  TriDB total torn reads = {tri_torn}/{n_reads} ({100 * tri_torn / n_reads:.1f}%); of these the "
        f"heap legs (vector,relational) tore {tri_heap_torn}/{n_reads} ({100 * tri_heap_torn / n_reads:.1f}%)"
    )
    print(
        "    -> vector+relational (heap, one MVCC snapshot) NEVER tear; residual tears are the GRAPH leg only,"
    )
    print(
        "       whose v1 read path is commit-visible not snapshot-isolated (DEV-1166) -> a narrow intra-stmt window."
    )
    print(f"    example TriDB torn reads: {tri_examples}")
    print(
        f"  Multi-store (3 reads, 3 instants):    torn reads = {ms_torn}/{n_reads} ({100 * ms_torn / n_reads:.1f}%)"
    )
    print(f"    example torn reads (multi-store): {ms_examples}")
    return {
        "n_reads": n_reads,
        "writer_gap_ms": gap_ms,
        "tridb_torn_reads": tri_torn,
        "tridb_torn_rate": tri_torn / n_reads,
        "tridb_heap_leg_torn_reads": tri_heap_torn,
        "tridb_heap_leg_torn_rate": tri_heap_torn / n_reads,
        "tridb_torn_examples": tri_examples,
        "tridb_note": (
            "vector+relational are heap-resident and share one MVCC snapshot -> never tear; "
            "residual TriDB tears are the native graph leg only, whose v1 read path is "
            "commit-visible (gph_xmin_visible = TransactionIdDidCommit) not snapshot-isolated "
            "(full per-tuple snapshot isolation is deferred to DEV-1166). Narrow intra-statement window."
        ),
        "multistore_torn_reads": ms_torn,
        "multistore_torn_rate": ms_torn / n_reads,
        "multistore_torn_examples": ms_examples,
    }


# ======================================================================================
# CLI
# ======================================================================================


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--entities", type=int, default=int(os.environ.get("WC_ENTITIES", "100"))
    )
    ap.add_argument("--fail-rate", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=1354)
    ap.add_argument("--reads", type=int, default=300)
    ap.add_argument("--gap-ms", type=float, default=5.0)
    ap.add_argument(
        "--out", type=Path, default=Path("bench/results/wiki_consistency.json")
    )
    ap.add_argument("--scenarios", default="1,2,3")
    args = ap.parse_args(argv)

    which = set(args.scenarios.split(","))
    results: dict = {
        "meta": {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "edim": EDIM,
            "engine_container": ENGINE_CONTAINER,
            "engine": "tridb/msvbase:gx10-v1-batchedge (one Postgres process, one WAL)",
            "multistore": "Milvus 19531 (STRONG reads) + Neo4j 7688 + pgvector 5434 (isolated tridb-wiki-*)",
            "note": "small entity set: consistency is correctness, not scale",
        }
    }
    if "1" in which:
        results["scenario1_atomicity"] = scenario1(
            args.entities, args.fail_rate, args.seed
        )
    if "2" in which:
        results["scenario2_crash"] = scenario2(max(2, args.entities))
    if "3" in which:
        results["scenario3_torn_reads"] = scenario3(args.reads, args.gap_ms)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2))
    print(f"\n[wiki_consistency] raw results -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
