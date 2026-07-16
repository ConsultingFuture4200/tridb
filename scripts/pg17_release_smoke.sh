#!/usr/bin/env bash
#
# pg17_release_smoke.sh — runtime smoke of the SHIPPED release image (advisor plan 076).
# Starts the prebaked tridb/postgres-trimodal:pg16|pg17 image exactly as a stranger would
# (docker run -d -e POSTGRES_PASSWORD=...), waits for readiness, and executes
# test/release_stock_smoke.sql: all three CREATE EXTENSIONs in dependency order, one
# direct public.tjs_open call, and one canonical graph_store.graph_query() (plan 075).
# A green Docker *build* alone is not verification — this is the runtime gate.
#
# Usage: scripts/pg17_release_smoke.sh [image]     (default tridb/postgres-trimodal:pg17)
#
# Lifecycle safety: container name / database name / password are generated per run,
# NO host port is published (psql runs via docker exec), and the container is always
# removed on exit — success or failure.
set -euo pipefail

IMAGE="${1:-tridb/postgres-trimodal:pg17}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL="$ROOT/test/release_stock_smoke.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run: docker build --build-arg PG_MAJOR=<16|17>" \
       "-f scripts/pg17/Dockerfile.release -t $IMAGE ." >&2
  exit 1
}

SUFFIX="$(od -An -N4 -tx4 /dev/urandom | tr -d ' ')"
NAME="tridb-release-smoke-$SUFFIX"
DB="smoke_$SUFFIX"
PW="$(od -An -N16 -tx8 /dev/urandom | tr -d ' \n')"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$NAME" -e POSTGRES_PASSWORD="$PW" "$IMAGE" >/dev/null

# Readiness: require two consecutive OK probes 1s apart — the official-image entrypoint
# briefly runs a temporary server during initdb, so a single pg_isready can race it.
ok=0
for _ in $(seq 1 60); do
  if docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1; then
    ok=$((ok + 1))
    [ "$ok" -ge 2 ] && break
  else
    ok=0
  fi
  sleep 1
done
if [ "$ok" -lt 2 ]; then
  echo "FAIL: $IMAGE not ready after 60s" >&2
  docker logs "$NAME" 2>&1 | tail -20 >&2
  exit 1
fi

docker exec -u postgres "$NAME" psql -U postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $DB" >/dev/null
OUT="$(docker exec -i -u postgres "$NAME" psql -U postgres -d "$DB" -v ON_ERROR_STOP=1 -f - < "$SQL")"
printf '%s\n' "$OUT" | tail -4
if ! printf '%s\n' "$OUT" | grep -q '^RELEASE SMOKE PASS$'; then
  echo "FAIL: PASS marker missing from smoke output ($IMAGE)" >&2
  exit 1
fi
echo "RELEASE SMOKE PASS ($IMAGE)"
