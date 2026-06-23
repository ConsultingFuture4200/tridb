/*
 * graphstore.h — Native adjacency-list graph store: access-method surface.
 *
 * // GX10-GATED: not built off-target.
 *
 * This header declares the C surface for TriDB's native graph store. It is an
 * INTERFACE SKELETON only — there is NO implementation in this file or this
 * directory. The implementing C lives behind the GX10 build gate (ARM64+CUDA,
 * 128GB), because it must compile against a live MSVBASE / PostgreSQL 13.4 fork
 * built with --with-blocksize=32 (32KB pages). It cannot be built on the dev box.
 *
 * Implementing issues: DEV-1164 (adjacency-list access method),
 * DEV-1165 (graph traversal iterator), DEV-1166 (verify shared txn manager).
 *
 * Authoritative layout contract: docs/graph_store_layout_v0.1.0.md (DEV-1163).
 * Build/gating status: docs/STATUS.md. See also src/graph_store/README.md.
 *
 * NON-NEGOTIABLE INVARIANTS this surface must uphold:
 *   - TR-1: traversal is a Volcano iterator (Open/Next/Close) with EARLY
 *     TERMINATION. gs_getnext() yields exactly ONE vertex-or-edge per call.
 *     No blocking / no full-materialization operators.
 *   - Never leave the Postgres process: this access method runs inside the
 *     forked Postgres backend and shares the ONE existing transaction manager
 *     and the ONE WAL. No second WAL, no cross-system transaction.
 *   - Graph topology is a native adjacency-list ACCESS METHOD backed by a
 *     B-tree over 32KB pages — never relational join tables.
 *   - v1 supports a SINGLE edge label: :related_to (entity -> entity).
 *
 * The header is kept compilable-shaped (include guards, typedefs, opaque
 * handles) so the GX10 implementer drops in C against a known surface rather
 * than designing one from zero. It is intentionally NOT compiled here.
 */

#ifndef TRIDB_GRAPH_STORE_GRAPHSTORE_H
#define TRIDB_GRAPH_STORE_GRAPHSTORE_H

/*
 * PostgreSQL 13.4 access-method API dependencies.
 *
 * These includes are the contract against the host Postgres fork. They are
 * NOT resolvable on the dev box (no server headers installed); they resolve
 * only inside the GX10 MSVBASE build tree. Listed explicitly so the surface
 * names its dependencies.
 *
 *   #include "postgres.h"          // base types, elog, palloc
 *   #include "access/amapi.h"      // IndexAmRoutine — access-method vtable
 *   #include "access/genam.h"      // generic AM scan structs
 *   #include "access/relscan.h"    // scan-descriptor base
 *   #include "storage/itemptr.h"   // ItemPointerData (block,offset on 32KB pages)
 *   #include "storage/bufmgr.h"    // shared buffer access (no private WAL)
 *   #include "utils/relcache.h"    // Relation
 *   #include "nodes/tidbitmap.h"   // (future) bitmap-scan seam
 *
 * They are kept as comments so this skeleton stays parseable off-target while
 * still documenting the exact PG 13.4 symbols the implementation binds to.
 */

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* -------------------------------------------------------------------------
 * On-disk geometry constants (mirror docs/graph_store_layout_v0.1.0.md).
 *
 * The fork builds Postgres with --with-blocksize=32, so every on-disk
 * adjacency page is 32KB. These constants exist so callers and the layout
 * spec share one source of truth; the implementation MUST static-assert them
 * against the live BLCKSZ at build time on GX10.
 * ------------------------------------------------------------------------- */

/* 32KB pages: the only block size this access method targets. */
#define GRAPHSTORE_BLOCKSZ          (32 * 1024)

/* v1 supports exactly one edge label. */
#define GRAPHSTORE_EDGE_LABEL       "related_to"

/* -------------------------------------------------------------------------
 * Opaque handle types.
 *
 * Callers hold these by pointer only. Their layouts are private to the
 * implementation (defined in the .c behind the GX10 gate); this header
 * forward-declares them so the surface compiles without exposing internals.
 * ------------------------------------------------------------------------- */

/*
 * GraphStore — a single native adjacency-list graph store instance, bound to
 * a host Postgres Relation and the current transaction's resource owner.
 * Opaque: lifecycle is graphstore_open() ... graphstore_close().
 */
typedef struct GraphStoreData GraphStore;
typedef GraphStore *GraphStoreHandle;

/*
 * GraphScanDesc — the Volcano traversal-iterator descriptor (TR-1).
 * Opaque: created by gs_open(), advanced one element per gs_getnext(),
 * released by gs_close(). Never materializes the full traversal frontier.
 */
typedef struct GraphScanDescData GraphScanDesc;
typedef GraphScanDesc *GraphScanDescHandle;

/* -------------------------------------------------------------------------
 * Stable identifiers.
 *
 * Vertex and edge ids are 64-bit logical identifiers, independent of physical
 * placement. The adjacency-list B-tree maps GraphVertexId -> on-page offset.
 * The physical ItemPointer (block,offset over 32KB pages) is private to the
 * implementation and never exposed across this surface.
 * ------------------------------------------------------------------------- */

typedef uint64_t GraphVertexId;
typedef uint64_t GraphEdgeId;

/* Sentinel for "no vertex" / "no edge". */
#define GRAPHSTORE_INVALID_ID  ((uint64_t) 0)

/*
 * Traversal direction for a scan. v1 canonical query is a forward
 * (src)-[:related_to]->(dst) walk; the enum reserves the other directions
 * for the layout spec without committing the implementation to them in v1.
 */
typedef enum GraphScanDirection
{
    GRAPH_SCAN_OUTGOING = 0,    /* src -> dst over :related_to (v1 canonical) */
    GRAPH_SCAN_INCOMING = 1,    /* dst <- src (reserved)                      */
    GRAPH_SCAN_BOTH     = 2     /* undirected view (reserved)                 */
} GraphScanDirection;

/*
 * What a single Next() step yielded. The iterator emits ONE element per call;
 * this tag tells the caller whether the current element is a vertex or an edge
 * so a traversal operator can pull exactly the next adjacency without
 * materializing the frontier.
 */
typedef enum GraphElementKind
{
    GRAPH_ELEM_NONE   = 0,      /* scan exhausted (gs_getnext returned false)  */
    GRAPH_ELEM_VERTEX = 1,
    GRAPH_ELEM_EDGE   = 2
} GraphElementKind;

/*
 * GraphElement — the single vertex-or-edge produced by one gs_getnext() step.
 * Owned by the GraphScanDesc; valid only until the next gs_getnext() or
 * gs_close(). The caller must copy out anything it needs to retain. Payload
 * pointers reference shared-buffer-backed tuples and must not be freed by the
 * caller.
 */
typedef struct GraphElement
{
    GraphElementKind kind;

    /* Set when kind == GRAPH_ELEM_VERTEX. */
    GraphVertexId    vertex_id;

    /* Set when kind == GRAPH_ELEM_EDGE: the :related_to edge endpoints. */
    GraphEdgeId      edge_id;
    GraphVertexId    edge_src;
    GraphVertexId    edge_dst;

    /*
     * Opaque payload tuple for the current element (e.g. the vertex's stored
     * columns surfaced via GRAPH_TABLE COLUMNS, such as embedding/chunk/
     * timestamp). NULL when the element carries no payload. Layout is defined
     * by the layout spec; treated as opaque bytes at this surface.
     */
    const void      *payload;
    size_t           payload_len;
} GraphElement;

/* -------------------------------------------------------------------------
 * Adjacency-list store lifecycle and mutation.
 *
 * All functions run inside a host Postgres backend and participate in the
 * current transaction. They emit WAL through the host's shared WAL only;
 * there is no private log. They raise via the host's elog/ereport on error
 * (no error return codes) — consistent with the PG 13.4 access-method API.
 * ------------------------------------------------------------------------- */

/*
 * graphstore_open — bind a graph store instance to an already-open host
 * Relation (the adjacency-list access-method index relation) for the current
 * transaction.
 *
 * Contract:
 *   - `index_relation` is a PostgreSQL Relation* for the graph access method,
 *     opened by the caller with the appropriate lock mode (typed as void* in
 *     this skeleton to avoid the off-target PG header dependency).
 *   - Returns a handle valid until graphstore_close(); the handle does NOT own
 *     the Relation and must not outlive the transaction.
 *   - Does not start a transaction and does not open a WAL: it joins the
 *     current backend's transaction and shared WAL.
 *   - Raises (ereport ERROR) if the relation is not a graph access method or
 *     was built under a block size other than GRAPHSTORE_BLOCKSZ.
 */
extern GraphStoreHandle graphstore_open(void *index_relation);

/*
 * graphstore_close — release a graph store handle.
 *
 * Contract:
 *   - Releases per-handle scan/buffer state; does NOT close the underlying
 *     Relation (the caller owns it) and does NOT commit or abort — durability
 *     is the host transaction manager's responsibility.
 *   - Idempotent on a NULL handle (no-op).
 */
extern void graphstore_close(GraphStoreHandle store);

/*
 * graphstore_insert_vertex — insert a vertex into the adjacency-list store.
 *
 * Contract:
 *   - `payload`/`payload_len` carry the vertex's stored columns (opaque here;
 *     layout per the spec). May be NULL/0 for a topology-only vertex.
 *   - Returns the assigned GraphVertexId (never GRAPHSTORE_INVALID_ID on
 *     success).
 *   - WAL-logged via the host shared WAL; visible/durable under the current
 *     transaction's commit. Raises on duplicate id or page-allocation failure.
 */
extern GraphVertexId graphstore_insert_vertex(GraphStoreHandle store,
                                              const void *payload,
                                              size_t payload_len);

/*
 * graphstore_insert_edge — insert one directed :related_to edge (src -> dst).
 *
 * Contract:
 *   - v1 edge label is fixed to GRAPHSTORE_EDGE_LABEL; this function inserts an
 *     edge of that label only.
 *   - Both `src` and `dst` must already exist (raises otherwise).
 *   - Appends `dst` to `src`'s adjacency list on its 32KB page, splitting via
 *     the B-tree when the list overflows the page. WAL-logged via shared WAL.
 *   - Returns the assigned GraphEdgeId (never GRAPHSTORE_INVALID_ID on
 *     success).
 */
extern GraphEdgeId graphstore_insert_edge(GraphStoreHandle store,
                                          GraphVertexId src,
                                          GraphVertexId dst);

/* -------------------------------------------------------------------------
 * Volcano traversal iterator (TR-1): Open / Next / Close.
 *
 * gs_getnext() yields EXACTLY ONE vertex-or-edge per call so an enclosing
 * operator can stop early (e.g. ORDER BY <-> ... LIMIT 5) without ever
 * materializing the traversal frontier. This is the load-bearing contract for
 * the efficiency thesis — it MUST NOT be implemented as
 * "collect all then return".
 * ------------------------------------------------------------------------- */

/*
 * gs_open — begin a traversal scan from a starting vertex.
 *
 * Contract:
 *   - `start` is the source vertex; for the v1 canonical query this is the
 *     `src:entity` bound by the GRAPH_TABLE MATCH pattern.
 *   - `direction` selects which adjacency to walk (v1: GRAPH_SCAN_OUTGOING).
 *   - Returns a scan descriptor positioned BEFORE the first element; the first
 *     gs_getnext() returns the first element. Returns NULL only if `start`
 *     does not exist.
 *   - Allocates lazily: opening a scan must NOT read the whole adjacency list.
 *   - The scan pins buffers from the host shared buffer pool; gs_close()
 *     releases them.
 */
extern GraphScanDescHandle gs_open(GraphStoreHandle store,
                                   GraphVertexId start,
                                   GraphScanDirection direction);

/*
 * gs_getnext — advance the scan by ONE element (the Next() of Open/Next/Close).
 *
 * Contract:
 *   - Produces exactly one vertex-or-edge per call into `*out` and returns
 *     true; returns false when the scan is exhausted (and sets out->kind =
 *     GRAPH_ELEM_NONE).
 *   - `*out` (and any payload it points to) is owned by the scan and valid
 *     only until the next gs_getnext()/gs_close(); the caller copies out what
 *     it must retain.
 *   - MUST be incremental: each call advances at most one adjacency step. No
 *     blocking, no look-ahead materialization. This is what enables early
 *     termination by the enclosing LIMIT operator.
 */
extern bool gs_getnext(GraphScanDescHandle scan, GraphElement *out);

/*
 * gs_close — end a traversal scan (the Close() of Open/Next/Close).
 *
 * Contract:
 *   - Releases the scan descriptor and unpins any buffers it held. Does NOT
 *     affect the owning GraphStore handle or the transaction.
 *   - Idempotent on a NULL handle (no-op). Safe to call after early
 *     termination (before the scan is exhausted).
 */
extern void gs_close(GraphScanDescHandle scan);

#ifdef __cplusplus
}
#endif

#endif /* TRIDB_GRAPH_STORE_GRAPHSTORE_H */
