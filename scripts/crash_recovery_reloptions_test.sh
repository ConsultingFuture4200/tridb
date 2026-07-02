#!/usr/bin/env bash
#
# crash_recovery_reloptions_test.sh — Oracle for DEV-1286: crash-recovery preserves index QUALITY.
#
# Drives test/hnsw_reloptions_recovery_test.sql through a crash/recover cycle to prove that a tuned
# HNSW index (WITH m=32, ef_construction=400) recovers at its TUNED quality after crash-immediate +
# WAL-redo — not silently at the hnswlib m=16/ef=200 defaults. Requires the tridb_hnsw_reloptions
# patch to be applied in the image (see the SQL header). On the UNPATCHED rebuild path the assert
# phase's recall oracle FAILS; with the patch it PASSES.
#
# Two-phase per the SQL's own contract (-v recovery_phase=seed|assert):
#   seed   -> create tuned table + index, CHECKPOINT, INSERT post-checkpoint rows (so recovery must
#             rebuild from heap, not the stale flat file).
#   crash  -> pg_ctl stop -m immediate (SIGQUIT, no shutdown checkpoint).
#   assert -> after restart, build fresh m=32 and fresh-default controls and run the recall oracle.
#
# Requires: scripts/x86build.sh --docker has produced tridb/msvbase:dev.
# Usage: scripts/crash_recovery_reloptions_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL="$ROOT/test/hnsw_reloptions_recovery_test.sql"

[[ -f "$SQL" ]] || { echo "reloptions recovery sql not found: $SQL" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --user root --entrypoint bash \
  -v "${SQL}:/tmp/hnsw_reloptions_recovery_test.sql:ro" "$IMAGE" -c '
set -e
B=/u01/app/postgres/product/13.4/bin
PSQL="$B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1"

echo "=== ORACLE: HNSW reloptions crash-recovery QUALITY (DEV-1286) ==="

D=/tmp/pg_hnsw_reloptions
rm -rf "$D"; mkdir -p "$D"; chown postgres:postgres "$D"
runuser -u postgres -- "$B/initdb" -A trust -D "$D" >/dev/null 2>&1
runuser -u postgres -- "$B/pg_ctl" -D "$D" -o "-p 5432" -w start >/dev/null 2>&1

echo "--- phase seed: tuned m=32/ef=400 index + post-checkpoint rows ---"
runuser -u postgres -- $PSQL -v recovery_phase=seed -f /tmp/hnsw_reloptions_recovery_test.sql

echo "--- crash: pg_ctl stop -m immediate (no shutdown checkpoint) ---"
runuser -u postgres -- "$B/pg_ctl" -D "$D" -m immediate stop >/dev/null 2>&1 || true
# Bounded, fail-loud wait for the postmaster to actually exit: poll for postmaster.pid removal up
# to ~60s, matching scripts/crash_recovery_test.sh:104-130.
stopped=0
for i in $(seq 1 120); do
  if [ ! -f "$D/postmaster.pid" ]; then stopped=1; break; fi
  sleep 0.5
done
if [ "$stopped" != "1" ]; then
  echo "=== DEV-1286 FAIL: postmaster did not exit within ~60s after -m immediate stop ==="
  exit 1
fi

echo "--- restart: WAL-redo rebuilds the tuned index from heap ---"
runuser -u postgres -- "$B/pg_ctl" -D "$D" -o "-p 5432" -w start >/dev/null 2>&1
# Bounded, fail-loud wait for the recovered postmaster to accept queries (SELECT 1) up to ~60s.
ready=0
for i in $(seq 1 120); do
  if [ "$(runuser -u postgres -- $PSQL -tA -c "SELECT 1;" 2>/dev/null || true)" = "1" ]; then
    ready=1; break
  fi
  sleep 0.5
done
if [ "$ready" != "1" ]; then
  echo "=== DEV-1286 FAIL: postmaster did not accept SELECT 1 within ~60s after restart ==="
  exit 1
fi

echo "--- phase assert: recovered tuned index matches fresh-m32, beats fresh-default ---"
# ON_ERROR_STOP=1: any RAISE EXCEPTION in the assert oracle (DEV-1286 FAIL (A)/(B)) halts psql
# nonzero -> set -e aborts here, and pipefail propagates it to the outer script.
runuser -u postgres -- $PSQL -v recovery_phase=assert -f /tmp/hnsw_reloptions_recovery_test.sql

runuser -u postgres -- "$B/pg_ctl" -D "$D" -m fast stop >/dev/null 2>&1 || true
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[crash_recovery_reloptions_test] PASS"
