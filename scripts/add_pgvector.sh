#!/usr/bin/env bash
# add_pgvector.sh — derive a pgvector-enabled TriDB fork image (gBrain-A-shim / advisor plan 039).
#
# gBrain (AgentBOX memory) assumes pgvector (vector(N) type, <=> cosine, hnsw vector_cosine_ops).
# TriDB's own vector leg is `vectordb` (float8[] + <->), NOT pgvector — incompatible at the type
# level. But gBrain does its fusion app-side and never uses TriDB's TJS operator, so gBrain-on-TriDB
# only needs pgvector (vector leg) + graph_store_am (native graph leg) in ONE database. We do NOT load
# `vectordb`, so pgvector's `hnsw` access method does not collide with TriDB's `hnsw` AM.
#
# This builds pgvector INSIDE a TriDB fork image (proven: v0.8.0 builds clean on aarch64 / PG 13.4)
# and commits a new image. Idempotent-ish: re-running rebuilds pgvector and re-commits.
#
# Usage: scripts/add_pgvector.sh [SRC_IMAGE] [DST_IMAGE] [PGVECTOR_TAG]
#   defaults: tridb/msvbase:gx10-v1  ->  tridb/msvbase:gx10-v1-pgv  (pgvector v0.8.0)
# Must run where the docker daemon has SRC_IMAGE (the GX10 / Spark). GX10-gated like every fork build.
set -euo pipefail

SRC_IMAGE="${1:-tridb/msvbase:gx10-v1}"
DST_IMAGE="${2:-tridb/msvbase:gx10-v1-pgv}"
# pgvector >= 0.8 floor: keep this in lockstep with the stock-image base tag
# (scripts/pg17/Dockerfile* -> PGVECTOR_VERSION) and the CREATE EXTENSION tjs_pg
# version guard (src/tjs_pg/tjs_pg--0.1.0.sql). The vector-first path needs
# hnsw.iterative_scan = relaxed_order, which pgvector only exposes from 0.8.
PGV_TAG="${3:-v0.8.0}"
PGCONFIG=/u01/app/postgres/product/13.4/bin/pg_config
BUILDER=pgvbuild_$$

docker image inspect "$SRC_IMAGE" >/dev/null 2>&1 || {
  echo "source image $SRC_IMAGE not present — build the fork first (scripts/gx10build.sh)"; exit 1; }

echo "[add_pgvector] building pgvector $PGV_TAG into $SRC_IMAGE ..."
docker rm -f "$BUILDER" >/dev/null 2>&1 || true
docker run --name "$BUILDER" --entrypoint bash "$SRC_IMAGE" -lc "
  set -e
  export PATH=/u01/app/postgres/product/13.4/bin:\$PATH
  cd /tmp && rm -rf pgvector
  git clone --depth 1 --branch $PGV_TAG https://github.com/pgvector/pgvector.git
  cd pgvector
  make       PG_CONFIG=$PGCONFIG
  make install PG_CONFIG=$PGCONFIG
  ls /u01/app/postgres/product/13.4/share/extension/vector.control >/dev/null
"
docker commit "$BUILDER" "$DST_IMAGE" >/dev/null
docker rm -f "$BUILDER" >/dev/null 2>&1 || true
echo "[add_pgvector] committed $DST_IMAGE (pgvector $PGV_TAG installed)"
echo "[add_pgvector] a gBrain DB then does: CREATE EXTENSION vector; CREATE EXTENSION graph_store_am;  (NOT vectordb)"
