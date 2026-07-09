# Plan 059: ADR-0013 Stage C — archive v0 graph_store_ext

> **Executor instructions**: Extension dual-install + docs. Engine verify. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- src/graph_store_ext scripts/graph_test.sh docs/decisions/0013-graph-store-v1-rewire.md`

## Status
- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: ADR-0013 Stage A/B already MERGED (plan 025) — confirm no remaining CREATE EXTENSION graph_store consumers
- **Category**: tech-debt
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

v0 heap extension `src/graph_store_ext/` still installs beside v1 AM in `scripts/graph_test.sh`.
README there still claims operators/benches target v0 — **false** after Stage A/B. Dual stores
double CI cost and confuse agents about which graph is “the” store.

## Current state

```
# src/graph_store_ext/README.md:8-10 — STALE: "operators and benches currently target" v0
# scripts/graph_test.sh — builds EXT_V0 + v1
# ADR-0013:46-47 Stage C archive open
# v1 SQL keeps v0-compat names (add_edge, neighbors) — KEEP those
```

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Grep consumers | `rg -n 'CREATE EXTENSION graph_store[^_]|graph_store_ext' --glob '!**/.venv/**'` | only archive/docs after |
| Engine | `make graph-test` | PASS without v0 install |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope:**
- Stop building/installing v0 in `graph_test.sh` (and any other scripts)
- Archive tree: move to `src/graph_store_ext/ARCHIVE.md` + pointer, or `archive/graph_store_ext/` — pick one approach that keeps git history readable
- Fix stale README claims
- ADR-0013 Stage C checkbox / STATUS note
- Ensure ENGINE_TESTS only need `graph_store_am`

**Out of scope:** deleting v0-compat SQL aliases on v1; rewriting historical benchmark numbers (label store measured if needed).

## Git workflow
- Branch: `advisor/059-archive-v0`
- Commit: `chore(graph): archive v0 heap extension Stage C (advisor 059)`

## Steps

### Step 1: Consumer audit

```bash
rg -n 'CREATE EXTENSION graph_store;|extension graph_store' test scripts bench tools
```

Every hit must use `graph_store_am` or v1 compat. Fix stragglers **before** removing v0 from harness.

### Step 2: Drop dual install from graph_test.sh

Build only `src/graph_store`. Remove EXT_V0 copy/install.

**Verify**: `make graph-test` green.

### Step 3: Archive the tree

Replace active README with “ARCHIVED — see ADR-0013; do not build”. Optionally leave sources for archaeology but exclude from all make targets.

### Step 4: ADR/STATUS

Mark Stage C done; note date/commit.

## Test plan
- Full engine suite without v0.
- Host tests unchanged.

## Done criteria
- [ ] No CI/engine path builds v0
- [ ] Stale “benches target v0” text gone
- [ ] `make graph-test` green
- [ ] ADR-0013 Stage C recorded complete
- [ ] Index DONE

## STOP conditions
- A required ENGINE_TEST still needs v0-only behavior — port to v1 first; do not archive half-way.
- Schema name collision docs still say dual-DB only — update.

## Maintenance notes
- Compat names on v1 (`add_edge`, `neighbors`) remain the public surface for old SQL.
