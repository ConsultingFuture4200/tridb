# MSVBASE Build Notes — x86_64 standin + reproducible upstream fixes

Updated: 2026-06-23. The dev workstation (x86_64, 62 GB, dual GTX 1070) was validated as a
**standin for the GX10 for all software work (Phases 0–2)**. `scripts/x86build.sh --docker`
builds the MSVBASE fork; `scripts/smoke_test.sh` proves it runs.

## Result

`tridb/msvbase:dev` builds (3.0 GB). Smoke test passes end-to-end:

```
CREATE EXTENSION vectordb;            -- loads
create table ... float8[10];          -- vector column
create index ... using hnsw(...);     -- HNSW index builds
-- TopK + relational filter returns correct top-5
-- EXPLAIN shows: Index Scan using t4_index ... Order By (vector_1 <-> ...)
--   => the VBASE relaxed-monotonicity ANN path (TR-1 early termination) is live
```

This validates the relational + vector legs (DEV-1162 acceptance) on the standin.

## Pinned upstream

Both build scripts default `PIN_COMMIT` to MSVBASE
`1a548db14d7a3f6f64808c99b9bc1aa01a25b71f` ("Fix vector constant parsing. (#20)") — the exact
upstream commit this build was validated against, so the x86 standin and the GX10 compile the
same source. Override with `--commit <sha>` to build a different revision (and re-validate).

## What the standin proves vs. what still needs the GX10

| Proven here (x86_64) | Still GX10-only |
| -- | -- |
| MSVBASE fork builds + runs; vectordb loads; HNSW index + ANN scan work | DEV-1160 marker #1 *as written* (ARM64 build sign-off) |
| All Phase 0–2 software is buildable/testable (native graph store, TJS, planner are arch-independent C) | Headline benchmark numbers (spec pins 128 GB in-memory; this box has 62 GB) |
| Reproducible build recipe (fixes below) | ARM alignment bugs (build with `-Wcast-align`, run ASan/UBSan, final GX10 compile) |

## Reproducible upstream fixes (all arch-independent — they hit the GX10 too)

The upstream MSVBASE Dockerfile/build had seven bugs, all now patched idempotently by
`scripts/x86build.sh` (and applicable to `gx10build.sh`). Surfaced only by actually building:

| # | Symptom | Root cause | Fix |
| -- | -- | -- | -- |
| 1 | Boost download 404 | `boostorg.jfrog.io` left JFrog in 2024 | rewrite → `archives.boost.io` |
| 2 | `groupadd: GID '999' already exists` | base image already has GID 999 | `groupadd/useradd -o` (non-unique) |
| 3 | `plpython.h: eval.h: No such file` | PG 13.4 PL/Python includes `eval.h`, removed in Python 3.11+ | drop `--with-python` (unused by TriDB) |
| 4 | SPTAG `'unique_lock' is not a member of 'std'` | modern GCC dropped transitive `<mutex>` | force-include std headers into SPTAG's CXX flags |
| 5 | OpenMP_C "not found" after #4 | vectordb derives `CMAKE_C_FLAGS` from `CMAKE_CXX_FLAGS`; C++ headers leaked to the C probe | scope force-include to SPTAG only |
| 6 | `IndexAmRoutine has no member 'amcanrelaxedorderbyop'`; `hnswlib::ResultIterator` missing | **`scripts/patch.sh` never ran** — Dockerfile COPYs the host tree; PG/hnswlib built unpatched, i.e. *without relaxed monotonicity* | apply spann/hnsw/Postgres patches on the host before `docker build` |
| 7 | every `docker run ... bash` auto-inits / `NEED TO SET PGUSERNAME` | image `ENTRYPOINT=docker-entrypoint.sh` auto-manages a cluster | smoke test uses `--entrypoint bash` + real binaries directly |

Fix #6 is the critical one: without it the build is a **clean build of the wrong database**
— stock PostgreSQL with no relaxed monotonicity, silently defeating the thesis. Only a real
build surfaces it. This resolves spec marker #1 on x86 *with documented deltas*; the GX10 run
must confirm the same recipe on ARM64.

## How to reproduce

```bash
scripts/x86build.sh --docker     # build tridb/msvbase:dev (applies all 7 fixes)
scripts/smoke_test.sh            # prove relational + vector legs work
```
