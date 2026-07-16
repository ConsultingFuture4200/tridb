# Plan 076: Smoke the complete stock release image and quickstart

> **Executor instructions**: Build and run both PG16 and PG17 release images. A successful Docker
> build alone is not verification. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- scripts/pg17/Dockerfile.release scripts/pg17/ README.md docs/INSTALL_stock_pg.md .github/workflows/ci.yml Makefile test/`

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: 072, 075
- **Category**: tests / dx / docs
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

The release image compiles and copies `tjs_pg`, but the public quickstart creates only `vector` and
`graph_store_am`, and CI never starts the built image. Packaging, dependency order, shared-library
loading, or canonical-query failures can therefore ship unnoticed. The release path needs a runtime
smoke that executes the actual tri-modal front door.

## Current state

- `scripts/pg17/Dockerfile.release:18-32` builds/copies both graph-store and TJS extension artifacts.
- `src/tjs_pg/tjs_pg.control:7` requires `vector` and `graph_store_am`.
- `README.md:161-167` and `docs/INSTALL_stock_pg.md:12-18` create only `vector` and
  `graph_store_am`; the source install route at lines 24-34 builds only graph-store.
- `.github/workflows/ci.yml:78-79` builds the release image but does not start it.
- Plan 075 supplies the canonical stock e2e surface this smoke should exercise.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Shell syntax | `bash -n scripts/pg17_release_smoke.sh` | exit 0 |
| PG17 release | `bash scripts/pg17_release_smoke.sh tridb/pg17-unfork:release` | `RELEASE SMOKE PASS` |
| Host checks | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `README.md`
- `docs/INSTALL_stock_pg.md`
- `.github/workflows/ci.yml`
- `scripts/pg17_release_smoke.sh` (create)
- `test/release_stock_smoke.sql` (create)
- `Makefile` (optional target only)

**Out of scope**:
- Changing release image contents unless the smoke reveals a packaging defect; STOP first.
- Publishing/pushing images.
- Benchmarking or GX10/fork validation.
- Duplicating the full stock regression suite in the smoke.

## Git workflow

Use an assigned `dustin/dev-NNNN` branch. Suggested commit:
`test(release): run stock tri-modal smoke`.

## Steps

### Step 1: Write a minimal release SQL smoke

Create `test/release_stock_smoke.sql`. In dependency order create `vector`, `graph_store_am`, then
`tjs_pg`; create a tiny vector/entity relation and HNSW index; initialize the native graph AM and one
edge; assert a direct `tjs_open` result; then assert plan 075's canonical `graph_query` returns the
expected chunk. Make every failure raise and finish with one unambiguous PASS marker.

**Verify**: running it against a development stock image returns the PASS marker and exit 0.

### Step 2: Add a lifecycle-safe Docker runner

Create `scripts/pg17_release_smoke.sh` using an image argument. Generate a unique container name,
database name/password, and available host port at runtime; register `trap` cleanup; start the image,
wait with `pg_isready`, run the SQL with `ON_ERROR_STOP=1`, and always remove the container. Do not
commit credentials and do not bind a fixed public port.

**Verify**: success prints `RELEASE SMOKE PASS`; deliberately break one SQL assertion in a temporary
copy and confirm the script exits nonzero, then restore it.

### Step 3: Run the image in CI for PG16 and PG17

After each release image build in the existing matrix, invoke the smoke script against that exact
tag. If the workflow does not currently matrix the release build, add the smallest PG16/17 matrix
consistent with the existing Docker build args. Optionally add a `stock-release-smoke` Make target.

**Verify**: local builds for PG16 and PG17 both start and print the marker; workflow YAML parses.

### Step 4: Correct quickstart and source-install instructions

Add `CREATE EXTENSION tjs_pg` after its dependencies in README and INSTALL. The source route must
also build/install `src/tjs_pg`. Show the canonical wrapper rather than advertising only a private
operator call.

**Verify**: follow the documented commands from a clean release container; they complete without
manual omitted steps.

## Test plan

Test successful startup/install/direct/canonical execution, SQL failure propagation, readiness
timeout, and cleanup on success/failure. Run the real smoke for both supported stock majors. Host
tests/lint and `git diff --check` remain green.

## Done criteria

- [ ] README/INSTALL install all three extensions in dependency order.
- [ ] CI starts each PG16/17 release image and executes native graph plus `tjs_pg` and canonical SQL.
- [ ] Deliberately broken SQL makes the runner fail.
- [ ] No container, credential, or fixed public port remains after the script exits.
- [ ] Shell syntax, host tests/lint, and both release smokes pass.

## STOP conditions

- Plan 075 is incomplete or canonical stock execution does not pass.
- The smoke exposes missing release artifacts; report the packaging defect before expanding scope.
- CI cannot run Docker containers in the current job architecture.
- Supporting PG16 would require dropping the documented PG16/17 extension target.

## Maintenance notes

Keep this smoke small and user-facing. New required extensions or packaging changes should update
the quickstart and this runtime gate in the same change.
