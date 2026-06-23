#!/usr/bin/env bash
#
# smoke_test.sh — run the Phase-0 relational+vector smoke test (DEV-1162) against the
# built MSVBASE fork image (tridb/msvbase:dev), proving the vectordb extension loads,
# the HNSW index builds, and the early-terminating ANN Index Scan path works.
#
# Requires: scripts/x86build.sh --docker has produced tridb/msvbase:dev.
# Usage: scripts/smoke_test.sh [image] [sql]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
SQL="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/test/smoke.sql}"

[[ -f "$SQL" ]] || { echo "smoke sql not found: $SQL" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

# Bypass docker-entrypoint.sh (it auto-inits a managed cluster + wants PGUSERNAME/etc.);
# the container runs as the postgres user, and the real binaries live under PG_INSTALL_DIR.
docker run --rm --entrypoint bash -v "${SQL}:/tmp/smoke.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  D=/tmp/pg; rm -rf "$D"; mkdir -p "$D"
  "$B/initdb" -A trust -D "$D" >/tmp/i.log 2>&1
  "$B/pg_ctl" -D "$D" -o "-p 5432" -w start >/tmp/s.log 2>&1
  "$B/psql" -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/smoke.sql
  rc=$?
  "$B/pg_ctl" -D "$D" -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'low canse|HINT:|redirecting log|logging collector'
echo "[smoke_test] PASS — relational + vector legs work on the standin build."
