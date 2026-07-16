# Plan 065: Fix the orphaned gate-env variable + small harness safety guards

> **Executor instructions**: Follow step by step; run every verification. STOP conditions halt you.
> Update this plan's row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat a41b0c7..HEAD -- bench/wikidata_h2h.py tools/wikidata_engine_load.py`
> If changed, compare "Current state" to live code; mismatch = STOP.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug (tech-debt-adjacent)
- **Planned at**: commit `a41b0c7`, 2026-07-15

## Why this matters

The engine loader computes the edge count the honesty gate needs and writes it to its load-manifest
under the key **`WD_ENGINE_EDGES`**. But the gate reads the environment variable
**`WH_ENGINE_EDGES`** (the `WH_` prefix inherited verbatim from the older `wiki_h2h.py`). So the
value the loader produces is **orphaned** — read by nothing — and a user who follows the loader's
own output to satisfy the gate exports the wrong variable and stays blocked. (This is why the D2
Gate-B run scripts had to hand-export `WH_ENGINE_EDGES`.) The mixed `WD_`/`WH_` namespace inside one
harness is a latent trap for every gate key.

Bundled into this small plan: two 1-line safety guards found in the same file — a divide-by-zero in
the report and a `numeric-id` edge fallback that skips the item-type check.

## Current state

- `tools/wikidata_engine_load.py:618` — writes the orphaned key:
  ```python
  "gate_env": {"WD_ENGINE_EDGES": engine.get("edges", stats.get("edges_kept"))},
  ```
  and its docstring `:25-27` wrongly conflates `WD_ENGINE_EDGES` with the `WH_ENGINE_EDGES` the gate
  needs.
- `bench/wikidata_h2h.py:586-606` (`oracle_meta_from_env`) reads the `WH_` namespace:
  ```python
  "engine_edges": os.environ.get("WH_ENGINE_EDGES"),
  "neo4j_edges": os.environ.get("WH_NEO4J_EDGES", ...),
  "hnsw_healthy_builds": os.environ.get("WH_HNSW_HEALTHY_BUILDS"),
  "hnsw_total_builds": os.environ.get("WH_HNSW_TOTAL_BUILDS"),
  ```
- `bench/wikidata_h2h.py:648,658` — the report divides by TriDB latency with no zero guard:
  ```python
  t_lat = tp[1]["median_latency_ms"]
  ...
  L.append(f"- **speedup: {b_lat / t_lat:.2f}×**")   # ZeroDivisionError if t_lat == 0.0
  ```
- `tools/wikidata_ingest.py:123` — the `numeric-id` fallback bypasses the item-type check:
  ```python
  return qid_to_int(val.get("id","")) if val.get("id") else val.get("numeric-id")
  ```
  The `id` path routes through `qid_to_int` (which rejects P/L/non-item targets); the `numeric-id`
  fallback returns the raw int with no Q-vs-P/L discrimination — contradicting the same code's stated
  intent ("prefer id so a non-item is rejected").

## Steps

1. **Gate-env namespace** — make the harness read the value the loader actually produces. Preferred
   fix (single source of truth): have `oracle_meta_from_env` **also** accept the `WD_` names, and
   have the loader's manifest be loadable directly. Minimal, low-risk version:
   - In `bench/wikidata_h2h.py` `oracle_meta_from_env`, change each `os.environ.get("WH_X")` to
     `os.environ.get("WH_X") or os.environ.get("WD_X")` for `ENGINE_EDGES`, `NEO4J_EDGES`,
     `HNSW_HEALTHY_BUILDS`, `HNSW_TOTAL_BUILDS`. This keeps existing `WH_`-based scripts working AND
     lets the `WD_`-named manifest values work.
   - Fix the loader docstring `tools/wikidata_engine_load.py:25-27` to state the value is read as
     `WD_ENGINE_EDGES` (matching what it writes), not `WH_ENGINE_EDGES`.
   - Also emit the baseline loader's edge count under the matching name so both sides align (check
     `tools/wikidata_baseline_load.py` for its `gate_env` key — it writes `WD_NEO4J_EDGES` per prior
     notes; confirm the harness now reads it via the `or WD_` fallback).

2. **Zero-guard the report** — `bench/wikidata_h2h.py:648`, before the division:
   ```python
   t_lat = tp[1]["median_latency_ms"]
   if not t_lat or t_lat <= 0:
       return "\n".join(L + ["> **COMPARISON INVALID — TriDB latency is zero/undefined.**"]), \
              ["tridb median_latency_ms is zero or missing"]
   ```
   (Match the existing early-return-with-blockers shape in `render_report` — read how it returns
   `(markdown, blockers)` on the gate-blocked path and mirror it exactly.)

3. **Item-type check on the edge fallback** — `tools/wikidata_ingest.py:123`: drop the raw
   `numeric-id` fallback, or gate it behind the item check. Simplest correct form:
   ```python
   # numeric-id without a Q-prefixed id cannot be confirmed an item target -> reject
   qid = val.get("id")
   return qid_to_int(qid) if qid else None
   ```
   (Only do this if a test confirms real dumps always carry `id` for item targets — see step 4;
   the existing tests in `tests/test_wikidata_ingest.py` exercise `entity_edges`.)

## Verification

1. `. .venv/bin/activate && make lint` → clean; `make test` → all pass.
2. Add/extend tests in `tests/test_wikidata_h2h.py`:
   - `oracle_meta_from_env` reads `WD_ENGINE_EDGES` when only that is set (monkeypatch env), and
     still reads `WH_ENGINE_EDGES` when that is set (back-compat).
   - `render_report` on a graded dict with `median_latency_ms == 0.0` returns a blocker, not a raise
     (wrap in a call and assert no exception + the blocker string present).
   Command: `python -m pytest tests/test_wikidata_h2h.py -q` → pass.
3. Extend `tests/test_wikidata_ingest.py`: an edge datavalue with only `numeric-id` (no `id`) is
   dropped (returns None / not emitted). `python -m pytest tests/test_wikidata_ingest.py -q` → pass.

## Done criteria

- `grep -c "WD_ENGINE_EDGES" bench/wikidata_h2h.py` ≥ 1 (harness now reads the WD name).
- `make test` + `make lint` green; the three new/extended tests pass.
- `grep -c "val.get(.numeric-id.)" tools/wikidata_ingest.py` == 0 (fallback removed) — OR the
  fallback is gated behind the item check (reviewer's choice per step 3's guard).

## Out of scope / do NOT touch

- The gate logic itself (`publication_gate`) — only how `oracle_meta_from_env` sources the values.
- Re-running any committed 1M artifacts under `bench/results/`.
- The `WH_` names used by the older `bench/wiki_h2h.py` (fork harness) — leave those; the fix is
  additive (`WH_ or WD_`).

## STOP conditions

- If real Wikidata dump fixtures show item edges that carry `numeric-id` **without** `id`, STOP —
  removing the fallback would drop valid edges; report and leave step 3 unapplied (do steps 1–2).
- If the drift check shows the gate-env names already unified, STOP and report.

## Maintenance note

The invariant: **the loader writes and the harness reads the same env-var name.** A reviewer adding
a new gate key must add it under one namespace and confirm both sides use it. Consider a follow-up
(not this plan) to load the loader's `gate_env` manifest block directly instead of via environment
variables, eliminating the namespace surface entirely.
