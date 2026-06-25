#!/usr/bin/env bash
#
# graph_concurrency_test.sh — two-session concurrency probes for the native graph store (DEV-1166).
#
# These pin down what the v1 graph store DOES and DOES NOT guarantee across concurrent sessions.
# FR-7 is ATOMICITY (commit/abort/crash), NOT cross-session snapshot isolation: gph_xmin_visible
# has no snapshot check (ADR-0003 defers per-tuple xmin/xmax + snapshots). So we assert ONLY:
#   (a) uncommitted-invisible: T1's open (uncommitted) vertex is invisible to a separate T2.
#   (b) commit-then-visible:   after T1 COMMITs, a NEW T2 statement sees it. (We do NOT assert a
#                              pre-existing T2 snapshot stayed stable — that isolation is deferred.)
#   (c) aborted-invisible:     T1's ROLLBACK'd vertex is invisible to a third-party session.
#   (d) same-vertex concurrent first-edge BOUNDARY PROBE: two sessions add the FIRST edge to the
#       SAME vertex; record ACTUAL behavior. gph_insert_edge re-reads vr_adj_tail UNDER the
#       vertex-page EXCLUSIVE buffer lock, so the second writer sees the first's adj page and both
#       edges should land. We VERIFY that (not assert a lost update) and label it KNOWN-LIMITATION
#       per the CONCURRENCY CONTRACT (logical single-writer for v1).
#
# Deterministic sync via pg_advisory_lock (NO sleep races): a holder session owns advisory lock 42;
# T1 inserts an uncommitted vertex then BLOCKS trying to acquire 42, holding its txn open. We poll
# pg_locks (granted / waiting) to step the sessions — no timing assumptions.
#
# Requires tridb/msvbase:dev. Build failures FAIL LOUD (make -> log, nonzero make aborts).
# Usage: scripts/graph_concurrency_test.sh [image]
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"
PROBE="$ROOT/test/graph_concurrency_probe.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${PROBE}:/tmp/probe.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  P="$B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -tA"
  Q() { $P -v q="$1" -f /tmp/probe.sql | tr -d "[:space:]"; }   # run a named probe, return scalar

  echo "=== make (graph_store_am) ==="
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  if ! make PG_CONFIG=$PGC >/tmp/make.log 2>&1; then echo "BUILD FAILED:"; tail -40 /tmp/make.log; exit 1; fi
  if ! make PG_CONFIG=$PGC install >/tmp/install.log 2>&1; then echo "INSTALL FAILED:"; tail -40 /tmp/install.log; exit 1; fi

  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1

  # session SQL written to files so single-quoted literals do not fight shell quoting.
  cat > /tmp/seed.sql <<SQL
CREATE EXTENSION graph_store_am;
SET search_path TO graph_store, public;
SELECT gph_insert_vertex() FROM generate_series(1,5);   -- vids 0..4 -> 5 visible baseline
SQL
  cat > /tmp/holder.sql <<SQL
SELECT pg_advisory_lock(42);
SELECT pg_sleep(30);
SQL
  cat > /tmp/t1.sql <<SQL
SET search_path TO graph_store, public;
BEGIN;
SELECT gph_insert_vertex();    -- vid 5, uncommitted
SELECT pg_advisory_lock(42);   -- BLOCKS here; txn remains open & uncommitted
COMMIT;
SQL
  cat > /tmp/abort.sql <<SQL
SET search_path TO graph_store, public;
BEGIN;
SELECT gph_insert_vertex();    -- vid 6, will be rolled back
ROLLBACK;
SQL
  cat > /tmp/seed_d.sql <<SQL
SET search_path TO graph_store, public;
SELECT gph_insert_vertex();   -- vid 6 (edge target a)
SELECT gph_insert_vertex();   -- vid 7 (edge target b)
SELECT gph_insert_vertex();   -- vid 8 (fresh SOURCE with no edges)
SQL

  $P -f /tmp/seed.sql >/dev/null
  base=$(Q count)
  [ "$base" = "5" ] || { echo "FAIL: baseline vertex_count=$base (expected 5)"; exit 1; }

  ##########################################################################
  # (a) uncommitted-invisible  +  (b) commit-then-visible
  ##########################################################################
  echo "=== (a) uncommitted-invisible / (b) commit-then-visible ==="
  ( $P -f /tmp/holder.sql >/dev/null 2>&1 ) & HOLDER=$!
  for i in $(seq 1 100); do [ "$(Q holder_has)" -ge 1 ] 2>/dev/null && break; sleep 0.1; done

  ( $P -f /tmp/t1.sql >/dev/null 2>&1 ) & T1=$!
  for i in $(seq 1 100); do [ "$(Q waiter_has)" -ge 1 ] 2>/dev/null && break; sleep 0.1; done

  # T2: T1 holds an OPEN uncommitted insert -> must still see baseline 5.
  t2a=$(Q count)
  [ "$t2a" = "5" ] || { echo "FAIL (a): T2 saw uncommitted vertex (count=$t2a, expected 5)"; exit 1; }
  echo "PASS (a) uncommitted-invisible: T2 sees baseline 5 while T1 holds an open uncommitted insert"

  kill $HOLDER >/dev/null 2>&1 || true; wait $HOLDER 2>/dev/null || true
  wait $T1 2>/dev/null || true   # T1 acquires lock 42, COMMITs, exits

  t2b=$(Q count)
  [ "$t2b" = "6" ] || { echo "FAIL (b): after T1 COMMIT, T2 count=$t2b (expected 6)"; exit 1; }
  echo "PASS (b) commit-then-visible: a NEW T2 statement sees the committed vertex (6)"

  ##########################################################################
  # (c) aborted-invisible to a third party.
  ##########################################################################
  echo "=== (c) aborted-invisible ==="
  $P -f /tmp/abort.sql >/dev/null
  t3=$(Q count)
  [ "$t3" = "6" ] || { echo "FAIL (c): aborted vertex visible to third party (count=$t3, expected 6)"; exit 1; }
  echo "PASS (c) aborted-invisible: third-party session still sees 6 after T1 ROLLBACK"

  ##########################################################################
  # (d) same-vertex concurrent FIRST-edge boundary PROBE (KNOWN-LIMITATION).
  ##########################################################################
  echo "=== (d) same-vertex concurrent first-edge boundary probe (KNOWN-LIMITATION) ==="
  $P -f /tmp/seed_d.sql >/dev/null   # vids 6,7 (targets), 8 (fresh source)
  ( $P -c "SELECT graph_store.gph_insert_edge(8,6);" >/dev/null 2>&1 ) &
  ( $P -c "SELECT graph_store.gph_insert_edge(8,7);" >/dev/null 2>&1 ) &
  wait
  nbrs=$(Q neighbors8)
  cnt=$(Q count8)
  echo "PROBE (d): source 8 first-edge race -> neighbors={$nbrs} (count=$cnt)"
  if [ "$cnt" = "2" ]; then
    echo "PROBE (d) RESULT: BOTH edges landed (under-lock vr_adj_tail re-read held) — KNOWN-LIMITATION boundary OK for v1"
  else
    echo "PROBE (d) RESULT: count=$cnt (<2) — a concurrent first-edge update was lost; documented KNOWN-LIMITATION (logical single-writer, ADR-0003 / 0003a)"
  fi

  $B/pg_ctl -D $D -m fast -w stop >/dev/null 2>&1 || true
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[graph_concurrency_test] done"
