# Plan 016: Design the v0→v1 graph-store rewire — point the operators and benchmarks at the native AM the thesis is about (design/spike, not a build)

> **Executor instructions**: This is a DESIGN plan — the deliverable is a design document + a
> measured spike, NOT a code migration. Follow it step by step; run every verification command.
> If anything in the "STOP conditions" section occurs, stop and report. When done, update the
> status row in `advisor-plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 408e852..HEAD -- src/graph_store/ src/graph_store_ext/ scripts/patches/tridb_tjs_operator.patch scripts/patches/tridb_tjs_open_operator.patch`
> On any in-scope change, re-verify the "Current state" excerpts before proceeding.

## Status

- **Priority**: P2 (strategically P1; sequenced after the P1 correctness/test plans)
- **Effort**: L (the design+spike itself is M; the migration it specifies is L and GX10-flavored)
- **Risk**: MED for the spike; the migration it plans is HIGH and deliberately NOT executed here
- **Depends on**: none (plans 010/017 touch the tjs_open patch — coordinate; this plan writes docs + one prototype harness, no patch edits)
- **Category**: tech-debt / architecture / direction
- **Planned at**: commit `408e852`, 2026-07-01

## Why this matters

TriDB's central architectural bet (golden rule 3, ADR-0002/0003) is a **native adjacency-list
access method** — the v1 store in `src/graph_store/` (32KB pages via the shared buffer manager,
GenericXLog WAL, `gph_*` SQL surface), built and passing 8 AM harnesses. But **nothing on the
shipped path uses it**: both operators (`tjs`, `tjs_open`) probe `graph_store.neighbors(...)` —
the v0 heap-backed *extension* (`src/graph_store_ext/`, SPI over a regular heap table) — and all
8 benchmark scripts PGXS-install the v0 extension (`EXT="$ROOT/src/graph_store_ext"`). Every
published headline (SM-2 15.1×, SIFT-1M filtered, GraphRAG +15.6pt, the neon sweep) measured the
predecessor store. ADR-0003 says v1 "supersedes" v0, yet v0 is load-bearing everywhere. Before
the 128 GB headline run — the launch artifact — the project must either (a) rewire to v1 and
measure the real thesis component, or (b) consciously document that v1 is future work and the
numbers describe the v0 composition. This plan produces the design + evidence to make that call.

## Current state

- v1 surface (`src/graph_store/graph_store_am--0.1.0.sql`, verified):
  `gph_insert_vertex() RETURNS bigint`, `gph_insert_edge(bigint, bigint)`,
  `gph_neighbors(bigint) RETURNS SETOF bigint`, `gph_traverse(bigint, OUT src, OUT dst)`,
  `gph_visits()`, `gph_page_reads()`, `gph_vertex_count()`, `gph_edge_count()`.
  Extension name `graph_store_am`; PGXS `MODULE_big = graph_store_am`; tested by the `AM_TESTS`
  harnesses (`scripts/graph_am_test.sh` etc.).
- v0 surface (`src/graph_store_ext/graph_store--0.1.0.sql`, verified): schema `graph_store` with
  `add_edge(src bigint, dst bigint)`, `neighbors(src bigint)`, `visits()`,
  `graph_query(canonical_sql text)` (the canonical-query front door / lowering). Heap-backed,
  documented limitations in `docs/graph_store_v0_limitations.md`.
- Operator coupling (verified in both patches): `tjs`'s `graphReachableT` and `tjs_open`'s
  `expandMultiSeedO` issue `SELECT dst FROM graph_store.neighbors(%lld) AS dst` via SPI. The
  canonical lowering (`graph_store.graph_query` in the v0 SQL) also lowers INTO `tjs(...)`.
- Bench coupling (verified): `scripts/bench_{graphrag,graphrag_h2h,sm2,filtered,public,live,gx10_sweep}.sh`
  and `scripts/graph_test.sh` all set `EXT="$ROOT/src/graph_store_ext"` and their SQL does
  `CREATE EXTENSION graph_store`.
- v1 AM hardening riders that belong to the migration (verified in code, from the audit):
  1. `gph_locate_vertex` (`src/graph_store/graph_am.c:197-246`) walks the whole vertex page
     chain per lookup, though vids are dense+monotone (`gm_next_vid`) — O(1) arithmetic
     addressing is possible; today edge ingest is O(E·V/recs_per_page) and every scan-open pays
     O(V).
  2. `GphMeta.gm_vertex_count` is `uint32` beside `uint64 gm_next_vid`/`gm_edge_count`
     (`src/graph_store/gph_page.h:60-63`, with a `uint32 gm_reserved` available) — widen during
     any format-touching migration.
  3. Per-Next() `gph_open_store(AccessShareLock)`/`relation_close` in `gph_neighbors`/
     `gph_traverse`, and visibility via `TransactionIdDidCommit` without a pinned snapshot
     (intra-traversal torn reads possible) — decide whether the v1 contract covers this before
     it becomes the operator substrate.
- Related open work you must NOT duplicate: CSR-lite sorted-adjacency spike (advisor plan 009,
  "leaning GO", prototype on branch `spike/009-contiguity`); the read-once adjacency scan already
  landed on master (`ff7f239`).
- Conventions: design docs `docs/*_v0.1.0.md`; ADRs `docs/decisions/NNNN-*.md` numbered (next
  free: 0013); spec changes by addendum.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Python layer | `make test && make lint` | exit 0 |
| AM harness (needs image) | `bash scripts/graph_am_test.sh tridb/msvbase:dev` | PASS |
| v0-vs-v1 microbench (this plan creates it) | `bash scripts/graph_v0v1_bench.sh tridb/msvbase:dev` | prints comparable numbers |

## Scope

**In scope** (deliverables):
- `docs/decisions/0013-graph-store-v1-rewire.md` (create — the ADR)
- `docs/graph_rewire_design_v0.1.0.md` (create — the migration design; or fold into the ADR if
  under ~150 lines)
- `scripts/graph_v0v1_bench.sh` + `test/graph_v0v1_bench.sql` (create — the measured spike)
- `advisor-plans/README.md` (status row)

**Out of scope** (explicitly NOT in this plan):
- ANY edit to the operator patches, bench scripts, or either extension's code — the migration
  itself is a follow-on executed only after the ADR is accepted.
- The CSR-lite layout decision (plan 009 owns it; your ADR should reference it as a sequencing
  question, not resolve it).
- The 128 GB benchmark itself.

## Git workflow

- Branch: `advisor/016-v0v1-rewire-design` from `origin/master`
- Commits: `docs(adr): 0013 graph-store v1 rewire design (advisor plan 016)` etc.
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Interface-gap matrix

Read both SQL surfaces (paths above) and produce, in the design doc, an exact call-site → v1
mapping table: for each of `graph_store.add_edge / neighbors / visits / graph_query` list every
consumer (the two patch call sites, each bench script's generated SQL — find them with
`grep -rn "graph_store\." scripts/ tools/ bench/ test/ src/graph_store_ext/`) and the v1
equivalent (`gph_insert_edge` needs vertices pre-created via `gph_insert_vertex` — note the
id-mapping problem: v0 `add_edge` takes arbitrary bigint ids, v1 assigns dense vids; this is THE
design decision). Document the chosen id strategy (recommend: a `gph_upsert_vertex(ext_id)`
mapping layer or teaching `gph_insert_edge` to auto-create — weigh both, pick one, justify).

**Verify**: the matrix covers every grep hit — paste the grep hit-count into the doc and match
row count.

### Step 2: Measured v0-vs-v1 spike

Create `scripts/graph_v0v1_bench.sh` + `test/graph_v0v1_bench.sql` (model the docker-exec
bootstrap on `scripts/graph_am_test.sh`): load the SAME synthetic graph (e.g. 50k vertices /
500k edges, deterministic seed) into v0 (`graph_store.add_edge`) and v1 (`gph_insert_vertex` +
`gph_insert_edge`), then measure per store: bulk-load wall-clock, `neighbors()` latency on 100
random vertices (hub + tail mix), and page reads (`gph_page_reads()` for v1;
`pg_statio_user_tables` for v0's heap). Emit a small comparison table. Expect v1 ingest to look
bad until the O(1) vid addressing lands — that number IS the evidence for rider (1); record it,
don't fix it here.

**Verify**: `bash -n` both files → exit 0; engine-gated run if the image exists (else
"engine-gated: unbuilt here" — the harness must still be committed runnable).

### Step 3: Write ADR-0013

Sections: Context (the coupling facts from Current state — inline them); Decision (staged
migration: Stage A = operators' SPI text swaps to a compatibility view or the `gph_*` calls,
Stage B = bench scripts install `graph_store_am` and corpus SQL emits v1 DDL, Stage C = v0
archived per ADR-0003's supersession, headline benchmarks re-run and docs updated to name the
store measured); Riders (the three hardening items from Current state, each with its
file:line); Consequences (published numbers change and must be regenerated — name which docs);
Alternatives considered (keep v0 as the permanent operator substrate and reposition v1 as
future work — spell out what that does to the thesis claim); Sequencing vs plan 009 (CSR-lite
would change v1's layout — decide rewire-then-CSR or CSR-then-rewire with one paragraph of
reasoning informed by Step 2's numbers).

**Verify**: `ls docs/decisions/0013-*.md` → exists; doc references the Step 2 table.

### Step 4: Surface the decision

Add one line to `docs/STATUS.md` (new banner, dated): "V1 REWIRE DESIGN (ADR-0013) — decision
pending maintainer review; headline numbers to date measure the v0 heap-backed graph ext (see
ADR-0013 Context)." This is the honest-labeling stopgap until the migration or the conscious
deferral.

**Verify**: `grep -n "ADR-0013" docs/STATUS.md` → the banner.

## Test plan

- The spike harness (Step 2) is the executable artifact; its numbers go in the ADR.
- `make test && make lint` unchanged (no Python code paths touched beyond none).

## Done criteria

- [ ] ADR-0013 + design doc exist with the interface matrix, id-strategy decision, staged plan,
      riders, and the v0-vs-v1 measured table (or "engine-gated" placeholder + runnable harness)
- [ ] `scripts/graph_v0v1_bench.sh` committed, `bash -n` clean
- [ ] STATUS banner added
- [ ] `make test && make lint` exit 0; `git status` clean outside scope
- [ ] `advisor-plans/README.md` status row updated

## STOP conditions

- You find an existing doc/ADR already specifying the v0→v1 operator rewire (search
  `grep -rn "gph_neighbors" docs/ plans/ advisor-plans/`) — reconcile instead of duplicating.
- The v1 AM harnesses (`make graph-test`) fail on the current image — the substrate isn't ready
  to design onto; report which suite fails.
- Step 2 shows v1 `neighbors()` slower than v0 even on warm cache at 50k/500k — that inverts the
  migration's premise; report the numbers and pause the ADR at "Context" stage.

## Maintenance notes

- The follow-on migration (Stages A-C) should be planned as its own executor plan(s) after the
  maintainer accepts ADR-0013 — expect one plan per stage, with the operator-patch edits
  sequenced after plans 010/017 have settled that file.
- Riders 1-2 (O(1) vid addressing, uint32→uint64 counter) belong in Stage A/B work, not before —
  they change the on-disk format and should ride one format bump.
- The 128 GB headline run should NOT start until this decision is made — otherwise it measures
  v0 again and the launch inherits the mislabel.
