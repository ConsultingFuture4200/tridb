-- Phase-0 smoke test (DEV-1162): relational + vector legs end-to-end on the MSVBASE fork.
-- Proves two of TriDB's three legs work before the native graph store exists.
-- Run via scripts/smoke_test.sh (drives the tridb/msvbase:dev image).

create extension vectordb;

create table t_table(id int, price int, vector_1 float8[10]);
insert into t_table
  select g, g * 2,
    ARRAY[(g % 11) * 1.0, (g % 7) * 1.0, (g % 3) * 1.0, (g % 5) * 1.0, (g % 2) * 1.0, 1, 1, 0, 4, 3]
  from generate_series(1, 100000) g;

create index t4_index on t_table using hnsw(vector_1) with (dimension = 10, distmethod = l2_distance);

\echo '--- TopK + relational filter (the relational+vector portion of the canonical query) ---'
select id, price from t_table
  where price > 15
  order by vector_1 <-> '{5,9,8,6,2,1,1,0,4,3}'
  limit 5;

\echo '--- Confirm the HNSW relaxed-monotonicity Index Scan path (TR-1 early termination) ---'
set enable_seqscan = off;
explain select id from t_table
  order by vector_1 <-> '{5,9,8,6,2,1,1,0,4,3}'
  limit 5;
-- Expected: "Index Scan using t4_index ... Order By: (vector_1 <-> ...)" — the ANN leg.
