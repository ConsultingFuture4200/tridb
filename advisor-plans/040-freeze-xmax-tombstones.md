# Plan 040: Freeze xmax so tombstones survive clog truncation

> **Executor instructions**: Follow step by step; honor STOP conditions. Native graph-store C
> (engine-gated verify). Update your row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat c216750..HEAD -- src/graph_store test/graph_freeze_test.sql test/graph_delete_test.sql docs/graph_store_freeze_design_v0.1.0.md`
> If freeze/tombstone code drifted, re-read live files before coding.

## Status
- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none (builds on shipped 036 freeze + 037 tombstone)
- **Category**: bug / correctness
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

Plan 037 stamps delete visibility on `es_xmax` / `vr_xmax`. Plan 036 freezes only **xmin** and then
advances `relfrozenxid` via `vac_update_relstats`. After freeze, clog may truncate past still-referenced
**xmax** values. Later reads call `TransactionIdDidCommit(xmax)` inside `gph_deleted_visible` and can
throw `could not access status of transaction`, or (after wrap) mis-classify tombstones. Long-lived
stores that delete then freeze are the gBrain path — this is a hard correctness failure mode.

## Current state

- Visibility (`src/graph_store/graph_am.c:96-99`):
  ```c
  gph_deleted_visible(uint32 flags, TransactionId xmax)
  {
      return (flags & GPH_FLAG_DELETED) && gph_xmin_visible(xmax);
  }
  ```
- Freeze adj chain freezes **only** `es_xmin` (`graph_am.c:968-969`):
  ```c
  if (gph_freeze_xid(&es->es_xmin, horizon))
      frozen_here++;
  ```
- Freeze vertex freezes **only** `vr_xmin` (`graph_am.c:1088-1089`) — never `vr_xmax`.
- After walk, `vac_update_relstats(..., horizon, ...)` advances `relfrozenxid` (`:1140-1147`).
- Tombstone sets `es_flags |= GPH_FLAG_DELETED` and `es_xmax = xid` (`:1220-1221`).
- Design note predates xmax: `docs/graph_store_freeze_design_v0.1.0.md` — update when xmax lands.
- In-tree PGXS: edit `src/graph_store/*` directly (no `scripts/patches/`).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Fast layer | `make test && make lint` | exit 0 |
| Engine | `make graph-test` (needs `tridb/msvbase:dev`) | ALL PASS |
| Freeze suite | via AM_TESTS `scripts/graph_freeze_test.sh` | PASS |
| Delete suite | `scripts/graph_delete_test.sh` | PASS |

## Scope

**In scope:**
- `src/graph_store/graph_am.c` — freeze loops for `es_xmax` / `vr_xmax`
- `test/graph_freeze_test.sql` — tombstone-then-freeze cases
- `docs/graph_store_freeze_design_v0.1.0.md` — document xmax freeze rules
- Optionally a one-line comment fix on `gph_tombstone_edge` claiming reclamation “rides freeze”

**Out of scope:** physical slot reclamation; full snapshot isolation (plan 056); changing freeze horizon API.

## Git workflow
- Branch: `advisor/040-freeze-xmax`
- Commits: `fix(graph): freeze xmax with tombstones (advisor 040)`
- Do NOT push unless asked.

## Steps

### Step 1: Define xmax freeze rules (same helper)

Extend `gph_freeze_xid` usage (or a sibling) so for each edge/vertex record:

| xmax state | action |
|---|---|
| Invalid / not deleted | leave xmax alone |
| `GPH_FLAG_DELETED` + xmax committed + xmax ≤ horizon | set xmax → `FrozenTransactionId` (tombstone stays visible-deleted forever) |
| `GPH_FLAG_DELETED` + xmax aborted (or will never commit) + ≤ horizon | clear `GPH_FLAG_DELETED` and set xmax → `InvalidTransactionId` (record LIVE again, matching aborted delete) |
| xmax in-progress / > horizon | **do not** freeze; **must not** advance `relfrozenxid` past this xmax |

**Critical**: never call `vac_update_relstats` with a horizon that leaves unfrozen xmax older than the new
`relfrozenxid`. Prefer: compute effective freeze horizon as min over unresolved xmax, or refuse freeze
when any deleted xmax is unresolved below the requested horizon (ereport).

**Verify**: static review; `make lint` N/A for C; no Python change.

### Step 2: Apply in adj + vertex freeze loops

In `gph_freeze_adj_chain` and the vertex loop in `gph_freeze`, freeze xmax with the rules above.
Count xmax freezes in the return total (or a separate counter — pick one and document).

**Verify (engine)**: extend freeze test (Step 3).

### Step 3: Tests — tombstone then freeze

Add to `test/graph_freeze_test.sql` (or a new sibling wired into `graph_freeze_test.sh`):

1. Insert edge; commit; `gph_tombstone_edge`; commit; freeze past delete xid → edge still **absent** from
   `gph_neighbors`; no clog error.
2. Begin; tombstone; freeze mid-txn is blocked or rolls back cleanly (document chosen rule).
3. Tombstone; **ROLLBACK** tombstone txn; freeze → edge still **present**.
4. Existing xmin-only freeze cases still pass.

**Verify**: `bash scripts/graph_freeze_test.sh` and `bash scripts/graph_delete_test.sh` + full `make graph-test`.

### Step 4: Design doc

Update `docs/graph_store_freeze_design_v0.1.0.md` §freeze rules with xmax table. Note plan 037 layering.

**Verify**: `rg -n "xmax|es_xmax" docs/graph_store_freeze_design_v0.1.0.md` shows the new section.

## Test plan
- Engine: freeze × delete matrix above.
- Host: `make test && make lint` still green (no Python change required).

## Done criteria
- [ ] Freeze freezes xmax under the rules in Step 1
- [ ] `relfrozenxid` never advances past unresolved deleted xmax
- [ ] New SQL coverage for tombstone-then-freeze passes on engine
- [ ] Design doc updated
- [ ] `make test` / `make lint` green here; `make graph-test` green on engine host
- [ ] No files outside scope
- [ ] `advisor-plans/README.md` row → DONE

## STOP conditions
- `gph_freeze_xid` semantics for Invalid/Frozen already differ from the table — re-read helper before inventing a second freeze path.
- Any existing freeze test fails after xmax changes — fix before adding new cases.
- Advancing `relfrozenxid` requires a different Postgres 13.4 API than `vac_update_relstats` — report, do not invent.

## Maintenance notes
- Plan 056 (snapshot SI) will rework visibility; xmax freeze must stay compatible with FrozenTransactionId.
- Plan 055 (live edge count) may reclaim tombstones later; freeze must leave deleted slots classifiable without clog.
- Reviewer: check aborted-delete clearing of `GPH_FLAG_DELETED` cannot race a concurrent reader under the single-writer contract.
