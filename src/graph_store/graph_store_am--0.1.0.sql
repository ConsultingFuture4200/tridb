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

CREATE FUNCTION gph_visits() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;

CREATE FUNCTION gph_vertex_count() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;
