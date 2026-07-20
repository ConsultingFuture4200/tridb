#!/usr/bin/env bash
#
# graph_writer_lock_test.sh — single-writer ENFORCEMENT gate on STOCK PostgreSQL 16/17
# (advisor plan 100). The contract graph_am.c used to only DOCUMENT is now enforced by a
# transaction-scoped EXCLUSIVE advisory lock keyed on gstore's relation OID, taken at every
# structural-write entry point. This gate proves, with deterministic sync (advisory lock
# 424200 + pg_locks polling — no sleep races, the graph_concurrency_test.sh pattern):
#
#   (a) writer-blocks-writer: while T1 holds an open transaction that inserted an edge,
#       a second writer (scalar gph_insert_edge) and the BATCH loader (gph_insert_edges)
#       both BLOCK on the writer advisory lock (visible in pg_locks, granted = false) —
#       the chosen semantic is BLOCK, not error;
#   (b) readers unaffected: traversal (gph_neighbors) answers promptly (statement_timeout
#       guarded) while the writer lock is held, and T1's uncommitted edge stays invisible;
#   (c) serialization is exact: after release, every blocked writer completes and the
#       final adjacency + visible edge counts are exact (nothing lost, nothing doubled);
#   (d) interleaved autocommit writers: two sessions fire 50 single-statement edge inserts
#       each, concurrently; final counts are exact (100 edges, 50 per source).
#
# A deadlock between the advisory lock and the batch loader would surface here as a poll
# timeout (fail loud) — the plan-100 STOP condition.
#
# Usage: scripts/graph_writer_lock_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/pg17-unfork:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_V1="$ROOT/src/graph_store"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run: docker build -t tridb/pg17-unfork:dev scripts/pg17/" >&2
  exit 1
}

docker run --rm --user postgres --entrypoint bash \
  -v "${EXT_V1}:/tmp/ext_v1:ro" "$IMAGE" -c '
  set -e
  B=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)  # works for the pg16/pg17 CI matrix
  PGC=$B/pg_config
  cp -r /tmp/ext_v1 /tmp/build_v1 && cd /tmp/build_v1
  echo "=== make (graph_store_am, stock) ==="
  make PG_CONFIG=$PGC >/tmp/make.log 2>&1 || { tail -30 /tmp/make.log; echo "BUILD FAILED"; exit 1; }
  make PG_CONFIG=$PGC install >/tmp/install.log 2>&1 || { tail -20 /tmp/install.log; echo "INSTALL FAILED"; exit 1; }

  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null
  $B/pg_ctl -D $D -o "-p 5499 -c listen_addresses=" -w start >/dev/null
  P="$B/psql -p 5499 -d postgres -v ON_ERROR_STOP=1"
  V() { $P -tAc "$1"; }

  $P -q -c "CREATE EXTENSION graph_store_am" \
     -c "SELECT graph_store.gph_insert_vertex() FROM generate_series(1,4)" >/dev/null   # vids 0..3

  GSTORE_OID=$(V "SELECT oid FROM pg_class WHERE relname='\''gstore'\''")
  # pg_locks probes for THE writer lock (classid = gstore OID, objid = 0, plan 100)
  WHELD="SELECT count(*) FROM pg_locks WHERE locktype='\''advisory'\'' AND granted AND classid=$GSTORE_OID AND objid=0"
  WWAIT="SELECT count(*) FROM pg_locks WHERE locktype='\''advisory'\'' AND NOT granted AND classid=$GSTORE_OID AND objid=0"
  # sync-lock probes (the 424200 gate the holder session owns)
  SHELD="SELECT count(*) FROM pg_locks WHERE locktype='\''advisory'\'' AND granted AND objid=424200"
  SWAIT="SELECT count(*) FROM pg_locks WHERE locktype='\''advisory'\'' AND NOT granted AND objid=424200"

  poll() { # poll "<query>" <want> <label>
    for i in $(seq 1 600); do [ "$(V "$1")" = "$2" ] && return 0; sleep 0.1; done
    echo "FAIL: poll timeout waiting for $3 (possible deadlock — plan-100 STOP condition)"; exit 1
  }

  cat > /tmp/holder.sql <<SQL
SELECT pg_advisory_lock(424200);
SELECT pg_sleep(120);
SQL
  cat > /tmp/t1.sql <<SQL
BEGIN;
SELECT graph_store.gph_insert_edge(0, 1);   -- takes the writer lock, txn stays open
SELECT pg_advisory_lock(424200);            -- BLOCKS here until the holder releases
COMMIT;
SQL

  ##########################################################################
  # (a) writer-blocks-writer (scalar AND batch), (b) readers unaffected
  ##########################################################################
  echo "=== (a) writer blocks writer / (b) reader proceeds ==="
  ( $P -f /tmp/holder.sql >/dev/null 2>&1 ) & HOLDER=$!
  poll "$SHELD" 1 "holder to acquire sync lock 424200"

  ( $P -f /tmp/t1.sql >/dev/null 2>&1 ) & T1=$!
  poll "$SWAIT" 1 "T1 to block on sync lock (writer lock now held by an open txn)"
  [ "$(V "$WHELD")" = "1" ] || { echo "FAIL: T1 does not hold the writer advisory lock"; exit 1; }
  echo "OK: T1 holds the writer lock inside an open transaction"

  ( $P -c "SELECT graph_store.gph_insert_edge(0, 2);" >/dev/null 2>&1 ) & T2=$!
  poll "$WWAIT" 1 "T2 (scalar insert) to BLOCK on the writer lock"
  echo "PASS (a1): second scalar writer BLOCKS (not error) on the writer lock"

  ( $P -c "SELECT graph_store.gph_insert_edges(1, ARRAY[2,3]::bigint[]);" >/dev/null 2>&1 ) & T3=$!
  poll "$WWAIT" 2 "T3 (batch gph_insert_edges) to BLOCK on the writer lock"
  echo "PASS (a2): batch loader BLOCKS behind the writer lock (no deadlock)"

  # (b) reader while the writer lock is held + two writers queue: must answer promptly
  # and must NOT see T1'\''s uncommitted edge. statement_timeout fails loud if blocked.
  rn=$($P -tAc "SET statement_timeout='\''5s'\''; SELECT count(*) FROM graph_store.gph_neighbors(0)" | tail -1)
  [ "$rn" = "0" ] || { echo "FAIL (b): reader saw $rn neighbors (expected 0: uncommitted + not blocked)"; exit 1; }
  rv=$($P -tAc "SET statement_timeout='\''5s'\''; SELECT graph_store.gph_vertex_count()" | tail -1)
  [ "$rv" = "4" ] || { echo "FAIL (b): gph_vertex_count()=$rv under held writer lock (expected 4)"; exit 1; }
  echo "PASS (b): reader traversal + counts answer promptly while the writer lock is held"

  ##########################################################################
  # (c) release -> everything serializes, exact final counts
  ##########################################################################
  echo "=== (c) serialization after release ==="
  kill $HOLDER >/dev/null 2>&1 || true; wait $HOLDER 2>/dev/null || true
  wait $T1 2>/dev/null || true
  wait $T2 2>/dev/null || true
  wait $T3 2>/dev/null || true

  n0=$(V "SELECT coalesce(array_agg(n ORDER BY n)::text, '\''{}'\'') FROM graph_store.gph_neighbors(0) n")
  n1=$(V "SELECT coalesce(array_agg(n ORDER BY n)::text, '\''{}'\'') FROM graph_store.gph_neighbors(1) n")
  ec=$(V "SELECT graph_store.gph_visible_edge_count()")
  [ "$n0" = "{1,2}" ] || { echo "FAIL (c): neighbors(0)=$n0 (expected {1,2})"; exit 1; }
  [ "$n1" = "{2,3}" ] || { echo "FAIL (c): neighbors(1)=$n1 (expected {2,3})"; exit 1; }
  [ "$ec" = "4" ]     || { echo "FAIL (c): visible_edge_count=$ec (expected 4)"; exit 1; }
  [ "$(V "$WHELD")" = "0" ] || { echo "FAIL (c): writer lock leaked after transactions ended"; exit 1; }
  echo "PASS (c): all writers serialized; exact counts (neighbors(0)={1,2}, neighbors(1)={2,3}, edges=4); lock released"

  ##########################################################################
  # (d) two interleaved autocommit writers, 50 single-statement inserts each
  ##########################################################################
  echo "=== (d) interleaved autocommit writers ==="
  for s in 2 3; do
    : > /tmp/w$s.sql
    for i in $(seq 1 50); do
      echo "SELECT graph_store.gph_insert_edge($s, $((s - 2)));" >> /tmp/w$s.sql
    done
  done
  ( $P -q -f /tmp/w2.sql >/dev/null 2>&1 ) & W2=$!
  ( $P -q -f /tmp/w3.sql >/dev/null 2>&1 ) & W3=$!
  wait $W2; wait $W3
  c2=$(V "SELECT count(*) FROM graph_store.gph_neighbors(2)")
  c3=$(V "SELECT count(*) FROM graph_store.gph_neighbors(3)")
  ec=$(V "SELECT graph_store.gph_visible_edge_count()")
  [ "$c2" = "50" ] || { echo "FAIL (d): neighbors(2) count=$c2 (expected 50)"; exit 1; }
  [ "$c3" = "50" ] || { echo "FAIL (d): neighbors(3) count=$c3 (expected 50)"; exit 1; }
  [ "$ec" = "104" ] || { echo "FAIL (d): visible_edge_count=$ec (expected 104 = 4 + 100)"; exit 1; }
  echo "PASS (d): 100 interleaved autocommit inserts serialized with exact final counts"

  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
'
echo "[graph_writer_lock_test] PASS"
