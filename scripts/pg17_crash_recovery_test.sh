#!/usr/bin/env bash
#
# pg17_crash_recovery_test.sh — tri-store WAL crash-recovery (REDO) on STOCK PostgreSQL 16/17
# (advisor plan 090: the stock-PG mirror of scripts/crash_recovery_test.sh, which targets the
# MSVBASE fork image). D2's ship surface is the stock extension, so the FR-7 crash property is
# proven HERE too, not only on the fork: each scenario CHECKPOINTs a baseline, mutates, then
# CRASHES the postmaster with `pg_ctl stop -m immediate` (SIGQUIT, NO shutdown checkpoint) so
# the committed page changes exist ONLY in the WAL — the restart is forced to run GenericXLog
# generic-REDO to reconstruct them.
#
#   Scenario 1 (committed):   commit a tri-store row, crash, restart -> present in all 3 stores.
#   Scenario 2 (uncommitted): leave a tri-store txn OPEN (never COMMIT), crash, restart -> NONE
#                             of the 3 writes visible (crash-aborted xid fails gph_xmin_visible).
#   Scenario 3 (committed tombstone):   checkpointed live edge 0->1, COMMIT a tombstone that
#                             lives ONLY in the WAL, crash -> tombstone REDONE (edge gone).
#   Scenario 4 (uncommitted tombstone): checkpointed edge 0->1, tombstone txn left OPEN
#                             (checkpointed durable, never COMMIT), crash -> edge LIVE again.
#   Scenario 5 (freeze, plan 090): committed edges CHECKPOINTed, then a COMMITTED gph_freeze()
#                             whose page rewrites live ONLY in the WAL, crash -> frozen state
#                             REDONE (visibility unchanged, relfrozenxid advanced, re-freeze at
#                             the same horizon is the idempotent no-op, and a higher-horizon
#                             pass finds 0 unfrozen records).
#
# Container conventions follow scripts/pg17_graph_test.sh (stock pgvector image, --user
# postgres, PGXS build of src/graph_store inside the container, throwaway 8KB clusters,
# fail-loud build). The vector store is pgvector `vector(8)` + hnsw instead of the fork's
# vectordb float8[]; the assertions are the SHARED test/crash_recovery_assert.sql (its vector
# check compares a type-agnostic text form, so one assert file drives both engines). Unlike
# the fork's vendored vectordb, pgvector's HNSW is WAL-logged — the shared assert still reads
# the heap backing so the load-bearing tri-store checks are identical on both engines.
#
# Usage: scripts/pg17_crash_recovery_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/pg17-unfork:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"
ASSERT="$ROOT/test/crash_recovery_assert.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run: docker build -t tridb/pg17-unfork:dev scripts/pg17/" >&2
  exit 1
}

docker run --rm --user postgres --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${ASSERT}:/tmp/assert.sql:ro" "$IMAGE" -c '
  set -e
  B=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)  # works for the pg16/pg17 CI matrix
  PGC=$B/pg_config
  PSQL="$B/psql -p 5499 -d postgres -v ON_ERROR_STOP=1"

  echo "=== make (graph_store_am, stock) ==="
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  # Fail LOUD on a build error (plan 072 shape): no `| tail` masking of a nonzero make.
  make PG_CONFIG=$PGC >/tmp/make.log 2>&1 || { tail -30 /tmp/make.log; echo "BUILD FAILED"; exit 1; }
  make PG_CONFIG=$PGC install >/tmp/install.log 2>&1 || { tail -20 /tmp/install.log; echo "INSTALL FAILED"; exit 1; }

  # --- helper: fresh cluster with the tri-store seed + pgvector HNSW index, baseline CHECKPOINT ---
  seed_cluster () {
    local D=$1
    rm -rf $D; mkdir -p $D
    $B/initdb -A trust -D $D >/dev/null 2>&1
    $B/pg_ctl -D $D -o "-p 5499 -c listen_addresses=" -w start >/dev/null 2>&1
    $PSQL >/dev/null <<SQL
CREATE EXTENSION vector;
CREATE EXTENSION graph_store_am;
SET search_path TO graph_store, public;
CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, embedding vector(8));
INSERT INTO entities SELECT k, '"'"'seed '"'"'||k, ('"'"'['"'"'||k||'"'"',0,0,0,0,0,0,0]'"'"')::vector(8) FROM generate_series(1,5) k;
CREATE INDEX entities_hnsw ON entities USING hnsw (embedding vector_l2_ops);
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
  INSERT INTO entities VALUES (5000, '"'"'committed'"'"', '"'"'[5000,0,0,0,0,0,0,0]'"'"'::vector);
  SELECT gph_insert_vertex();   -- vid 6
  SELECT gph_insert_edge(0, 6);
COMMIT;
SQL
  # CRASH: SIGQUIT, no shutdown checkpoint -> committed pages are only in the WAL.
  $B/pg_ctl -D $D1 -m immediate -w stop >/dev/null 2>&1 || true
  # restart -> startup runs WAL redo (GenericXLog generic REDO of our graph pages).
  $B/pg_ctl -D $D1 -o "-p 5499 -c listen_addresses=" -w start >/dev/null 2>&1
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
  ( PGAPPNAME=tridb_doomed $PSQL >/dev/null 2>&1 <<SQL
SET search_path TO graph_store, public;
BEGIN;
  INSERT INTO entities VALUES (6000, '"'"'doomed'"'"', '"'"'[6000,0,0,0,0,0,0,0]'"'"'::vector);
  SELECT gph_insert_vertex();   -- vid 6 (doomed)
  SELECT gph_insert_edge(0, 6);
  SELECT pg_sleep(3600);        -- hold the txn open indefinitely; the crash below interrupts it
COMMIT;
SQL
  ) &
  BGPID=$!
  # Sentinel: poll pg_stat_activity for the doomed session FINAL statement (the pg_sleep call)
  # being active — the three tri-store writes precede pg_sleep in the same BEGIN block, so
  # observing it active proves the uncommitted writes executed and the txn is open. Same
  # liveness-checked ~180s budget + never-self-committing pg_sleep(3600) as the fork driver
  # (DEV-1234 flake fix); scoped to PGAPPNAME=tridb_doomed (DEV-1331).
  sentinel=0
  for i in $(seq 1 360); do
    if ! kill -0 $BGPID 2>/dev/null; then
      echo "FAIL (uncommitted): doomed background session exited before reaching its in-flight state (a tri-store write errored before pg_sleep?)"
      $B/pg_ctl -D $D2 -m immediate -w stop >/dev/null 2>&1 || true
      exit 1
    fi
    n=$($PSQL -tA -c "SELECT count(*) FROM pg_stat_activity WHERE application_name='"'"'tridb_doomed'"'"' AND state='"'"'active'"'"' AND query LIKE '"'"'%pg_sleep%'"'"';" 2>/dev/null || echo 0)
    if [ "$n" = "1" ]; then sentinel=1; break; fi
    sleep 0.5
  done
  if [ "$sentinel" != "1" ]; then
    echo "FAIL (uncommitted): doomed txn never reached its in-flight state (pg_sleep sentinel not observed after ~180s) — poll timeout"
    echo "--- doomed session diagnostic (what is it stuck on?) ---"; $PSQL -c "SELECT pid,state,wait_event_type,wait_event,left(query,90) AS q FROM pg_stat_activity WHERE application_name='"'"'tridb_doomed'"'"';" 2>/dev/null || true
    kill $BGPID >/dev/null 2>&1 || true; $B/pg_ctl -D $D2 -m immediate -w stop >/dev/null 2>&1 || true
    exit 1
  fi
  # CRASH while the txn is open and uncommitted.
  $B/pg_ctl -D $D2 -m immediate -w stop >/dev/null 2>&1 || true
  kill $BGPID >/dev/null 2>&1 || true; wait $BGPID 2>/dev/null || true
  # restart -> the in-flight txn xid never committed; recovery treats it as aborted.
  $B/pg_ctl -D $D2 -o "-p 5499 -c listen_addresses=" -w start >/dev/null 2>&1
  echo "--- post-recovery assert (uncommitted) ---"
  $PSQL -v phase=uncommitted -f /tmp/assert.sql
  $B/pg_ctl -D $D2 -m fast -w stop >/dev/null 2>&1 || true

  ########################################################################
  # Scenario 3 — COMMITTED edge-tombstone survives crash via WAL redo.
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
  $B/pg_ctl -D $D3 -o "-p 5499 -c listen_addresses=" -w start >/dev/null 2>&1
  echo "--- post-recovery assert (committed_tombstone) ---"
  $PSQL -v phase=committed_tombstone -f /tmp/assert.sql
  $B/pg_ctl -D $D3 -m fast -w stop >/dev/null 2>&1 || true

  ########################################################################
  # Scenario 4 — UNCOMMITTED edge-tombstone is crash-aborted (edge reads LIVE again).
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
  ( PGAPPNAME=tridb_doomed $PSQL >/dev/null 2>&1 <<SQL
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
    n=$($PSQL -tA -c "SELECT count(*) FROM pg_stat_activity WHERE application_name='"'"'tridb_doomed'"'"' AND state='"'"'active'"'"' AND query LIKE '"'"'%pg_sleep%'"'"';" 2>/dev/null || echo 0)
    if [ "$n" = "1" ]; then sentinel=1; break; fi
    sleep 0.5
  done
  if [ "$sentinel" != "1" ]; then
    echo "FAIL (uncommitted_tombstone): doomed txn never reached its in-flight state (pg_sleep sentinel not observed after ~180s) — poll timeout"
    echo "--- doomed session diagnostic (what is it stuck on?) ---"; $PSQL -c "SELECT pid,state,wait_event_type,wait_event,left(query,90) AS q FROM pg_stat_activity WHERE application_name='"'"'tridb_doomed'"'"';" 2>/dev/null || true
    kill $BGPID >/dev/null 2>&1 || true; $B/pg_ctl -D $D4 -m immediate -w stop >/dev/null 2>&1 || true
    exit 1
  fi
  # CHECKPOINT (from a SEPARATE connection) while the doomed txn is open: this flushes the dirty
  # tombstone page to disk, so after recovery the tombstone bytes (GPH_FLAG_DELETED + es_xmax =
  # the doomed xid) are physically present — the edge is live ONLY because gph_deleted_visible
  # rejects the crash-aborted xid (xid-visibility is the load-bearing proof, not a lost record).
  $PSQL -c "CHECKPOINT;" >/dev/null 2>&1
  # CRASH while the tombstone txn is open and uncommitted.
  $B/pg_ctl -D $D4 -m immediate -w stop >/dev/null 2>&1 || true
  kill $BGPID >/dev/null 2>&1 || true; wait $BGPID 2>/dev/null || true
  $B/pg_ctl -D $D4 -o "-p 5499 -c listen_addresses=" -w start >/dev/null 2>&1
  echo "--- post-recovery assert (uncommitted_tombstone) ---"
  $PSQL -v phase=uncommitted_tombstone -f /tmp/assert.sql
  $B/pg_ctl -D $D4 -m fast -w stop >/dev/null 2>&1 || true

  ########################################################################
  # Scenario 5 — COMMITTED gph_freeze() page rewrites survive crash via WAL redo (plan 090).
  ########################################################################
  echo "=== scenario 5: committed gph_freeze, crash (-m immediate), WAL redo of frozen pages ==="
  D5=/tmp/pg_freeze
  seed_cluster $D5
  # Committed edges (autocommit: real xids strictly below the horizon captured next).
  $PSQL >/dev/null <<SQL
SET search_path TO graph_store, public;
SELECT gph_insert_edge(0, 1);
SELECT gph_insert_edge(0, 2);
SQL
  # Capture the freeze horizon, burn xids so it strictly precedes the oldest running xmin at
  # freeze time (same discipline as test/graph_freeze_test.sql), then CHECKPOINT: the PRE-freeze
  # record pages (normal xids) are durable on disk, so the freeze rewrite that follows lives
  # ONLY in the WAL — its survival post-crash is pure GenericXLog REDO. gph_freeze runs in
  # AUTOCOMMIT (its own txn), as its relfrozenxid contract requires.
  H=$($PSQL -tA -c "SELECT txid_current();")
  $PSQL -c "SELECT txid_current();" >/dev/null
  $PSQL -c "SELECT txid_current();" >/dev/null
  $PSQL -c "CHECKPOINT;" >/dev/null
  N=$($PSQL -tA -c "SELECT graph_store.gph_freeze(($H)::text::xid);")
  case "$N" in ""|0|*[!0-9]*)
    echo "FAIL (freeze): pre-crash gph_freeze($H) froze [$N] records (expected > 0 — scenario vacuous)"
    $B/pg_ctl -D $D5 -m immediate -w stop >/dev/null 2>&1 || true
    exit 1;;
  esac
  echo "pre-crash gph_freeze($H) froze $N records (only in WAL)"
  # No wal-flush barrier needed (plan 094): gph_freeze runs in its own xid-less transaction (it
  # only REWRITES xids), so its COMMIT still takes Postgres async path (XLogSetAsyncXactLSN) --
  # but gph_freeze() itself now issues a synchronous XLogFlush() of its own WAL before returning
  # (src/graph_store/graph_am.c), closing the plan 090 window at the source instead of relying on
  # a caller-side barrier. This scenario now tests that contract directly: crash immediately
  # after the freeze call returns, with no intervening committed write of any kind.
  # CRASH: the committed freeze rewrites (record pages + metapage horizon) must already be
  # durable on disk via gph_freeze own flush -- nothing else forces them out here.
  $B/pg_ctl -D $D5 -m immediate -w stop >/dev/null 2>&1 || true
  $B/pg_ctl -D $D5 -o "-p 5499 -c listen_addresses=" -w start >/dev/null 2>&1
  # A SECOND, post-restart horizon for the record-page probe (burn xids so it precedes the
  # assert session own xmin — see the freeze phase of test/crash_recovery_assert.sql).
  H2=$($PSQL -tA -c "SELECT txid_current();")
  $PSQL -c "SELECT txid_current();" >/dev/null
  $PSQL -c "SELECT txid_current();" >/dev/null
  echo "--- post-recovery assert (freeze) ---"
  $PSQL -v phase=freeze -v horizon=$H -v horizon2=$H2 -f /tmp/assert.sql
  $B/pg_ctl -D $D5 -m fast -w stop >/dev/null 2>&1 || true
'
echo "[pg17_crash_recovery_test] done"
