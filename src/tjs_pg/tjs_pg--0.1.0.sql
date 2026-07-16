-- tridb_tjs 0.1.0 — the fused tri-modal operator on stock PostgreSQL (ADR-0019).
\echo Use "CREATE EXTENSION tjs_pg" to load this file. \quit

-- pgvector version floor. The control file's `requires = 'vector'` guarantees the
-- extension is present, but not that it is >= 0.8 — the vector-first path REQUIRES
-- SET hnsw.iterative_scan = relaxed_order, which pgvector only exposes from 0.8.
DO $$
DECLARE v text;
BEGIN
  SELECT extversion INTO v FROM pg_extension WHERE extname = 'vector';
  IF v IS NULL THEN
    RAISE EXCEPTION 'tjs_pg requires the pgvector "vector" extension (CREATE EXTENSION vector first)';
  END IF;
  IF string_to_array(v, '.')::int[] < ARRAY[0,8]::int[] THEN
    RAISE EXCEPTION 'tjs_pg requires pgvector >= 0.8 (found %); the vector-first path needs hnsw.iterative_scan = relaxed_order', v;
  END IF;
END $$;

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
-- Vector-first: heap candidates the last call actually consumed. Filter-first:
-- qualifying rows examined by the fused statement BEFORE the top-k LIMIT (plan 074 —
-- a count capped at k carries no information about the work done).
CREATE FUNCTION tjs_open_candidates_examined() RETURNS bigint
AS 'MODULE_PATHNAME', 'tjs_open_candidates_examined_pg'
LANGUAGE C VOLATILE;

-- How the last call ended (plan 074):
--   'filter_first'       — fused single-statement path; no candidate stream at all.
--   'term_cond'          — TR-1 consecutive-drops early termination fired mid-stream.
--   'stream_end_unknown' — the pgvector stream ended before term_cond fired; pgvector
--                          does NOT disclose whether hnsw.max_scan_tuples or natural
--                          index exhaustion ended it. Right-censored: treat as possibly
--                          budget-shaped (ADR-0015 E3.3, ADR-0019 addendum 2026-07-16).
CREATE FUNCTION tjs_open_termination_reason() RETURNS text
AS 'MODULE_PATHNAME', 'tjs_open_termination_reason_pg'
LANGUAGE C VOLATILE;

-- Compatibility shim over tjs_open_termination_reason(): FALSE for known non-budget
-- endings ('filter_first', 'term_cond'); SQL NULL for 'stream_end_unknown' (the ending
-- is unobservable — budget or exhaustion). NEVER TRUE today: pgvector exposes no budget
-- signal, and this function refuses to manufacture one. Harnesses must treat NULL as
-- possibly-capped and refuse budget-shaped headlines (ADR-0015 E3.3).
CREATE FUNCTION tjs_open_budget_capped() RETURNS boolean
AS 'MODULE_PATHNAME', 'tjs_open_budget_capped_pg'
LANGUAGE C VOLATILE;

REVOKE EXECUTE ON FUNCTION tjs_open(regclass,integer,integer,integer,integer,text,text,vector,bigint,integer) FROM PUBLIC;

-- Bridges OFFERED to the guaranteed budget by the last vector-first call: every
-- filter-passing reach member exactly once (in-stream offers + phase-3b direct fetches),
-- counted whether or not it survives the bounded bridge heap — the fork's
-- tjs_open_bridges_injected() counts each materialized bridge row the same way (plan 087).
-- NOT a count of bridges that land in the FINAL k (that share is capped at k/2).
CREATE FUNCTION tjs_open_bridges_injected() RETURNS bigint
AS 'MODULE_PATHNAME', 'tjs_open_bridges_injected_pg'
LANGUAGE C VOLATILE;
