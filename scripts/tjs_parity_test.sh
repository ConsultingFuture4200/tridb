#!/usr/bin/env bash
#
# tjs_parity_test.sh — fork<->stock filter-first PARITY harness (advisor plan 071).
#
# Two independent implementations of the filter-first fused query exist:
#   FORK  (tridb/msvbase:dev): the Gate-A fused SQL statement — native typed BFS
#         (graph_store.gph_traverse_bfs) -> relational P31 filter -> exact vector rank.
#         The fork tjs_open has NO typed-traversal slot (bench/wikidata_h2h.py SURFACE
#         HONESTY note); the fused statement IS the fork's filter-first reference form
#         (ADR-0019 scope: "filter-first needs no operator on stock PG").
#   STOCK (tridb/pg17-unfork:dev): the tjs_pg operator's src-IS-NOT-NULL mode, which
#         runs the same plan behind the operator surface (src/tjs_pg/tjs_pg.c).
#
# ADR-0019 keeps the fork as the reference implementation until the stock operator
# reproduces its results — this harness makes that parity a TEST: the SAME deterministic
# corpus (dialect-toggled embedding column only; graph store byte-identical) and the SAME
# (anchor X, property P, type T, k, hops) queries through both engines, asserting equal
# top-k id arrays. Any drift prints PARITY MISMATCH and exits nonzero.
#
# HEAVY (needs both engine images; the fork image is ~9 GB, x86-only) — a manual /
# CI-dispatch gate, NOT a per-PR check. Filter-first ONLY; seedless parity is a
# separate, harder problem (budget-shaped recall, ADR-0015 E3.3).
#
# Corpus construction: 2000 entities, embedding[0] = id/2000 (strictly monotone), typed
# hubs 2--P1-->{1000..1100}, 3--P2-->{1200..1300}, 5--P1-->{1500..1560},
# 7--P1-->8--P1-->{1700..1750} (2-hop), p31 = {(id%3)+7}. Every query vector sits at or
# below the low edge of its candidate band, so candidate distances are strictly
# increasing in id — NO ties anywhere near a k-boundary (the stock operator's internal
# ORDER BY has no id tie-break, so a tie would be legal nondeterminism, not drift).
#
# Usage: scripts/tjs_parity_test.sh
#   FORK_IMAGE / STOCK_IMAGE env vars override the image tags.
#
set -euo pipefail
FORK_IMAGE="${FORK_IMAGE:-tridb/msvbase:dev}"
STOCK_IMAGE="${STOCK_IMAGE:-tridb/pg17-unfork:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_V1="$ROOT/src/graph_store"
EXT_TJS="$ROOT/src/tjs_pg"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

docker image inspect "$FORK_IMAGE" >/dev/null 2>&1 || {
  echo "image $FORK_IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }
docker image inspect "$STOCK_IMAGE" >/dev/null 2>&1 || {
  echo "image $STOCK_IMAGE not built — run: docker build -t tridb/pg17-unfork:dev scripts/pg17/" >&2; exit 1; }

# ---------------------------------------------------------------------------------
# The query set: name  X  ptype  T  k  hops  qv0   (T=0 -> no relational type filter;
# qv0 is the first embedding dimension of the query vector, remaining dims are 0).
# ---------------------------------------------------------------------------------
QUERIES=(
  "q1   2  p1  7  5  1  0.5"
  "q2   2  p1  8  5  1  0.5"
  "q3   2  p1  9  7  1  0.55"
  "q4   3  p2  7  5  1  0.6"
  "q5   3  p2  8  3  1  0.65"
  "q6   5  p1  9  5  1  0.75"
  "q7   7  p1  7  5  2  0.85"
  "q8   7  p1  9  4  2  0"
  "q9   2  p1  7  5  2  0.5"
  "q10  2  p1  0  5  1  0.5"
  "q11  5  p1  6  5  1  0.75"
)

# ---------------------------------------------------------------------------------
# Fixture generation. The graph-store setup is IDENTICAL across dialects (same AM
# source built in both images); only the embedding column/index/literals differ:
#   fork  = float8[] + vectordb hnsw + '{..}' literals + <-> SQL rank expression
#   stock = vector(8) + pgvector hnsw (vector_l2_ops) + '[..]'::vector parameter
# (the dialect split mirrors tools/wikidata_engine_load.py). Dense in-order vertex
# load satisfies graph_store.assume_dense_open's precondition (advisor 048), set on
# both sides exactly as the D1 wikidata run did (bench/wikidata_h2h.py emit).
# ---------------------------------------------------------------------------------
gen_fixture() { # $1 = fork|stock, writes $WORK/$1.sql
  local dialect="$1" out="$WORK/$1.sql"
  {
    echo '\pset pager off'
    echo '\pset format unaligned'
    echo '\t on'
    if [ "$dialect" = fork ]; then
      cat <<'SQL'
CREATE EXTENSION vectordb;
CREATE EXTENSION graph_store_am;
CREATE TABLE entities (id bigint PRIMARY KEY, p31 int[], embedding float8[8]);
INSERT INTO entities
  SELECT g, ARRAY[(g % 3) + 7], ARRAY[g::float8/2000,0,0,0,0,0,0,0]::float8[]
  FROM generate_series(0, 1999) AS g;
CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
  WITH (dimension = 8, distmethod = l2_distance);
SQL
    else
      cat <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS graph_store_am;
CREATE EXTENSION IF NOT EXISTS tjs_pg;
CREATE TABLE entities (id bigint PRIMARY KEY, p31 int[], embedding vector(8));
INSERT INTO entities
  SELECT g, ARRAY[(g % 3) + 7],
         (('[' || (g::float8/2000)::text || ',0,0,0,0,0,0,0]')::vector(8))
  FROM generate_series(0, 1999) AS g;
CREATE INDEX entities_hnsw ON entities USING hnsw (embedding vector_l2_ops)
  WITH (m = 16, ef_construction = 64);
SQL
    fi
    # graph store: identical on both sides (dense vids 0..1999, ext id == vid)
    cat <<'SQL'
DO $$
DECLARE g int; v bigint;
BEGIN
  FOR g IN 0..1999 LOOP
    v := graph_store.gph_upsert_vertex(g);
    IF v <> g THEN RAISE EXCEPTION 'dense vid drift: % != %', v, g; END IF;
  END LOOP;
END $$;
SELECT graph_store.register_edge_type('P1') AS p1 \gset
SELECT graph_store.register_edge_type('P2') AS p2 \gset
SELECT count(*) FROM (SELECT graph_store.gph_insert_edge(2, g, :p1)
                      FROM generate_series(1000, 1100) AS g) s;
SELECT count(*) FROM (SELECT graph_store.gph_insert_edge(3, g, :p2)
                      FROM generate_series(1200, 1300) AS g) s;
SELECT count(*) FROM (SELECT graph_store.gph_insert_edge(5, g, :p1)
                      FROM generate_series(1500, 1560) AS g) s;
SELECT graph_store.gph_insert_edge(7, 8, :p1);
SELECT count(*) FROM (SELECT graph_store.gph_insert_edge(8, g, :p1)
                      FROM generate_series(1700, 1750) AS g) s;
SET enable_seqscan = off;
SET graph_store.assume_dense_open = on;
SQL
    # the queries
    local line name x pt t k hops qv0
    for line in "${QUERIES[@]}"; do
      read -r name x pt t k hops qv0 <<<"$line"
      if [ "$dialect" = fork ]; then
        local typef=""
        [ "$t" != 0 ] && typef=" AND e.p31 @> ARRAY[$t]"
        cat <<SQL
SELECT '#PQ $name ids=' || coalesce(array_to_string(array_agg(id), ','), '')
FROM (SELECT e.id
      FROM graph_store.gph_traverse_bfs($x, $hops, :$pt) AS t(dst)
      JOIN entities e ON e.id = t.dst
      WHERE e.id <> $x$typef
      ORDER BY e.embedding <-> '{$qv0,0,0,0,0,0,0,0}', e.id
      LIMIT $k) q;
SQL
      else
        local filter=""
        [ "$t" != 0 ] && filter="p31 @> ARRAY[$t]"
        cat <<SQL
SELECT '#PQ $name ids=' || coalesce(array_to_string(array_agg(t), ','), '')
FROM tjs_open('entities', $k, 0, 0, $hops, 'id', '$filter',
              '[$qv0,0,0,0,0,0,0,0]'::vector, $x, :$pt) AS t;
SQL
      fi
    done
  } > "$out"
}

gen_fixture fork
gen_fixture stock

# ---------------------------------------------------------------------------------
# FORK side (mirrors scripts/graph_test.sh: PGXS-build the v1 AM in the fork image,
# throwaway cluster, fail-loud make). vectordb is preinstalled in the image.
# ---------------------------------------------------------------------------------
echo "=== fork side ($FORK_IMAGE) ==="
if ! docker run --rm --entrypoint bash \
    -v "${EXT_V1}:/tmp/ext_v1:ro" \
    -v "$WORK/fork.sql:/tmp/parity.sql:ro" "$FORK_IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  cp -r /tmp/ext_v1 /tmp/build_v1 && cd /tmp/build_v1
  echo "=== make (v1, fork) ==="
  make PG_CONFIG=$PGC >/tmp/make_v1.log 2>&1 || { tail -30 /tmp/make_v1.log; echo "BUILD FAILED (v1)"; exit 1; }
  tail -2 /tmp/make_v1.log
  make PG_CONFIG=$PGC install >/tmp/install_v1.log 2>&1 || { tail -20 /tmp/install_v1.log; echo "INSTALL FAILED (v1)"; exit 1; }
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  echo "=== run parity fixture (fork) ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/parity.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' > "$WORK/fork.log" 2>&1; then
  echo "FORK ENGINE RUN FAILED:" >&2
  tail -40 "$WORK/fork.log" >&2
  exit 1
fi
grep -c '^#PQ ' "$WORK/fork.log" >/dev/null || { echo "fork run produced no #PQ lines" >&2; tail -40 "$WORK/fork.log" >&2; exit 1; }

# ---------------------------------------------------------------------------------
# STOCK side (mirrors scripts/pg17_graph_test.sh: build v1 AM + tjs_pg against the
# stock headers, throwaway 8KB cluster, fail-loud make). pgvector is preinstalled.
# ---------------------------------------------------------------------------------
echo "=== stock side ($STOCK_IMAGE) ==="
if ! docker run --rm --user postgres --entrypoint bash \
    -v "${EXT_V1}:/tmp/ext_v1:ro" -v "${EXT_TJS}:/tmp/ext_tjs:ro" \
    -v "$WORK/stock.sql:/tmp/parity.sql:ro" "$STOCK_IMAGE" -c '
  set -e
  B=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)
  PGC=$B/pg_config
  for e in v1 tjs; do
    cp -r /tmp/ext_$e /tmp/build_$e && cd /tmp/build_$e
    echo "=== make ($e, stock) ==="
    make PG_CONFIG=$PGC >/tmp/make_$e.log 2>&1 || { tail -30 /tmp/make_$e.log; echo "BUILD FAILED ($e)"; exit 1; }
    tail -2 /tmp/make_$e.log
    make PG_CONFIG=$PGC install >/tmp/install_$e.log 2>&1 || { tail -20 /tmp/install_$e.log; echo "INSTALL FAILED ($e)"; exit 1; }
    tail -1 /tmp/install_$e.log
  done
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null
  $B/pg_ctl -D $D -o "-p 5499 -c listen_addresses=" -w start >/dev/null
  echo "=== run parity fixture (stock) ==="
  $B/psql -p 5499 -d postgres -v ON_ERROR_STOP=1 -f /tmp/parity.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' > "$WORK/stock.log" 2>&1; then
  echo "STOCK ENGINE RUN FAILED:" >&2
  tail -40 "$WORK/stock.log" >&2
  exit 1
fi
grep -c '^#PQ ' "$WORK/stock.log" >/dev/null || { echo "stock run produced no #PQ lines" >&2; tail -40 "$WORK/stock.log" >&2; exit 1; }

# ---------------------------------------------------------------------------------
# Differential: same top-k id array per query, in order. A missing line on either
# side is a failure (fail-loud), never a silent skip.
# ---------------------------------------------------------------------------------
declare -A FORK_IDS STOCK_IDS
while read -r _tag name ids; do FORK_IDS[$name]="${ids#ids=}"; done \
  < <(grep '^#PQ ' "$WORK/fork.log")
while read -r _tag name ids; do STOCK_IDS[$name]="${ids#ids=}"; done \
  < <(grep '^#PQ ' "$WORK/stock.log")

fail=0
for line in "${QUERIES[@]}"; do
  read -r name _rest <<<"$line"
  f="${FORK_IDS[$name]-<MISSING>}"
  s="${STOCK_IDS[$name]-<MISSING>}"
  if [ "$f" = "$s" ] && [ "$f" != "<MISSING>" ]; then
    echo "PARITY OK $name ids=$f"
  else
    echo "PARITY MISMATCH $name fork=$f stock=$s"
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "[tjs_parity_test] FAILED — fork and stock filter-first results diverge" >&2
  exit 1
fi
echo "[tjs_parity_test] PARITY OK on all ${#QUERIES[@]} queries"
