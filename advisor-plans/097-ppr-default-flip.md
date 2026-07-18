# Plan 097: Execute ADR-0021 — flip the stock seedless default to PPR

> **Executor instructions**: ADR-0021 was accepted by the maintainer 2026-07-18. The ADR's D1–D5
> ARE the spec — read `docs/decisions/0021-ppr-default-graph-scoring.md` first; this plan only
> sequences it. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat 997b679..HEAD -- src/tjs_pg/ test/ docs/ Makefile` (expect only the ADR-0021 doc commit in range)

## Status

- **Priority**: P1 | **Effort**: S–M | **Risk**: MED (default behavior change, fully gated)
- **Depends on**: 095, 096 (merged), ADR-0021 (Accepted)
- **Planned at**: commit `997b679` + ADR commit, 2026-07-18

## Steps

### Step 0: Ratify

Flip ADR-0021 Status → `Accepted (2026-07-18)`. Append a one-line `Superseded by ADR-0021 (D1)`
pointer to ADR-0020's Resolved-decisions item 3 (append-only, no rewrite).

### Step 1: D5 test migration FIRST (red before the flip)

Per ADR-0021 D5: flip `test/tjs_ppr_test.sql`'s default-inertness assertion (no-SET now expects
the PPR order; explicit `SET tjs.graph_scoring = membership` still expects membership order); add
explicit `SET tjs.graph_scoring = membership` to the setup of seedless tests that pin membership
semantics (`test/tjs_pg_test.sql` seedless blocks — PASS 4/5/8/9/10 and any other seedless
assertion; expectations unchanged); ensure at least one no-SET seedless assertion pins the PPR
default. **Negative control**: the migrated no-SET assertions must FAIL against the current
(membership-default) build.

### Step 2: The flip + D3 GUC exposure

In `src/tjs_pg/tjs_pg.c`: default `tjs.graph_scoring` → `ppr`; add `tjs.ppr_alpha` (double,
default 0.15, range (0,1)) and `tjs.ppr_rmax` (double, default 1e-3, sane positive range) as
`PGC_USERSET` GUCs replacing the fixed constants — documented as UNSWEPT research knobs in the
GUC descriptions and `tjs_pg--0.1.0.sql` comments. No other behavior change; filter-first
untouched.

### Step 3: D2 guidance docs

`docs/INSTALL_stock_pg.md` (+ README seedless mention if it names the default): state the new
default, the membership escape hatch, and the measured budget guidance table (PPR@8192 dominates
membership@65536 on the 200k gate; budget = latency knob; benchmarks must report the censor flag).

### Step 4: Full gates

`make stock-graph-test PG_MAJOR=17` (all suites) AND the same suites on `tridb/pg16-unfork:dev`;
`bash scripts/tjs_parity_test.sh` (11/11 — filter-first, unaffected, must stay green);
`make stock-crash-test PG_MAJOR=17`; `make test && make lint && git diff --check`.

## Scope

**In scope**: `src/tjs_pg/tjs_pg.c`, `src/tjs_pg/tjs_pg--0.1.0.sql`, `test/tjs_ppr_test.sql`,
`test/tjs_pg_test.sql` (SET-preambles only), `docs/decisions/0021-*.md` (status),
`docs/decisions/0020-*.md` (pointer line), `docs/INSTALL_stock_pg.md`, `README.md` (only if it
names the default).
**Out of scope**: filter-first; any algorithm change; budget default (stays 65536 per D2); fork
patches; the alpha/r_max SWEEP (future work D3 enables).

## STOP conditions

- Any seedless test's MEMBERSHIP-mode expectation would need to change (D5 says they must not).
- The parity harness or crash drivers regress.
- The GUC-ification of alpha/r_max changes any default-path result (defaults must reproduce the
  fixed-constant behavior byte-identically — assert via the existing ppr fixture expectations).

REPORT FORMAT: STATUS / STEPS+verifications / FILES CHANGED / NOTES / WORKTREE+commits.
