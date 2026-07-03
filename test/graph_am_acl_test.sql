-- graph_am_acl_test.sql — advisor plan 026: containment ACLs on the graph store container.
--
-- The gstore container relation holds NON-heap 32KB pages; any heap-path access
-- (SELECT/VACUUM/ANALYZE) misreads them, and the gph_* mutators write the shared graph. The
-- extension script REVOKEs both from PUBLIC. This suite proves, as a non-superuser probe role:
--   (1) SELECT * FROM gstore        -> insufficient_privilege
--   (2) gph_insert_vertex()         -> insufficient_privilege
--   (3) gph_insert_edge(...)        -> insufficient_privilege
--   (4) gph_neighbors(0)            -> NO privilege error (traversal is the query surface)
-- EXCEPTION-block pattern per test/tjs_filter_first_test.sql assertion 7.
-- Run by scripts/graph_am_acl_test.sh (AM harness: PGXS-builds src/graph_store in the image).

CREATE EXTENSION graph_store_am;
SET search_path TO graph_store, public;

-- Seed vid 0 (as superuser) so the probe's gph_neighbors(0) exercises a real vertex.
SELECT gph_insert_vertex();

CREATE ROLE tridb_acl_probe LOGIN;
-- Schema-level USAGE is orthogonal to the plan-026 REVOKEs: the extension's schema
-- (graph_store, non-relocatable) is not PUBLIC-usable by default, so without this grant EVERY
-- probe access would fail at the schema gate and the table/function ACLs would go untested.
-- This grants schema visibility ONLY — it does not restore SELECT on gstore or EXECUTE on the
-- mutators (deployers likewise grant USAGE to roles meant to use the read surface).
GRANT USAGE ON SCHEMA graph_store TO tridb_acl_probe;

SET ROLE tridb_acl_probe;

-- (1) container is not readable as a heap
DO $$ BEGIN
    BEGIN
        PERFORM * FROM gstore;
        RAISE EXCEPTION 'PUBLIC SELECT on gstore was accepted (REVOKE missing)';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    RAISE NOTICE 'PASS 1: SELECT on gstore denied (insufficient_privilege)';
END $$;

-- (2) vertex mutator is not PUBLIC-executable
DO $$ BEGIN
    BEGIN
        PERFORM gph_insert_vertex();
        RAISE EXCEPTION 'PUBLIC EXECUTE on gph_insert_vertex was accepted (REVOKE missing)';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    RAISE NOTICE 'PASS 2: gph_insert_vertex() denied (insufficient_privilege)';
END $$;

-- (3) edge mutator is not PUBLIC-executable
DO $$ BEGIN
    BEGIN
        PERFORM gph_insert_edge(0, 0);
        RAISE EXCEPTION 'PUBLIC EXECUTE on gph_insert_edge was accepted (REVOKE missing)';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    RAISE NOTICE 'PASS 3: gph_insert_edge(0,0) denied (insufficient_privilege)';
END $$;

-- (4) the read surface stays open: traversal must NOT fail on privileges
DO $$ BEGIN
    BEGIN
        PERFORM x FROM gph_neighbors(0) x;
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE EXCEPTION 'gph_neighbors(0) raised insufficient_privilege (read surface must stay PUBLIC)';
    END;
    RAISE NOTICE 'PASS 4: gph_neighbors(0) runs for the probe role (no privilege error)';
END $$;

RESET ROLE;
REVOKE USAGE ON SCHEMA graph_store FROM tridb_acl_probe;
DROP ROLE tridb_acl_probe;

\echo === graph_store_am containment ACLs (plan 026): ALL PASS ===
