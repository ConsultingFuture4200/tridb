# Plan 036: gph_freeze() + disarm forced anti-wraparound autovacuum (gBrain-A1 / DEV-1347)

> **Executor instructions**: Follow step by step; honor STOP conditions. This is native graph-store C
> (GX10-gated build): author + static-verify here, the engine build + FR-7 re-run happen on the GX10.
> Update your row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat cb3eb0a..HEAD -- src/graph_store docs/graph_store_freeze_design_v0.1.0.md`
> The design is `docs/graph_store_freeze_design_v0.1.0.md` — read it in full before coding; it is the spec.

## Status
- **Priority**: P1 (the #1 correctness gate for a long-lived / continuously-writing store)
- **Effort**: M
- **Risk**: MED (touches the graph store's txn-visibility metadata; must preserve FR-7 + the read path)
- **Depends on**: the freeze design (advisor 026, `docs/graph_store_freeze_design_v0.1.0.md`) — done
- **Category**: correctness / durability
- **Planned at**: commit `cb3eb0a`, 2026-07-04
- **Linear**: DEV-1347 · **gBrain spec**: `docs/gbrain_backend_hardening_v0.1.0.md` G1/Phase-A1

## Why this matters

TriDB's benchmark model bulk-loads then queries, so it never ages xids. **gBrain writes on timers for
months** and will. Today the graph store stores raw `vr_xmin`/`es_xmin` with **no freeze path**
(`docs/graph_store_freeze_design_v0.1.0.md:12-28`): after clog truncation, `TransactionIdDidCommit` on
an old stored xid raises "could not access status of transaction"; past ~2³¹ xids, wraparound flips
visibility. Worse — the `gstore` container is a plain heap to Postgres, so **forced anti-wraparound
autovacuum ignores `autovacuum_enabled=false`** (`:24`) and eventually walks the non-heap graph pages
*as a heap* → corruption. This is a prerequisite for a gBrain backend, not a nicety.

## Current state

- Hazard + design: `docs/graph_store_freeze_design_v0.1.0.md` — §hazard (`:12-28`), `gph_freeze(horizon)`
  design (`:30-59`), the `gm_frozen_horizon` metapage field repurposing the existing `uint32 gm_reserved`
  slot **with no page-layout change** (`:60-63`), v1 manual invocation (`:70`), monitoring via
  `age(relfrozenxid)` (`:81`).
- The store's write/visibility core: `src/graph_store/graph_am.c` — records carry `vr_xmin`/`es_xmin`;
  `gph_xmin_visible` gates the read path; all mutations already go through `GenericXLog`.
- Metapage: `src/graph_store/gph_page.h` — the `GphMeta` struct (has `gm_reserved` to repurpose).
- Engine-change workflow (vendor edit vs in-tree C): the graph store is **in-tree** PGXS
  (`src/graph_store/*.c`, built by `make graph-test` inside `tridb/msvbase:dev`), NOT a vendored patch —
  edit the `.c/.h`/`.sql` directly; no `scripts/patches/` registration.
- FR-7 + recovery suites: `scripts/txn_atomicity_test.sh`, `scripts/crash_recovery_test.sh` (must re-pass).

## Commands you will need
| Purpose | Command | Expected |
|---|---|---|
| Fast layer | `. .venv/bin/activate && make test && make lint` | green |
| Engine suite (GX10/Docker) | `make graph-test` | ALL PASS (author here, run on GX10) |
| Freeze round-trip | a new `test/graph_freeze_test.sql` (age rows, `gph_freeze`, assert visibility intact) | ALL PASS on GX10 |

## Scope

**In scope:** implement `graph_store.gph_freeze(horizon xid)` per the design — a `GenericXLog` page-walk
that freezes stored `vr_xmin`/`es_xmin` at/below the horizon (mark frozen, per the design's chosen
representation) and updates the container's `relfrozenxid`; repurpose `gm_reserved` → `gm_frozen_horizon`
(no layout change); the SQL surface in `graph_store_am--0.1.0.sql` (superuser-guarded, per advisor 026
ACLs); **disarm/redirect forced autovacuum on `gstore`** so it cannot walk graph pages as a heap (the
design's approach — a relopt/handler guard, `:73-88`); a `test/graph_freeze_test.sql`; monitoring note.

**Out of scope:** the autovacuum-hook / full table-AM auto-freeze stage (design `:73` "Later") — v1 is
**manual** `SELECT graph_store.gph_freeze(<horizon>)`; note it. Compaction of tombstoned slots (plan 037
piggybacks its compaction here later, but tombstones themselves are 037). No page-layout change.

## Git workflow
Branch `advisor/036-gph-freeze`; `feat(graph):` commits; do NOT push.

## Steps

### Step 1: `gm_frozen_horizon` metapage field
Repurpose `gm_reserved` → `gm_frozen_horizon` in `GphMeta` (`gph_page.h`); initialize on metapage
create; `StaticAssert` the struct size is unchanged.
**Verify**: struct-size assert holds; `make test`/`lint` green; incremental compile clean (GX10).

### Step 2: `gph_freeze(horizon)` core
Implement the page-walk in `graph_am.c` under `GenericXLog`: for each vertex/edge record with
`xmin <= horizon` and committed, freeze it (per the design's representation); advance the container
`relfrozenxid`; record `gm_frozen_horizon`. Idempotent; safe to re-run.
**Verify (GX10)**: a `test/graph_freeze_test.sql` that inserts, advances xids past a horizon, runs
`gph_freeze`, and asserts **every pre-horizon row stays correctly visible** and no "could not access
status of transaction" occurs; `crash_recovery_test.sh` still passes (freeze is WAL-durable).

### Step 3: SQL surface + autovacuum disarm
Expose `graph_store.gph_freeze(xid)` (superuser/owner only, matching advisor-026 ACLs) in
`graph_store_am--0.1.0.sql`. Guard the `gstore` container so forced anti-wraparound autovacuum does not
scan its non-heap pages as a heap (per design `:73-88` — capture the exact mechanism chosen in a comment).
**Verify**: ACL test (non-owner denied); the disarm mechanism documented + a note on how the operator
monitors `age(relfrozenxid)` and when to run freeze.

### Step 4: FR-7 non-regression
Re-run the full atomicity + crash-recovery suites; confirm `gph_freeze` inside a txn commits/rolls back
atomically with the heap + HNSW legs.
**Verify (GX10)**: `txn_atomicity_test.sh` + `crash_recovery_test.sh` PASS; `make graph-test` ALL PASS.

## Test plan
`graph_freeze_test.sql` (visibility preserved across a freeze horizon; idempotent re-run) + the FR-7/
recovery suites unchanged. Fast layer (`make test`/`lint`) green throughout.

## Done criteria
- [ ] `gm_frozen_horizon` repurposes `gm_reserved`; struct size unchanged (assert)
- [ ] `gph_freeze(horizon)` freezes pre-horizon rows under GenericXLog + advances `relfrozenxid`; idempotent
- [ ] SQL surface superuser-guarded; forced autovacuum disarmed on `gstore`; monitoring note
- [ ] `graph_freeze_test.sql` + FR-7 + crash-recovery ALL PASS on the GX10; `make test && make lint` green
- [ ] README row updated

## STOP conditions
- The freeze representation would require a page-layout change (it must not — `gm_reserved` repurpose only).
- Any FR-7 atomicity or crash-recovery assertion regresses.
- The autovacuum-disarm mechanism cannot be made reliable without a full table-AM handler — implement the
  manual freeze + monitoring, mark the auto-freeze STAGE deferred, and report (do not ship a false "safe").

## Maintenance notes
This is the long-lived-store gate; plan 037's tombstone compaction and any future incremental-ingest work
(DIRECTION-04) build on it. The auto-freeze (autovacuum-hook / table-AM) stage is the designed follow-on.
Reviewer focus: visibility-across-horizon correctness (Step 2) and that the autovacuum disarm genuinely
prevents the heap-walk, not just suppresses the log line.
