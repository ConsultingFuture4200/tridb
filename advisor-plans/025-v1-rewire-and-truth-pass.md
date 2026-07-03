# Plan 025: Execute ADR-0013 Stages A+B (operators + benches onto the v1 native graph AM) and make every public doc tell the truth about what was measured

> **Executor instructions**: Follow step by step; verify each step before the next. On any STOP
> condition, stop and report. Update your row in `advisor-plans/README.md` when done (unless a
> reviewer maintains it). This plan is authorized: the maintainer accepted ADR-0013 in the
> 2026-07-03 session — flip its Status to **Accepted** as part of Step 1.
>
> **Drift check (run first)**: `git diff --stat e345998..HEAD -- scripts/patches src/graph_store src/graph_store_ext scripts/bench_*.sh tools/*.py README.md docs/STATUS.md`
> On any in-scope drift vs the excerpts below, STOP. NOTE: plan 024 intentionally lands first and
> touches `tjs_operator.cpp`/`graph_store--0.1.0.sql` — that drift is EXPECTED; re-read those two
> files and proceed if the 024 changes are the only delta.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: MED-HIGH (swaps the graph probe under both operators; FR-7/TR-1 must be re-proven)
- **Depends on**: advisor-plans/024-operator-arg-hardening.md (same patch-chain tail; land 024 first)
- **Category**: tech-debt / architecture (launch-credibility critical)
- **Planned at**: commit `e345998`, 2026-07-03

## Why this matters

Every published headline — SM-2 15.1×, the 4.7 ms 1M filter-first flagship, filtered SIFT-1M,
GraphRAG +15.6pt — was measured with the operators probing the **v0 heap-backed extension**
(`src/graph_store_ext`, a plain heap table + SPI), not the **v1 native access method**
(`src/graph_store`) that the README sells as the thesis ("native adjacency-list PostgreSQL access
method … *not* relational join tables", README.md:74). ADR-0013 documents this and prescribes the
staged fix. One informed reader of ADR-0013 can currently dismantle the launch narrative.
Publication of new external numbers is FROZEN until this lands.

## Current state

- `docs/decisions/0013-graph-store-v1-rewire.md` — Status **Proposed**; defines Stage A (operators
  onto v1 via a `gph_upsert_vertex(ext_id) RETURNS bigint` id-mapping layer; port the `graph_query`
  front door), Stage B (flip the 9 bench drivers to `CREATE EXTENSION graph_store_am`, corpus
  generators emit vertex-materialization + `gph_insert_edge`, re-run headlines, record deltas),
  Stage C (archive v0 — OUT of this plan's scope). Read the whole ADR + its design doc
  `docs/graph_rewire_design_v0.1.0.md` (§2 call-site matrix, §3 id mapping) before writing code.
- v0 probe sites (post-chain vendor tree / patches): `tjs_operator.cpp` `graphReachableT` →
  `SELECT dst FROM graph_store.neighbors(%lld) AS dst`; `tjs_open_operator.cpp` `expandMultiSeedO` →
  `LATERAL graph_store.neighbors(f.src)`.
- v1 surface (`src/graph_store/graph_store_am--0.1.0.sql`): `gph_insert_vertex() RETURNS bigint`
  (dense vids, must pre-exist), `gph_insert_edge(bigint,bigint)`, `gph_neighbors(bigint) RETURNS
  SETOF bigint`, `gph_traverse`, `gph_edge_count()`, `gph_page_reads()`. The id gap: v0 accepts
  arbitrary bigint ids and auto-creates vertices; v1 vids are dense and must exist first.
- The 9 drivers set `EXT="$ROOT/src/graph_store_ext"` and their SQL does `CREATE EXTENSION
  graph_store`; corpus generators emit `SELECT graph_store.add_edge(s,d);` per edge
  (`tools/bench_sm2_corpus.py`, `tools/bench_corpus.py`, `tools/sweep_corpus.py` — and note
  plan 029 rewrites these same emitters; if 029 already landed, preserve its batching shape).
- Parity/oracle harness gap: `scripts/graph_v0v1_bench.sh` exists but is non-asserting (`|| true`)
  and unwired.
- Stale docs to fix in the truth pass: `README.md:24` badge ("SM-2 100% · ~15× faster"),
  `README.md:68,200` ("heuristic … **inert** until the filter-first operator lands (v1.1)") — filter-first
  SHIPPED at `f2c93be`; `docs/STATUS.md` header + DEV-1290/1285/1332 rows; `docs/benchmark_sm2_v0.1.0.md`
  and `docs/benchmark_sm2_1m_v0.1.0.md` lack "superseded by v0.2.0" banners.
- Engine iteration recipe + patch-chain conventions: identical to plan 024 "Current state" — read
  that section; this plan produces ONE new last-in-chain patch (`tridb_graph_v1_rewire.patch`).
- GX10 rerun environment: `ssh spark`, repo `~/code/tridb`, engine images `tridb/msvbase:gx10-*`
  built via `scripts/gx10build.sh --skip-clone --image <tag>` (origin unreachable from spark; rsync
  the repo, never git-pull there; ALWAYS scp result artifacts back BEFORE any rsync toward spark).
  The 1M SM-2 recipe: `docs/benchmark_sm2_1m_v0.2.0.md` "Repro" section. Baseline stack is already
  up on spark with the 1M corpus loaded (Milvus/Neo4j/Postgres containers `tridb-baseline-*`).

## Commands you will need

Same table as plan 024 (incremental compile, x86build, graph-test, graph_test.sh single file,
make test/lint), plus:

| Purpose | Command | Expected |
|---|---|---|
| GX10 rebuild | `ssh spark 'cd ~/code/tridb && bash scripts/gx10build.sh --skip-clone --image tridb/msvbase:gx10-v1'` (after rsync; reset vendor first per BUILD notes in memory of `msvbase_patches.sh` header) | `GX10 BUILD + SMOKE OK` |
| 1M SM-2 on v1 | per `docs/benchmark_sm2_1m_v0.2.0.md` Repro, image `gx10-v1`, `--join-order filter_first` | `#SM2 DONE` in transcript |

## Scope

**In scope:** new `scripts/patches/tridb_graph_v1_rewire.patch` (+ registration);
`src/graph_store/graph_store_am--0.1.0.sql` + `src/graph_store/graph_am.c` ONLY for adding
`gph_upsert_vertex` + porting `graph_query`/`add_edge`-compat shims INTO the v1 extension per the
design doc; the 9 `scripts/bench_*.sh`/test-driver EXT lines; the 3 corpus emitters; new
`test/graph_v0v1_parity_test.sql` (asserting) + Makefile wiring; README/STATUS/benchmark-doc truth
edits; ADR-0013 status flip + Stage A/B completion addendum; a NEW versioned benchmark doc
`docs/benchmark_sm2_1m_v0.3.0.md` recording the v1-measured numbers + deltas.

**Out of scope:** Stage C (archiving v0 — keep it building and tested this round); CSR-lite/plan 009;
any change to HNSW/vector code; deleting the old benchmark docs (banner them, don't rewrite).

## Git workflow

Branch `advisor/025-v1-rewire`; `feat(engine):`/`docs(bench):` commit style; do NOT push.

## Steps

### Step 1: Accept ADR-0013 + build the parity oracle FIRST
Flip ADR-0013 Status to `Accepted (2026-07-03, maintainer session)`. Create
`test/graph_v0v1_parity_test.sql`: load an identical deterministic edge set (≥3 pages of adjacency
for one hub, plus a random tail — reuse the shape from `scripts/graph_v0v1_bench.sh`) into BOTH
stores (separate databases — the extensions collide on the `graph_store` schema), assert equal
sorted neighbor sets for every probe vertex and equal edge counts. Wire into `ENGINE_TESTS`.
**Verify**: `bash scripts/graph_test.sh tridb/msvbase:dev test/graph_v0v1_parity_test.sql` → ALL PASS. (It runs against the CURRENT image — no engine change yet.)

### Step 2: Stage A — id mapping + operator probe swap (vendor edits → new patch)
Per design doc §3 Option A: add `gph_upsert_vertex(ext_id bigint) RETURNS bigint` (+ the mapping
storage the design doc specifies) to the v1 extension, and a v1-hosted
`graph_store_am.neighbors_ext(ext_id bigint)` convenience the operators can SPI-call with EXTERNAL
ids. Snapshot → edit `graphReachableT` and `expandMultiSeedO` SPI text to the v1 probe → incremental
compile → single-file engine tests (`test/tjs_filter_first_test.sql`, `test/tjs_open_smoke.sql`,
`test/canonical_e2e_test.sql`, `test/parse_canonical.sql` — these seed graphs via the front door, so
ALSO port `graph_store.add_edge`/`graph_query` equivalents into the v1 extension as the design doc's
front-door port, keeping identical SQL signatures so the tests keep passing with only the
`CREATE EXTENSION` line changed) → generate `tridb_graph_v1_rewire.patch`, register + sentinel.
**Verify**: full `bash scripts/x86build.sh --docker` then `make graph-test` → green, with the
drivers/tests updated in Step 3 running on v1.

### Step 3: Stage B — flip drivers + emitters
Change the 9 driver scripts' EXT mount to `src/graph_store` + `CREATE EXTENSION graph_store_am`
(via each script's SQL or the compat shim), and the 3 corpus emitters to materialize vertices then
`gph_insert_edge` through the map (preserve determinism: same manifest, same ids).
**Verify**: `make test && make lint` green (Python emitters have unit tests); `bash scripts/bench_sm2.sh tridb/msvbase:dev` at DEFAULT tiny scale completes with `#SM2 DONE` and sane parity (this exercises the whole v1 path end-to-end on x86; baseline stack local — if no local stack, run only the TriDB side per the script's structure and STOP-note it).

### Step 4: FR-7 / crash safety on the new path
Run `bash scripts/txn_atomicity_test.sh tridb/msvbase:dev` and `bash scripts/crash_recovery_test.sh tridb/msvbase:dev`.
**Verify**: both pass (they already target the v1 AM; what's new is the operators now share that path).

### Step 5: GX10 re-run of the flagship
Rsync repo → spark (excludes: `.venv vendor baseline/volumes data bench/out`; scp any wanted spark
artifacts back FIRST). Reset spark vendor + `gx10build.sh --skip-clone --image tridb/msvbase:gx10-v1`.
Re-run: (a) the 1M×128 SM-2 corpus TriDB-side with `--join-order filter_first` AND `vector_first`
per the v0.2.0 Repro; (b) `make graph-test IMAGE=tridb/msvbase:gx10-v1` on spark. Score against the
existing exact oracle (`bench/results/sm2_1m_exact_oracle.json` — corpus is deterministic and
UNCHANGED; if Step 3 changed corpus ids in ANY way, STOP).
**Verify**: transcripts show `#SM2 DONE`; recall vs oracle computed; suites green.

### Step 6: Truth pass + new benchmark doc
Write `docs/benchmark_sm2_1m_v0.3.0.md`: v1-measured numbers beside the v0.2.0 rows, deltas stated,
same honesty-box style. Fix README badge + the two "inert (v1.1)" paragraphs + the architecture
diagram caveat; banner the superseded SM-2 docs ("Superseded by v0.3.0 — and note: v0.1.0/v0.2.0
numbers were measured on the v0 heap store, see ADR-0013"); refresh `docs/STATUS.md` header + rows;
append the ADR-0013 Stage-A/B completion addendum with the measured deltas.
**Verify**: `grep -rn "inert" README.md` → no match; `grep -L "Superseded" docs/benchmark_sm2_v0.1.0.md docs/benchmark_sm2_1m_v0.1.0.md` → empty.

## Test plan
Parity oracle (Step 1) is the load-bearing new test; the rest is the existing suites now exercising
v1 + the GX10 re-measurement. All ENGINE_TESTS that create graphs must pass unmodified except their
`CREATE EXTENSION` line (the compat-shim requirement).

## Done criteria
- [ ] `make graph-test` green on x86 AND on `gx10-v1` (spark)
- [ ] `grep -rn "graph_store.neighbors" vendor/MSVBASE/src/tjs_operator.cpp vendor/MSVBASE/src/tjs_open_operator.cpp` → no matches
- [ ] All 9 drivers' `CREATE EXTENSION` lines target the v1 extension (`grep -l "graph_store_am" scripts/bench_*.sh scripts/graph_test.sh scripts/tjs_test.sh` covers all)
- [ ] `docs/benchmark_sm2_1m_v0.3.0.md` exists with v1 numbers + oracle recall
- [ ] README truth items fixed; superseded banners present; ADR-0013 Accepted + addendum
- [ ] README status row updated

## STOP conditions
- The design doc's id-mapping section (§3) materially disagrees with the v1 C you find (drifted design).
- Parity test finds ANY neighbor-set divergence between stores after the shim — report, do not "fix" by weakening the assert.
- v1-measured 1M recall vs the exact oracle drops below 0.95 for filter_first — that's a finding, not a tuning knob; report with transcripts.
- The corpus emitters cannot preserve byte-identical manifests (breaks oracle comparability).
- Two failed attempts at any engine-suite regression.

## Maintenance notes
Stage C (archive v0) is the deliberate follow-up once a full release cycle runs on v1. The compat
shims (`add_edge`/`graph_query` in v1) are the permanent public surface; `gph_*` stays the native
layer. Reviewers: scrutinize the id-mapping layer's transactionality (upsert under concurrent
inserts) and that no bench regenerated its corpus with different ids.
