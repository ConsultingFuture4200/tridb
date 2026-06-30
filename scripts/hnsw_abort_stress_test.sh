#!/usr/bin/env bash
#
# hnsw_abort_stress_test.sh — Oracle B for DEV-1235 Defect B (HNSW abort-stress crash).
#
# Defect B was reported as a backend crash under hundreds of cumulative ABORTED
# incremental HNSW inserts. This harness reproduces that load against the existing
# tridb/msvbase:dev image (no rebuild) across three abort patterns, then asserts the
# backend is still alive and an HNSW <-> scan returns the committed anchor.
#
#   P1 single-statement abort (shell-driven): each psql process runs an INSERT whose
#      aminsert succeeds then the statement fails (forced cast error in the same stmt)
#      -> transaction aborts; repeated across fresh connections.
#   P2 single-session BEGIN/INSERT/ROLLBACK x120 (one long-lived backend) — in SQL.
#   P3 abort-then-HNSW-scan interleave x120 (same backend) — in SQL.
#
# PASS = backend up after all aborts AND final scan returns the committed anchor 7777.
# A crash (segfault / backend restart / connection drop) -> nonzero, captured below.
#
# Requires: scripts/x86build.sh --docker has produced tridb/msvbase:dev (no rebuild here).
# Usage: scripts/hnsw_abort_stress_test.sh [image]
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL="$ROOT/test/hnsw_abort_stress_test.sql"

[[ -f "$SQL" ]] || { echo "abort-stress sql not found: $SQL" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --user root --entrypoint bash \
  -v "${SQL}:/tmp/hnsw_abort_stress_test.sql:ro" "$IMAGE" -c '
set -e
B=/u01/app/postgres/product/13.4/bin
PSQL="$B/psql -p 5432 -d postgres"

echo "=== ORACLE B: HNSW abort-stress (DEV-1235 Defect B) ==="

D=/tmp/pg_hnsw_abort
rm -rf "$D"; mkdir -p "$D"; chown postgres:postgres "$D"
runuser -u postgres -- "$B/initdb" -A trust -D "$D" >/dev/null 2>&1
# log_min_messages=panic-level still hits server log; keep PID for liveness check.
runuser -u postgres -- "$B/pg_ctl" -D "$D" -o "-p 5432" -l "$D/server.log" -w start >/dev/null 2>&1

echo "--- seed 50 rows + HNSW index + committed anchor 7777 ---"
runuser -u postgres -- $PSQL -v ON_ERROR_STOP=1 << "SQL"
CREATE EXTENSION vectordb;
CREATE TABLE s(id int, embedding float8[4]);
INSERT INTO s SELECT k, ARRAY[k::float8, 0, 0, 0] FROM generate_series(1, 50) k;
CREATE INDEX s_hnsw ON s USING hnsw(embedding) WITH (dimension=4, distmethod=l2_distance);
INSERT INTO s VALUES (7777, ARRAY[7777.0, 0, 0, 0]);
CHECKPOINT;
SQL

echo "--- P1: single-statement aborted inserts x150 (fresh connection each) ---"
# Each statement performs a real HNSW aminsert, then the same statement errors via a
# CTE that casts a non-numeric text to int -> the whole statement (incl. aminsert) aborts.
P1=0
for i in $(seq 1 150); do
  runuser -u postgres -- $PSQL -q -c \
    "WITH ins AS (INSERT INTO s VALUES (600000+$i, ARRAY[(600000+$i)::float8,9,9,9]) RETURNING id)
     SELECT (SELECT id FROM ins)::text::int / ((SELECT id FROM ins) - (600000+$i));" \
    >/dev/null 2>&1 || true
  P1=$((P1+1))
done
echo "P1 aborted-insert statements issued: $P1"

echo "--- P2 + P3: in-session BEGIN/ROLLBACK x120 and abort-then-scan x120 ---"
runuser -u postgres -- $PSQL -f /tmp/hnsw_abort_stress_test.sql

echo "--- liveness: is the backend still up and answering? ---"
ALIVE=$(runuser -u postgres -- $PSQL -tA -c "SELECT 1;" 2>/dev/null || echo "DEAD")
RESULT=$(runuser -u postgres -- $PSQL -tA -c \
  "SET enable_seqscan=off; SELECT id FROM s ORDER BY embedding <-> ARRAY[7777.0,0,0,0] LIMIT 1;" 2>/dev/null || echo "NO-RESULT")
COUNT=$(runuser -u postgres -- $PSQL -tA -c "SELECT count(*) FROM s;" 2>/dev/null || echo "?")

echo "total aborted incremental HNSW inserts: P1=150 + P2=120 + P3=120 = 390"
echo "liveness SELECT 1 -> $ALIVE (expected 1)"
echo "final HNSW nearest to 7777 -> $RESULT (expected 7777)"
echo "committed rows -> $COUNT (expected 51)"

# Surface any crash evidence from the server log.
if grep -qiE "PANIC|segmentation fault|server process .* was terminated|crash|terminating connection because of crash" "$D/server.log"; then
  echo "--- CRASH EVIDENCE in server.log ---"
  grep -iE "PANIC|segmentation fault|terminated|crash" "$D/server.log" | tail -20
  CRASH=1
else
  CRASH=0
fi

runuser -u postgres -- "$B/pg_ctl" -D "$D" -m fast stop >/dev/null 2>&1 || true

if [ "$ALIVE" = "1" ] && [ "$RESULT" = "7777" ] && [ "$COUNT" = "51" ] && [ "$CRASH" = "0" ]; then
  echo "=== ORACLE B PASS: backend survived 390 aborted HNSW inserts; scan correct; no crash ==="
  exit 0
else
  echo "=== ORACLE B FAIL/REPRO: Defect B condition observed (see evidence above) ==="
  exit 1
fi
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[hnsw_abort_stress_test] PASS"
