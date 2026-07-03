-- Plan 028 Step 3: pgvector 0.8.x iterative-scan equivalence probe (SPIKE, NOT SHIPPED)
--
-- Question: does `SET hnsw.iterative_scan = relaxed_order` give the operator model tjs needs
-- (resumable ordered candidate stream surviving a selective post-filter), and at what scan cost?
--
-- Corpus: 20k rows x vector(64), uniform random; predicate `category = 0` selects exactly 1%
-- (200 rows). k = 10, 20 query vectors. Ground truth computed by exact scan BEFORE the index
-- exists. Reported per config: avg recall@10, avg tuples pulled from the index per query
-- (Index Scan "Actual Rows" + "Rows Removed by Filter" from EXPLAIN ANALYZE), and how many of
-- the 20 queries came back starved (< 10 rows).
\set ON_ERROR_STOP on
SET client_min_messages = warning;

DROP TABLE IF EXISTS spike_items, spike_queries, spike_truth, spike_results CASCADE;
DROP FUNCTION IF EXISTS spike_rvec(int), spike_probe(text, text, bigint);

-- volatile random-vector generator (per-row evaluation guaranteed)
CREATE FUNCTION spike_rvec(dim int) RETURNS vector AS $$
DECLARE a float4[];
BEGIN
  SELECT array_agg(random()::float4) INTO a FROM generate_series(1, dim);
  RETURN a::vector;
END $$ LANGUAGE plpgsql VOLATILE;

SELECT setseed(0.028);
CREATE TABLE spike_items AS
SELECT i AS id, (i % 100) AS category, spike_rvec(64) AS embedding
FROM generate_series(1, 20000) i;

ALTER TABLE spike_items ALTER COLUMN embedding TYPE vector(64);  -- CTAS loses typmod; HNSW needs declared dims

CREATE TABLE spike_queries AS
SELECT qid, spike_rvec(64) AS q FROM generate_series(1, 20) qid;

-- exact ground truth (no vector index exists yet => flat scan, exact by construction)
CREATE TABLE spike_truth AS
SELECT s.qid, ARRAY(SELECT id FROM spike_items
                    WHERE category = 0
                    ORDER BY embedding <-> s.q LIMIT 10) AS ids
FROM spike_queries s;

CREATE INDEX ON spike_items USING hnsw (embedding vector_l2_ops) WITH (m = 16, ef_construction = 64);
ANALYZE spike_items;

-- one probe run: recall@10 + index tuples scanned per query under a given iterative-scan config
CREATE FUNCTION spike_probe(cfg_label text, mode text, max_tuples bigint)
RETURNS TABLE(config text, avg_recall_at_10 numeric, avg_index_tuples_scanned numeric,
              starved_queries_of_20 bigint) AS $$
DECLARE
  r record; jtxt text; node jsonb; ids bigint[]; hit int; scanned numeric;
  recalls numeric := 0; scans numeric := 0; starved bigint := 0; n int := 0;
BEGIN
  EXECUTE format('SET hnsw.iterative_scan = %s', mode);
  IF max_tuples IS NOT NULL THEN
    EXECUTE format('SET hnsw.max_scan_tuples = %s', max_tuples);
  END IF;
  SET enable_seqscan = off;
  FOR r IN SELECT s.qid, s.q, t.ids AS truth FROM spike_queries s JOIN spike_truth t USING (qid) LOOP
    -- pass 1: the actual result for recall
    ids := ARRAY(SELECT id FROM spike_items WHERE category = 0
                 ORDER BY embedding <-> r.q LIMIT 10);
    -- pass 2: EXPLAIN ANALYZE for index-tuple accounting
    EXECUTE 'EXPLAIN (ANALYZE, FORMAT JSON) SELECT id FROM spike_items WHERE category = 0
             ORDER BY embedding <-> $1 LIMIT 10' INTO jtxt USING r.q;
    SELECT j INTO node FROM jsonb_path_query(jtxt::jsonb,
        '$.** ? (@."Node Type" == "Index Scan")') AS j LIMIT 1;
    IF node IS NULL THEN
      RAISE EXCEPTION 'no Index Scan in plan for config %', cfg_label;
    END IF;
    scanned := COALESCE((node->>'Actual Rows')::numeric, 0)
             + COALESCE((node->>'Rows Removed by Filter')::numeric, 0);
    hit := (SELECT count(*) FROM unnest(ids) a JOIN unnest(r.truth) t ON a = t);
    recalls := recalls + hit / 10.0;
    scans := scans + scanned;
    IF coalesce(array_length(ids, 1), 0) < 10 THEN starved := starved + 1; END IF;
    n := n + 1;
  END LOOP;
  RETURN QUERY SELECT cfg_label, round(recalls / n, 3), round(scans / n, 1), starved;
END $$ LANGUAGE plpgsql;

\echo === corpus sanity ===
SELECT count(*) AS rows, count(*) FILTER (WHERE category = 0) AS predicate_rows FROM spike_items;

\echo === probe: iterative_scan configs vs exact truth (k=10, 1% predicate, 20 queries) ===
SELECT * FROM spike_probe('off (ef_search=40 default, starvation baseline)', 'off', NULL)
UNION ALL SELECT * FROM spike_probe('relaxed_order, max_scan_tuples=1000',  'relaxed_order', 1000)
UNION ALL SELECT * FROM spike_probe('relaxed_order, max_scan_tuples=5000',  'relaxed_order', 5000)
UNION ALL SELECT * FROM spike_probe('relaxed_order, max_scan_tuples=20000 (default, = full table)', 'relaxed_order', 20000)
UNION ALL SELECT * FROM spike_probe('strict_order, max_scan_tuples=20000 (ordering comparison)', 'strict_order', 20000);
