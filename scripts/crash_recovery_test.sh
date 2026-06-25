#!/usr/bin/env bash
#
# crash_recovery_test.sh — tri-store WAL crash-recovery (REDO) for FR-7 (DEV-1166).
#
# The existing scripts/graph_am_test.sh does a CLEAN `pg_ctl restart` (only proves checkpoint
# durability). This harness proves the WAL-REPLAY half: it CHECKPOINTs a baseline, runs a tri-store
# txn under default synchronous_commit (WAL fsynced at COMMIT), then CRASHES the postmaster with
# `pg_ctl stop -m immediate` (SIGQUIT, NO shutdown checkpoint). Committed page changes then exist
# ONLY in the WAL, so the restart is forced to run GenericXLog generic-REDO to reconstruct them.
#
#   Scenario 1 (committed):   commit a tri-store row, crash, restart -> assert present in all 3 stores.
#   Scenario 2 (uncommitted): leave a tri-store txn OPEN (never COMMIT), crash, restart -> assert
#                             NONE of the 3 writes visible (the crash-aborted xid fails
#                             TransactionIdDidCommit / gph_xmin_visible).
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker). Build failures FAIL LOUD (make -> log,
# nonzero make aborts with the log tail; no `| tail` masking — see scripts/graph_am_test.sh).
# Usage: scripts/crash_recovery_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"
ASSERT="$ROOT/test/crash_recovery_assert.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${ASSERT}:/tmp/assert.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  PSQL="$B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1"

  echo "=== make (graph_store_am) ==="
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  if ! make PG_CONFIG=$PGC >/tmp/make.log 2>&1; then echo "BUILD FAILED:"; tail -40 /tmp/make.log; exit 1; fi
  if ! make PG_CONFIG=$PGC install >/tmp/install.log 2>&1; then echo "INSTALL FAILED:"; tail -40 /tmp/install.log; exit 1; fi

  # --- helper: fresh cluster with the tri-store seed + HNSW index, baseline CHECKPOINT ---
  seed_cluster () {
    local D=$1
    rm -rf $D; mkdir -p $D
    $B/initdb -A trust -D $D >/dev/null 2>&1
    $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
    $PSQL >/dev/null <<SQL
CREATE EXTENSION vectordb;
CREATE EXTENSION graph_store_am;
SET search_path TO graph_store, public;
CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, embedding float8[8]);
INSERT INTO entities SELECT k, '"'"'seed '"'"'||k, ARRAY[k,0,0,0,0,0,0,0]::float8[] FROM generate_series(1,5) k;
CREATE INDEX entities_hnsw ON entities USING hnsw(embedding) WITH (dimension=8, distmethod=l2_distance);
SELECT gph_insert_vertex() FROM generate_series(1,6);   -- vids 0..5 -> 6 visible baseline
CHECKPOINT;   -- baseline durable on disk; everything AFTER this lives only in WAL until next ckpt
SQL
  }

  ########################################################################
  # Scenario 1 — COMMITTED tri-store row survives crash via WAL redo.
  ########################################################################
  echo "=== scenario 1: committed tri-store row, crash (-m immediate), WAL redo ==="
  D1=/tmp/pg_committed
  seed_cluster $D1
  # one committed tri-store txn AFTER the baseline checkpoint (synchronous_commit on by default).
  $PSQL >/dev/null <<SQL
SET search_path TO graph_store, public;
BEGIN;
  INSERT INTO entities VALUES (5000, '"'"'committed'"'"', ARRAY[5000,0,0,0,0,0,0,0]::float8[]);
  SELECT gph_insert_vertex();   -- vid 6
  SELECT gph_insert_edge(0, 6);
COMMIT;
SQL
  # CRASH: SIGQUIT, no shutdown checkpoint -> committed pages are only in the WAL.
  $B/pg_ctl -D $D1 -m immediate -w stop >/dev/null 2>&1 || true
  # restart -> startup runs WAL redo (GenericXLog generic REDO of our graph pages).
  $B/pg_ctl -D $D1 -o "-p 5432" -w start >/dev/null 2>&1
  echo "--- post-recovery assert (committed) ---"
  $PSQL -v phase=committed -f /tmp/assert.sql
  $B/pg_ctl -D $D1 -m fast -w stop >/dev/null 2>&1 || true

  ########################################################################
  # Scenario 2 — UNCOMMITTED tri-store txn is crash-aborted (nothing visible).
  ########################################################################
  echo "=== scenario 2: uncommitted tri-store txn, crash, recover -> nothing visible ==="
  D2=/tmp/pg_uncommitted
  seed_cluster $D2
  # Open a txn in a BACKGROUND psql that writes all three stores then BLOCKS (never commits),
  # holding the txn open. We crash the postmaster while that txn is in flight.
  ( $PSQL >/dev/null 2>&1 <<SQL
SET search_path TO graph_store, public;
BEGIN;
  INSERT INTO entities VALUES (6000, '"'"'doomed'"'"', ARRAY[6000,0,0,0,0,0,0,0]::float8[]);
  SELECT gph_insert_vertex();   -- vid 6 (doomed)
  SELECT gph_insert_edge(0, 6);
  SELECT pg_sleep(60);          -- hold the txn open; the crash below interrupts this
COMMIT;
SQL
  ) &
  BGPID=$!
  # give the background session time to issue the writes (poll for the doomed vertex being
  # self-visible to ITS OWN txn is not observable cross-session; poll a sentinel instead).
  for i in $(seq 1 50); do
    # the doomed INSERT holds a lock on entities pk 6000; once present in pg_locks the writes are in.
    n=$($PSQL -tA -c "SELECT count(*) FROM pg_stat_activity WHERE query LIKE '"'"'%pg_sleep(60)%'"'"' AND state='"'"'active'"'"';" 2>/dev/null || echo 0)
    [ "$n" = "1" ] && break
    sleep 0.2
  done
  # CRASH while the txn is open and uncommitted.
  $B/pg_ctl -D $D2 -m immediate -w stop >/dev/null 2>&1 || true
  kill $BGPID >/dev/null 2>&1 || true; wait $BGPID 2>/dev/null || true
  # restart -> the in-flight txn xid never committed; recovery treats it as aborted.
  $B/pg_ctl -D $D2 -o "-p 5432" -w start >/dev/null 2>&1
  echo "--- post-recovery assert (uncommitted) ---"
  $PSQL -v phase=uncommitted -f /tmp/assert.sql
  $B/pg_ctl -D $D2 -m fast -w stop >/dev/null 2>&1 || true
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[crash_recovery_test] done"
