#!/usr/bin/env bash
#
# graph_dump_restore_test.sh — logical backup/restore round-trip gate on STOCK PostgreSQL 16/17
# (advisor plan 099). Proves the documented logical procedure (docs/INSTALL_stock_pg.md):
#
#   dump:    pg_dump -Fc  (config tables ride via pg_extension_config_dump marks)
#          + COPY (SELECT * FROM gph_dump_vertices()) TO vertices.copy
#          + COPY (SELECT * FROM gph_dump_edges())    TO edges.copy
#   restore: pg_restore into a FRESH database, then replay: vertices in vid order via
#            gph_insert_vertex(), edges grouped by (src, type_id) via the typed batched
#            gph_insert_edges (plan 091), then gph_set_identity_mode(true) (guard re-verifies).
#
# The assertion is BYTE-EQUALITY of a deterministic tri-modal probe file (per-type
# gph_traverse_typed ordered outputs, sorted any-type edge set, visible counts, id map,
# edge_type dictionary, vector top-k, and a tjs_open query) run on source and restored DB
# and compared with diff. NEGATIVE CONTROL: one edge in edges.copy is corrupted and restored
# into a third database — the same diff MUST fail, proving the gate has teeth.
#
# Why the audit that motivated this (plan 099 Step 1): unmarked extension member tables are
# skipped SILENTLY by pg_dump, so a dump/restore cycle used to lose the ENTIRE graph
# (topology + id map + type dictionary) while relational + vector data survived — a restored
# database that looks healthy. Container conventions follow scripts/pg17_graph_test.sh.
#
# Usage: scripts/graph_dump_restore_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/pg17-unfork:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_V1="$ROOT/src/graph_store"
EXT_TJS="$ROOT/src/tjs_pg"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run: docker build -t tridb/pg17-unfork:dev scripts/pg17/" >&2
  exit 1
}

docker run --rm --user postgres --entrypoint bash \
  -v "${EXT_V1}:/tmp/ext_v1:ro" -v "${EXT_TJS}:/tmp/ext_tjs:ro" "$IMAGE" -c '
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

  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null
  $B/pg_ctl -D $D -o "-p 5499 -c listen_addresses=" -w start >/dev/null
  PSQL="$B/psql -p 5499 -v ON_ERROR_STOP=1"

  # ---------------------------------------------------------------- source corpus
  # Mirrors test/canonical_stock_e2e_test.sql (entity k -> embedding [k,0,...]; entity 40
  # stale) plus the plan-099 wrinkles the gate must cover: a second related_to source, typed
  # knows_about edges, a TYPE-INTERLEAVED source (vid 3: related, knows, related — proves the
  # per-type order contract and documents the any-type regrouping), one tombstoned edge
  # (1->30, the visible-edge filter), and identity_mode ON.
  $B/createdb -p 5499 srcdb
  $PSQL -d srcdb -q <<EOSQL
CREATE EXTENSION vector;
CREATE EXTENSION graph_store_am;
CREATE EXTENSION tjs_pg;
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
-- tombstone: 1->30 must NOT survive the logical dump
SELECT graph_store.remove_edge(1, 30);
EOSQL

  # ---------------------------------------------------------------- probe file (shared)
  # Deterministic text on ANY conforming database; byte-compared with diff below. Typed
  # traversals assert ORDER; the any-type probe asserts the SET (sorted) — the documented
  # regrouping caveat for type-interleaved sources (see gph_dump_edges comment).
  cat > /tmp/probe.sql <<EOSQL
\pset tuples_only on
\pset format unaligned
SELECT '\''relational_rows='\''  || count(*) FROM entities;
SELECT '\''vector_top3='\'' || array_agg(id)::text FROM (
  SELECT id FROM entities ORDER BY embedding <-> '\''[19,0,0,0,0,0,0,0]'\'' LIMIT 3) s;
SELECT '\''visible_edges='\'' || graph_store.gph_visible_edge_count();
SELECT '\''vertex_count='\'' || graph_store.gph_vertex_count();
SELECT '\''allocated_vids='\'' || graph_store.gph_allocated_vids();
SELECT '\''typed_'\'' || s.v || '\''_'\'' || t.id || '\''='\'' ||
       coalesce((SELECT array_agg(tr.dst ORDER BY tr.ord)::text
                 FROM graph_store.gph_traverse_typed(s.v, t.id, 0, -1)
                      WITH ORDINALITY AS tr(esrc, dst, ord)), '\''{}'\'')
FROM (VALUES (1::bigint), (2::bigint), (3::bigint)) s(v)
CROSS JOIN graph_store.edge_type t
ORDER BY s.v, t.id;
SELECT '\''anytype_'\'' || s.v || '\''='\'' ||
       coalesce((SELECT array_agg(dst ORDER BY dst)::text
                 FROM graph_store.gph_traverse_typed(s.v, 0, 0, -1) AS tr(esrc, dst)), '\''{}'\'')
FROM (VALUES (1::bigint), (2::bigint), (3::bigint)) s(v)
ORDER BY s.v;
SELECT '\''edge_types='\'' || string_agg(id || '\'':'\'' || name, '\'','\'' ORDER BY id) FROM graph_store.edge_type;
SELECT '\''vid_map='\'' || count(*) || '\''/'\'' || coalesce(sum(ext_id * 31 + vid), 0) FROM graph_store.gph_vid_map;
SELECT '\''identity_mode='\'' || identity_mode FROM graph_store.gph_am_meta;
SELECT '\''tjs_top2='\'' || array_agg(id)::text FROM (
  SELECT * FROM public.tjs_open('\''entities'\''::regclass, 2, 0, 0, 1, '\''id'\'',
      '\''ts IN (100)'\'', '\''[19,0,0,0,0,0,0,0]'\''::vector, 1, 1) AS t(id)) s;
EOSQL

  $PSQL -d srcdb -f /tmp/probe.sql > /tmp/probe_src.txt
  echo "=== source probe ==="; cat /tmp/probe_src.txt
  # sanity: the source must be non-trivial or the diff below could pass vacuously
  grep -q "^typed_1_1={10,20,40}$" /tmp/probe_src.txt || { echo "FAIL: source corpus sanity (typed_1_1)"; exit 1; }
  grep -q "^visible_edges=9$"      /tmp/probe_src.txt || { echo "FAIL: source corpus sanity (visible_edges)"; exit 1; }

  # ---------------------------------------------------------------- logical dump
  $B/pg_dump -p 5499 -Fc -f /tmp/dump.fc srcdb
  $PSQL -d srcdb -c "COPY (SELECT * FROM graph_store.gph_dump_vertices()) TO '\''/tmp/vertices.copy'\''"
  $PSQL -d srcdb -c "COPY (SELECT * FROM graph_store.gph_dump_edges())    TO '\''/tmp/edges.copy'\''"
  NVIDS=$(wc -l < /tmp/vertices.copy)
  echo "dumped: $NVIDS vids, $(wc -l < /tmp/edges.copy) edges"

  # restore_into <db> <edges-file>: the documented procedure, verbatim
  restore_into () {
    local db=$1 edges=$2
    $B/createdb -p 5499 $db
    $B/pg_restore -p 5499 -d $db /tmp/dump.fc
    $PSQL -d $db -q <<EOSQL
-- 1. re-materialize the FULL allocated vid range, in order (before ANY edge)
SELECT count(graph_store.gph_insert_vertex()) FROM generate_series(1, $NVIDS);
-- 2. replay edges grouped by (src, type_id), array order = dump order (typed batch, plan 091)
CREATE TEMP TABLE gph_edge_staging (src bigint, dst bigint, type_id int, ord bigserial);
COPY gph_edge_staging (src, dst, type_id) FROM '\''$edges'\'';
SELECT count(graph_store.gph_insert_edges(src, dsts, type_id)) FROM (
    SELECT src, type_id, array_agg(dst ORDER BY ord) AS dsts
    FROM gph_edge_staging GROUP BY src, type_id ORDER BY src, type_id
) g;
-- 3. identity fast-path: source had it ON; the DEV-1352 guard re-verifies the restored map
SELECT graph_store.gph_set_identity_mode(true);
EOSQL
  }

  # ---------------------------------------------------------------- round trip (must MATCH)
  echo "=== clean restore -> dstdb ==="
  restore_into dstdb /tmp/edges.copy
  $PSQL -d dstdb -f /tmp/probe.sql > /tmp/probe_dst.txt
  if ! diff -u /tmp/probe_src.txt /tmp/probe_dst.txt; then
    echo "FAIL: restored probe differs from source (round trip broken)"; exit 1
  fi
  echo "PASS: clean restore is byte-identical on all probes"

  # ---------------------------------------------------------------- negative control (must DIFFER)
  # corrupt ONE edge in the dump: first edge line 1->10 becomes 1->11
  sed "1s/^1\t10\t1\$/1\t11\t1/" /tmp/edges.copy > /tmp/edges_bad.copy
  cmp -s /tmp/edges.copy /tmp/edges_bad.copy && { echo "FAIL: corruption sed did not change the dump"; exit 1; }
  echo "=== corrupt restore -> dstbad ==="
  restore_into dstbad /tmp/edges_bad.copy
  $PSQL -d dstbad -f /tmp/probe.sql > /tmp/probe_bad.txt
  if diff -q /tmp/probe_src.txt /tmp/probe_bad.txt >/dev/null; then
    echo "FAIL: corrupted dump restored byte-identical — the gate has no teeth"; exit 1
  fi
  echo "PASS: negative control — corrupted dump detected by the probe diff"

  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
'
echo "[graph_dump_restore_test] PASS"
