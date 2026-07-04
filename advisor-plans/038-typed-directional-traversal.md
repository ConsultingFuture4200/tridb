# Plan 038: typed + directional + source-scoped native traversal (gBrain-B2 / DEV-1350)

> **Executor instructions**: Follow step by step; honor STOP conditions. Native graph-store C
> (GX10-gated build): author + static-verify here, engine build on the GX10. Update your row in
> `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat cb3eb0a..HEAD -- src/graph_store`
> Re-read the live insert + traversal in `graph_am.c` and the slot layout in `gph_page.h` before editing.

## Status
- **Priority**: P1
- **Effort**: M (traversal) + a design call on backlinks (reverse lookup)
- **Risk**: MED (traversal is the hot read path; must stay TR-1 Open/Next/Close + early-terminating)
- **Depends on**: none hard; complements plan 037 (typed `remove_edge`) and the B3 edge-property side-table
- **Category**: feature (data-model parity)
- **Planned at**: commit `cb3eb0a`, 2026-07-04
- **Linear**: DEV-1350 · **gBrain spec**: `docs/gbrain_backend_hardening_v0.1.0.md` G3+G4/Phase-B2

## Why this matters

gBrain's graph is **typed and directional**: `link_types` like `founded/founded_by`, `works_at/employs`,
`mentions`, `attended` with declared inverses, traversed by type and direction (`traversePaths(direction:
in|out|both, linkType)`, `getBacklinks`), always **source-scoped** (`source_id` is the tenancy/ACL
boundary — a past unscoped walk was a P0 leak). TriDB's format already carries the fields but the code
hardcodes a single type, out-direction only:
- Insert hardcodes: `es.es_edge_type_id = GPH_EDGE_TYPE_RELATED_TO` (`src/graph_store/graph_am.c:417`),
  `vr.vr_label_id = 1` (`:284`).
- Traversal filters to that one type: `if (slot->es_edge_type_id != GPH_EDGE_TYPE_RELATED_TO) continue;`
  (`src/graph_store/graph_am.c:653`).
- The slot **already has** `uint32 es_edge_type_id` (`gph_page.h:91`) and `uint32 vr_label_id` (`:76`);
  only one type constant is defined (`gph_page.h:32`). So typing is a code change, **not** a format change.

## Current state

- `src/graph_store/gph_page.h:32` `GPH_EDGE_TYPE_RELATED_TO 1`; `:76` `vr_label_id`; `:91` `es_edge_type_id`.
- `src/graph_store/graph_am.c:417` insert hardcode; `:653` traversal type filter; the traversal engine is
  `gs_open`/`gs_getnext`/`gs_close` (the Open/Next/Close iterator, TR-1). Adjacency is **out-edges only**
  (no reverse index) — backlinks/`direction=in` need a design call (Step 3).
- The id-map (`gph_vid_map`, `graph_store_am--0.1.0.sql`) already maps arbitrary external ids ↔ dense vids —
  reuse for gBrain page ids; the type dictionary is analogous.
- In-tree PGXS (edit `.c/.h/.sql`; `make graph-test`; no patch registration).

## Commands you will need
| Purpose | Command | Expected |
|---|---|---|
| Fast layer | `. .venv/bin/activate && make test && make lint` | green |
| Engine suite (GX10) | `make graph-test` | ALL PASS |
| Typed-traversal test | new `test/graph_typed_traversal_test.sql` | ALL PASS (GX10) |

## Scope

**In scope:**
1. **Typed insert** — add a `type_id` arg to `gph_insert_edge` / `add_edge`, writing the existing
   `es_edge_type_id` field (default `GPH_EDGE_TYPE_RELATED_TO` so all current callers + benches are
   byte-identical). 32-byte slot untouched.
2. **Type dictionary** — a small `graph_store.edge_type(name text, id int)` table (name↔id) so gBrain's
   `link_types` map to ids; owner-guarded registration function.
3. **Typed + direction + source filters on traversal** — `type_filter` (one/any), `direction` (out/in/both),
   and `source_id` scope params threaded into `gs_open`/`gs_getnext`, reading the existing field. Default
   args reproduce today's behavior exactly (out-only, `RELATED_TO`, unscoped) so the canonical query +
   `tjs()`/`neighbors()` are unchanged.
4. **Backlinks / `direction=in`** — see Step 3 STOP: pick reverse-index vs reverse-scan; if the reverse
   index is too invasive for this plan, ship `out` + `both`(via out only) now and file the reverse-index
   as a follow-on, honestly scoped.

**Out of scope:** per-edge **properties** (`link_source`, `context`, `origin`) — those go to the B3
relational side-table keyed on native `(src,dst,type)`, NOT here (topology stays native). Multi-hop path
materialization semantics beyond what `traversePaths` needs (cycle-safe + depth + frontier cap already
match TR-1). No slot-layout change.

## Git workflow
Branch `advisor/038-typed-traversal`; `feat(graph):` commits; do NOT push.

## Steps

### Step 1: Typed insert + dictionary (additive, default-preserving)
Add `type_id` (default `RELATED_TO`) to `gph_insert_edge`/`add_edge`; create the `edge_type` dictionary +
a `register_edge_type(name)` function. All existing callers pass nothing → identical bytes.
**Verify**: emitter/unit tests + `make test`/`lint` green; (GX10) an existing traversal test still ALL
PASS (default type unchanged); a new edge with a non-default type round-trips its `es_edge_type_id`.

### Step 2: Typed + source-scoped out-traversal
Thread `type_filter` + `source_id` into `gs_open`/`gs_getnext`, reading `es_edge_type_id` (and the
vertex/source scope). Preserve TR-1: still Open/Next/Close, still early-terminating, no full
materialization. Defaults = today's behavior.
**Verify (GX10)**: `test/graph_typed_traversal_test.sql` — mixed-type edges, traverse by one type / any
type / wrong type (empty), source-scoped (cross-source edges excluded); the canonical query + `tjs()`
answers **byte-identical** on the default path (parity oracle).

### Step 3: Direction / backlinks (design call)
Adjacency is out-only. For `direction=in`/`getBacklinks`, choose: (a) a reverse (dst→src) index/adjacency,
or (b) a bounded reverse scan. Capture the choice + cost in an ADR-style note. If (a) is too large for this
plan, ship out-direction + document backlinks as a scoped follow-on (do NOT fake it).
**Verify (GX10)**: whichever ships, a backlink test matches the recursive-CTE `traversePaths(direction:in)`
semantics gBrain expects (cycle-safe, depth + frontier cap).

## Test plan
`graph_typed_traversal_test.sql` (type filter, source scope, direction) + a **parity regression**: the
existing canonical/`tjs()`/`neighbors()` suites ALL PASS byte-identical on the default (untyped, out-only,
unscoped) path — typing must be invisible to current callers.

## Done criteria
- [ ] Typed insert + `edge_type` dictionary; default path byte-identical (parity oracle ALL PASS)
- [ ] `type_filter` + `source_id` + `direction` on traversal; TR-1 preserved (Open/Next/Close, early-term)
- [ ] Backlinks: reverse-lookup shipped OR honestly scoped as a follow-on with an ADR note
- [ ] `graph_typed_traversal_test.sql` + parity suites ALL PASS on GX10; `make test && make lint` green
- [ ] README row updated

## STOP conditions
- Any default-path (untyped/out/unscoped) answer changes — typing must be additive and invisible to
  current callers; if a test diverges, stop (do not re-pin).
- Threading the filters would force materializing the neighbor set (breaks TR-1) — keep the filter inline
  in `gs_getnext`, never a pre-collected set.
- The reverse index for backlinks needs a slot-layout or metapage-format change — stop, ship out-direction,
  and file the reverse index as a separate format-touching plan (frozen-core change needs its own ADR).

## Maintenance notes
This unlocks gBrain's typed knowledge graph on the native AM — the load-bearing retrieval upgrade
(`docs/gbrain_backend_hardening_v0.1.0.md`). Edge properties land in B3 (side-table). Plan 037's
`remove_edge` should gain the `type` arg once this ships. Reviewer focus: TR-1 preservation in the typed
`gs_getnext` (no materialization), default-path byte-identity, and source-scope correctness (the P0-leak
class).
