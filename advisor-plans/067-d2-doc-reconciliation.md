# Plan 067: Reconcile the outward-facing docs with the D2 un-fork reality

> **Executor instructions**: Follow step by step. This plan edits **documentation only** — no code,
> no scripts. Run the verification greps. Update this plan's row in `advisor-plans/README.md` when
> done.
>
> **Drift check (run first)**: `git diff --stat a41b0c7..HEAD -- README.md docs/STATUS.md CLAUDE.md CONTRIBUTING.md docs/INSTALL_stock_pg.md docs/decisions/0015-pg17-platform-spike.md`
> If any changed, read them fresh before editing; a section already fixed is a skip, not a conflict.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: docs
- **Planned at**: commit `a41b0c7`, 2026-07-15

## Why this matters

The D2 un-fork (commits `239d96c`→`a41b0c7`, 2026-07-13→15) updated the **roadmap and its ADRs** but
not the **outward-facing docs**. The result: the front door tells a stranger TriDB is an EOL-Postgres
fork — exactly the "why a fork of EOL Postgres?" hostile framing the landscape review names as the
top launch risk — while the repo has *measured* a 23.68× fusion win on stock PG17 + pgvector with no
fork (Gate B). The strongest current result is invisible; the weakest framing leads. This is the
highest-leverage docs work on the board because it is what a first-time reader or benchmark-reproducer
hits first. One coordinated pass fixes all of it.

## Current state (each is a confirmed drift)

- `README.md:25` — badge reads `PostgreSQL_13.4`; `:53` About says "built by **forking MSVBASE**";
  `:151` claims "GRAPH_TABLE surface parses on **stock PostgreSQL 13**" (doubly stale — the AM now
  installs on stock PG **16/17**, and PG13 is EOL); `:169-184` Quick Start shows only the forked
  image; `:203` Status has zero mention of D2/Gate A/B/stock-PG/`src/tjs_pg`/ADR-0019.
- `docs/STATUS.md:3` — "Updated: 2026-07-08"; newest banners are pre-D2. No Gate A/B, no ADR-0019,
  no `stock-pg` CI job, no `src/tjs_pg`, no un-fork commits.
- `CLAUDE.md:71` — "C for Postgres internals targets PG 13.4 access-method APIs; 32KB block size";
  `:23-24` "native C work compile *only on the GX10*". Contradicted by ADR-0015 E2 (zero PG13→17
  drift; 8KB works), `src/graph_store/gph_page.h` (BLCKSZ≥8192 capability), and the `stock-pg` CI
  job building the AM on stock PG16/17 x86 off-GX10.
- `CONTRIBUTING.md:65-67` — repeats "PostgreSQL 13.4 access-method APIs, 32 KB block size"; `:7-8,81`
  point at the two plan indexes as "the improvement roadmap" and never link
  `docs/tridb_productization_roadmap_v0.1.0.md` (the actual current strategy, with Addenda A1/A2).
- `docs/decisions/0015-pg17-platform-spike.md:3` — still "Status: Proposed", though its decision was
  made and executed (superseded/realized by ADR-0019 + the landed un-fork + Gate B PASS).
- `docs/INSTALL_stock_pg.md:56-57` — says "Exact fork phase/bridge parity (ADR-0012/0017 seed-bridge
  injection) **is follow-up**" — but bridge parity **landed** this session (commit `81b8023`;
  `src/tjs_pg/tjs_pg.c` has `bridge_topk`, the phase-3b drain, and `tjs_open_bridges_injected()`).
  This is stale-in-the-wrong-direction: it undersells a shipped capability.

## Steps

Edit factually and minimally. Keep the honest "fork is the launch **vehicle**, not the destination"
framing from ADR-0015 — do NOT overclaim (the fork is still the reference for seedless SM-4 parity).

1. **README.md**:
   - Badge `:25`: `PostgreSQL_13.4` → reflect both, e.g. `PostgreSQL_16%2F17_(stock)_%2B_13.4_fork`
     (or a clean two-badge form). Keep it truthful.
   - About `:53`: reframe to "installable as an extension on **stock PostgreSQL 16/17 + pgvector**,
     with an MSVBASE fork as the reference vehicle" — mention the graph AM (`graph_store_am`) +
     `tjs_pg` operator install on stock PG.
   - `:151`: "stock PostgreSQL 13" → "stock PostgreSQL 16/17".
   - Quick Start `:169-184`: add the stock-PG path first (point at `docs/INSTALL_stock_pg.md` and the
     `docker run tridb/postgres-trimodal:pg17` → `CREATE EXTENSION vector; CREATE EXTENSION graph_store_am;`
     flow), keep the fork path as the second/advanced option.
   - Status `:203`: add a D2 line — Gate A PASS (11.90× fork) + Gate B PASS (23.68× stock PG17 +
     pgvector), ADR-0019, `src/tjs_pg`, the `stock-pg` CI matrix. Link the roadmap.

2. **docs/STATUS.md**: add a top banner dated for the D2 session summarizing: un-fork landed (graph
   AM on stock PG16/17, `src/tjs_pg` operator), Gate A/B PASS with the numbers, ADR-0019 accepted,
   `stock-pg` CI matrix always-on. Bump the "Updated:" date. Keep the existing history below.

3. **CLAUDE.md**: qualify the two contradicted lines — C targets "PG 13.4 fork **and** stock PG
   16/17 access-method APIs"; note the graph AM is BLCKSZ-capability (8KB on stock, 32KB the fork
   perf target) and that stock-PG C builds/tests **off-GX10** via `scripts/pg17_graph_test.sh`. Add
   a one-line pointer to `docs/INSTALL_stock_pg.md`. Keep edits minimal — CLAUDE.md is a governance
   file.

4. **CONTRIBUTING.md**: fix the "PG 13.4 / 32 KB" line the same way; add a link to
   `docs/tridb_productization_roadmap_v0.1.0.md` as the current strategic roadmap, and a one-line
   note distinguishing it from the historical `plans/` and `advisor-plans/` batches (and that those
   two dirs number independently — see plan 068 if it exists / the DX finding).

5. **docs/decisions/0015-pg17-platform-spike.md:3**: change `Status: Proposed` →
   `Status: Accepted (2026-07-15) — realized by ADR-0019 + roadmap Addendum A2 (Gate B PASS)`.
   Add a one-line pointer at the top to ADR-0019. Do NOT rewrite the body (append-only ADR convention).

6. **docs/INSTALL_stock_pg.md:56-57**: change the "is follow-up" wording — bridge parity **landed**
   (commit `81b8023`); state that `tjs_open` now implements the ADR-0012 guaranteed-bridge injection
   (`tjs_open_bridges_injected()` counter) and that the remaining follow-up is the *seedless SM-4
   curve parity vs the fork*, not the bridge mechanism itself. Cross-check the actual code
   (`grep -n 'tjs_open_bridges_injected\|bridge_topk' src/tjs_pg/tjs_pg.c`) so the corrected wording
   is accurate.

## Verification

Docs-only; no build. Confirm the drifts are gone:
- `grep -c '13.4' README.md` — reduced (only the fork-reference mention remains, not the primary
  framing); manually read the About + Quick Start to confirm the stock path leads.
- `grep -ci 'gate b\|stock' docs/STATUS.md` ≥ 1.
- `grep -ci 'stock pg 16\|stock pg16\|16/17' CLAUDE.md CONTRIBUTING.md` ≥ 1 each.
- `grep -c 'Status: Accepted' docs/decisions/0015-pg17-platform-spike.md` == 1.
- `grep -ci 'is follow-up' docs/INSTALL_stock_pg.md` == 0 (the bridge-parity "follow-up" line is
  corrected).
- `grep -c 'tridb_productization_roadmap' CONTRIBUTING.md README.md` ≥ 1.

Then a human read-through of README top-of-file + STATUS top banner to confirm the framing now leads
with the stock-PG + Gate B story and reads honestly (no overclaim — the fork remains the reference
for seedless parity).

## Done criteria

All six grep checks above pass, and the README About/Quick-Start and STATUS banner lead with the
stock-PG installable framing + the Gate A/B numbers, with the fork positioned as the reference
vehicle.

## Out of scope / do NOT touch

- Any code, script, Dockerfile, or test.
- The ADR **bodies** (append/status-line only — ADRs are append-only per repo convention).
- The roadmap doc itself (it's already current with A1/A2) — only *link* it from the front docs.
- Do not delete `plans/` or `advisor-plans/` or renumber them (that's a separate DX finding).

## STOP conditions

- If reading the code shows bridge parity is NOT actually in `src/tjs_pg/tjs_pg.c` (the
  `tjs_open_bridges_injected`/`bridge_topk` grep returns nothing), STOP and report — do not "correct"
  the INSTALL doc to claim a capability that isn't there; the finding would be inverted.
- If any doc already reflects the D2 reality (someone did a doc pass first), skip that file and note
  it — do not revert a more-current version.

## Maintenance note

Root cause: a large engine/roadmap change updated ADRs but not README/STATUS/CLAUDE/CONTRIBUTING.
Going forward, a "does the front door still describe the repo?" check belongs in the definition of
done for any phase-completing change — consider a CONTRIBUTING note to that effect.
