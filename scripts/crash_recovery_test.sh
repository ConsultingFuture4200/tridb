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
#   Scenario 3 (committed tombstone, plan 037/DEV-1349): commit+CHECKPOINT a live edge 0->1, then
#                             COMMIT a gph_tombstone_edge(0,1) that lives ONLY in the WAL, crash,
#                             restart -> assert the tombstone was REDONE (edge 0->1 gone). Proves the
#                             repurposed es_xmax survives GenericXLog generic-REDO.
#   Scenario 4 (uncommitted tombstone, plan 037): commit+CHECKPOINT edge 0->1, then leave a txn that
#                             tombstones 0->1 OPEN (CHECKPOINTed durable but never COMMIT), crash,
#                             restart -> assert edge 0->1 reads LIVE again (the crash-aborted deleting
#                             xid is hidden by gph_deleted_visible; FR-7 abort-atomicity, not UNDO).
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
  SELECT pg_sleep(3600);        -- hold the txn open indefinitely; the crash below interrupts it
COMMIT;
SQL
  ) &
  BGPID=$!
  # Sentinel: poll pg_stat_activity for the doomed session FINAL statement (the pg_sleep call) being
  # active. Because the three tri-store writes precede pg_sleep in the same BEGIN block, observing
  # pg_sleep active proves all three uncommitted writes have already executed and the txn is open.
  #
  # ROBUST TO SUITE ORDERING / HOST LOAD (DEV-1234 P1b flake fix): this harness can run LAST in the
  # full `make graph-test` sequence, where a loaded box makes connect + 3 writes + HNSW work take far
  # longer than a standalone run. The old 40s (200 x 0.2s) budget + a self-expiring pg_sleep(60)
  # raced that load two ways: (1) the poll could time out before the doomed txn went active (the
  # observed "scenario-2 timeout"), and (2) pg_sleep(60) could ELAPSE and COMMIT the "doomed" txn,
  # making its writes durable and breaking the post-recovery "nothing visible" assert. Fixed by
  # holding the txn open for pg_sleep(3600) (always killed by the crash; never self-commits) and a
  # generous, liveness-checked ~180s budget.
  sentinel=0
  for i in $(seq 1 360); do
    # Bail loud if the doomed session died before reaching pg_sleep (e.g. a tri-store write errored)
    # instead of silently polling to timeout.
    if ! kill -0 $BGPID 2>/dev/null; then
      echo "FAIL (uncommitted): doomed background session exited before reaching its in-flight state (a tri-store write errored before pg_sleep?)"
      $B/pg_ctl -D $D2 -m immediate -w stop >/dev/null 2>&1 || true
      exit 1
    fi
    n=$($PSQL -tA -c "SELECT count(*) FROM pg_stat_activity WHERE query LIKE '"'"'%pg_sleep%'"'"' AND state='"'"'active'"'"';" 2>/dev/null || echo 0)
    if [ "$n" = "1" ]; then sentinel=1; break; fi
    sleep 0.5
  done
  # FAIL LOUD on poll timeout BEFORE crashing: if the sentinel never went active, the doomed writes
  # never ran, so crashing now would trivially "pass" scenario 2 with no in-flight tri-store state.
  if [ "$sentinel" != "1" ]; then
    echo "FAIL (uncommitted): doomed txn never reached its in-flight state (pg_sleep sentinel not observed after ~180s) — poll timeout"
    kill $BGPID >/dev/null 2>&1 || true; $B/pg_ctl -D $D2 -m immediate -w stop >/dev/null 2>&1 || true
    exit 1
  fi
  # CRASH while the txn is open and uncommitted.
  $B/pg_ctl -D $D2 -m immediate -w stop >/dev/null 2>&1 || true
  kill $BGPID >/dev/null 2>&1 || true; wait $BGPID 2>/dev/null || true
  # restart -> the in-flight txn xid never committed; recovery treats it as aborted.
  $B/pg_ctl -D $D2 -o "-p 5432" -w start >/dev/null 2>&1
  echo "--- post-recovery assert (uncommitted) ---"
  $PSQL -v phase=uncommitted -f /tmp/assert.sql
  $B/pg_ctl -D $D2 -m fast -w stop >/dev/null 2>&1 || true

  ########################################################################
  # Scenario 3 — COMMITTED edge-tombstone survives crash via WAL redo (plan 037).
  ########################################################################
  echo "=== scenario 3: committed edge-tombstone, crash (-m immediate), WAL redo ==="
  D3=/tmp/pg_committed_tomb
  seed_cluster $D3
  # Make edge 0->1 durable via an explicit CHECKPOINT, so ONLY the tombstone lives in the WAL:
  # its absence post-recovery then unambiguously proves the tombstone REDO ran (not a lost insert).
  $PSQL >/dev/null <<SQL
SET search_path TO graph_store, public;
SELECT gph_insert_edge(0, 1);
CHECKPOINT;                     -- edge 0->1 durable on disk
BEGIN;
  SELECT gph_tombstone_edge(0, 1);   -- tombstone lives ONLY in WAL until redo
COMMIT;
SQL
  # CRASH: SIGQUIT, no shutdown checkpoint -> the committed tombstone is only in the WAL.
  $B/pg_ctl -D $D3 -m immediate -w stop >/dev/null 2>&1 || true
  $B/pg_ctl -D $D3 -o "-p 5432" -w start >/dev/null 2>&1
  echo "--- post-recovery assert (committed_tombstone) ---"
  $PSQL -v phase=committed_tombstone -f /tmp/assert.sql
  $B/pg_ctl -D $D3 -m fast -w stop >/dev/null 2>&1 || true

  ########################################################################
  # Scenario 4 — UNCOMMITTED edge-tombstone is crash-aborted (edge reads LIVE again; plan 037).
  ########################################################################
  echo "=== scenario 4: uncommitted edge-tombstone, crash, recover -> edge live again ==="
  D4=/tmp/pg_uncommitted_tomb
  seed_cluster $D4
  # Durable live edge 0->1 first (checkpointed), then a DOOMED txn tombstones it and holds open.
  $PSQL >/dev/null <<SQL
SET search_path TO graph_store, public;
SELECT gph_insert_edge(0, 1);
CHECKPOINT;                     -- edge 0->1 durable on disk
SQL
  ( $PSQL >/dev/null 2>&1 <<SQL
SET search_path TO graph_store, public;
BEGIN;
  SELECT gph_tombstone_edge(0, 1);   -- doomed tombstone; deleting xid never commits
  SELECT pg_sleep(3600);             -- hold the txn open; the crash below interrupts it
COMMIT;
SQL
  ) &
  BGPID=$!
  # Same liveness-checked sentinel as scenario 2: observing pg_sleep active proves the tombstone
  # already executed inside the open txn (it precedes pg_sleep in the same BEGIN block).
  sentinel=0
  for i in $(seq 1 360); do
    if ! kill -0 $BGPID 2>/dev/null; then
      echo "FAIL (uncommitted_tombstone): doomed background session exited before reaching its in-flight state (the tombstone errored before pg_sleep?)"
      $B/pg_ctl -D $D4 -m immediate -w stop >/dev/null 2>&1 || true
      exit 1
    fi
    n=$($PSQL -tA -c "SELECT count(*) FROM pg_stat_activity WHERE query LIKE '"'"'%pg_sleep%'"'"' AND state='"'"'active'"'"';" 2>/dev/null || echo 0)
    if [ "$n" = "1" ]; then sentinel=1; break; fi
    sleep 0.5
  done
  if [ "$sentinel" != "1" ]; then
    echo "FAIL (uncommitted_tombstone): doomed txn never reached its in-flight state (pg_sleep sentinel not observed after ~180s) — poll timeout"
    kill $BGPID >/dev/null 2>&1 || true; $B/pg_ctl -D $D4 -m immediate -w stop >/dev/null 2>&1 || true
    exit 1
  fi
  # CHECKPOINT (from a SEPARATE connection) while the doomed txn is open: this flushes the dirty
  # tombstone page to disk, so after recovery the tombstone bytes (GPH_FLAG_DELETED + es_xmax = the
  # doomed xid) are physically present — the edge is live ONLY because gph_deleted_visible rejects
  # the crash-aborted xid, making xid-visibility (not a lost WAL record) the load-bearing proof.
  $PSQL -c "CHECKPOINT;" >/dev/null 2>&1
  # CRASH while the tombstone txn is open and uncommitted.
  $B/pg_ctl -D $D4 -m immediate -w stop >/dev/null 2>&1 || true
  kill $BGPID >/dev/null 2>&1 || true; wait $BGPID 2>/dev/null || true
  $B/pg_ctl -D $D4 -o "-p 5432" -w start >/dev/null 2>&1
  echo "--- post-recovery assert (uncommitted_tombstone) ---"
  $PSQL -v phase=uncommitted_tombstone -f /tmp/assert.sql
  $B/pg_ctl -D $D4 -m fast -w stop >/dev/null 2>&1 || true
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[crash_recovery_test] done"
