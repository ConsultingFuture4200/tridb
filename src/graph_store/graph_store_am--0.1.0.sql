/* graph_store_am 0.1.0 — TriDB native adjacency-list graph store (DEV-1164) */
-- complain if script is sourced in psql, rather than via CREATE EXTENSION
\echo Use "CREATE EXTENSION graph_store_am" to load this file. \quit

/*
 * The container relation. Its 32KB blocks hold the native graph pages (metapage, vertex pages,
 * adjacency pages) managed by the C code through the shared buffer manager + WAL. autovacuum is
 * disabled and it is NEVER accessed as a heap — all access goes through the gph_* functions.
 */
CREATE TABLE gstore (dummy "char") WITH (autovacuum_enabled = false);
COMMENT ON TABLE gstore IS
  'TriDB graph store page container (DEV-1164): 32KB blocks hold native graph pages. Do NOT access as a heap; use the gph_* functions.';

CREATE FUNCTION gph_insert_vertex() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;

CREATE FUNCTION gph_insert_edge(bigint, bigint) RETURNS void
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

CREATE FUNCTION gph_neighbors(bigint) RETURNS SETOF bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

-- Edge-emitting traversal (DEV-1165): one :related_to edge per Next(), so callers can surface the
-- edge endpoints and join dst back to its relational/vector payload (the canonical query's COLUMNS
-- projection). Use in a target-list / ProjectSet position (SELECT gph_traverse(x)), NOT a
-- FROM-clause FunctionScan, or early termination under LIMIT is lost. v1 edge slots carry no
-- stored edge id, so only (src, dst) are surfaced.
CREATE FUNCTION gph_traverse(bigint, OUT src bigint, OUT dst bigint) RETURNS SETOF record
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

CREATE FUNCTION gph_visits() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;

CREATE FUNCTION gph_vertex_count() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;

-- Store-wide directed-edge count (plan 006): the metapage gm_edge_count counter, the
-- avg_out_degree source for the FR-6 join-order heuristic. Raw (non-MVCC) counter — v1 has no
-- edge-delete path so it only grows; maintained under GenericXLog so aborts/crashes roll it back
-- with the page image. Used by the crash-recovery edge-count assertion.
CREATE FUNCTION gph_edge_count() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;
