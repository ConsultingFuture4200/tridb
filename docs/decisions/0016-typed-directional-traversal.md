# ADR-0016: Typed + directional + source-scoped traversal; backlinks (direction=in) design call

**Status:** Accepted (2026-07-04) — typed + source-scope + out-direction SHIPPED (plan 038 / DEV-1350,
authored, GX10-unbuilt); backlinks (`direction=in`/`both`) DEFERRED to a follow-on format-touching plan.
**Issue:** DEV-1350
**Related:** ADR-0005 (traversal iterator — the shared `gs_*` engine this extends), ADR-0002/0003
(adjacency-list layout + v1 core AM), ADR-0013 (id-map), plan 037 (typed `remove_edge`),
`docs/gbrain_backend_hardening_v0.1.0.md` G3+G4/Phase-B2.

## Context

gBrain's knowledge graph is **typed and directional**: `link_types`
(`founded/founded_by`, `works_at/employs`, `mentions`, `attended`) with declared inverses, traversed by
type and direction (`traversePaths(direction: in|out|both, linkType)`, `getBacklinks`), always
**source-scoped** (a past unscoped walk was a P0 tenancy leak). TriDB's on-disk format already carries the
fields (`GphEdgeSlot.es_edge_type_id`, `es_src_vid`), but the pre-038 code hardcoded a single type
(`GPH_EDGE_TYPE_RELATED_TO`) and out-direction only. So **typing is a code change, not a format change**.

## Decision

1. **Typed insert (additive):** 3-arg `gph_insert_edge(src, dst, type_id)` writes the caller's
   dictionary id into the existing `es_edge_type_id`; the 2-arg form defaults to `RELATED_TO` and is
   byte-identical to pre-038. A relational `edge_type(id, name)` dictionary + owner-guarded
   `register_edge_type(name)` map gBrain's link-type names to ids (topology native, name↔id mapping
   relational — golden rule 3). No slot-layout change (the 32-byte asserts are untouched).

2. **Typed + source-scoped out-traversal (additive):** `gs_open`/`gs_getnext` gain `type_filter`
   (`GPH_EDGE_TYPE_ANY`=0 = any, else a type id) and `source_scope` (`GRAPHSTORE_INVALID_ID` = unscoped,
   else a source vid), applied **inline per slot** in `gs_getnext` — never a pre-collected set, so TR-1
   early termination is preserved. Defaults `(RELATED_TO, INVALID)` reproduce the old single filter line
   exactly, so `gph_traverse`/`gph_neighbors` and the canonical query are byte-identical. Surfaced as
   `gph_traverse_typed(src, type_id, direction, source_id)`.

3. **Direction / backlinks — the design call.** The adjacency list is **out-edges only** (a vertex's
   `vr_adj_head` chains its *outgoing* `GphEdgeSlot`s). `direction=in`/`getBacklinks` needs a reverse
   (dst→src) lookup, for which there are two options:

   | Option | Mechanism | Cost | Verdict |
   |---|---|---|---|
   | (a) reverse **index/adjacency** | second per-vertex chain of *incoming* edges, or a `(dst→src)` btree, written on every `gph_insert_edge` | +1 page-chain (≈2× edge storage) + a second GenericXLog page per insert; **new metapage field or slot bit** to anchor the reverse head/tail | **format-touching** — needs its own ADR + the frozen-core discipline (ADR-0003) + a concurrency probe |
   | (b) bounded reverse **scan** | scan all adjacency pages, emit slots whose `es_dst_vid == target` | O(E) per backlink query — a full store scan; **violates TR-1's efficiency thesis** for a hot read path | rejected as the primary path |

   **We ship out-direction now and DEFER the reverse index (option a) to a separate, format-touching
   plan.** `direction=in`/`both` **raise** `feature_not_supported` (they do NOT silently fall back to
   out — that would drop in-edges and lie about the result). Repurposing a reserved slot/metapage field
   for the reverse head is *size-preserving* and therefore allowable, but it still changes the write path
   and crash/concurrency surface, so it earns its own ADR rather than riding this traversal plan — matching
   plan 038's STOP condition ("the reverse index needs a slot-layout or metapage-format change — stop, ship
   out-direction, file the reverse index as a separate format-touching plan").

## Consequences

- gBrain typed traversal (`traversePaths(linkType)`, source scope) runs on the native AM today; the
  default path is invisible to every pre-038 caller (parity oracle in `test/graph_typed_traversal_test.sql`).
- `getBacklinks` / `direction=in` are a named follow-on (reverse adjacency, option a) — not faked.
- **Source-scope caveat (honest):** because adjacency chains are per-vertex, `es_src_vid` is uniform
  within one scan, so the native `source_scope` filter is a defensive single-vertex assertion. gBrain's
  coarser *tenant* scoping (many vertices under one `source_id`) needs the relational vertex→source
  side-table (Phase-B3), joined at the surface — the native slot cannot express it without a format change.
  What the native store *does* guarantee structurally: every traversal is rooted at a start vertex; there
  is no unscoped all-edges walk to leak (the P0-leak class is unrepresentable here).
- Plan 037's `remove_edge` should gain the `type` arg once this ships (per its maintenance note).
