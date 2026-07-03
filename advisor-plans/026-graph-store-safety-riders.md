# Plan 026: Graph-store safety riders — ACLs, VACUUM/wraparound hazard documentation, freeze design note

> **Executor instructions**: Follow step by step; verify each step. On any STOP condition, stop and
> report. Update your row in `advisor-plans/README.md` when done (unless a reviewer maintains it).
>
> **Drift check (run first)**: `git diff --stat e345998..HEAD -- src/graph_store SECURITY.md docs/`
> (plan 025 may add functions to the v1 SQL — additive drift is fine; excerpt mismatches are not).

## Status

- **Priority**: P1 (riders are S; the design note is the durable part)
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (coordinate file-level with 025 if concurrent: both touch `graph_store_am--0.1.0.sql`)
- **Category**: security / docs
- **Planned at**: commit `e345998`, 2026-07-03

## Why this matters

The native graph store's container relation `gstore` is a plain heap table whose 32KB blocks hold
NON-heap page formats. Three hazards for anything longer-lived than a benchmark: (1) anti-wraparound
autovacuum IGNORES `autovacuum_enabled = false` and will eventually process those pages as heap —
garbage line pointers, likely crash/corruption; a stray manual `VACUUM`/`ANALYZE`/`SELECT * FROM
gstore` does the same immediately; (2) the store's visibility checks call `TransactionIdDidCommit`
on raw stored xids with NO freeze path — once the clog horizon passes them, lookups error
("could not access status of transaction") and past 2^31 xids visibility flips; (3) there are no
ACLs: `gstore` is PUBLIC-readable and the `gph_*` mutators are PUBLIC-executable with no ownership
check, so any connected user can read the raw pages or write the shared graph. Fixing (1)/(2)
properly is engine work (a real freeze pass / table AM handler — deferred); what ships NOW is the
containment: revoke access, document the hazard where operators will see it, and write the freeze
design note so the real fix is specified before anyone runs TriDB long-lived.

## Current state

- `src/graph_store/graph_store_am--0.1.0.sql:10` — `CREATE TABLE gstore (dummy "char") WITH
  (autovacuum_enabled = false);` + a COMMENT saying "Do NOT access as a heap" — comment-only
  enforcement, no REVOKE anywhere in the file.
- Same file: `gph_insert_vertex()`, `gph_insert_edge(bigint,bigint)`, `gph_neighbors(bigint)`,
  `gph_traverse`, counters — all default EXECUTE to PUBLIC.
- `src/graph_store/graph_am.c` — `gph_xmin_visible()` uses raw xids; no freeze machinery (do NOT
  change C in this plan).
- `SECURITY.md` (repo root) — exists; documents the raw-SQL-fragment trusted-input contract. Follow
  its section style for the new hazard section.
- Convention: extension SQL changes to `src/graph_store` are PGXS-built fresh by every AM harness —
  no image rebuild needed; `bash scripts/graph_am_test.sh tridb/msvbase:dev` exercises the extension.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| AM suite | `bash scripts/graph_am_test.sh tridb/msvbase:dev` | done banner, no failures |
| Full engine suite | `make graph-test` | green |
| Docs lint | `make lint` | clean (no Python touched — should be trivially green) |

## Scope

**In scope:** `src/graph_store/graph_store_am--0.1.0.sql` (REVOKEs + comments);
`SECURITY.md` (new "Graph store container hazards" section);
`docs/graph_store_freeze_design_v0.1.0.md` (create — design note, no code);
`test/graph_am_acl_test.sql` (create) + `Makefile` ENGINE_TESTS wiring.

**Out of scope:** any C change; the vectordb operator REVOKEs (`tjs`/`tjs_open` grants ride the
patch chain and belong to a fork-patch plan — note as deferred in the design note); the actual
freeze/table-AM implementation.

## Git workflow
Branch `advisor/026-graph-safety-riders`; `fix(security):`/`docs:` commits; do NOT push.

## Steps

### Step 1: ACLs in the extension script
Append to `graph_store_am--0.1.0.sql`:
```sql
-- Containment (advisor plan 026): the container holds NON-heap pages; any heap-path access
-- (SELECT/VACUUM/ANALYZE) misreads them. Deployers grant gph_* EXECUTE to trusted roles only.
REVOKE ALL ON TABLE gstore FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION gph_insert_vertex(), gph_insert_edge(bigint,bigint) FROM PUBLIC;
```
(Read functions stay PUBLIC — traversal is the query surface; only mutation + raw container access
are revoked. Superuser-run tests are unaffected by ACLs.)
**Verify**: `bash scripts/graph_am_test.sh tridb/msvbase:dev` → passes unchanged.

### Step 2: ACL regression test
`test/graph_am_acl_test.sql`: CREATE EXTENSION, `CREATE ROLE tridb_acl_probe LOGIN`, `SET ROLE`,
assert `SELECT * FROM gstore` and `SELECT gph_insert_vertex()` both fail with
`insufficient_privilege` (EXCEPTION-block pattern per `test/tjs_filter_first_test.sql` assertion 7),
and `gph_neighbors(0)` does NOT fail on privileges. RESET ROLE; DROP ROLE. Wire into ENGINE_TESTS.
**Verify**: `bash scripts/graph_test.sh tridb/msvbase:dev test/graph_am_acl_test.sql` → ALL PASS.

### Step 3: SECURITY.md hazard section
Add "Graph store container (gstore) hazards": (a) never VACUUM/ANALYZE/SELECT the container; (b)
anti-wraparound autovacuum LIMITATION — `autovacuum_enabled=false` does not exempt the relation from
forced wraparound vacuum, so long-lived deployments MUST monitor `age(relfrozenxid)` for `gstore`
and treat approach to `autovacuum_freeze_max_age` as an operational stop-the-world event until the
freeze pass ships; (c) raw-xid horizon: visibility of old graph writes depends on clog retention —
same monitoring; (d) pointer to the design note.
**Verify**: section present; wording contains "age(relfrozenxid)".

### Step 4: Freeze design note
`docs/graph_store_freeze_design_v0.1.0.md` (design ONLY): specify (1) a `gph_freeze()` maintenance
function that walks vertex/adjacency pages under GenericXLog rewriting committed xmins older than a
caller-provided horizon to `FrozenTransactionId` (mirror the crash-safety argument style of
ADR-0003); (2) a metapage `gm_frozen_horizon` field; (3) the trigger policy (manual first;
autovacuum-hook/table-AM later); (4) the interim operational guidance from Step 3; (5) explicitly
list what it does NOT solve (2^31 without running freeze; concurrent writers during freeze —
proposed lock level). Reference `gph_xmin_visible` and the DEV-1259/ADR-0009 precedent for
GenericXLog page rewrites. 1-2 pages, versioned-filename convention.
**Verify**: file exists; `make test && make lint` green; `make graph-test` green.

## Test plan
Step 2's ACL suite is the new coverage; existing AM suites prove no regression.

## Done criteria
- [ ] REVOKEs present in the extension SQL; ACL test in ENGINE_TESTS and passing
- [ ] SECURITY.md section + design note committed
- [ ] `make graph-test` green
- [ ] README status row updated

## STOP conditions
- Any existing AM harness fails after the REVOKEs (they run as superuser and must not — if one runs
  as a non-superuser role, report it rather than granting).
- Plan 025 has concurrently rewritten `graph_store_am--0.1.0.sql` in a way that conflicts — rebase
  onto its branch state and note it.

## Maintenance notes
The real fixes remain: freeze pass implementation (this note is its spec), a table-AM handler so
vacuum routes through TriDB code, and operator EXECUTE grants in the vectordb extension (fork-patch
plan). Reviewers: confirm no GRANT was added back to make a test pass.
