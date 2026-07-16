# ADR-0020: Stock TJS graph leg — bounded pull-based traversal replaces full-reach materialization

Status: Accepted (2026-07-16). Maintainer approved all five open decisions (§7) as proposed;
plan 077 Steps 3-5 implement this contract.

## Context

The stock `tjs_pg` operator (ADR-0019) violates TR-1 (spec §8, golden rule 1) on its graph
leg. Both physical paths obtain graph reachability by calling
`graph_store.gph_traverse_bfs`, which runs the ENTIRE bounded-depth BFS at SRF Open and
materializes the complete reach array before serving the first row
(`src/graph_store/graph_am.c`, `gph_traverse_bfs`, "Result is materialized once at Open").
Filter-first additionally invokes it in a FROM-clause `FunctionScan` inside the fused SPI
statement — the position `graphstore.h` explicitly warns forfeits early termination — and
seedless `reach_add_from_seed` (`src/tjs_pg/tjs_pg.c`) copies every returned id into a hash
per seed.

Measured evidence (`test/tjs_pg_tr1_test.sql`, the plan 077 Step 1 negative control, stock
PG17 via `scripts/pg17_graph_test.sh`, edge-step probe = `graph_store.gph_visits()` deltas):

| probe | reach | graph work observed | TR-1 requires |
|---|---:|---:|---|
| `gph_traverse_bfs(hub, 2, t) LIMIT 1` (target-list) | 1200 | **1200 edge-steps** | < 1200 |
| `tjs_open` filter-first, `k=1` | 1200 | **1200 edge-steps** | ≪ reach |
| `tjs_open` seedless, `m_seeds=1, k=5` | 1200 | **1200 edge-steps** + 1201 candidates examined (phase 3b drains the reach by id) | bounded |
| filter-first `k=1` on a 2× graph (reach 2400) | 2400 | **2400 edge-steps (2.00×)** | independent of graph size |
| contrast: `gph_traverse_typed(hub, t, 0, -1) LIMIT 1` | 1500 out-edges | **1 edge-step** | — (proof the AM layer already pulls) |

Work at `k=1` equals the full reachable set and scales linearly with graph size. The
single-hop AM iterator (DEV-1165 engine) is already pull-based — the defect is confined to
the multi-hop helper and the operator gluing on top of it.

## Decision

Replace the operator's whole-BFS reach acquisition with a **bounded pull-based multi-hop
traversal**, under the following semantic contract. This ADR ratifies the contract; the
implementation is plan 077 Steps 3–5 and MUST NOT begin before this ADR is Accepted.

### 1. The pull iterator (C level)

A multi-hop traversal iterator over the existing single-hop `gs_open/gs_getnext/gs_close`
engine — strict Open/Next/Close:

- **Open** allocates only: an empty visited hash, an empty frontier queue, and fixed
  bookkeeping. It performs ZERO edge-steps and never walks the graph.
- **Next** emits at most one newly-reached vertex per call, advancing the underlying
  single-hop scans incrementally (depth-ordered BFS). Each call checks
  `CHECK_FOR_INTERRUPTS()` and the work budget (§2).
- **Close** releases relation, scan, and memory state on normal completion AND early
  abandonment (LIMIT/k reached, term_cond, error).
- No complete reachable-set array, no tuplestore of reach, no FROM-clause graph SRF
  anywhere in the operator path.

### 2. Work/state bound — one knob bounds both

- **`tjs.graph_work_budget`** (GUC, per-backend; a documented operator setting, NOT a new
  query-language parameter — the ADR-0008 pinned surface is unchanged). Units:
  **edge-steps**, the same unit `gph_visits()` counts. Proposed default 65536, range
  128..2^30 (default value is an open decision, §7).
- The graph leg of ONE `tjs_open` call performs at most `tjs.graph_work_budget` edge-steps
  TOTAL (shared across all seeds in seedless mode, and inclusive of the phase 3b bridge
  drain bound, which is |reach| ≤ first-visits ≤ edge-steps).
- **State bound is implied by the work bound**: visited/frontier entries grow only on
  first-visit, and first-visits ≤ edge-steps, so graph-leg memory is O(min(budget, |V|)) —
  an explicit contract independent of |V| and |E|.

### 3. Exactness vs censoring (honest capping)

- If the traversal exhausts the reach within budget, the result is **exact** and
  **byte-identical to the pre-077 published contract** — the plan 071 parity harness
  (11/11) and `test/tjs_pg_test.sql` must pass unchanged with any budget ≥ the test
  graphs' reach. This is the acceptance criterion that the contract is preserved.
- If the budget halts the traversal first, the result is **censored, never silently
  "exact"**: computed over the deterministic traversal prefix (§5), and disclosed via §4.
  No error is raised; a capped answer with a flag is the contract (mirrors plan 074's
  honesty rule: disclosed, not manufactured).

### 4. Metric/termination API (plan 074 carried forward)

- `tjs_open_termination_reason()` values are UNCHANGED
  (`filter_first | term_cond | stream_end_unknown`) — the graph cap is orthogonal to how
  the candidate stream ended, so it is not a fourth reason.
- **New** `tjs_open_graph_censored() -> boolean`: true iff the last call's graph leg hit
  `tjs.graph_work_budget` before exhausting the reach. A REAL boolean, never NULL — unlike
  pgvector's stream end, we own this traversal, so the signal is observable (the E3.3
  censoring rule applies only where the signal genuinely does not exist).
- **New** `tjs_open_graph_examined() -> bigint`: edge-steps consumed by the last call.
- `tjs_open_candidates_examined()` keeps its plan 074 meaning (filter-first: qualifying
  rows examined pre-top-k; when uncensored this equals today's full qualifying count).

### 5. Deterministic order and tie-breaks

- Traversal order is deterministic: depth ascending, then adjacency-page slot order
  (insertion order) within a depth — so a budget-truncated prefix is reproducible for a
  given store layout, and the censored result is stable across runs.
- Final emission: ascending distance, ties broken by ascending id (the current qsorts are
  tie-unstable; the contract pins the tie-break). Bridge-slot occupants keep their slot on
  dedup, as today.

### 6. Path-specific contracts

**Filter-first (source-anchored, `src IS NOT NULL`)** — result = top-k by the index's own
distance metric among filter-passing traversed-reach members (excluding `src`). The fused
single-SQL-statement realization is replaced by: pull traversal (§1) → per-vertex filter
probe → distance recompute (the vector-first machinery, reused) → bounded top-k of k.
Uncensored ⇒ identical result set to today. Memory: O(k + min(budget, |V|)).

**Seedless (`src IS NULL, m_seeds > 0`)** — plan 087's fork-parity semantics are carried
forward VERBATIM as contract items, on top of the bounded traversal:

1. `seed_window = max(m_seeds*8, m_seeds+32)`; buffer the first `seed_window`
   filter-passing stream candidates; seeds are the `m_seeds` NEAREST within the window
   (not first-emitted).
2. Reach = union of the seeds' `hops`-bounded typed out-reach, seeds included; probes
   draw on the SHARED `tjs.graph_work_budget`, consumed nearest-seed-first.
3. Bridge share cap at finalize: `floor(k/2)`, min 1 when any bridge exists — bridges are
   guaranteed but never take all of k.
4. Uniform drop accounting: every streamed candidate competes for the vector top-k and
   the TR-1 drop counter sees the uniform improve-or-drop outcome; the seed window is
   exempt; bridges get NO term_cond exemption.
5. Phase 3b direct fetch of never-streamed reach members (filter respected, each offer
   counted in `candidates_examined`) — now bounded because |reach| is budget-bounded.

Graph SCORING stays reachability-membership (fork parity — the published contract the 071
harness pins). ADR-0012's bounded local-push design is adopted for what it bounds —
frontier-bounded work independent of |V|/|E|, with `tjs.graph_work_budget` as the
`r_max`-analogue knob — while its PPR-graded reserves ranking remains the documented
follow-on that rides this same iterator (reserves would replace membership; same budget).
Jumping straight to PPR would change ranked results and break parity; it needs its own
recall-curve gate (ADR-0012 addendum) and is out of scope here (open decision, §7).

### 7. Invariant mapping (plan 077's six, all satisfied)

| # | Invariant | Where satisfied |
|---|---|---|
| 1 | Open allocates fixed/budget-bounded state, never walks the graph | §1 Open |
| 2 | Next advances incrementally, checks termination/cancellation | §1 Next |
| 3 | Close releases everything on normal and early abandon | §1 Close |
| 4 | No reach array/tuplestore, no FROM-clause graph SRF | §1, §6 |
| 5 | Work/memory bound independent of \|V\|/\|E\|; capped = censored, disclosed | §2, §3, §4 |
| 6 | Topology stays the native graph AM; no edge tables/joins, no sidecar | §1 (gs_* engine); golden rules 2–3 untouched |

`gph_traverse_bfs` itself remains available as a TEST/ORACLE helper (materializing,
documented as such, banned from the operator path); whether to also rewrite or deprecate
it is left open (§Open decisions).

## Spec addendum (landed: `spec/tridb_spec_v0.1.0.md` Addendum A3, Step 0)

> ## Addendum A3 (2026-07-16) — TR-1 graph-work bound for the stock operator (plan 077)
>
> §8's Open/Next/Close + early-termination invariant is made mechanical for the stock
> `tjs_pg` operator: the graph leg of one `tjs_open` call performs at most
> `tjs.graph_work_budget` edge-steps (default 65536) and holds graph-leg state bounded by
> the same budget, independent of |V|/|E|. Reach acquisition is a pull-based multi-hop
> iterator over the native AM; no complete reachable set is ever materialized. A
> budget-capped call returns a deterministic-prefix result and MUST disclose it:
> `tjs_open_graph_censored() = true`, with `tjs_open_graph_examined()` reporting
> edge-steps; an uncensored result is byte-identical to the pre-077 contract (071 parity
> harness green). Seedless retains plan 087 fork-parity semantics: seed_window =
> max(m_seeds*8, m_seeds+32) with nearest-in-window seed selection, floor(k/2)-min-1
> bridge cap, uniform drop accounting. Benchmarks/harnesses MUST report the censor flag
> next to any headline (a capped run is a different operating point, not a win).

## Consequences

- `test/tjs_pg_tr1_test.sql` flips from the Step 1 negative control (expected red today)
  to a green gate: k=1 graph work strictly below full reach, flat in graph size up to the
  budget.
- Filter-first's `count(*) OVER ()` qualifying-count trick disappears with the fused
  statement; `candidates_examined` semantics are preserved by counting filter probes
  (equal when uncensored).
- The GX10 fork operator is untouched; this contract binds the stock extension only. No
  fork/GX10 build claims are made off-target.
- The bounded iterator becomes the substrate ADR-0012's PPR-graded follow-on plugs into.

## Alternatives rejected

- **Capped materializer** ("BFS but stop at N, then copy"): still a blocking Open — k=1
  pays the full cap every call; invariant 1 fails. This is the "materializer under a
  different name" the plan forbids.
- **LIMIT pushdown into the fused SQL statement**: LIMIT applies after ORDER BY; the
  FROM-clause `FunctionScan` still materializes the whole SRF result first
  (`graphstore.h` warning). No bound reaches the BFS.
- **Relational recursive-CTE traversal**: golden rule 3 — topology is never relational
  joins.
- **Tuplestore spill of the reach**: still blocking, adds I/O; invariant 4 fails.
- **PPR-graded scoring now**: changes ranked results, breaks 071 fork parity without its
  own recall gate; deferred (see §6/§7).

## Resolved decisions (maintainer, 2026-07-16)

1. **Default `tjs.graph_work_budget` value**: **65536** edge-steps, range **128..2^30**, as
   proposed.
2. **Seedless budget sharing**: **one shared pool consumed nearest-seed-first**, as proposed
   (not an equal per-seed slice).
3. **Scoring**: **reachability-membership now** (byte-identical uncensored results; the plan
   071 parity harness is the acceptance gate). ADR-0012's PPR-graded reserves remain the
   documented follow-on that rides this same iterator — not adopted in Step 3.
4. **Fate of `gph_traverse_bfs`**: **kept as the documented materializing TEST/ORACLE helper**,
   banned from the operator path by a static guard (Step 5) — not rewritten, not deprecated.
5. **Censor surfacing in `graph_query`**: **deferred** — the lowering (ADR-0008/plan 075) is
   not changed to expose `tjs_open_graph_censored()` alongside `last_join_order()` in this
   plan. `tjs_open_graph_censored()` remains directly callable after a `graph_query()` call in
   the same backend (it is backend-local state, not lowering-level plumbing), and Step 4's
   test suite exercises exactly that call sequence.
