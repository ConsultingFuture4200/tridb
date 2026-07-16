# Plan 066: Pin the pgvector base image + assert the ≥0.8 feature requirement at load

> **Executor instructions**: Follow step by step; run every verification. STOP conditions halt you.
> Update this plan's row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat a41b0c7..HEAD -- scripts/pg17/Dockerfile scripts/pg17/Dockerfile.release scripts/add_pgvector.sh`
> If changed, compare "Current state" to live code; mismatch = STOP.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dependency / migration
- **Planned at**: commit `a41b0c7`, 2026-07-15

## Why this matters

The entire D2 "installable on stock Postgres" story and the Gate B / SM-4 measured numbers depend on
**pgvector ≥ 0.8** (for `hnsw.iterative_scan = relaxed_order`, which the re-homed `tjs_open`
vector-first path *requires* and errors without). But both stock-PG Dockerfiles pull
`FROM pgvector/pgvector:pg${PG_MAJOR}` — a **mutable tag with no digest and no pgvector version
pin**. A retag of that upstream image (a new pgvector major, or a change to `iterative_scan`
defaults/semantics) can silently change the operator's recall surface or break it, with nothing
holding the benchmark reproducible. Python deps are fully pinned (`requirements.lock`); this is the
one unpinned runtime dependency on a load-bearing path, and it directly undercuts the roadmap's
"stranger reproduces the number" exit criterion.

## Current state

- `scripts/pg17/Dockerfile:5-6`:
  ```dockerfile
  ARG PG_MAJOR=17
  FROM pgvector/pgvector:pg${PG_MAJOR}
  ```
- `scripts/pg17/Dockerfile.release:10-11` and `:24` — same floating `FROM pgvector/pgvector:pg${PG_MAJOR}`
  in both the builder and final stages.
- `scripts/add_pgvector.sh:20` — a *separate* pgvector entry point (the fork shim) defaults
  `PGV_TAG=v0.8.0`; the two pgvector paths are not pinned to the same thing.
- The ≥0.8 requirement is stated in `docs/decisions/0019-tjs-open-stock-pg-rehome.md` and
  `docs/INSTALL_stock_pg.md:54-57`, and is what `src/tjs_pg/tjs_pg.c` enforces at runtime (it errors
  if `hnsw.iterative_scan != relaxed_order`), but nothing checks the pgvector *version*.

## Steps

1. **Pin the base image tag** in both Dockerfiles. pgvector publishes version-qualified tags of the
   form `pgvector/pgvector:0.8.0-pg17`. Change the `FROM` lines to an ARG-driven explicit version:
   ```dockerfile
   ARG PG_MAJOR=17
   ARG PGVECTOR_VERSION=0.8.0
   FROM pgvector/pgvector:${PGVECTOR_VERSION}-pg${PG_MAJOR}
   ```
   Apply to `scripts/pg17/Dockerfile` and both stages of `scripts/pg17/Dockerfile.release`.
   - **Verify the tag exists before committing**: `docker pull pgvector/pgvector:0.8.0-pg17` must
     succeed. If pgvector's tag scheme differs (check https://hub.docker.com/r/pgvector/pgvector/tags
     via `docker` — or `docker buildx imagetools inspect pgvector/pgvector:0.8.0-pg17`), use whatever
     the actual ≥0.8 version-qualified tag is and record it. STOP if no version-qualified tag ≥0.8
     exists (see STOP conditions).
   - Optional hardening (record in the plan's done note, do only if time allows): additionally
     digest-pin via `FROM pgvector/pgvector:0.8.0-pg17@sha256:<digest>` using the digest from
     `docker buildx imagetools inspect`.

2. **Assert the version at `CREATE EXTENSION` time** so a wrong/old pgvector fails loudly rather than
   silently changing recall. In `src/tjs_pg/tjs_pg--0.1.0.sql`, after the file's `\quit` guard and
   before the function definitions, add a guard that the installing `vector` extension is ≥0.8:
   ```sql
   DO $$
   DECLARE v text;
   BEGIN
     SELECT extversion INTO v FROM pg_extension WHERE extname = 'vector';
     IF v IS NULL THEN
       RAISE EXCEPTION 'tjs_pg requires the pgvector "vector" extension (CREATE EXTENSION vector first)';
     END IF;
     IF string_to_array(v, '.')::int[] < ARRAY[0,8]::int[] THEN
       RAISE EXCEPTION 'tjs_pg requires pgvector >= 0.8 (found %); the vector-first path needs hnsw.iterative_scan = relaxed_order', v;
     END IF;
   END $$;
   ```
   (Verify the `string_to_array(...)::int[] < ARRAY[0,8]` comparison behaves as intended for the
   real version strings — pgvector versions look like `0.8.0`. Test in step 4.)
   NOTE: `tjs_pg` already declares `requires = 'vector, graph_store_am'` in its control file, so the
   `vector` extension is present at install; this guard adds the *version* floor the control file
   can't express.

3. **Bump `scripts/add_pgvector.sh:20`'s `PGV_TAG` default to match** (or add a comment tying the two
   pgvector entry points to the same floor), so the fork shim and the stock image can't drift to
   different pgvector versions silently.

## Verification

1. `docker build -t tridb/pg17-unfork:dev scripts/pg17/` → builds on the pinned base.
2. `docker build --build-arg PG_MAJOR=16 -t tridb/pg16-unfork:dev scripts/pg17/` → builds.
3. `docker build -f scripts/pg17/Dockerfile.release -t tridb/postgres-trimodal:pg17 .` → builds.
4. `bash scripts/pg17_graph_test.sh tridb/pg17-unfork:dev test/tjs_pg_test.sql` → `ALL PASS`, and
   the CREATE EXTENSION version guard does NOT fire (the pinned image has ≥0.8). To prove the guard
   works: temporarily nothing — the guard only fires on <0.8, which the pinned image isn't; instead
   assert the guard SQL is present: `grep -c 'pgvector >= 0.8' src/tjs_pg/tjs_pg--0.1.0.sql` == 1.
5. `grep -c 'FROM pgvector/pgvector:pg' scripts/pg17/Dockerfile scripts/pg17/Dockerfile.release`
   across both files == 0 (no floating tag remains).

## Done criteria

- Both Dockerfiles use a version-qualified pgvector tag (`grep 'PGVECTOR_VERSION' scripts/pg17/Dockerfile` matches).
- `src/tjs_pg/tjs_pg--0.1.0.sql` contains the ≥0.8 assertion.
- All three image builds succeed and `test/tjs_pg_test.sql` is ALL PASS on PG16 + PG17.

## Out of scope / do NOT touch

- The pgvector fork shim's *behavior* (`scripts/add_pgvector.sh` beyond the tag default).
- The operator's runtime `iterative_scan` check in `tjs_pg.c` (it stays; the SQL version guard is
  additive belt-and-suspenders at install time).
- CI `.github/workflows/ci.yml` — the pinned base flows through automatically; no CI edit needed
  unless a build-arg default must change (it shouldn't).

## STOP conditions

- If pgvector does not publish a version-qualified tag ≥0.8 for `pg16`/`pg17` (only the floating
  `pgN` tag exists), STOP and report — the reviewer will decide between digest-pinning the floating
  tag (capturing today's digest) vs building pgvector from a pinned source ref.
- If the `string_to_array(...)::int[]` version comparison errors on a real version string, STOP and
  report — do not ship a guard that raises on valid installs.

## Maintenance note

When intentionally upgrading pgvector, bump `PGVECTOR_VERSION` in both Dockerfiles **and** re-run the
Gate B / SM-4 measurements (the recall surface is pgvector-version-sensitive per ADR-0015 E3 and
`docs/sm4_seedless_stock_v0.1.0.md`). The version floor in the SQL guard should track the minimum the
operator's iterative-scan path actually needs.
