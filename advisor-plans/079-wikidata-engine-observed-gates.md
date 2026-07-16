# Plan 079: Gate Wikidata engine reports on observed engine state

> **Executor instructions**: Preserve nonzero loader exits and distinguish partial observation from
> successful completion. Do not regenerate committed benchmark reports. Skip the advisor index.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- tools/wikidata_engine_load.py tests/test_wikidata_engine_load.py docs/INSTALL_stock_pg.md`

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug / tests
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

After an engine load fails, the manifest can populate `WD_ENGINE_EDGES` from the expected host slice
count. That makes downstream publication gates look engine-observed when the engine never confirmed
the state. Reports must retain useful partial evidence but never substitute expected input for live
engine measurements.

## Current state

- `tools/wikidata_engine_load.py:598-602` parses a failed command transcript, prints failure, and
  continues to produce a manifest.
- The SQL emits `#WDL ASSERT edges=% vertices=% OK` around line 316 before HNSW completion.
- `_FINAL_RE` at lines 438-450 parses only the later final marker.
- Lines 618-619 derive gate edges from `engine.get("edges", stats.get("edges_kept"))`, allowing the
  expected slice count to stand in for an engine count after failure.
- `docs/INSTALL_stock_pg.md` describes `WD_ENGINE_EDGES` as the actual engine count.

## Target state model

Record separately: command/load status (`emitted`, `failed`, `complete`), graph verification status,
HNSW health, and any observed engine edge/vertex counts. Parse the earlier assertion marker so a
failure after graph load can retain observed graph evidence. Emit gate environment keys only from
observed engine markers; emitted-only or pre-assert failures get no `WD_ENGINE_EDGES`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused | `.venv/bin/pytest tests/test_wikidata_engine_load.py -q` | all pass |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `tools/wikidata_engine_load.py`
- `tests/test_wikidata_engine_load.py`
- `docs/INSTALL_stock_pg.md` if gate/status wording changes

**Out of scope**:
- Regenerating manifests/results already committed.
- Changing SQL load semantics, graph counts, or HNSW construction.
- Converting failed commands to successful exits.

## Git workflow

Use assigned `dustin/dev-NNNN`; suggested commit:
`fix(bench): gate on observed engine counts`.

## Steps

### Step 1: Add transcript-state regression fixtures

Create representative transcript strings for: complete success; failure after the `ASSERT` marker
but before final/HNSW completion; failure before assertion; and emitted SQL mode. Assert the exact
status booleans/counts and presence/absence of gate keys. The post-assert failure must retain observed
graph counts while still being a failed load.

**Verify**: the pre-assert and post-assert gate assertions expose the current fallback bug.

### Step 2: Parse observed graph assertion independently

Add a strict `_ASSERT_RE` matching the existing marker and integer fields. Parse it independently of
`_FINAL_RE`. Build explicit manifest fields such as `load_status`, `graph_verified`, and
`hnsw_healthy`; choose names consistent with existing manifest vocabulary. The final marker may
imply all phases complete, but the earlier assertion must not imply HNSW health.

**Verify**: all transcript-state unit tests pass.

### Step 3: Remove expected-count gate fallback

Populate `WD_ENGINE_EDGES`/related engine gate values only from parsed engine observations. Keep host
slice expectations under their existing `WD_`/`WH_` expected keys, never under engine-observed names.
Emitted mode produces SQL but no observed gate environment.

**Verify**: `rg 'engine\.get\("edges", stats\.get' tools/wikidata_engine_load.py` returns no match;
focused tests pass.

### Step 4: Align docs and full verification

Clarify that graph counts can be observed even when a later index phase fails, but publication/load
completion remains false. Preserve command return codes.

**Verify**: `make test && make lint && git diff --check` exit 0.

## Test plan

Unit-test each state transition plus malformed/duplicate marker handling. Prefer the existing parser
seams; no Docker is needed. Assert failed command return state, no expected-count fallback, and no
gate keys in emitted-only mode.

## Done criteria

- [ ] Engine gate values originate only from parsed engine markers.
- [ ] Post-assert failure records observed graph counts but remains failed and HNSW-unhealthy.
- [ ] Pre-assert failure and emit mode expose no engine-count gate value.
- [ ] Focused/full tests, lint, and diff checks pass; no benchmark artifact changes.

## STOP conditions

- Live SQL no longer emits a stable assertion marker.
- Downstream schema validation forbids adding explicit status fields; report required migration.
- A proposed fix masks a nonzero loader command exit.

## Maintenance notes

Keep “expected,” “emitted,” “observed,” and “complete” as separate states in future pipeline phases.
Only an engine-originated marker may populate an `ENGINE_*` publication gate.
