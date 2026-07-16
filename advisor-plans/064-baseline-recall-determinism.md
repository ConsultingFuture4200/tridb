# Plan 064: Make the Wikidata baseline recall reproducible (deterministic Neo4j subset + tie-break)

> **Executor instructions**: Follow step by step; run every verification and confirm the expected
> result. On a STOP condition, stop and report. Update this plan's row in `advisor-plans/README.md`
> when done.
>
> **Drift check (run first)**: `git diff --stat a41b0c7..HEAD -- bench/wikidata_h2h.py tests/test_wikidata_h2h.py`
> If changed, compare "Current state" to live code first; mismatch = STOP.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `a41b0c7`, 2026-07-15

## Why this matters

The Wikidata h2h harness produces the headline "TriDB is N× faster than the multi-store baseline at
matched recall." The **baseline recall is the denominator of that speedup**, and it is currently not
reproducible run-to-run:

1. The Neo4j traversal caps its result with `LIMIT {frontier}` but has **no `ORDER BY`**, so for any
   frontier-capped knob it returns an *arbitrary* subset of the typed reach — different subsets on
   different runs, depending on Neo4j's internal ordering.
2. The graded ids are captured from the **last timed repeat**, not a fixed warm-up, so which subset
   is graded varies with run timing.

A stranger re-running the harness (the roadmap D1 "stranger reproduces the number" exit criterion)
can get a different baseline operating point and thus a different speedup ratio. This is a
benchmark-integrity bug, not a crash — and benchmark integrity is the entire point of this repo's
`publication_gate` discipline.

A second, related determinism gap (fold into this plan): the exact **oracle** ranks with
`np.argsort` (not stable) and the engine ranks by SQL `ORDER BY <-> `; at tie distances the two
break ties differently, so "matched recall 1.0 by construction" is not actually tie-safe. Pin both
with an id tie-break.

## Current state

- `bench/wikidata_h2h.py:459-465` — the Neo4j traversal, no `ORDER BY`:
  ```python
  cy = (
      f"MATCH (a:{cfg.neo4j_node_label})-[:P{p}*1..{hops}]->"
      f"(b:{cfg.neo4j_node_label}) WHERE a.id IN $ids AND b <> a "
      f"RETURN DISTINCT b.id AS id LIMIT {frontier}"   # <-- arbitrary subset
  )
  ```
- The baseline run loop grades ids from the last timed repeat (around `wikidata_h2h.py:505-521` —
  read it: the graded `ids`/`top` is reassigned inside the timed-repeat loop, so the median latency
  and the graded id set come from different iterations).
- `bench/wikidata_h2h.py:264` — the oracle rank: `top = cand_arr[np.argsort(-sims)][:k]` (argsort is
  not stable, no id tie-break).
- `bench/wikidata_h2h.py:380` — the engine emit orders by `ORDER BY e.embedding <-> '{qv}'` (no
  `, e.id` tie-break).

## Steps

1. **Deterministic Neo4j subset** — add a stable ordering before the `LIMIT` in the Cypher at
   `wikidata_h2h.py:464`:
   ```python
   f"RETURN DISTINCT b.id AS id ORDER BY b.id LIMIT {frontier}"
   ```

2. **Grade from a fixed call, not the last timed run** — in the baseline run loop, capture the
   graded id set from the warm-up call (the TriDB side already does this: it grades the warm-up's
   ids and only *times* the repeats). Make the baseline symmetric: run one warm-up that captures
   `ids`, then run `--runs` timed repeats that capture only latency. Read the current loop
   structure around `wikidata_h2h.py:495-525` and restructure so the graded `ids` come from a call
   made once, before the timing loop. Do NOT let the timed loop overwrite the graded ids.

3. **Tie-break the oracle and the engine emit** so "matched by construction" is actually tie-safe:
   - Oracle (`wikidata_h2h.py:264`): replace `np.argsort(-sims)` with a lexical sort that breaks
     ties by id ascending:
     ```python
     order = np.lexsort((cand_arr, -sims))   # primary: -sims asc (nearest first); tie: id asc
     top = cand_arr[order][:k]
     ```
     (Confirm `cand_arr` is the array of candidate ids aligned with `sims` — read the surrounding
     lines to be sure the lexsort keys align.)
   - Engine emit (`wikidata_h2h.py:380`): append `, e.id` to the ORDER BY:
     `f"ORDER BY e.embedding <-> '{qv}', e.id LIMIT {k}"`.
   - Also apply the same `, e.id` tie-break to the fused filter-first emit if it has a separate
     `ORDER BY` (search the file for `ORDER BY e.embedding` and fix every site).

4. **Soften the overstated wording** — in the report/docstring where it says recall is "1.0 by
   construction", change to "measured, tie-break-pinned" (search `wikidata_h2h.py` and
   `docs/wikidata_spike_v0.2.0.md` / `docs/gate_b_spike_v0.1.0.md` for "by construction"). This is a
   1-line honesty correction; keep it factual.

## Verification

1. `. .venv/bin/activate && make lint` → clean.
2. `make test` → all pass (378+ as baseline; your new test adds to it).
3. Add a determinism test to `tests/test_wikidata_h2h.py` (follow the existing test style in that
   file — it constructs synthetic adj/types and calls the pure functions). Assert:
   - the oracle `compute_oracle` returns the **same** ordering across two calls on a slice
     containing a tie (two candidates at equal distance) — and that the tie is broken by id
     ascending;
   - (if a pure helper for the baseline id-selection exists or can be extracted) the selected
     subset is deterministic given a fixed reach set.
   Command: `python -m pytest tests/test_wikidata_h2h.py -q` → all pass.
4. Grep confirms the Cypher fix: `grep -c 'ORDER BY b.id LIMIT' bench/wikidata_h2h.py` ≥ 1, and
   `grep -c 'RETURN DISTINCT b.id AS id LIMIT' bench/wikidata_h2h.py` == 0.

## Done criteria

- `grep 'RETURN DISTINCT b.id AS id ORDER BY b.id LIMIT' bench/wikidata_h2h.py` matches.
- `grep -c 'np.argsort(-sims)' bench/wikidata_h2h.py` == 0 (replaced by lexsort).
- `make test` and `make lint` green; the new determinism test passes.

## Out of scope / do NOT touch

- The live Neo4j/pg/Milvus connection code (`_connect_baseline`, `run_baseline` I/O) beyond the
  Cypher string and the grade-from-warm-up restructure.
- `publication_gate` itself (it's reused verbatim from `wiki_h2h.py`).
- The 1M measured artifacts already committed under `bench/results/` — do NOT re-run or edit them;
  they were measured on normalized embeddings where the tie/ordering issue did not bite, and the
  fix is forward-looking for reproducibility.

## STOP conditions

- If `np.lexsort` key alignment is ambiguous (you cannot confirm `cand_arr` aligns with `sims`),
  STOP and report — a wrong lexsort silently corrupts the oracle, worse than the tie issue.
- If restructuring the baseline loop to grade-from-warm-up would change the timed measurement
  (e.g. the warm-up is currently what's timed), STOP and report the loop structure — the reviewer
  will confirm which call should be graded vs timed.

## Maintenance note

Any new baseline knob that caps results (LIMIT/frontier/ef) must carry a deterministic tie-break, or
this reproducibility bug returns. The invariant to preserve: **graded ids come from a fixed call;
every capped result set is deterministically ordered.**
