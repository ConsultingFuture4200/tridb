-- tridb_tjs 0.1.0 — the fused tri-modal operator on stock PostgreSQL (ADR-0019).
\echo Use "CREATE EXTENSION tjs_pg" to load this file. \quit

-- tjs_open: fused tri-modal top-k.
--   src IS NOT NULL -> FILTER-FIRST (typed BFS reach -> relational filter -> exact rank);
--   src IS NULL     -> VECTOR-FIRST/SEEDLESS (owned relaxed-order pgvector HNSW stream ->
--                      per-candidate filter/graph predicates -> term_cond early termination).
-- Vector-first requires: SET hnsw.iterative_scan = relaxed_order (pgvector >= 0.8).
CREATE FUNCTION tjs_open(tbl regclass,
                         k integer,
                         term_cond integer,
                         m_seeds integer,
                         hops integer,
                         id_col text,
                         filter text,
                         query vector,
                         src bigint DEFAULT NULL,
                         edge_type integer DEFAULT 0)
RETURNS SETOF bigint
AS 'MODULE_PATHNAME', 'tjs_open_pg'
LANGUAGE C VOLATILE;

-- Per-backend honesty counters (mirror the fork's SM-3 probes).
CREATE FUNCTION tjs_open_candidates_examined() RETURNS bigint
AS 'MODULE_PATHNAME', 'tjs_open_candidates_examined_pg'
LANGUAGE C VOLATILE;

-- TRUE iff the last vector-first call's candidate stream ended (pgvector budget
-- hnsw.max_scan_tuples, or index exhaustion) before term_cond fired — the harness must
-- refuse budget-shaped headlines (ADR-0015 E3.3).
CREATE FUNCTION tjs_open_budget_capped() RETURNS boolean
AS 'MODULE_PATHNAME', 'tjs_open_budget_capped_pg'
LANGUAGE C VOLATILE;

REVOKE EXECUTE ON FUNCTION tjs_open(regclass,integer,integer,integer,integer,text,text,vector,bigint,integer) FROM PUBLIC;
