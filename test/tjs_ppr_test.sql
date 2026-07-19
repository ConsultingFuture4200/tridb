-- tjs_ppr_test.sql — plan 095 SPIKE: opt-in PPR-graded seedless scoring (tjs.graph_scoring).
-- Runs via scripts/pg17_graph_test.sh (stock PG 16/17 + pgvector + graph_store_am + tjs_pg).
--
-- Corpus (4 rows, vector(1)): S=0 is the ANN top-1 seed (dist 0.01 from q=[0]); A=2, B=1,
-- C=3 sit at the EXACT SAME vector distance from q (0.5), so vector-only / membership mode
-- can only break their tie by ascending id (B < A < C). Graph (typed 'P' edges, insertion
-- order matters -- gph_traverse_typed yields out-neighbors in insertion/adjacency-slot
-- order, ADR-0020 §5): S -> A, S -> B, S -> C, C -> A. A is thus MULTI-PATH-REINFORCED
-- (direct from S, AND indirectly via C); B and C are each reached only once. hops=2 so C's
-- (depth-1) push into A (depth-2) is walked; A's second pop (depth 2 >= hops) still banks
-- reserve but is not expanded further (it has no out-edges anyway).
--
-- Hand-computed forward push (alpha=0.15, r_max=1e-3, TJS_PPR_ALPHA/TJS_PPR_RMAX in
-- tjs_pg.c), single seed S with personalization weight 1.0 (only seed => p_S = 1):
--   pop S (residue 1.0):      reserve[S] += 0.15*1.0        = 0.15
--                              push_mass 0.85 / deg(S)=3 -> share 0.283333... to each of A,B,C
--   pop A (residue 0.283333): reserve[A] += 0.15*0.283333    = 0.0425      (A has no out-edges)
--   pop B (residue 0.283333): reserve[B] += 0.15*0.283333    = 0.0425      (B has no out-edges)
--   pop C (residue 0.283333): reserve[C] += 0.15*0.283333    = 0.0425
--                              push_mass 0.85*0.283333=0.240833 / deg(C)=1 -> residue[A] += 0.240833
--   pop A again (residue 0.240833, depth 2 >= hops -> credited, not expanded):
--                              reserve[A] += 0.15*0.240833   = 0.036125
--   TOTALS: reserve[S]=0.15, reserve[A]=0.078625, reserve[B]=0.0425, reserve[C]=0.0425
--
-- Finalize pool = {S,A,B,C} (all graph-reached; the vector top-k IS the reach set here, so
-- the bridge-cap floor(k/2)-min-1 guarantee is not exercised by this fixture -- see plan 095
-- ADR-0012 addendum for that interaction, covered by the existing plan-087 membership tests).
-- min-max over dist: mind=dist(S), maxd=dist(A)=dist(B)=dist(C) (the tie) =>
-- vecsim_norm(S)=1.0, vecsim_norm(A)=vecsim_norm(B)=vecsim_norm(C)=0.0 (A/B/C's fused score
-- is thus PURELY the reserve term -- directly attributable to graph reinforcement, no vector
-- noise). min-max over reserve: minr=0.0425 (B and C tie), maxr=0.15 (S) =>
--   fused(S) = 1.0 + (0.15-0.0425)/(0.15-0.0425)               = 1.0 + 1.0      = 2.0
--   fused(A) = 0.0 + (0.078625-0.0425)/(0.15-0.0425)           = 0.0 + 0.336046 = 0.336046
--   fused(B) = 0.0 + (0.0425-0.0425)/(0.15-0.0425)             = 0.0 + 0.0      = 0.0
--   fused(C) = 0.0 + (0.0425-0.0425)/(0.15-0.0425)             = 0.0 + 0.0      = 0.0
-- Descending fused, ties ascending id (plan 095 "ties by (score, id)"): S, A, B(1), C(3)
--   => PPR order: {0, 2, 1, 3}
-- Membership (pure vector distance, ties ascending id: B(1) < A(2) < C(3)):
--   => membership order: {0, 1, 2, 3}
-- The graded order visibly promotes A ahead of B: PPR credits A's reinforcing second path;
-- membership cannot see it (reachability is binary) and falls back to the id tie-break.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS graph_store_am;
CREATE EXTENSION IF NOT EXISTS tjs_pg;

CREATE TABLE entities (id bigint PRIMARY KEY, embedding vector(1));
INSERT INTO entities (id, embedding) VALUES
  (0, '[0.01]'),   -- S: the ANN top-1 seed
  (1, '[0.5]'),    -- B: reached once (direct from S only)
  (2, '[0.5]'),    -- A: reached twice (direct from S, and via C) -- tied on distance with B/C
  (3, '[0.5]');    -- C: reached once (direct from S only), and pushes into A
CREATE INDEX entities_hnsw ON entities USING hnsw (embedding vector_l2_ops)
  WITH (m = 16, ef_construction = 64);

DO $$
DECLARE g int; v bigint;
BEGIN
  FOR g IN 0..3 LOOP
    v := graph_store.gph_upsert_vertex(g);
    IF v <> g THEN RAISE EXCEPTION 'dense vid drift: % != %', v, g; END IF;
  END LOOP;
END $$;
SELECT set_config('tjs.ptype', graph_store.register_edge_type('P')::text, false);

-- insertion order fixes gph_traverse_typed's out-neighbor emission order (ADR-0020 §5):
-- S -> A, S -> B, S -> C (in that order), then C -> A (A's reinforcing second path).
SELECT graph_store.gph_insert_edge(0, 2, current_setting('tjs.ptype')::int);  -- S -> A
SELECT graph_store.gph_insert_edge(0, 1, current_setting('tjs.ptype')::int);  -- S -> B
SELECT graph_store.gph_insert_edge(0, 3, current_setting('tjs.ptype')::int);  -- S -> C
SELECT graph_store.gph_insert_edge(3, 2, current_setting('tjs.ptype')::int);  -- C -> A

SET hnsw.iterative_scan = relaxed_order;

-- (1) DEFAULT ASSERTION (ADR-0021 D1/D5): tjs.graph_scoring defaults to 'ppr' with NO explicit
-- SET. A is promoted ahead of B by its reinforcing second path (via C), even though A and B
-- are vector-distance-tied -- the same graded order as the explicit-ppr case (3). This is the
-- default-flip assertion: the default path must produce the PPR order without any SET.
DO $$
DECLARE got bigint[];
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 4, 1000, 1, 2, 'id', '',
    '[0.0]'::vector) AS t;
  IF got <> ARRAY[0,2,1,3]::bigint[] THEN
    RAISE EXCEPTION 'default (no SET) scoring: got % (expected {0,2,1,3}, ppr order)', got;
  END IF;
  RAISE NOTICE 'PASS 1: default tjs.graph_scoring (no SET) = ppr order {0,2,1,3}';
END $$;

-- (2) Explicit membership: identical to (1) -- byte-inert restatement of the default.
SET tjs.graph_scoring = 'membership';
DO $$
DECLARE got bigint[];
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 4, 1000, 1, 2, 'id', '',
    '[0.0]'::vector) AS t;
  IF got <> ARRAY[0,1,2,3]::bigint[] THEN
    RAISE EXCEPTION 'explicit membership scoring: got % (expected {0,1,2,3})', got;
  END IF;
  RAISE NOTICE 'PASS 2: explicit tjs.graph_scoring=membership = default order {0,1,2,3}';
END $$;

-- (3) PPR graded scoring: A is promoted ahead of B by its reinforcing second path (via C),
-- even though A and B are vector-distance-tied -- the graded order the plan requires.
SET tjs.graph_scoring = 'ppr';
DO $$
DECLARE got bigint[];
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 4, 1000, 1, 2, 'id', '',
    '[0.0]'::vector) AS t;
  IF got <> ARRAY[0,2,1,3]::bigint[] THEN
    RAISE EXCEPTION 'ppr scoring: got % (expected {0,2,1,3}: S, A (reinforced), B, C)', got;
  END IF;
  RAISE NOTICE 'PASS 3: tjs.graph_scoring=ppr promotes the reinforced vertex A: {0,2,1,3}';
END $$;
RESET tjs.graph_scoring;

-- (4) Graph-leg honesty counters still behave under ppr mode (ADR-0020, unaffected by the
-- scoring switch): graph_examined > 0 (the push touched edges), censored is false (the tiny
-- budget default 65536 comfortably covers this fixture's 4 edge-steps).
SET tjs.graph_scoring = 'ppr';
DO $$
DECLARE got bigint[]; ex bigint; capped boolean;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 4, 1000, 1, 2, 'id', '',
    '[0.0]'::vector) AS t;
  ex := tjs_open_graph_examined();
  capped := tjs_open_graph_censored();
  IF ex <> 4 THEN
    RAISE EXCEPTION 'ppr graph_examined=% (expected 4 edge-steps: S->A,S->B,S->C,C->A)', ex;
  END IF;
  IF capped IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'ppr graph_censored=% (expected false — default budget covers this fixture)', capped;
  END IF;
  RAISE NOTICE 'PASS 4: ppr mode graph_examined=4, graph_censored=false';
END $$;
RESET tjs.graph_scoring;

SET hnsw.iterative_scan = DEFAULT;
