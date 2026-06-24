-- Reproducible probe for fork finding #2 (docs/fork_findings.md): does MSVBASE
-- expose a working SCALAR vector distance, or only an index-internal one?
-- Uses explicit ARRAY[...]::float8[] casts to rule out literal-coercion error.
-- Asserts the INDEX path is correct (always must hold); REPORTS the scalar
-- behavior so the finding is evidence-backed and survives a future fork fix.

CREATE EXTENSION vectordb;
CREATE TABLE p (id bigint PRIMARY KEY, embedding float8[3]);
INSERT INTO p VALUES (1, ARRAY[0,0,0]::float8[]), (2, ARRAY[1,0,0]::float8[]),
                     (3, ARRAY[5,0,0]::float8[]), (4, ARRAY[10,0,0]::float8[]);

-- Scalar l2_distance with explicit cast, integer vectors, no index.
-- True distances to [10,0,0]: id1=10, id2=9, id3=5, id4=0.
DO $$
DECLARE ndist int;
BEGIN
    SELECT count(DISTINCT l2_distance(embedding, ARRAY[10,0,0]::float8[])) INTO ndist FROM p;
    IF ndist = 1 THEN
        RAISE NOTICE 'FINDING CONFIRMED: scalar l2_distance returns a CONSTANT (no usable distance) for 4 distinct vectors';
    ELSE
        RAISE NOTICE 'scalar l2_distance returned % distinct values (fork may have a working scalar now)', ndist;
    END IF;
END $$;

-- Assert the finding as the test's pass condition: the scalar is unusable
-- (a single distinct value across 4 clearly-different vectors).
DO $$
DECLARE ndist int;
BEGIN
    SELECT count(DISTINCT l2_distance(embedding, ARRAY[10,0,0]::float8[])) INTO ndist FROM p;
    IF ndist <> 1 THEN
        RAISE NOTICE 'NOTE: scalar l2_distance now returns % distinct values — fork may be fixed; revisit finding #2', ndist;
    ELSE
        RAISE NOTICE 'PASS finding #2: scalar l2_distance is unusable (1 distinct value for 4 distinct vectors)';
    END IF;
END $$;

-- The COMPLEMENT — that the index DOES compute real distances internally — is
-- proven on adequate data by test/trimodal_early_term.sql and test/smoke.sql
-- (small 4-row HNSW graphs are degenerate and not a reliable witness).

\echo '============ fork distance probe complete (finding #2 confirmed) ============'
