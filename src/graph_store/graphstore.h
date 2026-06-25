/*
 * graphstore.h — Native adjacency-list graph store: access-method surface.
 *
 * Shared types + the traversal-iterator contract for TriDB's native graph store. As of
 * DEV-1164/DEV-1165 this IS compiled: graph_am.c includes it and implements the v1 core
 * (page store, vertex/edge insert, and the Open/Next/Close traversal engine). The store builds
 * and its suites pass on the x86 standin (it is architecture-independent PG 13.4 access-method C
 * over 32KB pages); only the live GX10 ARM64 build + the 128GB benchmark remain hardware-gated.
 *
 * Implementing issues: DEV-1164 (adjacency-list access method, merged), DEV-1165 (traversal
 * iterator — this file's gs_* contract + the gph_traverse SRF), DEV-1166 (verify shared txn mgr).
 * Authoritative layout contract: docs/graph_store_layout_v0.1.0.md (DEV-1163). See also
 * docs/decisions/0003 (AM core), 0005 (traversal iterator), and src/graph_store/README.md.
 *
 * NON-NEGOTIABLE INVARIANTS this surface upholds:
 *   - TR-1: traversal is a Volcano iterator (Open/Next/Close) with EARLY TERMINATION.
 *     gs_getnext() yields exactly ONE edge per call, reading at most one adjacency page, so a
 *     LIMIT above stops before later chain pages are read. No blocking / full-materialization.
 *   - Never leave the Postgres process: runs inside the forked Postgres backend, sharing the ONE
 *     transaction manager and the ONE WAL. No second WAL, no cross-system transaction.
 *   - Graph topology is a native adjacency-list store over 32KB pages — never relational joins.
 *   - v1 supports a SINGLE edge label: :related_to (entity -> entity).
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

/*
 * Sentinel for "no vertex" / "no edge".
 *
 * Using UINT64_MAX (not 0) so that 0 remains a valid allocated id.
 * Matches the PostgreSQL pattern of InvalidBlockNumber = 0xFFFFFFFF.
 * graphstore_insert_vertex / graphstore_insert_edge never return this value.
 */
#define GRAPHSTORE_INVALID_ID  ((uint64_t) UINT64_MAX)

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
 * v1 implementation surface (graph_am.c).
 *
 * Mutation is exposed as SQL-callable functions, not a C handle API: gph_insert_vertex(),
 * gph_insert_edge(src, dst) (registered in graph_store_am--0.1.0.sql). They run inside the host
 * backend, emit WAL through the host's shared WAL only (GenericXLog), and commit/abort atomically
 * with the surrounding transaction (the FR-7 substrate, DEV-1166). The GraphStore* handle
 * lifecycle (graphstore_open/close/insert_*) sketched in earlier drafts is DEFERRED — v1 has no
 * cross-extension C consumer, so it is not needed.
 *
 * Traversal (TR-1 Open/Next/Close) is implemented internally in graph_am.c as a static engine
 * over GraphScanDescData:
 *
 *     bool gs_open (GraphScanDesc *scan, Relation rel, GraphVertexId start, GraphScanDirection);
 *     bool gs_getnext(Relation rel, GraphScanDesc *scan, GraphElement *out);  // one EDGE per call
 *     void gs_close(GraphScanDesc *scan);
 *
 * The Relation is caller-managed (not retained by the scan) and no buffer pin is held across
 * Next() calls, so the iterator is leak-free under early abandon (LIMIT). It is surfaced to SQL /
 * SPI consumers as two SRFs: gph_neighbors(src) -> SETOF bigint (out-neighbor vids) and
 * gph_traverse(src) -> TABLE(src, dst) (one :related_to edge per Next()). Cross-extension
 * consumers (e.g. the TJS operator in vectordb, DEV-1169) drive the traversal through these SRFs
 * via SPI rather than C-linking the static engine across shared-object boundaries.
 *
 * EARLY TERMINATION is the load-bearing contract: gs_getnext() reads at most one adjacency page
 * per call, so an enclosing LIMIT stops before later chain pages are read — never "collect all
 * then return". The gph_traverse SRF MUST be used in a target-list / ProjectSet position, not a
 * FROM-clause FunctionScan (which materializes to a tuplestore and forfeits early termination).
 * ------------------------------------------------------------------------- */

#ifdef __cplusplus
}
#endif

#endif /* TRIDB_GRAPH_STORE_GRAPHSTORE_H */
