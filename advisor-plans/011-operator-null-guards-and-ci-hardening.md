# Plan 011: Guard tjs/tjs_open against NULL-argument backend crashes, quote table_name (making SECURITY.md true), and harden the CI workflow

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `advisor-plans/README.md` — unless a reviewer dispatched you and told you
> they maintain the index.
>
> **Drift check (run first)**: `git diff --stat 408e852..HEAD -- scripts/patches/tridb_tjs_operator.patch scripts/patches/tridb_tjs_open_operator.patch SECURITY.md .github/workflows/ci.yml`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (if plan 010 landed first, re-verify hunk offsets in the tjs_open patch)
- **Category**: security
- **Planned at**: commit `408e852`, 2026-07-01

## Why this matters

Three defensive-maintenance items on a repo being prepared for open-sourcing. (1) The SQL-callable
C operators `tjs()` and `tjs_open()` are declared non-`STRICT` yet immediately dereference their
`text` arguments — `SELECT tjs(NULL, ...)` crashes the backend: an unprivileged, single-statement
denial of service. (2) `SECURITY.md` tells readers the `table_name` argument "is *not*
string-interpolated" — but both operators interpolate the raw string into executed SQL after only
a catalog existence check; the published security policy is contradicted by the code. (3) The CI
workflow grants the default (potentially read-write) token to jobs that only need read access, and
pins actions to mutable major tags — standard hardening debt before accepting outside PRs.

## Current state

- Operator declarations ship inside the fork patches (the vendored tree is gitignored; the
  `.patch` files are the source of truth):
  - `scripts/patches/tridb_tjs_operator.patch` — `CREATE FUNCTION tjs(table_name text, k integer,
    term_cond integer, src bigint, attr_exp text, filter_exp text, orderby_exp text) RETURNS SETOF
    record AS 'MODULE_PATHNAME' LANGUAGE C STABLE;` — **no STRICT**. The entry point does
    `text *tablename = PG_GETARG_TEXT_PP(argc++);` and later `PG_GETARG_TEXT_PP` for
    attr/filter/orderby with **no `PG_ARGISNULL` anywhere** (grep the patch: zero hits).
  - `scripts/patches/tridb_tjs_open_operator.patch` — same pattern:
    `CREATE FUNCTION tjs_open(...) ... LANGUAGE C STABLE;` and raw `PG_GETARG_TEXT_PP` calls.
  - Contrast (the repo's own hardened exemplars): `tridb_vec_probe` in the tjs patch is
    `LANGUAGE C STRICT`, and `src/planner/join_order.c` explicitly guards `PG_ARGISNULL`.
- Both operators build their vector-leg SQL as (tjs_open shown; tjs is identical in shape):

  ```c
  snprintf(sourceText, sizeof(sourceText), "select %s from %s order by %s",
           text_to_cstring(attr_exp_text), text_to_cstring(tablename), orderby.c_str());
  ```

  after `Oid table_oid = getrelidO(std::string(text_to_cstring(tablename)));` (a
  `RangeVarGetRelid` existence check). `tjs_open`'s `fetchBridgeRowsO` also interpolates
  `*estate->tbl_name` into a second SQL string.
- `SECURITY.md` (lines ~35-41) already honestly documents attr/filter/orderby as a
  trusted-input-only injection surface (that part is by-design and NOT in scope to change), but
  then claims:

  ```
  (The `table_name` argument is *not* part of this surface — it is resolved via the catalog with
  `RangeVarGetRelid`, not string-interpolated.)
  ```

  That sentence is false today.
- `.github/workflows/ci.yml` (44 lines, full content verified at plan time): triggers
  `push: {}`, `pull_request: {}`, `workflow_dispatch: {}`; three jobs (`python`, `patches`,
  `engine` — the last gated `if: github.event_name == 'workflow_dispatch'`); **no `permissions:`
  block anywhere**; actions are `actions/checkout@v4` (3 uses) and `actions/setup-python@v5`
  (1 use) — mutable tags, not SHAs. No `${{ }}` injection of untrusted context exists (verified —
  do not go hunting for one).
- Fast patch gate: `bash scripts/ci_check_patches.sh` re-applies and sentinel-verifies the chain
  without compiling.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Patch chain applies | `bash scripts/ci_check_patches.sh` | exit 0 |
| Python tests / lint | `make test && make lint` | exit 0 |
| Workflow syntax | `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"` | exit 0, no output |
| Engine run (only if image exists) | `make graph-test` | PASS |

## Scope

**In scope** (the only files you should modify):
- `scripts/patches/tridb_tjs_operator.patch`
- `scripts/patches/tridb_tjs_open_operator.patch`
- `SECURITY.md` (one sentence)
- `.github/workflows/ci.yml`
- `test/tjs_open_smoke.sql` or `test/canonical_e2e_test.sql` (add NULL-arg regression asserts)
- `advisor-plans/README.md` (your status row)

**Out of scope** (do NOT touch):
- The attr/filter/orderby expression surface and its SECURITY.md carve-out — documented,
  by-design trusted-input contract. Do not attempt to parameterize those fragments.
- `baseline/docker-compose.yml` dev credentials — settled prior finding, dev-only.
- Any other patch file; `vendor/MSVBASE/` (never committed).

## Git workflow

- Branch: `advisor/011-operator-null-guards` from `origin/master`
- Commit per logical unit, e.g. `fix(tjs): STRICT + NULL guards on SQL-callable operators (advisor plan 011)`,
  `docs(security): correct table_name interpolation claim`, `ci: least-privilege permissions + SHA-pinned actions`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Declare both operators STRICT and add defensive NULL guards

In each patch's `sql/vectordb.sql` hunk, change the `CREATE FUNCTION tjs(...)` /
`CREATE FUNCTION tjs_open(...)` tail from `LANGUAGE C STABLE;` to `LANGUAGE C STABLE STRICT;`.
(The probe counters `tjs_candidates_examined` etc. take no args — leave them.)

In each operator's C entry point (the `Datum tjs(PG_FUNCTION_ARGS)` / `Datum tjs_open(...)`
first-call block), add a defensive guard BEFORE the first `PG_GETARG_TEXT_PP` (belt-and-braces —
STRICT already prevents the call, but the repo convention per `join_order.c` is to guard
explicitly; mirror its comment style):

```c
for (int _i = 0; _i < PG_NARGS(); _i++)
    if (PG_ARGISNULL(_i))
        ereport(ERROR, (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
                errmsg("tjs_open: argument %d must not be NULL", _i + 1)));
```

Keep hunk `+`-line counts consistent when editing the `.patch` files.

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0.

### Step 2: Quote the table identifier so SECURITY.md's claim becomes true

In both patches, after the existing `getrelid*()` existence check, interpolate the **quoted**
identifier instead of the raw string: build the FROM name via PostgreSQL's
`quote_identifier(text_to_cstring(tablename))` (from `utils/builtins.h`, already included in both
files) and use that in every `snprintf`/string concatenation that currently uses
`text_to_cstring(tablename)` or `*estate->tbl_name` in generated SQL (`tjs`: the vector-leg
`sourceText`; `tjs_open`: the vector-leg `sourceText` AND `fetchBridgeRowsO`'s `sql`). Note
`quote_identifier` returns the input unchanged when quoting is unneeded, so normal names are
byte-identical — no behavior change for every existing caller.

Then in `SECURITY.md`, replace the parenthetical claim with the now-true statement:
`(The table_name argument is resolved via the catalog with RangeVarGetRelid and then interpolated
as a quoted identifier (quote_identifier), so it cannot break out of the generated SQL.)`

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0, and
`grep -c "quote_identifier" scripts/patches/tridb_tjs_operator.patch scripts/patches/tridb_tjs_open_operator.patch`
→ ≥ 1 per file.

### Step 3: Add NULL-arg regression asserts

Add to `test/tjs_open_smoke.sql` (or `test/canonical_e2e_test.sql` for `tjs` — pick the file that
already exercises each operator) a block asserting a NULL argument yields a clean NULL/zero-row
result, not a crash:

```sql
-- STRICT: NULL arg returns no rows (previously: backend segfault)
SELECT count(*) FROM tjs_open(NULL, 5, 0, 0, 0, 'id', '', 'embedding <-> ''[0,0,0,0]''') AS t(id bigint);
```

(For a STRICT function called in FROM, the row is NULL → count 0. Follow the assert style already
used in the file — exception-raising DO blocks.)

**Verify**: engine-gated — run `bash scripts/graph_test.sh tridb/msvbase:dev <file>` if the image
exists; otherwise mark "engine-gated: unbuilt here".

### Step 4: Harden ci.yml

Three edits to `.github/workflows/ci.yml`:
1. Add a top-level least-privilege block after `name: CI`:
   ```yaml
   permissions:
     contents: read
   ```
2. Pin both actions to full commit SHAs with a trailing version comment. Resolve the SHA for the
   current major tags via `gh api repos/actions/checkout/git/ref/tags/v4 --jq .object.sha` (and
   the equivalent for `setup-python` v5); if `gh` or network is unavailable, STOP and report
   rather than inventing a SHA. Format: `uses: actions/checkout@<sha> # v4`.
3. Leave triggers and the `engine` gate exactly as they are.

**Verify**: the YAML-load command from the table → exit 0; `grep -c "permissions:" .github/workflows/ci.yml` → 1;
`grep -c "actions/checkout@v4$" .github/workflows/ci.yml` → 0.

## Test plan

- Step 3's SQL asserts are the regression tests for the crash class.
- `make test && make lint` → unchanged pass count (no Python touched).
- CI behavior itself verifies on the next GitHub push (note this in your report; it is not
  verifiable locally beyond YAML validity).

## Done criteria

- [ ] `bash scripts/ci_check_patches.sh` exits 0
- [ ] `grep -n "STRICT" scripts/patches/tridb_tjs_operator.patch scripts/patches/tridb_tjs_open_operator.patch` shows both operator declarations STRICT
- [ ] `grep -n "PG_ARGISNULL" <both patches>` shows the defensive guard in each entry point
- [ ] `grep -n "quote_identifier" <both patches>` shows quoted interpolation at every generated-SQL site using table_name
- [ ] `SECURITY.md` no longer claims table_name is "not string-interpolated"
- [ ] `ci.yml`: permissions block present, zero mutable-tag `uses:` lines, YAML loads
- [ ] `make test && make lint` exit 0; `git status` shows only in-scope files
- [ ] `advisor-plans/README.md` status row updated

## STOP conditions

- The patches' CREATE FUNCTION or entry-point code doesn't match the excerpts (drift — plan 010
  may have landed; re-locate the sites, and if the shape changed materially, report).
- `ci_check_patches.sh` fails twice after an edit (hunk-header arithmetic — report the raw error).
- `quote_identifier` is unavailable in the fork's PG 13.4 headers (it is in `utils/builtins.h` in
  stock 13.4; if the fork diverges, report instead of hand-rolling quoting).
- You cannot resolve real action SHAs (no network/gh) — do steps 1–3, report step 4 blocked.

## Maintenance notes

- Reviewer should scrutinize: hunk-count arithmetic in both patches, and that STRICT doesn't
  change any legitimate caller (the canonical lowering in `src/graph_store_ext/graph_store--0.1.0.sql`
  always passes non-NULL — verified at plan time).
- Follow-up deferred: bounding the `filter_exp` length before the fixed 100KB `sourceText`
  buffer (silent `snprintf` truncation → confusing planner error). Cheap; fold into the next
  operator-touching change (017 is a natural host).
- When a future multi-query surface exposes these operators beyond the canonical lowering, the
  attr/filter/orderby fragments need real validation/binding — SECURITY.md already says so.
