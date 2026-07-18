# ADR-0021: Make PPR-graded scoring the default for the stock seedless graph leg

Status: **Proposed** (drafted by the advisor 2026-07-18; maintainer acceptance required —
no behavior changes until Accepted and the implementation plan executes).
Supersedes: ADR-0020 "Resolved decisions" item 3 (membership-now) for the SEEDLESS path only.
Builds on: ADR-0012 (bounded forward-push PPR design + both measured addenda), ADR-0019
(stock rehome), ADR-0020 (bounded pull iterator, budget, censoring).

## Context — the evidence that gates this decision

ADR-0020 decision 3 deliberately shipped the bounded pull graph leg with binary
reachability-membership scoring so the rewrite could be proven byte-identical (071 parity
harness as the acceptance gate), and named PPR-graded reserves as the documented follow-on
behind its own recall gate. Both gates have now been run, on two corpora with independent,
scoring-agnostic gold:

| gate | corpus | gold | headline (PPR vs membership) | knobs under pressure? |
|---|---|---|---|---|
| plan 095 (2026-07-17 addendum, ADR-0012) | HotpotQA, 1490 nodes, mean deg ≈ 1, stock engine | task gold (supporting paragraphs) | recall@5 **0.927 vs 0.880** (+4.7 pt), joint@5 **+9.3 pt**; wins all 12 points; both modes uncensored | no — term_cond inert, budget never binds |
| plan 096 (2026-07-18 addendum, ADR-0012) | enwiki 200k articles, **14.68M real hyperlink edges**, stock engine | held-out hyperlinks (editorial, removed from load, probe-verified absent) | recall@20 **0.119–0.123 vs 0.081–0.083** (**+47 % rel**), recall@10 +15 % rel; wins **all 18 matched points** | **yes** — budget binds (censored 0.84–1.00) |

Two operational findings from the 200k gate shape the defaults below:

1. **The graph budget is a latency knob, not a recall knob, at this scale**: recall is nearly
   flat across a 32× budget range while latency scales with the budget. The dominant measured
   operating point is **PPR @ budget 8192: recall 0.120 @ 135 ms**, which beats
   membership @ 65536 (0.081 @ ~640 ms) on BOTH axes.
2. **`term_cond` is inert on both corpora** ({8,32,128} moves recall ≤ 0.002): in the current
   seedless regime the graph leg, not the vector-stream drop rule, decides termination.

PPR's win is also not bought with extra work: at the top budget PPR examined slightly fewer
edge-steps than membership (62.5k vs 64.7k mean) while retrieving substantially more gold.
Its latency cost is ≈1.3–1.4× at budgets ≤ 8192 and ≈2.3× at 65536 (the push touches more
SPI fetches per edge-step at high budgets) — disclosed, and dominated by the budget guidance
below.

## Decision (proposed)

### D1. Default scoring: `tjs.graph_scoring = ppr` (seedless path only)

The stock operator's seedless graph leg defaults to PPR-graded scoring. Membership remains
fully supported via `SET tjs.graph_scoring = membership` — one SET, tested, and the mode the
fork-parity posture relies on (see D4). **Filter-first is untouched**: it stays exact
membership semantics (the pinned Gate-A/B contract; PPR was never implemented there and this
ADR does not extend it there).

### D2. Budget default: keep `tjs.graph_work_budget = 65536`; publish the measured guidance

The default stays as ADR-0020 ratified it. Rationale: lowering the default to the measured
sweet spot (8192) would change behavior for membership-mode users and make wiki-scale results
censored-by-default; a default-flip ADR should change one thing. Instead, the operator
documentation and benchmark harnesses carry the measured guidance: *on hyperlink-density
graphs, budget ≈ 8192 with PPR dominates larger budgets on both recall and latency; the
budget is the latency knob*. Benchmarks continue to report the censor flag next to every
headline (spec Addendum A3 rule, unchanged).

### D3. Expose `tjs.ppr_alpha` (default 0.15) and `tjs.ppr_rmax` (default 1e-3) as GUCs — labeled unswept

Currently fixed C constants at the host-reference values. Exposing them costs nothing,
unblocks the sweep this ADR's evidence lacks, and keeps the published defaults exactly the
measured configuration. Both are documented as **unswept research knobs**: the two gate
tables were measured only at the defaults. (Alternative considered: keep them hardcoded
until swept — rejected because the sweep itself needs the knobs, and hardcoded-but-influential
constants are the less honest shape.)

### D4. Fork-parity posture (the strategic consequence — decide knowingly)

Flipping the default makes the stock seedless result diverge BY DEFAULT from the fork's
membership-scored seedless semantics (which plan 087 deliberately aligned). Post-D2 the stock
extension is the ship surface and the fork is the launch vehicle (ADR-0019: fork moves toward
maintenance); this ADR ratifies that the DEFAULT product behavior may now exceed the fork
rather than mirror it. Fork parity remains a supported, tested mode
(`tjs.graph_scoring = membership`), and any future fork↔stock seedless differential harness
compares in membership mode. The filter-first parity harness (071, 11/11) is unaffected —
filter-first semantics do not change.

### D5. Migration contract for the test/gate suite (the implementation plan must satisfy all)

- `test/tjs_ppr_test.sql`: the default-inertness assertion flips — "no SET" now equals the
  PPR order; explicit `SET ... = membership` still equals the membership order.
- Seedless tests that pin membership semantics (`test/tjs_pg_test.sql` PASS 4/5/8/9/10 etc.)
  gain an explicit `SET tjs.graph_scoring = membership` in their setup — they test membership
  mode, which remains a contract; their expectations do not change.
- At least one seedless test asserts the DEFAULT (no SET) produces the PPR-graded order, so
  the default cannot silently regress either way.
- The full stock gate set (all suites, both PG majors), the 071 filter-first parity harness,
  and the crash drivers must pass; `make test`/`make lint` green.
- ADR-0020's "Resolved decisions" item 3 gets a pointer to this ADR (append a superseded-by
  line; no history rewrite).

## What this does NOT decide

- No filter-first scoring change; no fork C changes; no new query-language parameters
  (ADR-0008 surface pinned).
- No claim that PPR helps every workload: both gates are entity/link-shaped corpora. The
  Wikidata truthy proving ground (ADR-0018) has not measured PPR; its filter-first primary
  experiments are out of PPR's scope by design.
- No term_cond redesign — its two-corpus inertness in the seedless regime is recorded as an
  open question (candidate future work: certified rank-join bound, landscape §2 #5).

## What would revert or amend this decision

- A task-grounded corpus where PPR measurably loses to membership at matched budgets.
- A workload where the 1.3–2.3× seedless latency multiple is unacceptable and budget tuning
  cannot recover it (the guidance in D2 is the first remedy).
- The alpha/r_max sweep (enabled by D3) finding the defaults badly placed — amend defaults
  with the sweep table attached.

## Implementation

A single advisor plan (097) executes on acceptance: the GUC default flip + D3 GUC exposure
in `src/tjs_pg/tjs_pg.c` / `--0.1.0.sql` comments, the D5 test migration, INSTALL/README
operator-docs update with the D2 guidance table, and the ADR-0020 pointer. Effort S–M; every
gate in D5 is the verification. Until that plan lands, nothing changes.
