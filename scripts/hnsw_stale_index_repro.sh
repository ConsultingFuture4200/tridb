#!/usr/bin/env bash
#
# hnsw_stale_index_repro.sh — repro for the process-global HNSW in-RAM index-map
# cache-invalidation gap (advisor plan 023; relates to DEV-1259 Phase C, UPCORE-02).
#
# Proves that a single long-lived (pooled) backend serves a STALE or wrong-dimension
# HNSW graph after DROP+CREATE (same name) / REINDEX / recreate-at-new-dimension,
# because MSVBASE's process-global `vector_index_map` (src/hnswindex_scan.cpp:27-28)
# is populated once on a LoadIndex cache-miss (~line 113) and NEVER erased.
#
# The whole scenario runs on a SINGLE psql session on purpose: the map is a static
# class member, i.e. process-global PER BACKEND. A fresh backend would cache-miss and
# (correctly) rebuild, hiding the bug — so all four scenarios must share one backend.
#
# Requires: scripts/x86build.sh --docker has produced tridb/msvbase:dev (engine-gated).
# Usage: scripts/hnsw_stale_index_repro.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL="$ROOT/test/hnsw_stale_index.sql"

[[ -f "$SQL" ]] || { echo "stale-index sql not found: $SQL" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || { echo "image $IMAGE not built — run scripts/x86build.sh --docker (engine-gated)" >&2; exit 1; }

docker run --rm --user root --entrypoint bash \
  -v "${SQL}:/tmp/hnsw_stale_index.sql:ro" "$IMAGE" -c '
set -e
B=/u01/app/postgres/product/13.4/bin

echo "=== HNSW stale-index-map repro (advisor plan 023 / DEV-1259) ==="

D=/tmp/pg_hnsw_stale
rm -rf "$D"; mkdir -p "$D"; chown postgres:postgres "$D"
runuser -u postgres -- "$B/initdb" -A trust -D "$D" >/dev/null 2>&1
runuser -u postgres -- "$B/pg_ctl" -D "$D" -o "-p 5432" -w start >/dev/null 2>&1

echo "--- running all four scenarios on ONE persistent connection (one backend) ---"
# One psql invocation == one backend == the process-global map persists across
# scenarios. Scenario D may crash the backend (the demonstrated OOB read); tolerate
# a nonzero exit so the driver still reports what happened.
runuser -u postgres -- "$B/psql" -p 5432 -d postgres -f /tmp/hnsw_stale_index.sql || \
  echo "(psql exited nonzero — see output above; scenario D OOB may have crashed the backend)"

runuser -u postgres -- "$B/pg_ctl" -D "$D" -m fast stop >/dev/null 2>&1 || true
' 2>&1 | grep -vE "redirecting log|logging collector"
echo "[hnsw_stale_index_repro] done — inspect returned_id vs fresh_id per scenario (A..D)"
