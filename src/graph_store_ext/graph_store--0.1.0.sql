-- graph_store extension v0 — native adjacency-list graph store.
-- complain loudly if loaded outside CREATE EXTENSION
\echo Use "CREATE EXTENSION graph_store" to load this file. \quit

CREATE SCHEMA graph_store;

-- Adjacency list: one row per vertex, out-neighbors co-located in nbrs[].
-- This is adjacency-list storage, NOT an edge join table.
CREATE TABLE graph_store.adjacency (
    vid  bigint PRIMARY KEY,
    nbrs bigint[] NOT NULL DEFAULT '{}'
);

-- add_edge(src, dst): append dst to src's adjacency list (upsert).
CREATE FUNCTION graph_store.add_edge(src bigint, dst bigint)
RETURNS void
LANGUAGE sql
AS $$
    INSERT INTO graph_store.adjacency (vid, nbrs)
    VALUES (src, ARRAY[dst])
    ON CONFLICT (vid)
    DO UPDATE SET nbrs = graph_store.adjacency.nbrs || EXCLUDED.nbrs;
$$;

-- neighbors(src): Open/Next/Close traversal iterator over src's out-neighbors.
CREATE FUNCTION graph_store.neighbors(src bigint)
RETURNS SETOF bigint
AS 'MODULE_PATHNAME', 'graph_neighbors'
LANGUAGE C STRICT;

-- visits(): session traversal-step counter — proves TR-1 early termination.
CREATE FUNCTION graph_store.visits()
RETURNS bigint
AS 'MODULE_PATHNAME', 'graph_visits'
LANGUAGE C;
