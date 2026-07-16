# Plan 083: Bind baseline datastore ports to loopback by default

> **Executor instructions**: Do not copy credential values into new files or plan output. Preserve
> the three-system baseline architecture. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- baseline/docker-compose.yml baseline/README.md .env.example tests/`

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security / tests / docs
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

Docker Compose publishes Neo4j, object storage, Milvus, metrics, and Postgres on every host interface
while using development credentials. Running the documented local baseline on a laptop or shared
server can expose all stores to the surrounding network. Loopback should be the safe default, with
remote exposure requiring a deliberate override.

## Current state

- `baseline/docker-compose.yml:23-25` publishes two Neo4j ports without a host address.
- Lines 72-74 publish object-store API/console, lines 91-93 publish Milvus/metrics, and lines 115-116
  publish Postgres the same way (Postgres already has an overridable host port).
- Unqualified Compose port mappings bind on all interfaces by default.
- The compose file intentionally represents three independent systems; that comparison architecture
  must remain unchanged.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused | `.venv/bin/pytest tests/test_baseline_compose_security.py -q` | all pass |
| Compose | `env -u BASELINE_BIND docker compose -f baseline/docker-compose.yml config` | every published IP is `127.0.0.1` |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `baseline/docker-compose.yml`
- `baseline/README.md` (or the existing baseline operation doc)
- `.env.example`
- `tests/test_baseline_compose_security.py` (create)

**Out of scope**:
- Changing service images, credentials, internal service listeners, volumes, or topology.
- Adding a fourth system or merging transaction managers.
- Claiming development credentials are suitable for remote deployment.

## Git workflow

Use assigned `dustin/dev-NNNN`; suggested commit:
`fix(baseline): bind datastore ports locally`.

## Steps

### Step 1: Add a compose exposure regression test

Create a host test that parses the Compose ports structurally enough to enumerate every published
mapping (PyYAML only if already available; otherwise parse `docker compose config --format json` in
an optional integration test plus a deterministic file assertion). Require every mapping to start
with `${BASELINE_BIND:-127.0.0.1}:` and reject bare `host:container` forms. Name all expected
services/ports so missing coverage cannot pass vacuously.

**Verify**: the test fails against current unqualified mappings.

### Step 2: Add one loopback-default bind variable

Prefix every published mapping with `${BASELINE_BIND:-127.0.0.1}:`, preserving existing host-port
variables such as `BASELINE_PG_PORT`. Quote the full interpolation. Do not alter container-internal
listeners because Milvus dependencies need internal connectivity.

**Verify**: with `BASELINE_BIND` unset, rendered config publishes only `127.0.0.1`; with
`BASELINE_BIND=0.0.0.0`, rendered config shows the explicit override for every published port.

### Step 3: Document deliberate remote use

Add an empty/default-safe `BASELINE_BIND` entry to `.env.example` and explain in baseline docs that
remote binding is explicit and development credentials must be replaced plus host firewalling
applied. Do not include actual secret values in new prose.

**Verify**: `docker compose ... config` succeeds in both modes; no newly added secret-like literals.

## Test plan

Static test enumerates all current published ports and loopback interpolation. Compose rendering
tests unset/default and explicit override. Optionally bring the stack up locally and use `docker
port`/`ss` to confirm loopback, then `make baseline-down`; this is not required when Docker services
are unavailable.

## Done criteria

- [ ] Every baseline host port binds `127.0.0.1` when `BASELINE_BIND` is unset.
- [ ] `BASELINE_BIND=0.0.0.0` is the only documented all-interface opt-in.
- [ ] Service topology/internal connectivity and host-port overrides remain intact.
- [ ] Focused/full tests, lint, Compose config, and diff checks pass.

## STOP conditions

- Compose version in supported environments cannot parse nested bind/port interpolation.
- A CI/deployment workflow relies on implicit all-interface binding and cannot set an explicit env
  override; report the workflow before changing semantics.
- The fix appears to require altering internal service addresses.

## Maintenance notes

Any newly published baseline port must use `BASELINE_BIND`. Loopback binding reduces exposure but
does not make development credentials production-safe.
