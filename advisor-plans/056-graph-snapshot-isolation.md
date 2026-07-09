# Plan 056: Graph-leg snapshot isolation (DEV-1166 residual tears)

> **Executor instructions**: Large correctness change. Prefer design confirmation if API unclear.
> Depends on freeze xmax (040) for clog-safe xids. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- src/graph_store bench/wiki_consistency.py docs/benchmark_wiki_consistency_v0.1.0.md`

## Status
- **Priority**: P2
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: plan 040 (freeze xmax) strongly recommended first
- **Category**: correctness / direction
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

Cross-modal **write** atomicity and crash recovery win (wiki consistency S1/S2). Residual tears in S3
are **native graph only**: visibility uses `TransactionIdDidCommit`, not the active MVCC snapshot
(`graph_am.c:64-71`). Heap vector/rel legs are 0% tear; graph can see commits that happen after the
statement snapshot. Completing FR-7 isolation is the unfinished third leg of the one-WAL product story.

## Current state

```c
/* graph_am.c:64-80 — commit-visible, not snapshot */
return TransactionIdDidCommit(xmin);
```

- Measured: `docs/benchmark_wiki_consistency_v0.1.0.md`, `bench/wiki_consistency.py` S3
- DEV-1166 named in STATUS as residual
- Single-writer contract still in file header — concurrent readers are the SI target

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Engine | `make graph-test` + concurrency probe | PASS |
| Consistency | live `wiki_consistency` S3 | graph tear rate → 0% target |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope:** graph read visibility vs `GetActiveSnapshot()` / `XidInMVCCSnapshot` (PG 13.4 APIs);
tests for concurrent reader seeing stable graph; re-measure S3; docs.

**Out of scope:** multi-writer adjacency races (still single-writer unless separately designed);
custom rmgr.

## Git workflow
- Branch: `advisor/056-graph-si`
- Commit: `fix(graph): snapshot-aware visibility (advisor 056)`

## Steps

### Step 1: Spike API on PG 13.4

Confirm which snapshot helpers are available in the fork headers. Document chosen API in a short
design note if ADR-0003a needs an addendum.

### Step 2: Thread snapshot into visibility

Replace pure `TransactionIdDidCommit` checks with snapshot-aware tests for xmin/xmax, matching heap
semantics as closely as GenericXLog records allow.

### Step 3: Concurrency tests

Extend `graph_concurrency_test.sh` / probe: writer commits edges while long reader holds snapshot;
reader must not observe mid-statement commits.

### Step 4: Re-run wiki_consistency S3

Target: residual graph tears → 0% (or document remaining known bound).

## Test plan
- FR-7 crash/abort still pass (plan 037/036 paths).
- Freeze + SI composition (040).

## Done criteria
- [ ] Visibility uses active snapshot (code evidence)
- [ ] Concurrent isolation test green
- [ ] Consistency doc updated with new S3 numbers
- [ ] Index DONE or BLOCKED with API gap report

## STOP conditions
- PG 13.4 GenericXLog records cannot carry enough for true SI without format change — write design
  addendum; do not half-implement.
- FR-7 abort tests fail — fix before claiming SI.

## Maintenance notes
- Product messaging: only claim “one snapshot across three stores” after this lands.
