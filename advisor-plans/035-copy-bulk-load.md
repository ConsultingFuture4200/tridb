# Plan 035: COPY-based bulk load — unblock the 128GB saturation run + a fair at-scale SM-2 (DEV-1346 / PERF-11)

> **Executor instructions**: Follow step by step; the load-time claim needs a before/after number.
> On any STOP condition, stop and report. Update your row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat 876a696..HEAD -- tools/bench_sm2_corpus.py baseline/`
> Plan 025 may have changed the emitter's edge path to the v1 `gph_insert_edge` shape — apply the
> COPY rework to the LIVE shape.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW (load path only; answers unchanged — same corpus, faster ingest)
- **Depends on**: plan 025 (v1 emission shape). Enables the at-scale head-to-head tracked in DEV-1332.
- **Category**: perf / benchmarking
- **Planned at**: commit `876a696`, 2026-07-04
- **Linear**: DEV-1346 (fully verifiable on the x86 standin — tooling + compose only)

## Why this matters

The 128 GB memory-saturation headline is **INSERT-bound** (`docs/gtm_opensource_v0.1.0.md:180`, a
v1-launch non-goal). The loader can't reach the scale where the *query* phase — the thing the benchmark
measures — becomes interesting, because ingest dominates wall-clock:

- Edges load **one SPI call per edge**: `SELECT graph_store.add_edge(s,d)` in a loop
  (`tools/bench_sm2_corpus.py:118-119`), each routing through the id-map upsert.
- Entities load as batched multi-row `INSERT ... VALUES` with full `float8[]` vector literals per row
  (`tools/bench_sm2_corpus.py:83-92`).
- The fork requires all rows inserted **before** index build — no incremental insert
  (`tools/bench_sm2_corpus.py:82`), so you can't stream-load.

At 1M / ~48k edges this completes; a true saturation run (1–2 orders more) never reaches the query phase.
There is also a **baseline-side** blocker: `baseline/sm2.py:254-255` — PGlite does **not** support
`COPY FROM STDIN`, so a fair at-scale head-to-head needs the baseline Postgres swapped for a COPY-capable
image, else the two sides' load asymmetry is uncontrolled and the comparison is not defensible.

## Current state

- `tools/bench_sm2_corpus.py:83-92` (entity INSERTs), `:118-119` (per-edge `add_edge`), `:82` (all-rows-
  before-index constraint). Sibling emitters `tools/bench_corpus.py`, `tools/sweep_corpus.py`,
  `tools/filtered_corpus.py` share the pattern.
- The COPY pattern already exists: `tools/seed_corpus.py:111-113` uses `\copy entity(...) FROM ... CSV
  HEADER`. The SM-2 generator simply never adopted it.
- `baseline/sm2.py:254-255` — the PGlite `COPY FROM STDIN` limitation; `baseline/docker-compose.yml`
  defines the baseline Postgres image.
- Answer contract: the corpus generator is deterministic (seed 42); the parity oracle + `#SM2 RESULT`
  diffs must stay byte-identical — this plan changes HOW rows load, never WHICH rows.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Emitter unit tests | `.venv/bin/python -m pytest tests/ -q -k corpus` | pass |
| Load timing (TriDB) | `time` a corpus load into `tridb/msvbase:dev` at a fixed scale, INSERT vs COPY path | before/after captured |
| Baseline up/down | `make baseline-up` / `make baseline-down` (note `PGPORT=5433` on this box) | stack healthy |
| Parity | `bash scripts/graph_test.sh tridb/msvbase:dev test/graph_v0v1_parity_test.sql` | ALL PASS |

## Scope

**In scope:** rework `tools/bench_sm2_corpus.py` to (a) load entities via `COPY`/`\copy` (CSV or binary)
instead of `INSERT ... VALUES`, and (b) bulk-load edges from a COPY'd staging table through a **set-based**
`add_edge` (or the v1 `gph_insert_edge` batched over a staging relation) instead of the per-edge `SELECT`;
the same rework in the sibling emitters where they gate an at-scale run; swap the baseline PG off PGlite
(or add a COPY-capable image) in `baseline/docker-compose.yml` + `baseline/sm2.py` so both sides load
comparably; README row + a note in the SM-2 recipe doc.

**Out of scope:** the fork's no-incremental-insert constraint (an engine limitation, not a loader one —
note it, don't fight it); the id-map cost itself (plans 033/034); the actual 128 GB GX10 run (this plan
makes it *possible*; running it is a GX10 session); the query/measurement code.

## Git workflow
Branch `advisor/035-copy-bulk-load`; `perf(bench):` commit with load-time before/after; do NOT push.

## Steps

### Step 1: Entity COPY path
Replace the entity `INSERT ... VALUES` batches with a `COPY entity(...) FROM STDIN` (CSV or binary),
mirroring `tools/seed_corpus.py:111-113`. Vectors as CSV `float8[]` text or a binary encoder — pick by
measured load time.
**Verify**: emitter unit tests pass; a fixed-scale load is byte-identical in the resulting rows (a
`SELECT count(*)` + a checksum of a few rows vs the INSERT path) and faster (record ms).

### Step 2: Edge bulk-load path
COPY edges into a staging table `(src, dst)`, then one set-based call that inserts them through the graph
store (`INSERT INTO ... SELECT` over the staging table via `add_edge`/`gph_insert_edge`, or a new
`add_edges_from_staging()`), replacing the per-edge loop. If plan 025 landed, target the v1 native edge
path; group only what the store supports and note any residual per-edge C.
**Verify**: `test/graph_v0v1_parity_test.sql` ALL PASS byte-identical; edge-load ms recorded before/after.

### Step 3: Baseline symmetry
Swap the baseline PG off PGlite to a COPY-capable Postgres image in `baseline/docker-compose.yml`; update
`baseline/sm2.py:254-255` to COPY-load. Confirm the baseline still answers SM-2 identically (it's a
different PG image, so re-run `make sm2` and check answer parity + that recall is unchanged).
**Verify**: `make baseline-up && make sm2` (PGPORT=5433) green with unchanged answer parity; baseline load
now uses COPY.

### Step 4: Full validation
`make test && make lint`; a load-time table (INSERT vs COPY, both sides) in the commit body; update the
SM-2 recipe doc so the at-scale run is one command.
**Verify**: all green; load-time improvement documented; answers byte-identical throughout.

## Test plan
Answer-invariance (parity oracle + `make sm2` answer parity) is the correctness test — this plan must not
change any result. Perf evidence = entity-load + edge-load + baseline-load before/after ms.

## Done criteria
- [ ] Entities load via COPY; rows byte-identical to the INSERT path; load-time recorded
- [ ] Edges bulk-load via staging + set-based insert; parity oracle ALL PASS; load-time recorded
- [ ] Baseline PG is COPY-capable; `make sm2` answer parity unchanged
- [ ] `make test && make lint` green; SM-2 recipe doc + README row updated
- [ ] (follow-on, GX10) the 128 GB saturation run + a re-measured vector-first-on-v1 row become runnable

## STOP conditions
- Any answer/parity change (the corpus rows or query results differ between INSERT and COPY paths).
- The baseline image swap changes baseline recall or breaks the merge logic — report; do not proceed with
  an asymmetric baseline.
- The fork rejects the set-based edge insert (v1 path constraints) — capture the residual per-edge cost
  and note what remains INSERT-bound rather than forcing an unsafe rewrite.

## Maintenance notes
This unblocks two things beyond the saturation run: a **loader-symmetric** at-scale SM-2 head-to-head
(DEV-1332) and a **re-measured vector-first row on the v1 AM** (currently stale, carried from
`benchmark_sm2_1m_v0.1.0.md`), completing the 3-way at-scale comparison. The fork's no-incremental-insert
constraint is orthogonal and remains — this plan only removes the ingest-throughput ceiling, not the
build-before-query ordering. Reviewer focus: byte-identical rows across load paths and baseline recall
invariance after the image swap.
