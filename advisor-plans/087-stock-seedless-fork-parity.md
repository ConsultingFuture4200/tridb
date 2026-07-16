# Plan 087: Align stock seedless `tjs_open` semantics with the fork (bridge cap, seed window, stream accounting)

> **Executor instructions**: Follow step by step; run every verification. This plan changes the
> stock operator's SEEDLESS phase semantics only — the filter-first path must remain byte-identical
> (plan 071's parity harness is the guard). Skip the advisor index update. Do not touch the fork
> patch.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- src/tjs_pg/tjs_pg.c test/tjs_pg_test.sql scripts/patches/tridb_tjs_open_operator.patch`
> Plans 073 (m_seeds bounds) and 074 (counter honesty) land before this one and touch the same file;
> re-read the seedless block after their merges before editing.

## Status

- **Priority**: P1
- **Effort**: S–M
- **Risk**: MED (changes seedless ranking composition)
- **Depends on**: 073, 074 (same-file serialization); coordinate with 077 (which later rewrites the
  graph leg but must inherit these semantics)
- **Category**: correctness / fork-parity
- **Planned at**: commit `a780b46`, 2026-07-16

## Why this matters

The stock operator's seedless phase claims fork parity in its own comments ("FINALIZE (fork
parity)") but diverges from the fork in three measurable ways. On bridge-dense corpora the stock
result set can become 100% bridges — silently deleting the vector modality — and its seeds/recall
behavior differs from the fork under relaxed-order HNSW. ADR-0019 holds the fork as the reference
implementation; these are drift bugs, not design choices.

## Current state (all verified against live code)

Three divergences, in `src/tjs_pg/tjs_pg.c` vs `scripts/patches/tridb_tjs_open_operator.patch`:

1. **Bridge share is uncapped in finalize.** Stock `tjs_pg.c:656-681`: finalize sorts
   `bridge_topk` and copies bridges into `final_items` while `n_final < k` — bridges can take ALL k
   slots. The fork patch caps the reserved bridge share:
   ```c
   // fork patch (added lines ~511-517):
   //    modality on dense graphs, so cap the reserved bridge share at k/2.
   uint32_t bridge_cap = k / 2;
   if (bridge_cap == 0 && !bridges_v.empty()) bridge_cap = 1;  // min 1 when any bridge exists
   ```
   The fork comment is explicit: "bridges-take-all would silently delete the vector modality on
   dense graphs — hence the k/2 cap."

2. **Seed selection: first-m vs nearest-in-window.** Stock `tjs_pg.c:548-551` seeds from the FIRST
   `m_seeds` stream hits:
   ```c
   if (seeds_taken < m_seeds)
   {
       reach_add_from_seed(reach, cand, hops, edge_type);
       seeds_taken++;
   }
   ```
   The fork buffers a `seed_window = m_seeds * 8` (floor `m_seeds + 32`) prefix and selects the
   `m_seeds` NEAREST within it (patch lines ~655-685), which is materially different under
   pgvector's relaxed-order iterative scan where early stream order is not distance order.

3. **Bridges bypass the vector top-k and the drop counter.** Stock `tjs_pg.c:553-559`: a candidate
   that is in `reach` is offered to `bridge_topk` and then `continue`s — it is never offered to the
   vector `topk` and "bridges never touch the drop counter". The fork admits every streamed
   candidate to the vector queue and counts termination progress uniformly, so early-stop dynamics
   diverge on bridge-dense prefixes (stock can defer `term_cond` firing arbitrarily).

Plan 077 later replaces the full-reach graph leg; it depends on an honest semantic baseline. Fixing
parity semantics FIRST (small, local edits) keeps 077's diff reviewable.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Stock PG17 | `bash scripts/pg17_graph_test.sh tridb/pg17-unfork:dev test/tjs_pg_test.sql` | `ALL PASS`, exit 0 |
| Stock PG16 | same script with the PG16-built image (`docker build --build-arg PG_MAJOR=16 -t tridb/pg16-unfork:dev scripts/pg17/`) | `ALL PASS`, exit 0 |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `src/tjs_pg/tjs_pg.c` (seedless phase only)
- `test/tjs_pg_test.sql`

**Out of scope**:
- The filter-first path (`m_seeds = 0`) — zero behavior change allowed there.
- The fork patch (`scripts/patches/`) — reference only, never edited.
- The graph-leg materialization defect (plan 077).
- Counter/metric semantics beyond what plan 074 defined (extend, don't redesign).

## Git workflow

Use assigned `dustin/dev-NNNN`. Suggested commit: `fix(tjs): fork-parity seedless semantics`.

## Steps

### Step 1: Lock the divergences with failing tests

Extend `test/tjs_pg_test.sql` with a deterministic bridge-dense fixture: a graph where the
reachable set is large and near the query, so bridges would fill all k slots today. Assert
(a) at most `ceil(k/2)` of the returned ids are bridge-only (not vector-stream winners) when
vector candidates exist — mirror the fork's `bridge_cap` rule including the min-1-when-any rule;
(b) a seed-selection case where the first stream hit is NOT the nearest candidate in the window and
the fork rule picks a different (nearer) seed set — assert the resulting reach/bridge membership
matches nearest-in-window selection.

**Verify (negative control)**: both new assertions FAIL against the current stock build. If either
passes pre-fix, STOP — the fixture is not discriminating.

### Step 2: Cap the bridge share in finalize

Implement the fork rule exactly: `bridge_cap = k / 2`, min 1 when any bridge exists; bridges fill
at most `bridge_cap` slots, vector winners (dedup'd) fill the rest, merged output still ascending
by distance. Keep plan 074's counters consistent (`tjs_bridges_injected` counts offers that landed
in the FINAL set, or document what it counts — match the fork's meaning; state which in the SQL
comment).

**Verify**: Step 1(a) passes; the full suite is green on PG17.

### Step 3: Nearest-in-window seed selection

Buffer the first `seed_window = m_seeds * 8` (floor `m_seeds + 32`) streamed candidates (id + dist)
and select the `m_seeds` nearest as seeds, then proceed. Bound the buffer by `seed_window` (it is
already bounded by plan 073's `m_seeds ≤ 10000` guard — do not add a second knob). Candidates
buffered for seed selection must still be offered to the vector top-k exactly once (no double
count, no loss).

**Verify**: Step 1(b) passes; existing positive-seed tests still pass.

### Step 4: Unify stream accounting

Offer every streamed candidate (bridge or not) to the vector `topk` and let the drop counter see
the uniform improve-or-drop outcome, matching the fork; a reach member is ADDITIONALLY offered to
`bridge_topk`. If reading the fork patch contradicts this (i.e. the fork also skips the drop
counter for bridges), STOP and report the exact fork hunk — do not guess; the fork is the contract.

**Verify**: full `test/tjs_pg_test.sql` on PG17 AND PG16 prints `ALL PASS`; term_cond tests from
plan 074 still pass unchanged.

## Test plan

Bridge-dense finalize cap (boundary: k=1, k=2, k odd), nearest-in-window vs first-m seed
divergence, uniform drop-counter accounting under a bridge-heavy prefix, filter-first (`m_seeds=0`)
unchanged, and the whole existing suite on stock PG16 + PG17. Host `make test && make lint`.

## Done criteria

- [ ] Bridge share in the final k is capped exactly as the fork (k/2, min 1 when any bridge).
- [ ] Seeds are the m nearest within the fork's window rule, not the first m stream hits.
- [ ] Every streamed candidate participates in vector top-k + drop-counter accounting.
- [ ] Filter-first results are byte-identical pre/post (plan 071 harness, if merged, stays green).
- [ ] Stock PG16 + PG17 suites `ALL PASS`; host tests/lint green; only in-scope files changed.

## STOP conditions

- The fork patch's actual semantics differ from any excerpt above (read the patch first; the patch
  is authoritative over this plan's paraphrase).
- Plan 073/074 changes have not merged and the same-file edit would conflict.
- Matching the fork requires changing the filter-first path or the pinned SQL surface.
- A parity test can only be written flaky (relaxed-order nondeterminism) — report; do not commit a
  flaky test.

## Maintenance notes

When plan 077 replaces the graph leg, these three semantics (bridge cap, seed window, uniform
accounting) are contract items its ADR must carry forward. The eventual seedless differential
harness (071's follow-up) should assert them cross-engine with a recall band.
