-- Relaxed-monotonicity vector iterator acceptance suite (DEV-1168 / FR-3).
--
-- Exercises the tridb_vec_open/next/close iterator (vendor/MSVBASE/src/tridb_vector_iter.cpp)
-- through the test-only SQL probe tridb_vec_probe(index, query, k, stop), which dumps the
-- iterator's (tid, distance, examined) stream. The probe surfaces hnswlib's INTERNAL per-candidate
-- distance (QueryResult::GetDistance) — the only real distance available, since the fork's scalar
-- `<->` returns 0 outside an index scan (docs/fork_findings.md). No SQL re-rank anywhere here.
--
-- Asserts the three FR-3 acceptance properties:
--   (1) APPROX-NON-DECREASING: the emitted distance stream is non-decreasing up to a BOUNDED number
--       of inversions (relaxed monotonicity is NOT strict ascending).
--   (2) EARLY TERMINATION: for k=5 the iterator examines < 25% of the corpus.
--   (3) TOP-K PARITY: the stopping scan's top-k TID set matches a full-drain (no-stop) oracle's
--       top-k by >= 99%.
--
-- Seed pattern mirrors test/trimodal_early_term.sql: float8[8] embeddings, dim0 dominant so
-- nearest-to-q is well-defined, HNSW (l2_distance) index.

CREATE EXTENSION vectordb;

CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, embedding float8[8]);

-- 2000 entities. dim0 = i (dominant); other dims tiny — so "near q=[1000,...]" ~ id near 1000.
INSERT INTO entities
SELECT i, 'e ' || i,
       ARRAY[i, i%10, i%10, i%10, i%10, i%10, i%10, i%10]::float8[]
FROM generate_series(1, 2000) AS i;

CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

-- NB: the query vector is inlined as a literal in each DO block below — psql `\set`/`:'q'`
-- interpolation does NOT expand inside dollar-quoted ($$) bodies.

-- 1) APPROX-NON-DECREASING (bounded inversions) --------------------------------
-- Pull the FULL raw ANN stream IN ORDER (stop=false drains it) and count strict inversions (a row
-- whose distance is LESS than the previous row's). Relaxed monotonicity is NOT strict ascending:
-- the leading top-k candidates are discovered out of order, so SOME inversions are expected. The
-- property is that the stream is *approximately* non-decreasing — inversions are a small fraction
-- of the stream, not chaos. (The short stopping prefix is too small to measure a trend; this runs
-- on the long oracle stream where the relaxed-monotone shape is observable.)
DO $$
DECLARE
    rec record;
    prev float8 := -1;
    inversions int := 0;
    n int := 0;
BEGIN
    FOR rec IN
        SELECT distance FROM tridb_vec_probe('entities_hnsw'::regclass, '{1000,0,0,0,0,0,0,0}'::float8[], 5, false)
    LOOP
        n := n + 1;
        IF prev >= 0 AND rec.distance < prev - 1e-6 THEN
            inversions := inversions + 1;
        END IF;
        prev := rec.distance;
    END LOOP;

    IF n = 0 THEN
        RAISE EXCEPTION 'approx-non-decreasing FAILED: iterator emitted 0 candidates';
    END IF;
    -- Bounded: relaxed-mono allows inversions, but they must be a minority of the stream.
    IF inversions > n / 4 THEN
        RAISE EXCEPTION 'approx-non-decreasing FAILED: % inversions over % candidates (not relaxed-monotone)', inversions, n;
    END IF;
    RAISE NOTICE 'PASS approx-non-decreasing: % inversions over % emitted candidates (bounded, < 25%%)', inversions, n;
END $$;

-- 2) EARLY TERMINATION: examined < 25%% of the 2000-corpus for k=5 -------------
DO $$
DECLARE examined int; corpus int := 2000;
BEGIN
    -- The probe echoes the iterator's examined-counter on every row; one row suffices.
    SELECT p.examined INTO examined
    FROM tridb_vec_probe('entities_hnsw'::regclass, '{1000,0,0,0,0,0,0,0}'::float8[], 5, true) AS p
    LIMIT 1;

    IF examined IS NULL THEN
        RAISE EXCEPTION 'early-termination FAILED: no candidates examined';
    END IF;
    IF examined >= corpus / 4 THEN
        RAISE EXCEPTION 'early-termination FAILED: examined % of % (>= 25%%)', examined, corpus;
    END IF;
    RAISE NOTICE 'PASS early termination: iterator examined % of % candidates (< 25%%) for k=5', examined, corpus;
END $$;

-- 3) TOP-K PARITY vs no-stop oracle (>= 99%%) ----------------------------------
-- Oracle = full drain (stop=false): the iterator's tolerance is raised so the relaxed-mono stop
-- never fires, draining the whole ANN stream. The stopping scan's top-k TID set must match the
-- oracle's top-k TID set by >= 99%. Compare TOP-K by distance (the iterator emits in stream order,
-- so rank each leg by distance, take 5, intersect).
DO $$
DECLARE
    overlap int;
    k int := 5;
    parity float8;
BEGIN
    CREATE TEMP TABLE stop_topk AS
        SELECT tid FROM tridb_vec_probe('entities_hnsw'::regclass, '{1000,0,0,0,0,0,0,0}'::float8[], k, true)
        ORDER BY distance ASC LIMIT k;

    CREATE TEMP TABLE oracle_topk AS
        SELECT tid FROM tridb_vec_probe('entities_hnsw'::regclass, '{1000,0,0,0,0,0,0,0}'::float8[], k, false)
        ORDER BY distance ASC LIMIT k;

    SELECT count(*) INTO overlap
    FROM stop_topk s JOIN oracle_topk o ON s.tid = o.tid;

    parity := overlap::float8 / k::float8;
    IF parity < 0.99 THEN
        RAISE EXCEPTION 'top-k parity FAILED: % of % top-k TIDs match oracle (parity %, < 0.99)',
            overlap, k, parity;
    END IF;
    RAISE NOTICE 'PASS top-k parity: % of % top-k TIDs match the no-stop oracle (>= 99%%)', overlap, k;

    DROP TABLE stop_topk; DROP TABLE oracle_topk;
END $$;

\echo '============ relaxed-monotonicity vector iterator (DEV-1168 / FR-3): VERIFIED ============'
