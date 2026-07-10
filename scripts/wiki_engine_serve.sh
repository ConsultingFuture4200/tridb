#!/usr/bin/env bash
#
# wiki_engine_serve.sh — stand up a PERSISTENT, PORT-EXPOSED MSVBASE-fork engine loaded with a
# prepared tri-modal wiki slice (tools/wiki_engine_load.py prepare), so a client can measure
# tjs_open latency over TCP at the SAME boundary as the multi-store baseline (timer parity).
#
# Unlike scripts/wiki_engine_load.sh (which loads and exits a throwaway cluster), this keeps the
# engine UP: it builds graph_store, initdb, starts postgres, runs /data/load.sql, writes
# /out/load.done on success (or /out/load.fail), and then sleeps so the container stays queryable
# via the published port and `docker exec`.
#
# SECURITY (advisor 044): the published TCP port is password-protected. A random password is
# generated per run and written to "$OUT/pg_password" (host mode 0600). Connect with:
#   PGPASSWORD="$(cat "$OUT/pg_password")" psql -h 127.0.0.1 -p <port> -U postgres -d postgres
# The unix-socket path inside the container (used by the build/install/load steps below, and by
# `docker exec` transcripts such as bench/wiki_h2h.py's) stays trust-auth — only TCP requires the
# password. By default the port is published to 127.0.0.1 only; set TRIDB_SERVE_BIND=0.0.0.0 to
# publish on all interfaces (e.g. a shared-lab timer-parity run) — password auth still applies,
# trust never does over TCP.
#
# Usage: wiki_engine_serve.sh <image> <prep_dir> <host_port> <container_name> <out_dir>
set -euo pipefail
IMAGE="${1:?image}"; PREP="${2:?prep_dir}"; PORT="${3:?host_port}"; NAME="${4:?container}"; OUT="${5:?out_dir}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"
PREP="$(cd "$PREP" && pwd)"; mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
# container runs as the non-root postgres user (initdb refuses root) and must write /out. 770 is
# a partial hardening vs the old 777 (world-writable): it still only works if the container's uid
# maps to (or shares a group with) the invoking host user — residual risk on hosts where it does
# not; widen back to 777 there rather than fail, but prefer a fixed shared group / --user mapping
# if you control the image.
chmod 770 "$OUT"
[ -f "$PREP/load.sql" ] || { echo "no load.sql in $PREP" >&2; exit 1; }
chmod -R a+rX "$PREP" 2>/dev/null || true
docker rm -f "$NAME" >/dev/null 2>&1 || true
rm -f "$OUT/load.done" "$OUT/load.fail" "$OUT/pg_password"

BIND="${TRIDB_SERVE_BIND:-127.0.0.1}"
PGPASS="$(openssl rand -hex 16)"
( umask 077 && printf '%s' "$PGPASS" > "$OUT/pg_password" )

docker run -d --name "$NAME" --entrypoint bash \
  -p "${BIND}:${PORT}:5432" \
  -e TRIDB_PGPASS="$PGPASS" \
  -v "${EXT}:/tmp/ext:ro" -v "${PREP}:/data:ro" -v "${OUT}:/out" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  make PG_CONFIG=$PGC >/out/make.log 2>&1 || { echo BUILD_FAILED; tail -40 /out/make.log; touch /out/load.fail; sleep infinity; }
  make PG_CONFIG=$PGC install >/out/install.log 2>&1 || { echo INSTALL_FAILED; tail -40 /out/install.log; touch /out/load.fail; sleep infinity; }
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/out/initdb.log 2>&1
  # initdb -A trust also trusts TCP loopback by default; strip its "host" lines and require a
  # password for ALL host (TCP) connections instead. "local" (unix socket) stays trust — that is
  # what the build/install/load steps below and docker-exec transcripts use (no -h => socket).
  grep -v "^host" $D/pg_hba.conf > $D/pg_hba.conf.new && mv $D/pg_hba.conf.new $D/pg_hba.conf
  echo "host all all 0.0.0.0/0 scram-sha-256" >> $D/pg_hba.conf
  echo "host all all ::/0 scram-sha-256" >> $D/pg_hba.conf
  $B/pg_ctl -D $D -o "-p 5432 -c listen_addresses=*\
 -c maintenance_work_mem=${PGMEM:-4GB} -c work_mem=256MB -c statement_timeout=0" -w start >/out/pgctl.log 2>&1
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 \
    -c "SET password_encryption = \$\$scram-sha-256\$\$;" \
    -c "ALTER USER postgres WITH PASSWORD \$\$$TRIDB_PGPASS\$\$;" >/out/setpw.log 2>&1
  if $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /data/load.sql >/out/load.log 2>&1; then
    touch /out/load.done
  else
    echo LOAD_FAILED; tail -30 /out/load.log; touch /out/load.fail
  fi
  # keep postgres up + container alive for client queries
  sleep infinity
'
echo "[serve] $NAME up on ${BIND}:${PORT} (image $IMAGE, prep $PREP); password: $OUT/pg_password (mode 0600); poll $OUT/load.done"
