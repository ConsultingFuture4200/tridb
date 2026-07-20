/* graph_store_am 0.1.0 -> 0.2.0 upgrade (advisor plan 100).
 *
 * Carries a genuine 0.1.0 install forward to the 0.2.0 surface via
 *   ALTER EXTENSION graph_store_am UPDATE TO '0.2.0';
 * The new shared library must already be installed (make install) before the UPDATE:
 * the SRFs below bind MODULE_PATHNAME symbols (gph_allocated_vids) that only exist in
 * the 0.2.0 .so, and the plan-100 single-writer advisory lock lives entirely in C
 * (no SQL object — writers acquire it on every structural-write entry point).
 *
 * DDL derivation (plan 100, with the advisor's base-commit correction): the practical
 * "0.1.0" release boundary is the last PUSHED master, which is 997b679 — NOT the a780b46
 * SHA the plan text pinned (stale; the 093-096 merges were pushed). This script is
 * `git diff 997b679..HEAD` over the extension SQL: the plan-099 logical backup/restore
 * surface (pg_extension_config_dump marks + the dump SRFs) and the plan-100 contract
 * comment. The 0.1.0 fixtures the upgrade gate installs (test/fixtures/upgrade/) are
 * vendored verbatim from 997b679.
 */
\echo Use "ALTER EXTENSION graph_store_am UPDATE TO '0.2.0'" to load this file. \quit

-- ----------------------------------------------------------------------------
-- Plan 100: the single-writer contract is now ENFORCED in C (graph_am.c): every
-- structural write (gph_insert_vertex/edge/edges, gph_tombstone_*, gph_freeze)
-- takes a transaction-scoped advisory lock keyed on gstore's relation OID.
-- Concurrent writers BLOCK until the holder's transaction ends; readers are
-- unaffected. Restate the contract on the container relation's comment.
-- ----------------------------------------------------------------------------
COMMENT ON TABLE gstore IS
  'TriDB graph store page container (DEV-1164): 32KB blocks hold native graph pages. Do NOT access as a heap; use the gph_* functions. '
  'Structural writes serialize per graph via a transaction-scoped advisory lock keyed on this relation''s OID (advisor plan 100): '
  'concurrent writers BLOCK until the holder''s transaction ends; readers are unaffected (MVCC-consistent).';

-- ----------------------------------------------------------------------------
-- Plan 099: logical-backup contract. Extension member tables are SKIPPED by
-- pg_dump unless marked; unmarked, a dump/restore cycle silently reset the
-- dictionary to the seed row and dropped the whole ext->vid mapping. The
-- edge_type filter excludes id 1 because CREATE EXTENSION re-seeds it on
-- restore (PK conflict otherwise). gph_am_meta is DELIBERATELY left unmarked
-- (see its comment in the base script).
-- ----------------------------------------------------------------------------
SELECT pg_catalog.pg_extension_config_dump('graph_store.edge_type', 'WHERE id <> 1');
SELECT pg_catalog.pg_extension_config_dump('graph_store.gph_vid_map', '');

-- ----------------------------------------------------------------------------
-- Plan 099: the logical dump surface (full commentary in the base 0.2.0 script
-- and docs/INSTALL_stock_pg.md "Backup and restore").
-- ----------------------------------------------------------------------------

-- gph_allocated_vids(): the allocated-vid horizon (metapage gm_next_vid) — every vid ever
-- assigned is in [0, horizon). NOT the visible count (aborted inserts leave holes a
-- vid-preserving restore must re-materialize).
CREATE FUNCTION gph_allocated_vids() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;

-- gph_dump_vertices() — the vertex replay set: ALL allocated vids, in vid order
-- (allocation-preserving; see the base script for the placeholder-vid caveat).
CREATE FUNCTION gph_dump_vertices() RETURNS SETOF bigint
LANGUAGE sql VOLATILE
AS $$
    SELECT g FROM generate_series(0, graph_store.gph_allocated_vids() - 1) g
$$;

-- gph_dump_edges() — every MVCC-visible, non-tombstoned edge as (src, dst, type_id),
-- streamed over the existing typed read path in (src vid, type_id, adjacency slot) order.
CREATE FUNCTION gph_dump_edges()
RETURNS TABLE (src bigint, dst bigint, type_id int)
LANGUAGE sql VOLATILE
AS $$
    SELECT v.vid, tr.dst, t.id
    FROM graph_store.gph_dump_vertices() WITH ORDINALITY AS v(vid, vord)
    CROSS JOIN graph_store.edge_type t
    CROSS JOIN LATERAL graph_store.gph_traverse_typed(v.vid, t.id, 0, -1)
         WITH ORDINALITY AS tr(esrc, dst, ord)
    ORDER BY v.vord, t.id, tr.ord
$$;

COMMENT ON FUNCTION graph_store.gph_dump_vertices() IS
    'TriDB logical dump (plan 099): the vertex replay set — all allocated vids in vid order '
    '(allocation-preserving, includes tombstoned/aborted placeholder vids). Restore: call '
    'graph_store.gph_insert_vertex() once per row, in order, BEFORE any edge replay. '
    'See docs/INSTALL_stock_pg.md.';
COMMENT ON FUNCTION graph_store.gph_dump_edges() IS
    'TriDB logical dump (plan 099): every visible, non-tombstoned edge as (src, dst, type_id) '
    'in (src, type_id, adjacency) order. Restore: group by (src, type_id) preserving row order '
    'and replay via graph_store.gph_insert_edges(src, dst_array, type_id). '
    'See docs/INSTALL_stock_pg.md.';
