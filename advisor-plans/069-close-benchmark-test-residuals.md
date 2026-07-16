# Plan 069: Close three small benchmark test/determinism residuals from the 062–068 batch

> **Executor instructions**: Follow step by step; run every verification. On a STOP condition, stop
> and report. SKIP updating advisor-plans/README.md (the reviewer maintains it).
>
> **Drift check (run first)**: `git diff --stat 9f5bcf9..HEAD -- bench/wikidata_h2h.py bench/wikidata_sm4_seedless.py tools/wikidata_engine_load.py tests/`
> If any changed, compare "Current state" to live code first; mismatch = STOP.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (follows 062–068, all merged)
- **Category**: tests / bug
- **Planned at**: commit `9f5bcf9`, 2026-07-16

## Why this matters

The 062–068 audit-fix batch closed the load-bearing bugs but left three small, real residuals the
executors explicitly flagged as out-of-scope. Each is host-testable Python, low-risk, and worth
tidying so the benchmark's determinism/coverage story is complete:

1. **Baseline rerank lacks an id tie-break** (064 residual). Plan 064 made the *oracle* and the
   *engine* emit tie-safe, but the *baseline* rerank — the pg `<=>` rerank and the `--no-pg-rerank`
   numpy fallback — still break ties nondeterministically. On a slice with tie distances the
   baseline recall can vary run-to-run, the same reproducibility gap 064 fixed on the other two legs.
2. **SM-4 harness recall reducer is untested and un-extractable** (TESTS-02). The per-query recall
   math is inlined in `run_point`, so nothing is unit-testable — and this module already shipped one
   runtime bug (the `SET ... = %s` bind-param bug, fixed post-hoc). Extract a pure helper and test
   its boundary cases.
3. **Stock-dialect engine-loader branch is untested** (TESTS-01). `tools/wikidata_engine_load.py`
   branches on `dialect == "stock"` (pgvector `vector(dim)`, `[..]` literals, pgvector HNSW), but
   `tests/test_wikidata_engine_load.py` only exercises the default `"fork"` dialect. A typo in the
   stock branch merges green and only surfaces at 1M load time on the Spark.

## Current state

- `bench/wikidata_h2h.py:486` — pg rerank, no id tie-break:
  ```python
  f"ORDER BY embedding <=> %s::vector LIMIT %s",
  ```
- `bench/wikidata_h2h.py:510` — numpy fallback rerank, no id tie-break:
  ```python
  top = [int(x) for x in arr[np.argsort(-(emb[arr] @ qv))][:k]]
  ```
  (`arr` is the candidate-id array aligned with `emb[arr]`; confirm this by reading the surrounding
  lines before editing.)
- `bench/wikidata_sm4_seedless.py:76` `run_point`, with the recall math inlined at `:96`:
  ```python
  recalls.append(len(o & set(ids)) / max(1, len(o)))
  ```
- `tests/test_wikidata_engine_load.py` — `grep -c 'stock\|dialect'` returns 0 (fork-only coverage).
- No `tests/test_wikidata_sm4*.py` exists.

## Steps

1. **Baseline rerank tie-break** (`bench/wikidata_h2h.py`):
   - `:486` pg rerank: add a stable secondary key so ties are deterministic. The select returns ids
     ordered by distance; append `, id` (or the pk column) to the ORDER BY:
     `f"ORDER BY embedding <=> %s::vector, id LIMIT %s"` — **read the actual query** (what columns it
     selects, whether `id` is the pk name in the baseline pg table `wd_entity`) and use the correct
     column. If the reranked table's id column is not named `id`, use the right name.
   - `:510` numpy fallback: replace `np.argsort(-(emb[arr] @ qv))` with an id-tie-broken lexsort,
     mirroring plan 064's oracle fix:
     ```python
     sims = emb[arr] @ qv
     order = np.lexsort((arr, -sims))   # primary -sims (nearest first), tie: id asc
     top = [int(x) for x in arr[order][:k]]
     ```
     Confirm `arr` aligns with `sims` before trusting the lexsort (same footgun 064 called out).

2. **SM-4 testable recall seam** (`bench/wikidata_sm4_seedless.py`):
   - Extract the per-query recall computation into a module-level pure helper, e.g.:
     ```python
     def recall_at_k(ids: list[int], oracle_ids: list[int]) -> float:
         """Set-overlap recall of `ids` vs the exact `oracle_ids` (empty oracle -> 1.0 by
         convention: a query with no ground truth cannot lower recall). Mirrors bench.wiki_h2h
         grading semantics."""
         o = set(oracle_ids)
         if not o:
             return 1.0
         return len(o & set(ids)) / len(o)
     ```
     **Judgment call to make and document**: the current inline code uses `max(1, len(o))` as the
     denominator, which scores an empty-oracle query as recall **0** and folds it into the mean —
     understating a point's recall if any sampled query has an empty oracle. Decide the correct
     convention (empty oracle → 1.0 is the standard "no ground truth, nothing to miss" reading; the
     wiki_h2h grader's convention should be checked and matched — grep `bench/wiki_h2h.py` for how it
     handles empty oracle). Whichever you pick, make the helper and the call site agree, and note the
     behavior change (if any) in NOTES. Update `run_point:96` to call the helper.
   - Do NOT change the timing/DB logic — only extract the pure recall math.

3. **Stock-dialect loader tests** (`tests/test_wikidata_engine_load.py`):
   - Parametrize the existing SQL-emission tests over `dialect in ("fork", "stock")` (find the tests
     that call `iter_load_sql` / `sql_prologue` / `vec_literal` / `entity_copy_row` /
     `sql_hnsw_and_health` — they currently pass no dialect or `"fork"`). For `"stock"`, assert the
     pgvector-specific tokens appear: `vector(<dim>)` column type, `[..]` bracket vector literals (not
     `{..}`), `vector_l2_ops`, and the pgvector `CREATE EXTENSION vector` (not `vectordb`). Read the
     stock branch in `tools/wikidata_engine_load.py` to get the exact tokens to assert.

## Verification

1. `. .venv/bin/activate && make lint` → clean. `make test` → all pass (382 baseline + your new tests).
2. New/extended tests:
   - `tests/test_wikidata_sm4_seedless.py` (new): `recall_at_k` boundary cases — perfect overlap →
     1.0, disjoint → 0.0, partial → correct fraction, empty oracle → the chosen convention.
     `python -m pytest tests/test_wikidata_sm4_seedless.py -q` → pass.
   - `tests/test_wikidata_engine_load.py` (extended): stock-dialect emission asserts the pgvector
     tokens; fork-dialect still asserts the float8[]/vectordb tokens.
     `python -m pytest tests/test_wikidata_engine_load.py -q` → pass.
   - A determinism assertion for the numpy fallback rerank if a pure seam exists (optional — the
     lexsort mirrors 064's already-tested oracle path).
3. Greps: `grep -c 'np.argsort(-(emb\[arr\]' bench/wikidata_h2h.py` == 0 (fallback now lexsort);
   `grep -c 'def recall_at_k' bench/wikidata_sm4_seedless.py` == 1;
   `grep -c 'stock' tests/test_wikidata_engine_load.py` ≥ 1.

## Done criteria

- All three greps above satisfied.
- `make test` + `make lint` green; new SM-4 test file + extended loader tests pass.

## Out of scope / do NOT touch

- The engine emit / oracle tie-break (already done in 064).
- `publication_gate`, the live DB connection logic, committed `bench/results/` artifacts.
- The C operator, docs, advisor-plans/.
- Any file other than: `bench/wikidata_h2h.py`, `bench/wikidata_sm4_seedless.py`,
  `tests/test_wikidata_engine_load.py`, `tests/test_wikidata_sm4_seedless.py` (new).

## STOP conditions

- If the pg rerank query's id column isn't obvious (the select/table shape is ambiguous), STOP and
  report the query — a wrong tie-break column silently corrupts the baseline.
- If `arr`/`sims` alignment for the numpy lexsort can't be confirmed, STOP (same 064 footgun).
- If `make test` isn't green at baseline before your changes, STOP (environment).

## Maintenance note

Every result-capping/ranking site in the benchmark (oracle, engine emit, baseline pg rerank, baseline
numpy rerank) must now carry a deterministic id tie-break — this plan finishes the set 064 started.
The SM-4 recall seam means the harness's recall math is testable going forward; keep it pure.
