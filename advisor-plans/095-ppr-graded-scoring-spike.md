# Plan 095: SPIKE — PPR-graded graph scoring (ADR-0012 reserves) on the bounded iterator, behind a recall gate

> **Executor instructions**: This is a SPIKE with a measured verdict, not a default-on ship.
> Deliverables: (1) opt-in graded scoring in the stock operator, default OFF and byte-inert when
> off; (2) a measured membership-vs-PPR recall comparison on the local HotpotQA corpus; (3) a
> GO/NO-GO recommendation appended to ADR-0012 as a dated addendum. The maintainer decides default
> adoption — you never flip the default. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat 6de2e30..HEAD -- src/tjs_pg/ test/ bench/ docs/decisions/0012-tjs-open-multiseed-retrieval.md Makefile`

## Status

- **Priority**: P2 (direction; the retrieval-quality bet)
- **Effort**: M–L
- **Risk**: MED (opt-in only; default path must be provably inert)
- **Depends on**: 077 (merged — `gph_traverse_bounded` is the substrate), 071/087 (parity guards)
- **Category**: direction / spike / perf-quality
- **Planned at**: commit `6de2e30`, 2026-07-17

## Why this matters

Graph scoring today is binary reachability-membership (ADR-0020 decision 3): the graph leg gates
and guarantees, but never *ranks*. ADR-0012 §1 specifies the graded alternative — bounded
forward-push Personalized PageRank (Andersen–Chung–Lang FOCS'06) whose reserves vector is the graph
score, computed incrementally, never sorted-to-emit. The host reference (plan 007,
`bench/tjs_open_ref.py`, real HotpotQA) measured FR-fused recall@5 **0.937 vs the blocking
oracle's 0.883** — graded fusion beat both vector-only and the oracle while examining ~0.71% of
the corpus. The 077 iterator is exactly the substrate this rides. What's missing is the in-engine
implementation and an honest recall gate before anyone considers making it default.

## Current state (verified)

- `src/tjs_pg/tjs_pg.c` seedless path: reach via SPI-cursor pulls of
  `graph_store.gph_traverse_bounded(seed, hops, type_id, budget)` (plan 077), shared
  nearest-seed-first budget; membership drives bridge injection (floor(k/2)-min-1 cap, plan 087);
  final rank is pure vector distance with id tie-break.
- `docs/decisions/0012-tjs-open-multiseed-retrieval.md:107+`: bounded forward-push PPR — push mass
  from seeds, reserves accumulate per visited vertex; the reserves ARE the graph score; TR-1
  compliance = incremental read, no global sort barrier.
- Host reference + data are LOCAL: `bench/tjs_open_ref.py`, `data/hotpot/{manifest.json,
  corpus_emb.npy,dev_slice.json}` (1490 paragraphs, 745 edges, 150 questions, BGE-768 — plan 007);
  grading machinery: `bench/graphrag_report.py` (`evidence_scores`), `bench/graphrag_live_report.py`
  (plan 085).
- Parity guards that MUST stay green with the feature OFF: `scripts/tjs_parity_test.sh` (11/11),
  `test/tjs_pg_test.sql`, `test/tjs_pg_tr1_test.sql` (all in `STOCK_TESTS`).

## The spike contract

1. **Opt-in switch**: GUC `tjs.graph_scoring` = `membership` (default) | `ppr`. Registered in
   tjs_pg `_PG_init`. NOT a query-language parameter (pinned surface unchanged).
2. **Default inertness is a hard gate**: with `membership`, results are byte-identical — the
   whole existing stock gate set passes unchanged, and the parity harness stays 11/11.
3. **PPR mode (seedless path only for this spike)**: forward-push over the SAME bounded traversal
   (each push step counts against `tjs.graph_work_budget`; censoring semantics and disclosure per
   ADR-0020 unchanged). Reserves accumulate per reached vertex; seeds weighted by their vector
   proximity (nearest-in-window seeds keep plan 087 selection). Fusion: replace the binary
   bridge-guarantee ranking input with a documented fusion of vector distance and reserve score —
   implement the FR (Fagin-rank) composition `bench/tjs_open_ref.py` measured as the winner, or
   document precisely why you deviated. Bridge-cap (k/2 min 1) still applies to graph-sourced
   candidates. Deterministic tie-breaks (score, then id).
4. **TR-1**: no global sort of reserves before emission; the push frontier and reserve map are
   budget-bounded exactly like the membership reach (state O(min(budget,|V|))). Materializing all
   reserves then sorting once at finalize over ≤k+bridge candidates is fine (that is the existing
   bounded top-k pattern); materializing/sorting the full reserve map is NOT.
5. **Filter-first**: out of scope for the spike (source-anchored membership is the pinned Gate-A
   semantics); PPR there is a later ADR if the seedless gate says GO.

## The recall gate (the spike's actual product)

Load the HotpotQA corpus into the STOCK image (entities + `vector(768)` embeddings + typed edges
from `data/hotpot/manifest.json` — write a small loader or adapt the existing corpus-emit
machinery; stock dialect) and run the 150 questions through seedless `tjs_open` twice: `membership`
vs `ppr`, at k=5 and k=10, sweeping `term_cond` over at least {8, 32, 128} at fixed budget. Grade
evidence recall@k / joint recall with `bench/graphrag_report.evidence_scores` via the plan-085
grader (or its reducers directly). Report per-point: recall, examined, graph_examined,
censored-fraction. The comparison table + curve goes in the ADR addendum. Include the host
reference's numbers as context, clearly labeled host-vs-engine.

**Honesty bars**: same corpus, same queries, same budget both modes; report censored fractions
next to every number; if PPR loses or ties, that IS the verdict — write NO-GO/INCONCLUSIVE with
the table; never tune membership down to make PPR win.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Stock gates (feature off) | `make stock-graph-test PG_MAJOR=17` + `bash scripts/tjs_parity_test.sh` | all green / 11-11 |
| PG16 focused | `bash scripts/pg17_graph_test.sh tridb/pg16-unfork:dev test/tjs_pg_test.sql` | ALL PASS |
| New suite | `bash scripts/pg17_graph_test.sh tridb/pg17-unfork:dev test/tjs_ppr_test.sql` | ALL PASS |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**: `src/tjs_pg/tjs_pg.c`, `src/tjs_pg/tjs_pg--0.1.0.sql` (GUC docs/comments),
`test/tjs_ppr_test.sql` (create; deterministic PPR-mode unit fixture + default-inertness assert),
`Makefile` (add the new suite to `STOCK_TESTS`), `bench/` (the HotpotQA stock loader + comparison
runner, new file(s)), `docs/decisions/0012-tjs-open-multiseed-retrieval.md` (dated addendum with
the measured table + GO/NO-GO recommendation).

**Out of scope**: flipping any default; filter-first scoring; `graph_store` C (the iterator is
sufficient — if you believe it isn't, STOP and report why); fork patches; committed benchmark
artifacts; the pinned SQL surface.

## Git workflow

Branch `advisor/095-ppr-spike`. Split commits: `feat(tjs): opt-in ppr graded scoring`,
`bench(ppr): hotpot membership-vs-ppr recall gate`, `docs(adr): 0012 addendum — spike verdict`.

## Steps

### Step 1: Deterministic engine fixture first

`test/tjs_ppr_test.sql`: a small graph where graded scoring VISIBLY differs from membership (e.g. a
multi-path-reinforced vertex vs a barely-reachable one at equal vector distance) with hand-computed
expected order under the documented fusion; plus the default-inertness assert (same query, GUC off
→ byte-identical to the membership expectation). Negative control: the PPR-mode expectations must
FAIL before implementation.

### Step 2: Implement opt-in PPR per the contract

**Verify**: new suite ALL PASS (PG17 + PG16); entire existing stock gate set green with default
off; parity 11/11.

### Step 3: HotpotQA recall gate

Loader + runner + grading per "The recall gate". Persist raw outputs under `bench/results/` only
if the repo's convention is to commit them — otherwise scratch + summarized table in the addendum.

**Verify**: both modes ran on identical inputs; the table is complete (no dropped points); a
sanity row reproduces membership ≈ the current engine behavior.

### Step 4: ADR-0012 addendum + verdict

Dated addendum: implementation summary, the table, censored fractions, host-reference context,
explicit GO / NO-GO / INCONCLUSIVE recommendation and what would change it. Append-only.

**Verify**: `make test && make lint && git diff --check` green; `rg 'tjs.graph_scoring' src docs
test` finds GUC, docs, and tests.

## Done criteria

- [ ] `tjs.graph_scoring=ppr` produces the fixture's hand-computed graded order; default is
      provably byte-inert (full gate set + parity green).
- [ ] TR-1/budget/censoring semantics identical in both modes; no full-reserve sort.
- [ ] Membership-vs-PPR HotpotQA table measured on the stock engine, both modes, honest labels.
- [ ] ADR-0012 addendum carries the verdict; no default flipped.

## STOP conditions

- PPR cannot be expressed on the bounded iterator without materializing the reserve map for a
  global sort — report the structural conflict (that is a real finding against ADR-0012's design).
- The corpus loader would require >M effort (e.g. embedding regeneration — `corpus_emb.npy` should
  make this unnecessary) — report scope before building.
- Default inertness cannot be achieved byte-identically — STOP; that breaks the ratified ADR-0020
  contract.
- Any gate (parity/stock suites) fails with the feature OFF.

## Maintenance notes

If the verdict is GO, default adoption is its own ADR (supersedes ADR-0020 decision 3) and needs
the wiki/wikidata-scale re-measure, not just HotpotQA. If NO-GO, keep the GUC as a research knob or
remove it in a follow-up — do not leave it half-documented.
