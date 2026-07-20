#!/usr/bin/env bash
#
# extension_upgrade_test.sh — the 0.1.0 -> 0.2.0 ALTER EXTENSION UPDATE gate on STOCK
# PostgreSQL 16/17 (advisor plan 100). Proves the versioned-upgrade mechanism carries a
# GENUINE 0.1.0 install forward with its data intact:
#
#   1. install the vendored 0.1.0 extension SQL (test/fixtures/upgrade/, verbatim from
#      997b679 — the last pushed master, the practical "0.1.0" release boundary; the plan
#      text's a780b46 pin was stale) alongside the 0.2.0 base + upgrade scripts;
#   2. CREATE EXTENSION ... VERSION '0.1.0' for both extensions, load a tri-modal corpus
#      (the plan-099 round-trip corpus: typed edges, an interleaved source, a tombstone,
#      identity_mode ON, HNSW index);
#   3. ALTER EXTENSION graph_store_am UPDATE TO '0.2.0'; ALTER EXTENSION tjs_pg UPDATE
#      TO '0.2.0';
#   4. assert the SAME probe file is byte-identical before and after (the graph survives
#      an upgrade), and that the 0.2.0-only surface (dump SRFs, config-dump marks, the
#      plan-100 writer advisory lock) works on the PRE-EXISTING data.
#
# Container conventions follow scripts/pg17_graph_test.sh / graph_dump_restore_test.sh.
#
# Usage: scripts/extension_upgrade_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/pg17-unfork:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_V1="$ROOT/src/graph_store"
EXT_TJS="$ROOT/src/tjs_pg"
FIXTURES="$ROOT/test/fixtures/upgrade"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run: docker build -t tridb/pg17-unfork:dev scripts/pg17/" >&2
  exit 1
}

docker run --rm --user postgres --entrypoint bash \
  -v "${EXT_V1}:/tmp/ext_v1:ro" -v "${EXT_TJS}:/tmp/ext_tjs:ro" \
  -v "${FIXTURES}:/tmp/fixtures:ro" "$IMAGE" -c '
  set -e
  B=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)  # works for the pg16/pg17 CI matrix
  PGC=$B/pg_config
  # Fail LOUD on a build error (plan 072 shape): no `| tail` masking of a nonzero make.
  for e in v1 tjs; do
    cp -r /tmp/ext_$e /tmp/build_$e && cd /tmp/build_$e
    echo "=== make ($e, stock) ==="
    make PG_CONFIG=$PGC >/tmp/make_$e.log 2>&1 || { tail -30 /tmp/make_$e.log; echo "BUILD FAILED ($e)"; exit 1; }
    make PG_CONFIG=$PGC install >/tmp/install_$e.log 2>&1 || { tail -20 /tmp/install_$e.log; echo "INSTALL FAILED ($e)"; exit 1; }
  done
  # Vendor the genuine 0.1.0 base scripts next to the installed 0.2.0 + upgrade scripts,
  # so CREATE EXTENSION ... VERSION '\''0.1.0'\'' installs the REAL released surface.
  SHAREEXT=$($PGC --sharedir)/extension
  cp /tmp/fixtures/graph_store_am--0.1.0.sql /tmp/fixtures/tjs_pg--0.1.0.sql $SHAREEXT/

  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null
  $B/pg_ctl -D $D -o "-p 5499 -c listen_addresses=" -w start >/dev/null
  PSQL="$B/psql -p 5499 -v ON_ERROR_STOP=1"
  V() { $B/psql -p 5499 -d updb -tAc "$1"; }

  # ---------------------------------------------------------------- 0.1.0 install + corpus
  $B/createdb -p 5499 updb
  $PSQL -d updb -q <<EOSQL
CREATE EXTENSION vector;
CREATE EXTENSION graph_store_am VERSION '\''0.1.0'\'';
CREATE EXTENSION tjs_pg VERSION '\''0.1.0'\'';
EOSQL
  v1=$(V "SELECT extversion FROM pg_extension WHERE extname='\''graph_store_am'\''")
  v2=$(V "SELECT extversion FROM pg_extension WHERE extname='\''tjs_pg'\''")
  [ "$v1" = "0.1.0" ] && [ "$v2" = "0.1.0" ] || { echo "FAIL: 0.1.0 install got graph_store_am=$v1 tjs_pg=$v2"; exit 1; }
  # the 0.2.0-only surface must NOT exist yet, and no config-dump marks either
  [ "$(V "SELECT to_regprocedure('\''graph_store.gph_dump_vertices()'\'') IS NULL")" = "t" ] || \
    { echo "FAIL: gph_dump_vertices() already present at 0.1.0 (fixture is not the 0.1.0 surface)"; exit 1; }
  [ "$(V "SELECT extconfig IS NULL FROM pg_extension WHERE extname='\''graph_store_am'\''")" = "t" ] || \
    { echo "FAIL: extconfig already marked at 0.1.0"; exit 1; }
  echo "PASS: genuine 0.1.0 installed (no 0.2.0 surface present)"

  # The plan-099 round-trip corpus, loaded ON THE 0.1.0 INSTALL (pre-existing data).
  $PSQL -d updb -q <<EOSQL
CREATE TABLE entities (
    id bigint PRIMARY KEY, chunk text, ts int, embedding vector(8)
);
INSERT INTO entities
SELECT k, '\''chunk '\'' || k, CASE WHEN k = 40 THEN 999 ELSE 100 END,
       ('\''['\'' || k || '\'',0,0,0,0,0,0,0]'\'')::vector(8)
FROM generate_series(1, 500) AS k;
CREATE INDEX entities_hnsw ON entities USING hnsw (embedding vector_l2_ops)
    WITH (m = 16, ef_construction = 64);
DO \$\$
DECLARE g int; v bigint;
BEGIN
    FOR g IN 0..500 LOOP
        v := graph_store.gph_upsert_vertex(g);
        IF v <> g THEN RAISE EXCEPTION '\''dense vid drift: % != %'\'', v, g; END IF;
    END LOOP;
END \$\$;
SELECT graph_store.gph_set_identity_mode(true);
SELECT graph_store.add_edge(1, 10);
SELECT graph_store.add_edge(1, 20);
SELECT graph_store.add_edge(1, 30);
SELECT graph_store.add_edge(1, 40);
SELECT graph_store.add_edge(2, 50);
SELECT graph_store.add_edge(2, 60);
SELECT set_config('\''t.k'\'', graph_store.register_edge_type('\''knows_about'\'')::text, false);
SELECT graph_store.gph_insert_edge(1, 15, current_setting('\''t.k'\'')::int);
-- type-interleaved source: related, knows, related
SELECT graph_store.add_edge(3, 80);
SELECT graph_store.gph_insert_edge(3, 81, current_setting('\''t.k'\'')::int);
SELECT graph_store.add_edge(3, 82);
-- tombstone: 1->30 must stay invisible across the upgrade
SELECT graph_store.remove_edge(1, 30);
EOSQL

  # ---------------------------------------------------------------- probe (0.1.0-safe)
  # Same shape as graph_dump_restore_test.sh minus the 0.2.0-only gph_allocated_vids().
  cat > /tmp/probe.sql <<EOSQL
\pset tuples_only on
\pset format unaligned
SELECT '\''relational_rows='\''  || count(*) FROM entities;
SELECT '\''vector_top3='\'' || array_agg(id)::text FROM (
  SELECT id FROM entities ORDER BY embedding <-> '\''[19,0,0,0,0,0,0,0]'\'' LIMIT 3) s;
SELECT '\''visible_edges='\'' || graph_store.gph_visible_edge_count();
SELECT '\''vertex_count='\'' || graph_store.gph_vertex_count();
SELECT '\''typed_'\'' || s.v || '\''_'\'' || t.id || '\''='\'' ||
       coalesce((SELECT array_agg(tr.dst ORDER BY tr.ord)::text
                 FROM graph_store.gph_traverse_typed(s.v, t.id, 0, -1)
                      WITH ORDINALITY AS tr(esrc, dst, ord)), '\''{}'\'')
FROM (VALUES (1::bigint), (2::bigint), (3::bigint)) s(v)
CROSS JOIN graph_store.edge_type t
ORDER BY s.v, t.id;
SELECT '\''edge_types='\'' || string_agg(id || '\'':'\'' || name, '\'','\'' ORDER BY id) FROM graph_store.edge_type;
SELECT '\''vid_map='\'' || count(*) || '\''/'\'' || coalesce(sum(ext_id * 31 + vid), 0) FROM graph_store.gph_vid_map;
SELECT '\''identity_mode='\'' || identity_mode FROM graph_store.gph_am_meta;
SELECT '\''tjs_top2='\'' || array_agg(id)::text FROM (
  SELECT * FROM public.tjs_open('\''entities'\''::regclass, 2, 0, 0, 1, '\''id'\'',
      '\''ts IN (100)'\'', '\''[19,0,0,0,0,0,0,0]'\''::vector, 1, 1) AS t(id)) s;
SELECT '\''canonical='\'' || string_agg(c, '\''|'\'') FROM graph_store.graph_query(\$q\$
    SELECT chunk
    FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
      COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
    WHERE src.id = 1 AND timestamp IN (100)
    ORDER BY src_embedding <-> '\''[19,0,0,0,0,0,0,0]'\''
    LIMIT 2
\$q\$) AS c;
EOSQL

  $PSQL -d updb -f /tmp/probe.sql > /tmp/probe_pre.txt
  echo "=== pre-upgrade probe (0.1.0) ==="; cat /tmp/probe_pre.txt
  grep -q "^typed_1_1={10,20,40}$" /tmp/probe_pre.txt || { echo "FAIL: corpus sanity (typed_1_1)"; exit 1; }
  grep -q "^visible_edges=9$"      /tmp/probe_pre.txt || { echo "FAIL: corpus sanity (visible_edges)"; exit 1; }

  # ---------------------------------------------------------------- THE upgrade
  echo "=== ALTER EXTENSION ... UPDATE TO 0.2.0 ==="
  $PSQL -d updb -c "ALTER EXTENSION graph_store_am UPDATE TO '\''0.2.0'\''"
  $PSQL -d updb -c "ALTER EXTENSION tjs_pg UPDATE TO '\''0.2.0'\''"
  v1=$(V "SELECT extversion FROM pg_extension WHERE extname='\''graph_store_am'\''")
  v2=$(V "SELECT extversion FROM pg_extension WHERE extname='\''tjs_pg'\''")
  [ "$v1" = "0.2.0" ] && [ "$v2" = "0.2.0" ] || { echo "FAIL: post-upgrade graph_store_am=$v1 tjs_pg=$v2"; exit 1; }

  # ---------------------------------------------------------------- pre-existing data survives
  $PSQL -d updb -f /tmp/probe.sql > /tmp/probe_post.txt
  if ! diff -u /tmp/probe_pre.txt /tmp/probe_post.txt; then
    echo "FAIL: post-upgrade probe differs from pre-upgrade (upgrade damaged the data)"; exit 1
  fi
  echo "PASS: post-upgrade probe byte-identical on the pre-existing data"

  # ---------------------------------------------------------------- 0.2.0 surface on old data
  av=$(V "SELECT graph_store.gph_allocated_vids()")
  [ "$av" = "501" ] || { echo "FAIL: gph_allocated_vids()=$av (expected 501)"; exit 1; }
  de=$(V "SELECT count(*) FROM graph_store.gph_dump_edges()")
  ve=$(V "SELECT graph_store.gph_visible_edge_count()")
  [ "$de" = "$ve" ] || { echo "FAIL: gph_dump_edges() rows=$de != visible_edges=$ve"; exit 1; }
  nc=$(V "SELECT coalesce(array_length(extconfig,1),0) FROM pg_extension WHERE extname='\''graph_store_am'\''")
  [ "$nc" = "2" ] || { echo "FAIL: expected 2 pg_extension_config_dump marks post-upgrade, got $nc"; exit 1; }
  echo "PASS: 0.2.0 surface live on old data (allocated_vids=$av, dump_edges=$de, config marks=$nc)"

  # Plan-100 writer lock is live post-upgrade: a structural write holds the advisory lock
  # (classid = gstore OID, objid = 0) for its transaction; write path still works.
  wl=$(V "BEGIN;
          SELECT graph_store.gph_insert_edge(2, 70);
          SELECT '\''lock='\'' || count(*) FROM pg_locks
          WHERE locktype='\''advisory'\'' AND granted
            AND classid=(SELECT oid FROM pg_class WHERE relname='\''gstore'\'') AND objid=0;
          ROLLBACK;")
  echo "$wl" | grep -q "^lock=1$" || { echo "FAIL: writer advisory lock not held during post-upgrade write (got: $wl)"; exit 1; }
  ve2=$(V "SELECT graph_store.gph_visible_edge_count()")
  [ "$ve2" = "9" ] || { echo "FAIL: rolled-back post-upgrade write leaked an edge (visible=$ve2)"; exit 1; }
  V "SELECT graph_store.add_edge(2, 70)" >/dev/null
  t2=$(V "SELECT array_agg(dst ORDER BY ord)::text FROM graph_store.gph_traverse_typed(2, 1, 0, -1) WITH ORDINALITY AS tr(esrc, dst, ord)")
  [ "$t2" = "{50,60,70}" ] || { echo "FAIL: post-upgrade committed write wrong (typed_2_1=$t2)"; exit 1; }
  echo "PASS: plan-100 writer lock held during writes; post-upgrade write path intact"

  # A FRESH default install lands on 0.2.0 directly.
  $B/createdb -p 5499 defdb
  $PSQL -d defdb -q -c "CREATE EXTENSION vector" -c "CREATE EXTENSION graph_store_am"
  dv=$($B/psql -p 5499 -d defdb -tAc "SELECT extversion FROM pg_extension WHERE extname='\''graph_store_am'\''")
  [ "$dv" = "0.2.0" ] || { echo "FAIL: fresh default install is $dv (expected 0.2.0)"; exit 1; }
  echo "PASS: fresh CREATE EXTENSION defaults to 0.2.0"

  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
'
echo "[extension_upgrade_test] PASS"
