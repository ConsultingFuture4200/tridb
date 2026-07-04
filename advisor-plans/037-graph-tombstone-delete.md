# Plan 037: native graph delete via gph_tombstone_edge/vertex (gBrain-B1 / DEV-1349)

> **Executor instructions**: Follow step by step; honor STOP conditions. Native graph-store C
> (GX10-gated build): author + static-verify here, engine build + FR-7 re-run on the GX10. Update your
> row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat cb3eb0a..HEAD -- src/graph_store`
> Re-read the live `graph_am.c` traversal + `gph_page.h` flag defs before editing.

## Status
- **Priority**: P1
- **Effort**: S–M
- **Risk**: LOW (additive mutator against an existing, already-honored read seam)
- **Depends on**: none for the mutator; compaction of tombstoned slots later rides plan 036's freeze pass
- **Category**: feature (data-model parity)
- **Planned at**: commit `cb3eb0a`, 2026-07-04
- **Linear**: DEV-1349 · **gBrain spec**: `docs/gbrain_backend_hardening_v0.1.0.md` G2/Phase-B1

## Why this matters

gBrain deletes and re-attributes memories constantly: `removeLink`, `deleteFactsForPage`,
soft-delete/purge, and the nightly "dream" consolidation that merges/supersedes facts. TriDB's native
graph store has **no delete/update path** — a hard gap for that workload. But the seam is half-built:
`GPH_FLAG_DELETED` is defined (`src/graph_store/gph_page.h:40`) and **already honored on read**
(`src/graph_store/graph_am.c:651`, `if (slot->es_flags & GPH_FLAG_DELETED) continue;`) — nothing sets
it. So delete is a small additive mutator, not a redesign.

## Current state

- `src/graph_store/gph_page.h:40` — `#define GPH_FLAG_DELETED 0x0001` ("soft-delete (MVCC seam; unused
  in v1 core)"); `:92` — `uint32 es_flags`.
- `src/graph_store/graph_am.c:651` — the read path already filters `GPH_FLAG_DELETED`. Insert paths
  (`gph_insert_vertex` ~`:255`, `gph_insert_edge` ~`:376`) go through `GenericXLog`; mirror that for the flag set.
- No `gph_delete_*`/`gph_tombstone_*` exists (grep is empty) — this plan adds it.
- FR-7 seam: `scripts/txn_atomicity_test.sh` (extend with a delete round-trip).
- In-tree PGXS (edit `.c/.h/.sql` directly; built by `make graph-test`; no `scripts/patches/` registration).

## Commands you will need
| Purpose | Command | Expected |
|---|---|---|
| Fast layer | `. .venv/bin/activate && make test && make lint` | green |
| Engine suite (GX10) | `make graph-test` | ALL PASS |
| Delete round-trip | extend `scripts/txn_atomicity_test.sh` / a `test/graph_delete_test.sql` | ALL PASS (GX10) |

## Scope

**In scope:** `graph_store.gph_tombstone_edge(src, dst)` (and, once plan 038 lands typed edges, an
optional `type` arg) and `graph_store.gph_tombstone_vertex(vid)` — C functions that locate the slot(s)
and set `GPH_FLAG_DELETED` under `GenericXLog` (crash-safe, atomic with the host txn); the SQL surface in
`graph_store_am--0.1.0.sql` (owner-guarded per advisor-026 ACLs); a `removeLink`-shaped convenience
(`graph_store.add_edge` already exists as the insert compat — add a `remove_edge` compat mirroring it);
a `test/graph_delete_test.sql`; extend the FR-7 test with a delete that rolls back atomically. The read
path is **unchanged** (it already filters the flag).

**Out of scope:** physical compaction / slot reclamation of tombstoned records — that piggybacks on plan
036's `gph_freeze` page rewrite (note the seam). Vertex-delete cascade semantics beyond marking (gBrain's
`links` FK uses `ON DELETE CASCADE` app-side; here, tombstoning a vertex tombstones its out-edges — decide
and document whether in-edges are also swept, see STOP). HNSW vector delete (already works via VACUUM/`markDelete`).

## Git workflow
Branch `advisor/037-graph-tombstone`; `feat(graph):` commits; do NOT push.

## Steps

### Step 1: Edge tombstone
`gph_tombstone_edge(src,dst)`: locate the edge slot(s) for `(src→dst)`, set `GPH_FLAG_DELETED` under
`GenericXLog`. Idempotent (tombstoning an already-deleted or absent edge is a no-op, not an error).
**Verify (GX10)**: insert an edge, traverse (present), tombstone, traverse (absent); the same under a
rolled-back txn leaves the edge present (FR-7).

### Step 2: Vertex tombstone
`gph_tombstone_vertex(vid)`: set the vertex flag AND tombstone its out-edges (so traversal from/over it
stops). Decide + document in-edge handling (see STOP).
**Verify (GX10)**: tombstoned vertex is invisible as a source and its out-edges vanish from traversal;
atomic under rollback.

### Step 3: SQL surface + FR-7
Expose `gph_tombstone_edge/vertex` + a `remove_edge` compat in `graph_store_am--0.1.0.sql` (owner-guarded).
Extend `scripts/txn_atomicity_test.sh` (or `test/graph_delete_test.sql`) with a delete-then-rollback and a
delete-then-commit-then-crash-recovery assertion.
**Verify (GX10)**: `make graph-test` ALL PASS incl. the new delete cases; `make test && make lint` green.

## Test plan
`graph_delete_test.sql`: insert→traverse→tombstone→traverse (gone)→rollback path (still there); FR-7
atomicity + crash-recovery extended with a delete. Read-path behavior is unchanged (regression check: the
existing traversal suites still pass).

## Done criteria
- [ ] `gph_tombstone_edge/vertex` set `GPH_FLAG_DELETED` under GenericXLog; idempotent; read path untouched
- [ ] SQL surface owner-guarded; `remove_edge` compat added
- [ ] Delete round-trip + FR-7 atomicity (delete rolls back) + crash-recovery ALL PASS on GX10
- [ ] `make test && make lint` green; README row updated

## STOP conditions
- Setting the flag would require touching the read path or the 32-byte slot layout (it must not — the
  flag field already exists and is already read).
- In-edge sweep on vertex-delete needs a reverse index the store doesn't have yet (plan 038 introduces
  backlink traversal) — if so, tombstone the vertex + its out-edges now, document that dangling in-edges
  to a tombstoned vertex are filtered at read time (the target vertex reads as deleted), and note the
  full reverse-sweep as a 038 follow-on. Do NOT block.
- Any existing traversal/FR-7 assertion regresses.

## Maintenance notes
Physical reclamation of tombstoned slots is deliberately deferred to plan 036's freeze/compaction pass —
tombstones accumulate until then (fine at gBrain scale). Once plan 038 lands typed edges, add the optional
`type` arg so `removeLink(from,to,linkType)` maps exactly. Reviewer focus: FR-7 atomicity of the flag set
and the in-edge-handling decision.
