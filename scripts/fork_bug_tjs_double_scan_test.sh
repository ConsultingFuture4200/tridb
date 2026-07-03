#!/usr/bin/env bash
#
# fork_bug_tjs_double_scan_test.sh — CI regression harness for the tjs() shape of the DEV-1236
# double-scan snapshot/UAF bug (advisor plan 012).
#
# Runs test/fork_bug_tjs_double_scan.sql against the forked-MSVBASE image and ASSERTS the patched
# outcome: a sibling scan of the operator's own target table issued in the SAME plpgsql block as a
# tjs() call now COMPLETES cleanly (the snapshot fix is in), returns the graph-restricted top-k, and
# the backend STAYS UP. A backend crash (signal 11 / lost connection / no PASS / no backend_alive)
# FAILS LOUD.
#
# If this harness FAILS on a freshly built image, that is a REAL FINDING — the double-scan fix does
# not cover the tjs() shape. Report it; do NOT soften the assert (advisor plan 012 STOP condition).
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker).
# Usage: scripts/fork_bug_tjs_double_scan_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL="$ROOT/test/fork_bug_tjs_double_scan.sql"

[[ -f "$SQL" ]] || { echo "test sql not found: $SQL" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

EXT="$ROOT/src/graph_store_ext"

OUT="$(docker run --rm --entrypoint bash -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/fork_bug.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  # The test SQL does CREATE EXTENSION graph_store (tjs graph leg + sibling scan), so PGXS-build
  # and install it exactly like scripts/graph_test.sh — the image does not ship it preinstalled.
  cp -r /tmp/ext /tmp/build_ext && cd /tmp/build_ext
  make PG_CONFIG=$PGC >/tmp/me.log 2>&1 || { tail -20 /tmp/me.log; echo "EXT BUILD FAILED"; exit 1; }
  make PG_CONFIG=$PGC install >>/tmp/me.log 2>&1 || { tail -20 /tmp/me.log; echo "EXT INSTALL FAILED"; exit 1; }
  D=/tmp/pg; rm -rf "$D"; mkdir -p "$D"
  "$B/initdb" -A trust -D "$D" >/tmp/i.log 2>&1
  "$B/pg_ctl" -D "$D" -o "-p 5432" -w start >/tmp/s.log 2>&1
  # NO ON_ERROR_STOP: a crash kills the connection; a plain assert-exception must still let the
  # liveness probe run so the harness can distinguish "assert failed" from "backend crashed".
  "$B/psql" -p 5432 -d postgres -f /tmp/fork_bug.sql 2>&1
  "$B/pg_ctl" -D "$D" -m fast stop >/dev/null 2>&1 || true
' 2>&1)"

echo "$OUT"

fail() { echo "[fork_bug_tjs_double_scan_test] FAIL — $1" >&2; exit 1; }

# A crash would show a lost connection / signal-11 termination.
echo "$OUT" | grep -qiE 'server (closed the connection|process was terminated)|connection to server was lost|terminated by signal|server crashed' \
  && fail "backend crashed (double-scan fix does not cover the tjs() shape — real finding, do NOT soften)"

# Required: the block completed and emitted its PASS notice (absent if the assert raised).
echo "$OUT" | grep -qE 'PASS tjs double-scan' \
  || fail "did not see the PASS notice — tjs()+sibling-scan block did not complete cleanly"

# Required: the backend survived (the liveness probe ran and returned 1).
echo "$OUT" | grep -qE 'backend_alive' \
  || fail "liveness probe (backend_alive) never executed — backend did not survive"
echo "$OUT" | grep -A2 'backend_alive' | grep -qE '(^| )1( |$)' \
  || fail "backend_alive did not return 1"

echo "[fork_bug_tjs_double_scan_test] PASS — tjs()+sibling-scan completes; backend survived."
