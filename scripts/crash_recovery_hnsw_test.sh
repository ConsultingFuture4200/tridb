#!/usr/bin/env bash
#
# crash_recovery_hnsw_test.sh — Oracle A for DEV-1235 Defect A: HNSW index crash/WAL recovery.
#
# Proves the DEV-1235 fix: a committed HNSW index insert is visible after crash-immediate +
# WAL-redo. On the UNPATCHED build, this test FAILS (returns wrong row). With the patch, it PASSES.
#
# Scenario (Oracle A):
#   1. Seed cluster: 30 rows + HNSW index (ambuild writes flat file).
#   2. CHECKPOINT (baseline durable on disk).
#   3. INSERT distinctive row R=9001 (committed, WAL-fsynced, in heap but NOT in flat file).
#   4. pg_ctl stop -m immediate (SIGQUIT, no shutdown checkpoint).
#   5. Restart -> startup runs WAL-redo, heap now contains R.
#   6. SET enable_seqscan=off; SELECT ... ORDER BY <-> R LIMIT 1 -> must return 9001.
#      On the patched build: LoadIndex rebuilds from heap and finds R. PASS.
#      On the unpatched build: LoadIndex reads stale flat file and returns wrong row. FAIL.
#
# Requires: scripts/x86build.sh --docker has produced tridb/msvbase:dev.
# Usage: scripts/crash_recovery_hnsw_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL="$ROOT/test/hnsw_recovery_test.sql"

[[ -f "$SQL" ]] || { echo "recovery sql not found: $SQL" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --user root --entrypoint bash \
  -v "${SQL}:/tmp/hnsw_recovery_test.sql:ro" "$IMAGE" -c '
set -e
B=/u01/app/postgres/product/13.4/bin
PSQL="$B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1"

echo "=== ORACLE A: HNSW crash/WAL recovery (DEV-1235) ==="

D=/tmp/pg_hnsw_crash
rm -rf "$D"; mkdir -p "$D"; chown postgres:postgres "$D"
runuser -u postgres -- "$B/initdb" -A trust -D "$D" >/dev/null 2>&1
runuser -u postgres -- "$B/pg_ctl" -D "$D" -o "-p 5432" -w start >/dev/null 2>&1

echo "--- seeding 30 rows + HNSW index (ambuild) ---"
runuser -u postgres -- $PSQL << '\''SQL'\''
CREATE EXTENSION vectordb;
CREATE TABLE t(id int, embedding float8[4]);
INSERT INTO t SELECT k, ARRAY[k::float8, 0, 0, 0] FROM generate_series(1, 30) k;
CREATE INDEX t_hnsw ON t USING hnsw(embedding) WITH (dimension=4, distmethod=l2_distance);
CHECKPOINT;
SQL

echo "--- INSERT distinctive row R=9001 AFTER baseline checkpoint (committed) ---"
runuser -u postgres -- $PSQL -c \
  "INSERT INTO t VALUES (9001, ARRAY[9001.0, 0, 0, 0]);"

echo "--- crash: pg_ctl stop -m immediate (no shutdown checkpoint) ---"
runuser -u postgres -- "$B/pg_ctl" -D "$D" -m immediate stop >/dev/null 2>&1 || true
sleep 1

echo "--- restart: WAL-redo recovers committed heap tuple ---"
runuser -u postgres -- "$B/pg_ctl" -D "$D" -o "-p 5432" -w start >/dev/null 2>&1

echo "--- oracle A query: HNSW scan must return R=9001 ---"
runuser -u postgres -- $PSQL -f /tmp/hnsw_recovery_test.sql

# Verify the key result row
result=$(runuser -u postgres -- $PSQL -tA -c \
  "SET enable_seqscan=off; SELECT id FROM t ORDER BY embedding <-> ARRAY[9001.0, 0, 0, 0] LIMIT 1;")
echo "Oracle A result: $result (expected: 9001)"
if [ "$result" = "9001" ]; then
  echo "=== ORACLE A PASS: crash-recovered HNSW index returns R=9001 (DEV-1235 fix confirmed) ==="
else
  echo "=== ORACLE A FAIL: expected 9001 got $result (patch not applied or regression) ==="
  runuser -u postgres -- "$B/pg_ctl" -D "$D" -m fast stop >/dev/null 2>&1 || true
  exit 1
fi

runuser -u postgres -- "$B/pg_ctl" -D "$D" -m fast stop >/dev/null 2>&1 || true
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[crash_recovery_hnsw_test] PASS"
