# Security Policy

TriDB is a research-grade, single-node DBMS: a fork of MSVBASE (a PostgreSQL 13.4 fork) plus a native
graph access method, run as an **embedded engine inside a PostgreSQL backend**. It exposes no network
service of its own and runs with the privileges of the Postgres process that loads it. This document
covers how to report a vulnerability and the known security considerations a reviewer or operator
should be aware of.

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue or PR:

- Preferred: open a [GitHub private security advisory](https://github.com/ConsultingFuture4200/tridb/security/advisories/new)
  ("Report a vulnerability") on this repository.
- If you cannot use GitHub advisories, contact the maintainers through the repository's listed contact
  channel. *(Maintainers: add a dedicated security email here if you want one.)*

Please include: the affected component (file/operator), a reproduction (SQL or steps), the impact, and
any suggested remediation. We aim to acknowledge reports promptly; because this is a
volunteer/research project, please allow reasonable time for a fix before public disclosure.

## Supported versions

TriDB is pre-1.0 and under active development. Security fixes are applied to the `master` branch only;
there are no maintained release branches yet.

## Known security considerations (by design — read before deploying)

These are **intentional v1 scoping decisions**, documented so reviewers and operators are not surprised.
None is a vulnerability *in v1's intended use*, but each is a real surface if the engine is exposed
beyond that use.

- **The `tjs()` / `multicol_topk()` operators take raw SQL-fragment arguments** (`attr_exp`,
  `filter_exp`, `orderby_exp`) that are interpolated into the vector-leg query — they are SQL
  *expressions*, so they cannot be parameter-bound or quoted (the same design as MSVBASE's
  `topk`/`multicol_topk`). **They are therefore a SQL-injection surface IF fed untrusted input.** In v1
  they are fed **exclusively** from the controlled lowering of the single canonical query (ADR-0007,
  DEV-1167), never from end users, which is the mitigation. **Do not expose these operators' expression
  arguments to untrusted callers.** A future multi-query surface must validate/bind these fragments
  before exposing the operators externally. (The `table_name` argument is resolved via the catalog
  with `RangeVarGetRelid` and then interpolated as a quoted identifier (`quote_identifier`), so it
  cannot break out of the generated SQL.)
- **No multi-tenant isolation / row-level security is implemented** beyond what stock PostgreSQL
  provides. TriDB is a single-tenant, local-hardware engine; do not treat it as a hardened multi-user
  service.
- **The engine is a PostgreSQL *fork*** pinned at 13.4 (MSVBASE's fork point). It does **not** receive
  upstream PostgreSQL security backports automatically. Operators must account for this when running it
  anywhere reachable; treat it as a research engine, not a maintained production Postgres.
- **Local baseline credentials are dev-only fixtures.** `baseline/` (the Milvus + Neo4j + Postgres
  comparison stack) uses placeholder credentials (e.g. `testpassword`, `postgres`) read from env with
  local defaults. These are **not** secrets and exist only to stand up the local benchmark; never reuse
  them anywhere real.
- **The inherited MSVBASE image entrypoint provisions a SUPERUSER open to `0.0.0.0/0`.** The vendored
  `scripts/pg_scripts/docker-entrypoint.sh` (baked into any TriDB-built image) creates a database
  superuser and appends a `host all all 0.0.0.0/0` rule to `pg_hba.conf` — appropriate only for a
  throwaway local dev container. **Any TriDB image published beyond a local dev box MUST** override the
  entrypoint to: scope `pg_hba.conf` to the container network (not `0.0.0.0/0`), drop SUPERUSER for the
  application role, and set a rotated, non-default `PGPASSWORD`. This is a publish-time checklist item,
  not a code change in this repo; TriDB does not use upstream's `dockerrun.sh` (which supplies a weak
  default password), but the superuser-on-all-interfaces posture is inherited by the image.

## Graph store container (`gstore`) hazards

The native graph store keeps its pages in `graph_store.gstore`, a container relation whose 32KB
blocks hold **non-heap page formats** (metapage, vertex pages, adjacency pages) managed by the
`gph_*` C functions. Treating it as a heap corrupts or crashes. Operators of anything longer-lived
than a benchmark must know:

- **Never `VACUUM`, `ANALYZE`, or `SELECT` the container directly.** Any heap-path access misreads
  the native pages — garbage line pointers, likely crash or corruption. The extension script
  REVOKEs PUBLIC access to `gstore` and PUBLIC EXECUTE on the mutators (`gph_insert_vertex`,
  `gph_insert_edge`) as containment; deployers grant mutator EXECUTE to trusted roles only. The
  read/traversal surface (`gph_neighbors`, `gph_traverse`, counters) stays PUBLIC-executable, but
  note the extension's `graph_store` schema itself carries no PUBLIC `USAGE` by default — grant
  schema `USAGE` to roles meant to query the graph.
- **Anti-wraparound autovacuum LIMITATION.** `gstore` is created with `autovacuum_enabled = false`,
  but that reloption does **not** exempt a relation from the forced anti-wraparound vacuum: once
  `age(relfrozenxid)` for `gstore` approaches `autovacuum_freeze_max_age`, PostgreSQL will vacuum
  it as a heap regardless — with the corruption consequences above. Long-lived deployments MUST
  monitor `age(relfrozenxid)` for `gstore` and treat approach to `autovacuum_freeze_max_age` as an
  operational stop-the-world event (dump/rebuild the graph, or halt writes) until the graph-store
  freeze pass ships.
- **Raw-xid visibility horizon.** Graph records store raw `xmin` values and visibility checks call
  `TransactionIdDidCommit` with no freeze path. Once the clog horizon passes a stored xid, lookups
  error (`could not access status of transaction`); past 2^31 xids, visibility comparisons flip.
  Same monitoring applies: `age(relfrozenxid)` growth on `gstore` tracks this exposure too.
- **Design note:** the specified fix (a `gph_freeze()` maintenance pass) is
  `docs/graph_store_freeze_design_v0.1.0.md`. Until it ships, TriDB's graph store is safe for
  benchmark- and research-lifetime workloads, not for deployments that burn through xids for
  months.

## Out of scope

- The vendored MSVBASE source under `vendor/` (re-cloned + patched at build time) — report upstream
  issues to [microsoft/MSVBASE](https://github.com/microsoft/MSVBASE); TriDB-specific patches under
  `scripts/patches/` are in scope.
- Denial-of-service from adversarial query shapes against a deliberately-exposed engine (TriDB is not
  designed to be internet-facing).
