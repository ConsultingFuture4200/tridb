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

## Out of scope

- The vendored MSVBASE source under `vendor/` (re-cloned + patched at build time) — report upstream
  issues to [microsoft/MSVBASE](https://github.com/microsoft/MSVBASE); TriDB-specific patches under
  `scripts/patches/` are in scope.
- Denial-of-service from adversarial query shapes against a deliberately-exposed engine (TriDB is not
  designed to be internet-facing).
