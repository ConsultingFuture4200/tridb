# Plan 028: PG17 platform feasibility spike + the public "why a fork" ADR

> **Executor instructions**: This is a SPIKE — the deliverable is EVIDENCE AND A DECISION DOCUMENT,
> not a working port. Timebox mentality: prefer a documented blocker over a heroic workaround.
> Follow steps in order; on any STOP condition, stop and report. Update your row in
> `advisor-plans/README.md` when done (unless a reviewer maintains it).
>
> **Drift check (run first)**: `git rev-parse --short HEAD` — this plan reads the repo but writes
> ONLY new files (spike dir + one ADR); drift elsewhere is irrelevant. If `docs/decisions/0015-*`
> already exists, renumber yours to the next free NNNN.

## Status

- **Priority**: P1 (gates the two biggest strategic decisions: fork-repo migration and DEV-1259 Phase B investment)
- **Effort**: M (spike-scoped)
- **Risk**: LOW (additive; no shipped code touched)
- **Depends on**: none (fully parallel to all other plans)
- **Category**: migration / direction
- **Planned at**: commit `e345998`, 2026-07-03

## Why this matters

TriDB ships as a fork of MSVBASE's PostgreSQL **13.4** — past EOL, the only fork in the surveyed
2026 landscape (pgvector, VectorChord, pgvectorscale, ParadeDB, Apache AGE all ship as extensions on
current PG), unavailable on managed Postgres, and practitioner sentiment says "why a fork of EOL
Postgres?" will be the top hostile launch question. Meanwhile PostgreSQL 19 ships SQL/PGQ
`GRAPH_TABLE` natively but lowers it to relational joins — potentially reframing TriDB's best
end-state as "the native executor for the GRAPH_TABLE Postgres just standardized." Before investing
the quarter's engine effort into the 13.4 fork (DEV-1259 Phase B) or migrating the 18-patch chain to
a fork repo, we need evidence: what EXACTLY binds TriDB to the fork, and what would a stock-PG17 +
pgvector port cost? The second deliverable — the public "why a fork" ADR — is needed at launch
REGARDLESS of the spike's outcome.

## Current state

What the fork provides that stock PG lacks (from the patch chain + ADRs — verify each in Step 1):
- **Relaxed-monotonicity index scans**: MSVBASE's base `Postgres.patch` adds `amcanrelaxedorderbyop`
  to the AM API + executor changes in `nodeIndexscan.c`/`nodeSort.c`/`genam.c` (see
  `scripts/patches/tridb_relaxed_order_executor_guard.patch` for the TriDB-hardened form). This is
  the mechanism behind VBASE-style ordered ANN streaming that `tjs()`'s vector-first body consumes
  via `xs_orderbyvals[0]`.
- **The MSVBASE HNSW index AM itself** (hnswlib-backed, in the fork's `src/`), which TriDB patched
  heavily (reloptions, recovery rebuild, NEON kernel, guards).
- 32KB block size build (`--with-blocksize=32`) for the graph AM's page layout.
- What does NOT need the fork: `src/graph_store` (native AM), `src/graph_store_ext`, `src/planner`
  are PGXS extensions compiled against whatever server headers are present — but written against
  PG 13 APIs (candidates for API drift: `table_open`, GenericXLog usage, AM handler struct fields,
  SPI, `TupleDescAttr`, buffer manager calls).
- Stock-PG comparators to evaluate: pgvector ≥0.8 "iterative index scans" (its answer to
  post-filter starvation) vs the fork's relaxed iterator; PG17's `MemoryContext` / AM API deltas.
- The operators (`tjs`, `tjs_open`, filter-first) live INSIDE the fork's vectordb module today; a
  port would re-host `execTJS` etc. as an extension calling pgvector's index (or its own AM).
- Landscape/positioning source to cite: `docs/landscape_review_v0.1.0.md` (F3, research sections).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Spike sandbox image | `docker run -d --name pg17spike -e POSTGRES_PASSWORD=spike postgres:17` then `docker exec` builds | container healthy |
| Get build deps in sandbox | `docker exec pg17spike bash -c 'apt-get update && apt-get install -y build-essential postgresql-server-dev-17 git'` | exit 0 |
| PGXS compile attempt | inside container: `make PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config` against a copied `src/graph_store` | success OR a captured error log (both are spike data) |
| pgvector | `docker exec pg17spike bash -c 'git clone --depth 1 --branch v0.8.1 https://github.com/pgvector/pgvector && cd pgvector && make && make install'` | installs |
| Python/lint (docs only) | `make lint` | clean |

## Scope

**In scope (all NEW files):** `spike/pg17/` — build logs, compile-error inventories, any
compat-shim scratch code (clearly marked NOT SHIPPED); `docs/decisions/0015-pg17-platform-spike.md`
(the ADR, structure below). Container work happens in throwaway docker, never against the repo's
images.

**Out of scope:** ANY modification to `src/`, `scripts/patches/`, `vendor/`, Makefile, CI. No
"quick fixes" to make TriDB code compile on 17 land in the shipped tree — shims stay in `spike/`.

## Git workflow
Branch `advisor/028-pg17-spike`; `docs(adr):`/`spike:` commits; do NOT push.

## Steps

### Step 1: Fork-dependency inventory (reading, no containers)
Produce `spike/pg17/fork_dependency_inventory.md`: for each of the 18 patches + the 3 PGXS
extensions, classify: [fork-only mechanism | PG-13-API usage | portable]. For the relaxed-monotonicity
mechanism specifically, cite the exact AM-API fields/executor hooks the base patch adds and note the
PG-17 equivalent or absence. List every PG symbol the graph AM uses that changed between 13 and 17
(check against PG release notes — WebSearch allowed).
**Verify**: file exists, all 18 patches + 3 extensions classified.

### Step 2: PGXS compile probe on PG17
In the sandbox: copy `src/graph_store`, `src/planner`, `src/graph_store_ext` in; attempt PGXS builds
with PG17 headers; capture full compiler output to `spike/pg17/compile_{graph_store,planner,ext}.log`.
For each error class: one-line diagnosis + est. fix size (S/M/L). NOTE: PG17 default block size is
8KB — record which graph-AM assumptions (32KB pages) break at compile vs runtime, and what a
`--with-blocksize=32` self-built PG17 would change.
**Verify**: three logs exist; inventory updated with measured (not guessed) error counts.

### Step 3: pgvector iterative-scan equivalence probe
Install pgvector 0.8.x in the sandbox. Write `spike/pg17/pgvector_iterative_probe.sql`: build a
20k×64 corpus with a selective predicate; measure (a) recall@10 + rows-scanned under
`SET hnsw.iterative_scan = relaxed_order` with increasing `hnsw.max_scan_tuples`, vs (b) exact.
Answer in the ADR: does the iterative scan give the operator model tjs needs (resumable
ordered stream with per-candidate visibility), and what is lost vs the fork's `xs_orderbyvals`
relaxed iterator (list concrete API gaps: no per-tuple distance exposure? termination control?).
**Verify**: probe runs; numbers in the ADR, not adjectives.

### Step 4: The ADR
`docs/decisions/0015-pg17-platform-spike.md`, Status: Proposed (decision = maintainer's). Sections:
Context (fork liability, PG19 GRAPH_TABLE — cite `docs/landscape_review_v0.1.0.md`); Evidence
(Steps 1-3 measured results); Options with honest costs: (A) stay on the 13.4 fork (own the EOL
posture, CVE monitoring, fork-repo migration), (B) port to stock PG17+ as extensions (itemized:
graph AM port S/M/L from Step 2, operator re-host on pgvector iterative scans with the Step-3 gap
list, what dies: 32KB pages? relaxed executor?), (C) hybrid (graph+planner as extensions on stock
PG now, vector leg stays forked until pgvector parity). Include the PUBLIC-facing "why a fork today"
paragraph (honest, citable in README/launch FAQ) regardless of option. Recommendation section
permitted but marked as advisory.
**Verify**: ADR exists, numbered correctly, `make lint` green; every claim in Options traces to a
Step 1-3 artifact.

## Test plan
Spike artifacts ARE the evidence; no shipped-code tests. `make test && make lint` must stay green
(nothing shipped changed).

## Done criteria
- [ ] Inventory + 3 compile logs + pgvector probe committed under `spike/pg17/`
- [ ] ADR-0015 committed with measured numbers and the public why-a-fork paragraph
- [ ] `git diff --name-only e345998..HEAD` shows ONLY `spike/pg17/*`, `docs/decisions/0015-*`, README row
- [ ] README status row updated

## STOP conditions
- No network access for the sandbox installs → report which steps completed offline.
- Any step tempts you to modify shipped source to proceed — that IS the stop.
- Step 2 requires building PostgreSQL itself from source to make progress (blocksize experiments) —
  record the requirement and its cost in the ADR instead of doing it.

## Maintenance notes
This ADR gates: the fork-repo migration (F8), DEV-1259 Phase-B investment shape (C1), and the launch
FAQ. Re-run the pgvector probe against each pgvector minor until the decision is made. If option B/C
is chosen, plan 025's compat shims become the porting seam — note the connection.
