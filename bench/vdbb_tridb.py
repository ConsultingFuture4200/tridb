"""VectorDBBench client adapter for TriDB — drives the RECOGNIZED harness at scale.

Lets the standard VectorDBBench tool run TriDB (esp. the filtered IntFilter cases,
e.g. Performance768D1M1P/99P on Cohere-1M) so the recall-vs-QPS-under-filter numbers
are produced by the same harness everyone else publishes with — not a bespoke script.
It speaks TriDB's actual vector surface (CREATE EXTENSION vectordb; float8[dim] column;
HNSW `WITH (dimension=, distmethod=l2_distance)`; `<->` ordering with a `WHERE` filter),
which is pgvector-adjacent, so this mirrors VDBB's pgvector client with TriDB's DDL.

STATUS: import-validated on the x86 standin. The full live case is GX10/engine-gated —
it needs a RUNNING TriDB Postgres (the engine container started with a published port)
and the Cohere-1M dataset download. Runbook at the bottom. The host-side filtered recall/
latency curve is already produced live by scripts/bench_filtered.sh (make bench-filtered);
this adapter is the bridge to the recognized tool for the multi-client QPS headline.
"""

from __future__ import annotations

from contextlib import contextmanager

from pydantic import BaseModel, SecretStr

from vectordb_bench.backend.clients.api import (
    DBCaseConfig,
    DBConfig,
    MetricType,
    VectorDB,
)
from vectordb_bench.backend.filter import Filter, FilterOp

TABLE = "vdbb_tridb"


class TriDBConfig(DBConfig):
    """Connection config for a running TriDB Postgres (the engine cluster)."""

    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: SecretStr = SecretStr("postgres")
    db_name: str = "postgres"

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password.get_secret_value() if self.password else None,
            "dbname": self.db_name,
        }


class TriDBCaseConfig(BaseModel, DBCaseConfig):
    """Index/search knobs. TriDB HNSW exposes m / ef_construction reloptions
    (DEV-1286); distmethod is fixed to l2_distance for the `<->` L2 path."""

    metric_type: MetricType = MetricType.L2
    m: int = 16
    ef_construction: int = 200

    def index_param(self) -> dict:
        return {"m": self.m, "ef_construction": self.ef_construction}

    def search_param(self) -> dict:
        return {}


class TriDB(VectorDB):
    """VectorDBBench client for TriDB (forked MSVBASE engine)."""

    name = "TriDB"
    # the filtered IntFilter case is the on-thesis target; NonFilter also supported.
    supported_filter_types = [FilterOp.NonFilter, FilterOp.NumGE]
    thread_safe = False  # one libpq connection per process

    def __init__(
        self,
        dim: int,
        db_config: dict,
        db_case_config: DBCaseConfig | None,
        collection_name: str = TABLE,
        drop_old: bool = False,
        **kwargs,
    ) -> None:
        self.dim = dim
        self.db_config = db_config
        self.case_config = db_case_config or TriDBCaseConfig()
        self.table = collection_name
        self._where = ""  # set by prepare_filter for the filtered cases
        if drop_old:
            with self.init():
                self._cur.execute(f"DROP TABLE IF EXISTS {self.table}")
                self._cur.execute("CREATE EXTENSION IF NOT EXISTS vectordb")
                self._cur.execute(
                    f"CREATE TABLE {self.table} "
                    f"(id bigint PRIMARY KEY, label int, embedding float8[{dim}])"
                )
                self._conn.commit()

    @contextmanager
    def init(self):
        import psycopg

        self._conn = psycopg.connect(**self.db_config)
        self._cur = self._conn.cursor()
        try:
            yield
        finally:
            self._cur.close()
            self._conn.close()
            self._conn = self._cur = None

    @staticmethod
    def _lit(vec: list[float]) -> str:
        return "{" + ",".join(repr(float(x)) for x in vec) + "}"

    def insert_embeddings(
        self,
        embeddings: list[list[float]],
        metadata: list[int],
        labels_data: list[str] | None = None,
        **kwargs,
    ) -> tuple[int, Exception | None]:
        try:
            # metadata[i] is the int id; reuse it (mod 100) as the filterable label
            rows = ",".join(
                f"({int(metadata[i])},{int(metadata[i]) % 100},'{self._lit(e)}'::float8[])"
                for i, e in enumerate(embeddings)
            )
            self._cur.execute(
                f"INSERT INTO {self.table} (id,label,embedding) VALUES {rows} "
                "ON CONFLICT (id) DO NOTHING"
            )
            self._conn.commit()
            return len(embeddings), None
        except Exception as e:  # noqa: BLE001 — VDBB contract returns the error
            return 0, e

    def optimize(self, data_size: int | None = None):
        p = self.case_config.index_param()
        self._cur.execute(
            f"CREATE INDEX IF NOT EXISTS {self.table}_hnsw ON {self.table} "
            f"USING hnsw(embedding) WITH (dimension = {self.dim}, "
            f"distmethod = l2_distance, m = {p['m']}, ef_construction = {p['ef_construction']})"
        )
        self._conn.commit()

    def prepare_filter(self, filters: Filter):
        """IntFilter -> `WHERE label >= v`; NonFilter -> no predicate."""
        if filters.type == FilterOp.NumGE:
            self._where = f"WHERE label >= {int(filters.int_value)}"
        else:
            self._where = ""

    def search_embedding(self, query: list[float], k: int = 100) -> list[int]:
        self._cur.execute(
            f"SELECT id FROM {self.table} {self._where} "
            f"ORDER BY embedding <-> '{self._lit(query)}' LIMIT {k}"
        )
        return [r[0] for r in self._cur.fetchall()]


# --------------------------------------------------------------------------- #
# GX10 RUNBOOK (the recognized-harness, multi-client QPS headline — engine-gated)
# --------------------------------------------------------------------------- #
# 1. Start a persistent TriDB server from the engine image with a published port:
#      docker run -d -p 5432:5432 --entrypoint bash tridb/msvbase:gx10 -c \
#        '... build+install src/graph_store_ext; initdb; pg_ctl start -o "-p 5432 -h 0.0.0.0"; sleep inf'
# 2. Register this client with VectorDBBench (entry in its client registry / use the
#    python API: pass TriDB + TriDBConfig + TriDBCaseConfig).
# 3. Run the filtered case:  vectordbbench tridb --case Performance768D1M1P ...
#    (Cohere-1M, 1% filter). The host-side recall/latency curve is already live via
#    `make bench-filtered`; this yields the concurrent-QPS-under-filter headline.
