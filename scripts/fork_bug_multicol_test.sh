#!/usr/bin/env bash
#
# fork_bug_multicol_test.sh — CI regression harness for the DEV-1236 SIGSEGV (DEV-1249).
#
# Runs test/fork_bug_multicol_double_scan.sql against the forked-MSVBASE image and ASSERTS
# the patched outcome: the unordered count(*) over the HNSW index raises a CLEAN ERROR
# ("requires an ORDER BY <-> distance clause") instead of crashing the backend, the backend
# STAYS UP (liveness probe returns 1), and the ORDER BY <-> control still returns rows.
#
# Must run WITHOUT -v ON_ERROR_STOP=1 so the expected ERROR does not halt the liveness probe.
# A backend crash (signal 11 / "connection to server was lost" / no backend_alive) FAILS LOUD.
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker).
# Usage: scripts/fork_bug_multicol_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL="$ROOT/test/fork_bug_multicol_double_scan.sql"

[[ -f "$SQL" ]] || { echo "test sql not found: $SQL" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

OUT="$(docker run --rm --entrypoint bash -v "${SQL}:/tmp/fork_bug.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  D=/tmp/pg; rm -rf "$D"; mkdir -p "$D"
  "$B/initdb" -A trust -D "$D" >/tmp/i.log 2>&1
  "$B/pg_ctl" -D "$D" -o "-p 5432" -w start >/tmp/s.log 2>&1
  # NO ON_ERROR_STOP: the expected clean ERROR must not abort the liveness probe.
  "$B/psql" -p 5432 -d postgres -f /tmp/fork_bug.sql 2>&1
  "$B/pg_ctl" -D "$D" -m fast stop >/dev/null 2>&1 || true
' 2>&1)"

echo "$OUT"

fail() { echo "[fork_bug_multicol_test] FAIL — $1" >&2; exit 1; }

# A crash would show a lost connection / signal-11 termination and no liveness row.
echo "$OUT" | grep -qiE 'server (closed the connection|process was terminated)|connection to server was lost|terminated by signal|server crashed' \
  && fail "backend crashed (DEV-1236 regression — SIGSEGV not fixed)"

# Required: the clean, descriptive ERROR replaced the crash.
echo "$OUT" | grep -qiE 'ERROR:.*ORDER BY' \
  || fail "did not see the expected clean ERROR mentioning ORDER BY <-> distance"

# Required: the backend survived the ERROR (the liveness probe ran and returned 1).
echo "$OUT" | grep -qE 'backend_alive' \
  || fail "liveness probe (backend_alive) never executed — backend did not survive"
echo "$OUT" | grep -A2 'backend_alive' | grep -qE '(^| )1( |$)' \
  || fail "backend_alive did not return 1"

# Required: the ORDER BY <-> positive control still returns its 5 rows.
echo "$OUT" | grep -qE '\(5 rows\)' \
  || fail "ORDER BY <-> positive control did not return 5 rows"

echo "[fork_bug_multicol_test] PASS — DEV-1236 crash is a clean ERROR; backend survived; control OK."
