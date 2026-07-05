#!/usr/bin/env bash
# gbrain_graph_cold.sh — the I/O-bound regime for the storage-locality thesis. Measures PAGES TOUCHED
# per single-hop hub expansion (native co-located adjacency vs relational index+scattered heap) — the
# cache-independent storage metric — plus COLD first-touch latency (small shared_buffers + restart).
# This is where TriDB's read-once-per-page adjacency should actually pay off, if it ever does.
# Runs inside the pgvector fork image with src/graph_store at /tmp/ext_v1. GX10-gated.
set -euo pipefail
export PATH=/u01/app/postgres/product/13.4/bin:$PATH
: "${N:=200000}" "${AVG_DEG:=6}" "${HUBS:=40}" "${HUB_FANOUT:=3000}"

cp -r /tmp/ext_v1 /tmp/gsbuild && cd /tmp/gsbuild
make >/tmp/gs.log 2>&1 && make install >>/tmp/gs.log 2>&1

D=/tmp/coldpg; rm -rf "$D"; initdb -D "$D" >/dev/null 2>&1
# SMALL shared_buffers so the graph does not sit resident -> buffer misses = real reads.
START="pg_ctl -D $D -o '-p 5599 -c shared_buffers=16MB -c max_parallel_workers_per_gather=0' -w"
eval "$START start" >/dev/null 2>&1
PSQL="psql -p 5599 -d postgres -v ON_ERROR_STOP=1 -qtA"

$PSQL >/dev/null <<SQL
CREATE EXTENSION graph_store_am;
CREATE TABLE pages (id int PRIMARY KEY);
CREATE TABLE links (id serial PRIMARY KEY, from_page_id int, to_page_id int, deleted_at timestamptz);
INSERT INTO pages(id) SELECT g FROM generate_series(1,$N) g;
INSERT INTO links(from_page_id,to_page_id) SELECT g,1+floor(random()*$N)::int FROM generate_series(1,$N) g, generate_series(1,$AVG_DEG) s;
INSERT INTO links(from_page_id,to_page_id) SELECT h,1+floor(random()*$N)::int FROM generate_series(1,$HUBS) h, generate_series(1,$HUB_FANOUT) s;
CREATE INDEX idx_links_from ON links(from_page_id) WHERE deleted_at IS NULL;
ANALYZE pages; ANALYZE links;
SELECT count(graph_store.gph_upsert_vertex(id)) FROM (SELECT id FROM pages ORDER BY id) p;
SELECT count(graph_store.gph_insert_edge(graph_store.gph_upsert_vertex(from_page_id),graph_store.gph_upsert_vertex(to_page_id),1)) FROM links WHERE deleted_at IS NULL;
SQL
echo "corpus: N=$N edges=$($PSQL -c 'SELECT count(*) FROM links') shared_buffers=16MB"
echo "measuring COLD (post-restart) first-touch single-hop for $HUBS fresh hubs; PAGES TOUCHED + ms"

# restart -> cold shared_buffers.
eval "$START restart" >/dev/null 2>&1

REL_PAGES=0; NAT_PAGES=0; REL_MS=0; NAT_MS=0; NH=0
for H in $(seq 1 $HUBS); do
  VID=$($PSQL -c "SELECT graph_store.gph_upsert_vertex($H)")
  # RELATIONAL: pages touched (shared hit+read) + exec time, first touch this hub.
  REL=$($PSQL -c "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) SELECT to_page_id FROM links WHERE from_page_id=$H AND deleted_at IS NULL")
  rp=$(echo "$REL" | grep -oE 'shared hit=[0-9]+ read=[0-9]+|shared read=[0-9]+|shared hit=[0-9]+' | grep -oE '[0-9]+' | awk '{s+=$1} END{print s+0}')
  rms=$(echo "$REL" | grep -oE 'Execution Time: [0-9.]+' | grep -oE '[0-9.]+')
  # NATIVE: adjacency pages touched — counter delta measured IN ONE SESSION (\gset), else the
  # per-backend counter reads across separate connections give a meaningless 0.
  np=$($PSQL <<SQL
SELECT graph_store.gph_page_reads() AS r0 \gset
SELECT count(*) FROM graph_store.gph_neighbors($VID) \gset
SELECT graph_store.gph_page_reads() - :r0
SQL
)
  NAT=$($PSQL -c "EXPLAIN (ANALYZE, TIMING OFF) SELECT n FROM graph_store.gph_neighbors($VID) n")
  nms=$(echo "$NAT" | grep -oE 'Execution Time: [0-9.]+' | grep -oE '[0-9.]+')
  REL_PAGES=$((REL_PAGES + rp)); NAT_PAGES=$((NAT_PAGES + np)); NH=$((NH+1))
  REL_MS=$(awk "BEGIN{print $REL_MS + $rms}"); NAT_MS=$(awk "BEGIN{print $NAT_MS + $nms}")
done
echo
echo "=== COLD single-hop hub expansion (degree ~$HUB_FANOUT), averaged over $NH fresh hubs ==="
awk "BEGIN{printf \"  relational: %.1f pages touched, %.3f ms avg\n\", $REL_PAGES/$NH, $REL_MS/$NH}"
awk "BEGIN{printf \"  native AM:  %.1f pages touched, %.3f ms avg\n\", $NAT_PAGES/$NH, $NAT_MS/$NH}"
awk "BEGIN{printf \"  page-locality ratio (rel/native): %.1fx fewer reads for the native store\n\", ($NAT_PAGES>0)?$REL_PAGES/$NAT_PAGES:0}"

eval "$START stop" >/dev/null 2>&1 || true
echo; echo "[gbrain_graph_cold] done"
