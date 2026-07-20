# Plan 103: Seedless filter probe — guarded ExprState fast path (issue #31)

> **Executor instructions**: the byte-parity suite is the heart of this plan — the fast path
> ships ONLY if the same queries through both probe paths are IDENTICAL (result sets,
> disclosure counters, termination reasons) across the whole filter matrix. The SPI probe is
> never removed: it stays as the universal fallback AND the error-parity anchor (it is
> prepared first, unconditionally, so every fragment SPI would reject still errors
> identically). Never modify `tridb-wikidata-pg17` on the Spark; the gate reuses
> `tridb-issue30-pg17` (host port 5460), measuring the container's CURRENT 102 build BEFORE
> installing this branch's `.so`. Skip the advisor index update.

## Status

- **Priority**: P1 (issue #31 — the residual constant that keeps seedless `tjs_open`
  1.4–2.3x behind plain pgvector at matched recall)
- **Effort**: M (one focused C hot-path change; the parity suite and Spark gate are the bulk)
- **Risk**: MED (touches the seedless per-candidate flow; DEV-1169 drop-rule semantics and
  plan-102 budget disclosure must be byte-identical on both paths)
- **Depends on**: plan 102 (diagnosis: ~4.7–5.5 us/candidate SPI execution, plan already
  cached — `advisor-plans/102-seedless-tail-latency.md` H2), ADR-0019 (operator owns the
  scan loop), plan 095/097/102 GUC default-inertness discipline, CONTRIBUTING extension
  versioning (0.2.0 RELEASED — GUC-only change, no SQL-surface change, packaging untouched)
- **Planned at**: 2026-07-20
- **Branch**: `advisor/103-filter-probe-expr`

## The defect (issue #31, measured in plan 102)

Plan 102's fixed-drain ablation isolated the seedless matched-recall gap to the
per-candidate relational filter probe: one `SPI_execute_plan` per stream candidate at
~4.7–5.5 us (filter='' 81–98 ms vs filter='true' 184–231 ms at ~20–25k fixed drain — the
probe roughly DOUBLES the deep-drain wall, ~+105 ms on the p95 outliers). The plan is
already cached (`SPI_prepare` once per call); the cost is SPI per-execution machinery.
pgvector's native executor qual on the identical predicate is sub-microsecond. At recall
parity the operator must drain as deep as pgvector does, so no budget knob removes this
constant.

## Design

**Fast path**: compile the filter fragment ONCE per `tjs_open` call to an `ExprState`
evaluated against the candidate's already-fetched heap slot. The seedless loop already
fetches the heap tuple BEFORE the probe (`index_fetch_heap` fills `slot`, then the SPI
probe re-fetches the same logical row by PK) — so no order-of-operations change is needed:
the slot is in hand, the expression evaluates on it, and the per-candidate SPI execution
(its own PK btree descent + heap fetch + executor startup) disappears for the common case.

**Compilation** (once per call, in the per-call SPI proc memory context, AFTER the SPI
fallback plan is prepared):

1. Build `SELECT 1 FROM <schema>.<rel> WHERE (<fragment>)` (same identifiers, same quoting
   as the SPI probe text) and `raw_parser()` it; extract the raw `whereClause`.
2. **Raw eligibility guard**: walk the raw tree; any `SubLink` (subquery) or `ParamRef`
   (`$n` — the SPI probe binds `$1` to the candidate id; a fragment referencing it cannot
   be compiled standalone) → silently use the SPI path.
3. `ParseState` + `addRangeTableEntryForRelation`/`addNSItemToQuery` for the scanned
   relation (the one namespace the SPI probe exposes), then
   `transformWhereClause(..., EXPR_KIND_WHERE)` + `assign_expr_collations` — the exact
   parse-analysis WHERE would get, same search_path, no elevated context.
4. **Cooked eligibility guard**: every `Var` must have `varno == 1` and
   `varlevelsup == 0`; any `SubPlan`/`SubLink`/`Param`/`Aggref`/`WindowFunc`/
   `GroupingFunc`/`CurrentOfExpr` → SPI path (belt-and-braces; the raw guard and
   EXPR_KIND_WHERE already exclude most).
5. `expression_planner()` + `ExecInitQual()` (qual semantics: NULL = false, exactly
   WHERE's), evaluated per candidate with `econtext->ecxt_scantuple = slot` via a
   standalone `ExprContext` (`ResetExprContext` per candidate).

**Error parity by construction**: the SPI fallback plan is prepared FIRST,
unconditionally, exactly as today — so every fragment SPI_prepare rejects (unknown column,
syntax error, aggregate in WHERE, SRF in WHERE, ...) raises the same error from the same
place as today, before the fast-path compiler ever runs. A fragment that passes
SPI_prepare cannot then fail the standalone transform on the supported subset (the only
textual difference is the removed `id = $1 AND` conjunct; `ParamRef` is excluded
pre-transform). The per-call SPI_prepare cost is status quo (it happens today).

**ACL guard**: the SPI path enforces privileges through the executor. The fast path
requires table-level `SELECT` on the scanned relation (`pg_class_aclcheck(...,
ACL_SELECT)`); a caller with only column-level grants silently gets the SPI path rather
than a reimplementation of column-level enforcement. (Chosen deliberately: column-level
ACL enforcement lives in one place — the executor — and the fast path never widens NOR
narrows what a caller can do; it only requires the strictly-stronger table grant to take
the shortcut.)

**Escape hatch GUC**: `tjs.filter_probe` = `auto` (default) | `spi` (PGC_USERSET) — a
debugging/compat switch forcing the SPI probe. GUC-only change: no SQL-surface change, so
0.2.0 packaging is untouched (same reasoning as plan 102 / CONTRIBUTING "Extension
versioning": upgrade scripts are needed for released SURFACE changes; a GUC is not one).

**Observability (non-vacuity proof, no new SQL function)**: `tjs.last_filter_probe_mode`
— a read-mostly report GUC (string: `none` | `expr` | `spi`) the operator sets per
seedless call at the probe decision point. Readable via `current_setting()`, so the parity
suite can ASSERT the fast path actually engaged (and that the SubLink case actually fell
back) without any new SQL function. Setting it manually is a harmless no-op (overwritten
by the next call); documented as a debug register. A new SQL function
(`tjs_open_filter_probe_mode()`) was rejected: it would be a surface change forcing
0.2.0→0.3.0 scaffolding for a debug observable.

**Semantics note (concurrency)**: the SPI probe re-fetches the candidate BY PK, so under
concurrent updates it may evaluate a DIFFERENT tuple version than the one the distance was
recomputed on; the ExprState path evaluates the SAME version the scan yielded.
Byte-identical in quiescent state (the parity suite proves it); under concurrency the fast
path is arguably MORE consistent (filter and distance see one version). Both paths assume
the id column is unique (the operator's existing identity contract — dedup, bridge
fetch-by-id, and the SPI probe itself all already assume it); with duplicate ids the two
paths could legitimately diverge, as could the SPI path against itself.

**Unchanged**: DEV-1169 drop-rule semantics (the probe only computes `passes`; the drop
counter logic is untouched on both paths). Filter-first, the phase-3b bridge fetch, and
the PPR finalize fetch keep their embedded-filter SPI statements (bounded, not the hot
path). The injection/privilege surface is not widened: the fragment is ALREADY
interpolated into SPI SQL today (SECURITY.md: internal-only argument); the fast path
parses the same text with the same search_path and no elevated context.

## Steps

1. **Plan doc** (this file) — first commit.
2. **C fix** (`src/tjs_pg/tjs_pg.c`): GUCs + compile + guards + per-candidate `ExecQual`,
   SPI fallback untouched. `fix(tjs): ...`
3. **Parity suite** `test/tjs_filter_probe_test.sql`, wired into `STOCK_TESTS`: same
   seedless queries under `tjs.filter_probe = auto` vs `= spi`, asserting IDENTICAL result
   sets, `tjs_open_candidates_examined()`, and `tjs_open_termination_reason()` across:
   simple predicates, array `@>`, NULL-involving predicates, type coercions, function
   calls (incl. a STABLE user function), error parity on a nonexistent column (same
   SQLSTATE both paths), a SubLink filter (must report `spi` mode under `auto` and still
   agree), empty filter (`none`), the plan-102 scan-budget interaction (cap + disclosure
   identical on both paths), a runtime-error filter (same SQLSTATE both paths), and a
   column-grant-only role (ACL guard falls back to SPI and works). Non-vacuity: assert
   `tjs.last_filter_probe_mode = 'expr'` for every eligible case under `auto`.
   `test(tjs): ...`
4. **Gates**: `make stock-graph-test PG_MAJOR=17` AND `PG_MAJOR=16`,
   `make stock-crash-test PG_MAJOR=17`, `bash scripts/tjs_parity_test.sh` (11/11),
   `make test PY=...`, `make lint PY=...`, `git diff --check`.
5. **Acceptance gate (Spark, issue #31 targets)**: reuse `tridb-issue30-pg17` (host port
   5460, 1M Gate B slice loaded, edge parity verified, HNSW built). Measure the seedless
   leg (`bench/wd_allpg_baseline.py`, single-backend core-pinning, same 50 pinned queries
   seed 1354, oracle `bench/results/wd_1m_oracle.json`) same-day, three rows minimum:
   (a) pre-103 = the container's current 102 build, measured BEFORE installing the new
   `.so`; (b) post-103 build; (c) plain pgvector iterative sweep. Also report the plan-102
   operating points (`tjs.vector_scan_budget`) post-103. **TARGETS**: at matched recall
   (~0.80) median within ~1.1x of plain pgvector AND p95 within 2x. Report the table
   verbatim whatever it shows; misses reported honestly with attribution.
6. **Docs**: Addendum A2 (append-only) to `docs/benchmark_allpg_baseline_v0.1.0.md`;
   GUC docs in `docs/INSTALL_stock_pg.md`; evidence in this plan doc. `docs(...)`

## STOP conditions

- The parse/transform approach cannot achieve error-parity with SPI on the supported
  subset (a fragment SPI accepts that the standalone transform rejects, or vice versa,
  that guarding cannot route to the SPI path).
- The parity suite finds ANY result divergence that cannot be eliminated.
- Observability genuinely requires a new SQL-surface function → report the packaging
  question instead of shipping a surface change.
- Any existing suite regresses (incl. DEV-1169 coverage, plan-102 budget disclosure).
- The fast path measures SLOWER than the SPI probe.

## Boundaries

Never push; never merge to master; never touch the main checkout; `tridb-wikidata-pg17`
and every other Spark container except `tridb-issue30-pg17` untouched. Commit early and
often (`fix(tjs):`, `test(tjs):`, `bench(tjs):`, `docs(...)`).

REPORT FORMAT: STATUS / DESIGN AS BUILT (eligibility guard, ACL choice, observability
choice) / PARITY SUITE coverage + results / GATES / ACCEPTANCE TABLE verbatim vs #31
targets / FILES CHANGED / NOTES / WORKTREE+commits.
