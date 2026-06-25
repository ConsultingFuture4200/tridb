-- Regression test for fork finding #2 (docs/fork_findings.md), now FIXED by
-- scripts/patches/l2_distance_scalar.patch (plan 005). MSVBASE's scalar
-- l2_distance returned a constant 0 for any vector dim < 16 (the static L2Space
-- was built with dim=0, so hnswlib selected L2SqrSIMD16Ext, which sums only full
-- 16-float blocks). The patch computes the Euclidean distance directly. This file
-- now ASSERTS the corrected scalar behavior; it RAISEs (fails) if it regresses.
-- Uses explicit ARRAY[...]::float8[] casts to rule out literal-coercion error.

CREATE EXTENSION vectordb;
CREATE TABLE p (id bigint PRIMARY KEY, embedding float8[3]);
INSERT INTO p VALUES (1, ARRAY[0,0,0]::float8[]), (2, ARRAY[1,0,0]::float8[]),
                     (3, ARRAY[5,0,0]::float8[]), (4, ARRAY[10,0,0]::float8[]);

-- Scalar l2_distance must now return four DISTINCT real distances (was 1 constant).
DO $$
DECLARE ndist int;
BEGIN
    SELECT count(DISTINCT l2_distance(embedding, ARRAY[10,0,0]::float8[])) INTO ndist FROM p;
    IF ndist = 4 THEN
        RAISE NOTICE 'PASS finding #2 FIXED: scalar l2_distance yields 4 distinct real distances';
    ELSE
        RAISE EXCEPTION 'REGRESSION: scalar l2_distance returned % distinct values, expected 4 (l2_distance_scalar.patch not effective)', ndist;
    END IF;
END $$;

-- Exact distances to [10,0,0]: id1=10, id2=9, id3=5, id4=0 (Euclidean).
DO $$
DECLARE d1 float8; d2 float8; d3 float8; d4 float8;
BEGIN
    SELECT l2_distance(embedding, ARRAY[10,0,0]::float8[]) INTO d1 FROM p WHERE id = 1;
    SELECT l2_distance(embedding, ARRAY[10,0,0]::float8[]) INTO d2 FROM p WHERE id = 2;
    SELECT l2_distance(embedding, ARRAY[10,0,0]::float8[]) INTO d3 FROM p WHERE id = 3;
    SELECT l2_distance(embedding, ARRAY[10,0,0]::float8[]) INTO d4 FROM p WHERE id = 4;
    IF abs(d1 - 10) > 1e-6 OR abs(d2 - 9) > 1e-6 OR abs(d3 - 5) > 1e-6 OR abs(d4 - 0) > 1e-6 THEN
        RAISE EXCEPTION 'REGRESSION: wrong scalar distances id1=% (exp 10) id2=% (exp 9) id3=% (exp 5) id4=% (exp 0)', d1, d2, d3, d4;
    END IF;
    RAISE NOTICE 'PASS exact scalar distances: id1=% id2=% id3=% id4=%', d1, d2, d3, d4;
END $$;

-- Ordering: a SQL re-rank by scalar distance now yields the correct ascending order
-- (id4,id3,id2,id1) -- the capability finding #2 said was impossible. This unblocks
-- exact ground-truth tests and the DEV-1168 finalize design.
DO $$
DECLARE ordered bigint[];
BEGIN
    SELECT array_agg(id ORDER BY l2_distance(embedding, ARRAY[10,0,0]::float8[]))
      INTO ordered FROM p;
    IF ordered <> ARRAY[4, 3, 2, 1]::bigint[] THEN
        RAISE EXCEPTION 'REGRESSION: scalar re-rank order % != {4,3,2,1}', ordered;
    END IF;
    RAISE NOTICE 'PASS scalar re-rank order: %', ordered;
END $$;

\echo '============ fork distance probe: finding #2 FIXED (scalar l2_distance) ============'
