# Plan 071: Fork↔stock `tjs_open` filter-first parity harness (golden differential test)

> **Executor instructions**: Follow step by step; run every verification. STOP conditions halt you.
> SKIP updating advisor-plans/README.md. This plan builds a DIFFERENTIAL test across two engine
> images — the anti-false-green step (§Verification 4) is MANDATORY, not optional.
>
> **Drift check (run first)**: `git diff --stat 77b2d7a..HEAD -- src/tjs_pg/ test/tjs_pg_test.sql test/tjs_filter_first_test.sql scripts/`
> If the operator surfaces changed, re-read them before writing queries; mismatch = STOP.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW (test-only; adds a new script + test, changes no operator code)
- **Depends on**: 062, 063 (merged — the stock operator's arg guards + metric fix)
- **Category**: tests / tech-debt
- **Planned at**: commit `77b2d7a`, 2026-07-16

## Why this matters

Two independent implementations of the fused operator now exist — the fork C++ `tjs_open`
(`scripts/patches/tridb_tjs_open_operator.patch`, built into `tridb/msvbase:dev`) and the stock-PG
C `tjs_open` (`src/tjs_pg/tjs_pg.c`, built into `tridb/pg17-unfork:dev`). ADR-0019 documents that
the fork remains the reference until the stock operator's results reproduce, but **nothing
automatically checks they agree** — parity lives in prose. This harness makes drift a test failure:
run the SAME filter-first query on the SAME corpus through both operators and assert the same top-k
ids. It is heavy (needs both images; the fork image is ~9.5 GB, x86-only) so it is a manual /
CI-dispatch gate, not a per-PR check.

## The surface mapping (the load-bearing subtlety — read both test files to confirm)

The two operators express the *same* filter-first query differently:

- **Fork** (`test/tjs_filter_first_test.sql`, and the D1 wikidata emit): the graph reach is encoded
  **inside the filter string** and the rank is a **SQL expression**. Confirm the exact arg list by
  reading the fork operator's SQL registration + the test. The filter-first invocation shape used by
  the Wikidata Gate-A run was (approximately):
  `tjs_open('entities', k, term_cond, m_seeds, hops, 'id', 'P31 @> ARRAY[T] AND src=X AND ptype=P', 'embedding <-> ''{...qvec...}''')`
  — i.e. `src=`/`ptype=` in the filter are the fork's graph-reach encoding, and the rank is a
  `<->` expression string. **VERIFY this against the fork image**: `docker run --rm tridb/msvbase:dev`
  and inspect `\df tjs_open` (or read the registration in the patch) to get the real signature.
- **Stock** (`test/tjs_pg_test.sql`, `src/tjs_pg/tjs_pg--0.1.0.sql`): the graph reach is **positional
  args** `src` + `edge_type`, and the rank is a **`vector` parameter**:
  `tjs_open('entities', k, term_cond, 0, hops, 'id', 'P31 @> ARRAY[T]', '[...qvec...]'::vector, X, P)`.

The corpus is identical **except the embedding column**: fork = `float8[]` + `vectordb` HNSW +
`{...}` literals; stock = `vector(dim)` + pgvector HNSW + `[...]` literals. The graph store
(`graph_store_am`) is byte-identical in both. So the fixture is one corpus generator with a
fork/stock literal toggle (the `dialect` split already in `tools/wikidata_engine_load.py` is the
reference for the exact token differences).

## Steps

1. **Shared corpus fixture** (`test/parity_corpus.sql.tmpl` or generated inline by the script):
   a small deterministic corpus — e.g. 2000 entities, embeddings chosen so the filter-first top-k is
   unambiguous (NO ties near the k-boundary; reuse the discriminating construction style from
   `test/tjs_pg_test.sql`'s PASS 1c), a typed hub `2 --P--> {1000..1100}`, and a P31 type on a
   subset. Emit the embedding column + literals per a `DIALECT` toggle (fork `float8[]`/`{}` vs stock
   `vector`/`[]`), and build the matching HNSW index per dialect. Keep the graph-store setup
   (`gph_upsert_vertex` in id order, `register_edge_type`, `gph_insert_edge`) identical.

2. **`scripts/tjs_parity_test.sh`**: a host script that, for a fixed set of ~10 filter-first queries
   (each an (anchor X, property P, type T) triple with a k):
   - loads the corpus into the **fork** image (`tridb/msvbase:dev`, the fork-dialect fixture) and runs
     the fork `tjs_open` filter-first form, capturing the top-k id array per query;
   - loads the corpus into the **stock** image (`tridb/pg17-unfork:dev`, stock-dialect fixture) and runs
     the stock `tjs_open` filter-first form (`SET graph_store.assume_dense_open = on` as the D1 run
     did), capturing the top-k id array per query;
   - **diffs** the two id arrays per query and prints `PARITY OK qN` / `PARITY MISMATCH qN fork=... stock=...`;
     exits non-zero on any mismatch.
   Mirror `scripts/pg17_graph_test.sh`'s container-invocation style for the stock side and
   `scripts/graph_test.sh`'s for the fork side. The queries + expected-equal contract are the test.

3. Do NOT wire it into per-PR CI (too heavy). Add a `.PHONY` Makefile target `tjs-parity-test` that
   runs the script, and a one-line note in `docs/INSTALL_stock_pg.md` (or CONTRIBUTING) that it is a
   manual/dispatch parity gate. (Keep this minimal — the harness is the deliverable.)

## Verification

1. `bash scripts/tjs_parity_test.sh` → every query prints `PARITY OK`, exit 0. (Needs both images;
   build them if absent: fork via `scripts/x86build.sh --docker`, stock via
   `docker build -t tridb/pg17-unfork:dev scripts/pg17/`.)
2. `make -n tjs-parity-test` shows the script invocation; `grep -c 'tjs-parity-test' Makefile` ≥ 2.
3. `make lint` and `make test` unchanged (no Python touched, or if a small generator helper is added,
   it is host-tested).
4. **ANTI-FALSE-GREEN (MANDATORY)**: prove the harness actually catches divergence. Temporarily
   perturb ONE side's query so the two operators must return different top-k (e.g. change the stock
   query's `k` by one, or its query vector, or drop the P31 filter on one side) and confirm the
   script prints `PARITY MISMATCH` and exits non-zero. Then revert the perturbation and confirm
   `PARITY OK` again. Report both outcomes with the actual id arrays. **A harness that cannot be made
   to fail is worthless — if you cannot make it report MISMATCH on a deliberate divergence, STOP and
   report: the test is not actually comparing the operators.**

## Done criteria

- `scripts/tjs_parity_test.sh` exists and prints `PARITY OK` for all queries on matched inputs
  (exit 0), AND prints `PARITY MISMATCH` (exit ≠ 0) under the deliberate §4 perturbation.
- `make tjs-parity-test` target exists.

## Out of scope / do NOT touch

- The operator C on either side (fork patch or `src/tjs_pg/tjs_pg.c`) — this is a TEST that observes
  them, never a change to them.
- The seedless / bridge path — this harness is **filter-first only** (the semantically-identical,
  Gate-A/B path). Seedless parity is a separate, harder plan (the fork's exact phase semantics vs
  stock's differ by design; comparing them needs a recall-band tolerance, not an exact id diff).
- Per-PR CI wiring (the harness is dispatch/manual — too heavy for every push).
- advisor-plans/; any operator or benchmark source.

## STOP conditions

- If the fork `tjs_open` filter-first signature cannot be confirmed from the fork image / patch (you
  cannot construct a query you're confident is the fork's filter-first equivalent), STOP and report
  the fork signature you found — a guessed-wrong fork query makes the whole parity claim meaningless.
- If the two operators legitimately DISAGREE on matched inputs (a real MISMATCH you can't attribute
  to a query-construction error), STOP and report it as a FINDING with the diverging id arrays — that
  is a genuine parity bug the harness was built to catch, and the reviewer must see it, not have it
  papered over.
- If §4 anti-false-green cannot produce a MISMATCH, STOP (the harness isn't comparing the operators).
- If either engine image cannot be built in this environment, STOP and report (do not claim parity
  without running both).

## Maintenance note

This is the drift guard ADR-0019 implied but didn't provide. When either operator changes its
filter-first behavior, this harness must be re-run; a divergence is a parity regression to
investigate, not a test to relax. Extending it to the seedless path (with a recall-band tolerance) is
the natural follow-up.
