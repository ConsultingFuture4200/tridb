-- DEV-1166: helper queries for scripts/graph_concurrency_test.sh, selected by :q.
-- Kept in a mounted file so single-quoted SQL literals don't fight the harness's shell quoting.
-- All run with -tA (tuples-only, unaligned) so the harness can capture a bare scalar.

\if :{?q}
\else
\echo 'graph_concurrency_probe.sql requires -v q=<name>'
\quit
\endif

SELECT :'q' = 'count'        AS q_count,
       :'q' = 'holder_has'   AS q_holder_has,
       :'q' = 'waiter_has'   AS q_waiter_has,
       :'q' = 'neighbors8'   AS q_neighbors8,
       :'q' = 'count8'       AS q_count8 \gset

\if :q_count
SELECT graph_store.gph_vertex_count();
\endif

\if :q_holder_has
-- 1 when the advisory lock objid=42 is GRANTED to some session (the holder owns it).
SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 42 AND granted;
\endif

\if :q_waiter_has
-- 1 when a session is WAITING on advisory lock objid=42 (T1 is blocked in the queue).
SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 42 AND NOT granted;
\endif

\if :q_neighbors8
SELECT coalesce(string_agg(x::text, ',' ORDER BY x), '') FROM graph_store.gph_neighbors(8) x;
\endif

\if :q_count8
SELECT count(*) FROM graph_store.gph_neighbors(8);
\endif
