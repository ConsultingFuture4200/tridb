# Plan 075: Lower the canonical TriDB query through the stock operator

> **Executor instructions**: Read ADR-0019 and the canonical-query tests before editing. Follow the
> exact v1 template; do not create a new query language or broaden its grammar. Skip the advisor
> index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- src/graph_store/graph_store_am--0.1.0.sql src/tjs_pg/ test/ Makefile .github/workflows/ci.yml README.md spec/ docs/decisions/0019-tjs-open-stock-pg-rehome.md`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: 071, 072
- **Category**: bug / migration / tests
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

The documented v1 front door is `graph_store.graph_query(text)`, but its lowering only recognizes the
fork's seven/eight-argument `tjs` function. A stock install can create both extensions and call
`tjs_open` directly, yet the canonical query fails. This plan connects the already pinned canonical
template to the stock operator and proves end-to-end behavior on PG16/17.

## Current state

- `src/graph_store/graph_store_am--0.1.0.sql:373-407` explains why v1 accepts a single pinned text
  template: stock PG cannot parse bare `GRAPH_TABLE` yet.
- Lines 435-451 parse that template and currently recognize the fork brace vector literal.
- Lines 502-522 only resolve the fork `tjs` signatures.
- `src/tjs_pg/tjs_pg--0.1.0.sql:24-36` exposes stock
  `tjs_open(regclass, k, term_cond, m_seeds, hops, id_col, filter, query vector, src, edge_type)`.
- The canonical wrapper returns `SETOF text` chunks. Stock `tjs_open` returns entity IDs, so an
  adapter must join those IDs back to the canonical `entities` relation and preserve emitted order.
- `Makefile`'s `STOCK_TESTS` lacks a canonical-query suite, while fork `ENGINE_TESTS` includes
  canonical parsing/e2e tests.
- `README.md:153` currently implies the stock front door already lowers successfully.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused PG17 | `bash scripts/pg17_graph_test.sh tridb/pg17-unfork:dev test/canonical_stock_e2e_test.sql` | all PASS markers |
| Full stock | `make stock-graph-test` | all listed suites pass |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `src/graph_store/graph_store_am--0.1.0.sql`
- `test/canonical_stock_e2e_test.sql` (create)
- `Makefile`
- `.github/workflows/ci.yml`
- `README.md`
- `spec/tridb_spec_v0.1.0.md` (addendum only) or a new versioned addendum
- `docs/decisions/0019-tjs-open-stock-pg-rehome.md` (addendum only)

**Out of scope**:
- General SQL/PGQ parsing, PG19 native `GRAPH_TABLE`, or a second canonical query.
- Relational edge joins; graph topology must remain the native graph AM.
- Changes to `tjs_open` ranking/traversal internals.
- Pretending the canonical edge label has a mapping that is not present in catalog state.

## Git workflow

Use `dustin/dev-NNNN` after issue assignment. Suggested commit:
`feat(query): lower canonical query on stock pg`.

## Steps

### Step 1: Characterize the current failure and fork contract

Finish plan 071 first and retain its parity output. Add a stock e2e fixture modeled on the fork
canonical tests: create vector, graph-store, and `tjs_pg`; load a tiny deterministic `entities`
table, HNSW index, vertices, and typed edges. Assert the canonical wrapper currently fails because
the compatible lowering is absent. Include off-template rejection assertions so grammar widening is
detectable.

**Verify**: the current stock focused suite fails only at the expected lowering assertion.

### Step 2: Add catalog-safe stock lowering

In `graph_query`, retain fork detection and add a stock branch only when the exact `tjs_open`
signature and required types/extensions exist. Avoid `to_regprocedure` expressions that error when
the `vector` type is absent. Map the pinned template to: `k`, `term_cond=0`, `m_seeds=0`, `hops=1`,
`id_col='id'`, the parsed relational filter, parsed query vector, pinned source ID, and a catalog-
justified edge type. Convert accepted brace numeric vectors to pgvector bracket syntax with the
existing closed numeric grammar; accepting both brace/bracket literal dialects is allowed, but no
other template expansion is.

If the canonical edge label cannot be deterministically mapped to the integer edge type using
existing catalog state, STOP and specify the missing contract rather than silently using “any edge”.

**Verify**: direct stock `tjs_open` and canonical wrapper return the same ordered IDs on the fixture.

### Step 3: Restore canonical text chunks in emitted order

Join stock result IDs back to the pinned entity relation to return the canonical text/chunk column.
Carry ordinality from `tjs_open`; never rely on heap order after the join. Quote identifiers with
Postgres formatting helpers and keep parsed values parameterized wherever possible.

**Verify**: tests assert exact chunk values and order, including a filter that removes a nearer row.

### Step 4: Put the e2e suite in stock gates and correct docs

Add the suite to `STOCK_TESTS` and the matching PG16/17 CI suite list. Update README with the actual
`graph_store.graph_query($$...$$)` invocation. Add a spec addendum and ADR-0019 addendum describing
the stock lowering and its intentionally narrow grammar; do not rewrite historical text.

**Verify**: `make stock-graph-test`, PG16 CI-equivalent run, `make test`, and `make lint` pass.

## Test plan

The new SQL suite must cover extension installation, stock-lowering selection, exact IDs/chunks and
order, relational filter, brace/bracket vector handling if both are supported, `last_join_order`,
off-template rejection, and behavior when `tjs_pg` is absent. Confirm direct/canonical parity on the
same fixture and preserve all existing fork tests.

## Done criteria

- [ ] A stock PG16/17 install executes the one canonical v1 query through `tjs_open`.
- [ ] Results are the expected chunks in operator order; no relational edge join exists.
- [ ] Off-template text still fails closed.
- [ ] New suite appears in both `STOCK_TESTS` and CI and passes on PG16/17.
- [ ] README, spec addendum, and ADR addendum state the implemented behavior without overclaim.

## STOP conditions

- Plan 071 finds a real fork/stock filter-first semantic mismatch.
- No deterministic existing mapping exists from the canonical edge label to `edge_type`.
- Supporting stock requires broadening beyond the single spec query.
- Extension packaging now requires a versioned upgrade script; follow that established scheme.
- A proposed implementation models graph edges as relational joins.

## Maintenance notes

The wrapper is a compatibility front door until native parser support is viable. Keep its accepted
grammar pinned. Any later PG19 work must still lower topology to the native adjacency-list AM, not
to relational edge tables.
