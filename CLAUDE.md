# TriDB — Project Instructions

Tri-modal (vector + graph + relational) DBMS. Clone of AkasicDB, built by forking
MSVBASE and adding a native adjacency-list graph store inside the same Postgres process.
Full context: `README.md`, `spec/tridb_spec_v0.1.0.md`, Linear project TriDB.

## Golden rules

1. **TR-1 is non-negotiable.** Every operator honors Open/Next/Close + early termination.
   No blocking operators. A blocking operator forfeits the entire efficiency thesis —
   reject any design that materializes a full intermediate result.
2. **Never leave the Postgres process.** One transaction manager, one WAL. The graph
   store is a Postgres access method, not a sidecar. No second WAL, no cross-system txn.
3. **Graph is native, never relational joins.** Topology is an adjacency-list access
   method. Do NOT model edges as relational join tables — that is the path TriDB rejects.
4. **One canonical query for v1** (spec §5). Don't generalize the surface; assemble from
   existing SQL/PGQ + pgvector standards, no new query language.
5. **Three stores only.** Vector / graph / relational = similarity / traversal / filter.
   BM25 seam architected but closed for v1.

## Hardware reality

Target hardware is the **GX10 (ARM64 + CUDA, 128 GB)**. The MSVBASE fork build and all
native C work compile *only on the GX10*. A non-GX10 workstation can build/test the
hardware-independent layer (design, build script, seed tooling, baseline harness) but
must NOT claim the C access-method or the live benchmark is done — those are GX10-gated.
When you produce gated C, mark it clearly as unbuilt-here and stop short of "passes".

## Build & test commands

Hardware-independent layer (works on any x86_64/ARM64 dev box):

```bash
# Setup
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

# Tests (buildable-anywhere suite: seed corpus, harness, planner design checks)
make test                    # -> pytest tests/   (Python only, fast, no Docker)

# Lint/format
make lint                    # -> ruff check . && ruff format --check .

# Generate a seed corpus
python3 tools/seed_corpus.py --entities 1000 --dim 768 --out data/seed/
```

Engine layer (the graph store + tri-modal SQL suites) — needs the `tridb/msvbase:dev`
image from `scripts/x86build.sh --docker`:

```bash
make smoke-test              # -> test/smoke.sql (vector + relational) in the image
make graph-test              # -> all four test/*.sql engine suites, fail-fast
make test-all                # -> test + lint + smoke-test + graph-test (full verify)

# Stand up the multi-system baseline (DEV-1171)
make baseline-up             # -> docker compose -f baseline/docker-compose.yml up -d
make baseline-down
```

GX10-only (do not run off-target):

```bash
scripts/gx10build.sh         # builds the MSVBASE fork (PG 13.4, HNSW, --with-blocksize=32)
```

## Conventions

- Design docs: `docs/`, versioned (`*_v0.1.0.md`). ADRs: `docs/decisions/NNNN-*.md`, numbered.
- Spec evolution: append an addendum / bump version, do not silently rewrite.
- Commits: `type(scope): summary`. Branch names match Linear: `dustin/dev-NNNN`.
- C for Postgres internals targets PG 13.4 access-method APIs; 32KB block size.
- Python: `uv`/`pip`, `ruff`, `pytest`.

## Issue map

Phase 0 plumbing: DEV-1160 (spike, GX10), 1161 (build script), 1162 (smoke test).
Phase 1 graph store: DEV-1163 (layout design), 1164 (access method, GX10), 1165 (iterator,
GX10), 1166 (txn verify, GX10). Phase 2 TJS: DEV-1167 (SQL/PGQ surface), 1168 (HNSW
iterator), 1169 (TJS operator), 1170 (join order). Phase 3 bench: DEV-1171 (baseline),
1172 (harness), 1173 (report).
