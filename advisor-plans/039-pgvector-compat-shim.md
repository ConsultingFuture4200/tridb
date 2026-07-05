# Plan 039: pgvector-compat shim for gBrain (gBrain vector-leg / DEV pending)

> **Executor instructions**: This is a BUILD/packaging plan (GX10-gated docker work), largely already
> executed + proven. Update your row in `advisor-plans/README.md` if you extend it.
>
> **Status: PROVEN + IMAGE BUILT (2026-07-04).** `scripts/add_pgvector.sh` builds pgvector v0.8.0 into
> a TriDB fork image; validated live on the Spark (see below).

## Status
- **Priority**: P1 (unblocks the gBrain-on-TriDB adapter + the head-to-head benchmark)
- **Effort**: S (pgvector is a portable standalone extension; no fork surgery)
- **Risk**: LOW — additive; does NOT touch `vectordb`, the graph AM, or any TriDB core
- **Depends on**: a built fork image (`tridb/msvbase:gx10-v1`)
- **Category**: integration / packaging
- **Planned + executed at**: commit — (this batch), 2026-07-04
- **gBrain spec**: `docs/gbrain_backend_hardening_v0.1.0.md` G6/B4 (cosine) — supersedes the
  "normalize-at-write / IP-body" approaches for the gBrain direction.

## Why (the decision)

gBrain assumes **pgvector**: `content_chunks.embedding vector(1536)`, `<=>` cosine, `'[...]'` literals,
`USING hnsw (embedding vector_cosine_ops)`. TriDB's own vector leg is `vectordb` — `float8[]` + `<->`
+ `'{...}'` literals + `WITH (dimension, distmethod)` — **incompatible at the type level**, and there is
no pgvector in the MSVBASE fork. The maintainer chose the **pgvector-compat shim** so gBrain's vector
code runs UNMODIFIED, rather than rewriting gBrain's queries adapter-side.

The key architectural fact that makes this trivial: **gBrain fuses app-side** (it calls `searchVector`,
`searchKeyword`, `traverseGraph` separately and RRF-fuses in TypeScript) — it never uses TriDB's TJS
operator. So gBrain-on-TriDB needs only **pgvector (vector leg) + `graph_store_am` (native graph leg)**
in one database. We do **not** load `vectordb`, so pgvector's `hnsw` access method does not collide with
TriDB's `hnsw` AM. The benchmark then cleanly isolates the real thesis — **native-graph traversal vs
gBrain's relational-`links` + recursive-CTE, holding the vector (pgvector) and BM25 (tsvector) legs
constant.**

## What was done + proven (live on the Spark, `tridb/msvbase:gx10-v1`, aarch64 / PG 13.4)

1. `scripts/add_pgvector.sh` clones pgvector v0.8.0, `make` + `make install` against the image's
   `pg_config` — **builds clean on ARM64 / PG 13.4**; committed image `tridb/msvbase:gx10-v1-pgv`.
2. **pgvector functional on ARM:** `CREATE EXTENSION vector`; `vector(4)` column; `'[...]'` literals;
   `CREATE INDEX ... USING hnsw (e vector_cosine_ops)`; `<=>` cosine query returns correct ranking.
3. **Coexistence proven:** in ONE database, `CREATE EXTENSION vector` + `CREATE EXTENSION graph_store_am`
   both succeed; a pgvector cosine query AND a native `graph_store.neighbors()` traversal both return
   correct results in the same process/txn manager. (`vectordb` NOT loaded → no `hnsw` AM collision.)

## Remaining (to make it a first-class build artifact)
- **Wire into the reproducible build**: `scripts/gx10build.sh` should accept a `--with-pgvector` flag
  (or a post-build hook) that runs `scripts/add_pgvector.sh` so the `-pgv` image is regenerated from
  scratch, not just a committed container. Pin the pgvector tag (v0.8.0).
- **CREATE EXTENSION order in the gBrain adapter**: `vector` before any `vector(N)` DDL; `graph_store_am`
  before any `gph_*` call. The adapter's `initSchema` owns this (plan: the TriDBEngine).
- **Multi-vector dims (gBrain B5):** gBrain also has `embedding_image vector(1024)` /
  `embedding_multimodal vector(1024)` — pgvector supports multiple vector columns of differing dim per
  row natively, so B5 is satisfied by pgvector with no extra work.
- **Not needed anymore for gBrain:** the normalize-at-write / true-IP tjs-body work (G6b) and A2 (TriDB's
  own HNSW abort-durability) — gBrain's vectors are pgvector, not TriDB `vectordb`. A2 remains relevant
  only for a *pure-TriDB* tri-modal deployment, not the gBrain backend. Re-scope accordingly.

## Non-regression
Purely additive: a new extension in the image; `vectordb` / `graph_store_am` / the TJS operator / the
frozen graph core are all untouched. A pure-TriDB deployment that never `CREATE EXTENSION vector` is
byte-identical.

## Done criteria
- [x] pgvector builds + installs on the fork image (aarch64 / PG 13.4)
- [x] pgvector cosine + hnsw functional on ARM
- [x] pgvector + graph_store_am coexist in one DB (vector + graph legs both correct)
- [x] `scripts/add_pgvector.sh` reproduces the `-pgv` image
- [ ] `--with-pgvector` wired into `scripts/gx10build.sh` (reproducible from scratch)
- [ ] a smoke test in the engine suite that asserts the coexistence (like the sql above)
