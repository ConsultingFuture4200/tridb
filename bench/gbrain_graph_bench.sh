#!/usr/bin/env bash
# gbrain_graph_bench.sh — gBrain graph-leg head-to-head: TriDB native adjacency AM vs gBrain's
# relational `links` table walked the way gBrain walks it (indexed single-hop + recursive CTE).
#
# Isolates the thesis: SAME topology, SAME database, one process — only the STORE differs. Vector and
# BM25 legs are irrelevant to this comparison and omitted. The relational side is TUNED (the exact
# idx_links_from/idx_links_to partial indexes gBrain ships) so this is a fair baseline, not a strawman.
#
# Runs INSIDE the pgvector fork image with src/graph_store mounted at /tmp/ext_v1. Params via env:
#   N (pages) AVG_DEG (edges/page) HUBS HUB_FANOUT DEPTH REPS
# Prints a timing table. GX10-gated (native AM builds against the image's PG 13.4).
set -euo pipefail
export PATH=/u01/app/postgres/product/13.4/bin:$PATH
: "${N:=50000}" "${AVG_DEG:=4}" "${HUBS:=20}" "${HUB_FANOUT:=2000}" "${DEPTH:=3}" "${REPS:=7}"

# 1. Build + install the native graph AM from the mounted source (like graph_test.sh).
cp -r /tmp/ext_v1 /tmp/gsbuild && cd /tmp/gsbuild
make >/tmp/gs.log 2>&1 && make install >>/tmp/gs.log 2>&1

# 2. Fresh cluster.
D=/tmp/benchpg; rm -rf "$D"; initdb -D "$D" >/dev/null 2>&1
pg_ctl -D "$D" -o "-p 5599 -c shared_buffers=2GB -c max_parallel_workers_per_gather=0" -w start >/dev/null 2>&1
PSQL="psql -p 5599 -d postgres -v ON_ERROR_STOP=1 -qtA"

$PSQL >/dev/null <<SQL
CREATE EXTENSION graph_store_am;
-- gBrain-faithful graph tables (topology subset; other columns don't affect traversal speed).
CREATE TABLE pages (id int PRIMARY KEY, source_id text DEFAULT 'default');
CREATE TABLE links (id serial PRIMARY KEY, from_page_id int, to_page_id int,
                    link_type text DEFAULT 'related', deleted_at timestamptz);
INSERT INTO pages(id) SELECT g FROM generate_series(1, $N) g;
-- regular edges: each page -> AVG_DEG pseudo-random targets
INSERT INTO links(from_page_id, to_page_id)
  SELECT g, 1 + floor(random()*$N)::int
  FROM generate_series(1, $N) g, generate_series(1, $AVG_DEG) s;
-- hub edges: HUBS low-id hub pages -> HUB_FANOUT targets each (power-law-ish knowledge graph)
INSERT INTO links(from_page_id, to_page_id)
  SELECT h, 1 + floor(random()*$N)::int
  FROM generate_series(1, $HUBS) h, generate_series(1, $HUB_FANOUT) s;
-- gBrain's exact partial indexes (TUNED baseline).
CREATE INDEX idx_links_from ON links(from_page_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_links_to   ON links(to_page_id)   WHERE deleted_at IS NULL;
ANALYZE pages; ANALYZE links;
-- Mirror the SAME topology into the native AM.
SELECT count(graph_store.gph_upsert_vertex(id)) FROM (SELECT id FROM pages ORDER BY id) p;
SELECT count(graph_store.gph_insert_edge(
         graph_store.gph_upsert_vertex(from_page_id),
         graph_store.gph_upsert_vertex(to_page_id), 1))
  FROM links WHERE deleted_at IS NULL;
SQL

echo "corpus: N=$N avg_deg=$AVG_DEG hubs=$HUBS hub_fanout=$HUB_FANOUT depth=$DEPTH reps=$REPS"
echo "edges: $($PSQL -c 'SELECT count(*) FROM links')"

# helper: median execution-time (ms) of a query over REPS runs, via EXPLAIN ANALYZE server-side time.
median_ms() {
  local q="$1" times=()
  for _ in $(seq 1 "$REPS"); do
    t=$($PSQL -c "EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON) $q" 2>/dev/null | grep -oE 'Execution Time: [0-9.]+' | grep -oE '[0-9.]+')
    times+=("$t")
  done
  printf '%s\n' "${times[@]}" | sort -n | awk '{a[NR]=$1} END{print a[int((NR+1)/2)]}'
}

echo
echo "=== A. single-hop expansion from hub #1 (degree ~$HUB_FANOUT) — the atomic graph op ==="
$PSQL >/dev/null -c "SELECT graph_store.gph_page_reads();"  # warm counter
NREADS0=$($PSQL -c "SELECT graph_store.gph_page_reads();")
$PSQL >/dev/null -c "SELECT count(*) FROM graph_store.neighbors(1);"
NREADS1=$($PSQL -c "SELECT graph_store.gph_page_reads();")
REL_A=$(median_ms "SELECT to_page_id FROM links WHERE from_page_id=1 AND deleted_at IS NULL")
NAT_A=$(median_ms "SELECT n FROM graph_store.neighbors(1) n")
echo "  relational (idx_links_from scan): ${REL_A} ms"
echo "  native AM (neighbors, read-once/page): ${NAT_A} ms   [adjacency page reads for the hub: $((NREADS1 - NREADS0))]"

echo
echo "=== B. multi-hop traversal to depth $DEPTH from hub #1 (recursive, same query shape) ==="
REL_B=$(median_ms "WITH RECURSIVE g(id,d) AS (
    SELECT 1,0
    UNION ALL
    SELECT l.to_page_id, g.d+1 FROM g JOIN links l ON l.from_page_id=g.id AND l.deleted_at IS NULL WHERE g.d < $DEPTH)
  SELECT count(DISTINCT id) FROM g")
NAT_B=$(median_ms "WITH RECURSIVE g(id,d) AS (
    SELECT 1,0
    UNION ALL
    SELECT n.n, g.d+1 FROM g CROSS JOIN LATERAL graph_store.neighbors(g.id) AS n(n) WHERE g.d < $DEPTH)
  SELECT count(DISTINCT id) FROM g")
echo "  relational recursive-CTE over links: ${REL_B} ms"
echo "  native AM (recursive over neighbors): ${NAT_B} ms"

echo
echo "=== C. single-hop from a REGULAR node (low degree ~$AVG_DEG) — the common case ==="
REL_C=$(median_ms "SELECT to_page_id FROM links WHERE from_page_id=$((HUBS+7)) AND deleted_at IS NULL")
NAT_C=$(median_ms "SELECT n FROM graph_store.neighbors($((HUBS+7))) n")
echo "  relational: ${REL_C} ms    native AM: ${NAT_C} ms"

pg_ctl -D "$D" -m immediate stop >/dev/null 2>&1 || true
echo; echo "[gbrain_graph_bench] done"
