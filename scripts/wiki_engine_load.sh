#!/usr/bin/env bash
#
# wiki_engine_load.sh — RUN a prepared full-Wikipedia tri-modal load (tools/wiki_engine_load.py
# prepare) against a LIVE MSVBASE-fork engine image (DEV-1354, Phase 2).
#
# Mirrors scripts/bench_sm2.sh: in the image, PGXS-build + install src/graph_store (the v1
# native AM, graph_store_am) into a throwaway cluster, initdb, then run the generated
# /data/load.sql — which \copy-loads articles, builds the HNSW index, materializes the dense
# vertices, bulk-inserts the induced edges by vid, flips identity mode, and asserts the
# native row/edge counts + a sample tjs_open (top-k, early termination, bridge injection).
#
# The prep dir (from `python tools/wiki_engine_load.py prepare --out DIR ...`) must contain
# articles.copy, edges.copy, load.sql. It is mounted read-only as /data.
#
# vectordb is already installed in the fork image; only graph_store_am is rebuilt from source.
#
# Usage: scripts/wiki_engine_load.sh <image> <prep_dir>
#   e.g. scripts/wiki_engine_load.sh tridb/msvbase:gx10-v1 /home/bob/wiki_load_100k
#
set -euo pipefail
IMAGE="${1:?usage: wiki_engine_load.sh <image> <prep_dir>}"
PREP="${2:?usage: wiki_engine_load.sh <image> <prep_dir>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"   # v1 native AM (graph_store_am, ADR-0013 Stage B)

PREP="$(cd "$PREP" && pwd)"
[ -f "$PREP/load.sql" ] || { echo "no load.sql in $PREP — run tools/wiki_engine_load.py prepare first" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built" >&2; exit 1; }

# \copy reads /data/* as the in-container psql user; make the prep files world-readable so the
# load works regardless of the container's uid mapping.
chmod -R a+rX "$PREP" 2>/dev/null || true

echo "[wiki_load] loading $PREP into LIVE engine ($IMAGE)"
docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${PREP}:/data:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  if ! make PG_CONFIG=$PGC >/tmp/make.log 2>&1; then echo "BUILD FAILED:"; tail -40 /tmp/make.log; exit 1; fi
  if ! make PG_CONFIG=$PGC install >/tmp/install.log 2>&1; then echo "INSTALL FAILED:"; tail -40 /tmp/install.log; exit 1; fi
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  # modest work/maintenance budget for the bulk load + HNSW build (box RAM is tight while the
  # sanctioned link-pred job is resident); PGMEM overrides it for a roomier host.
  $B/pg_ctl -D $D -o "-p 5432 -c maintenance_work_mem=${PGMEM:-1GB} -c work_mem=128MB" -w start >/dev/null 2>&1
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /data/load.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[wiki_load] done"
