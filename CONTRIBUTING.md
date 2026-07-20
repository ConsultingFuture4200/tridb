# Contributing to TriDB

Thanks for taking a look. TriDB is a tri-modal (vector + graph + relational) DBMS that runs all three
in **one PostgreSQL query plan, one process, one transaction manager** — a clean-room realization of
AkasicDB built on a fork of MSVBASE (VBASE, OSDI '23) + Chimera's (PVLDB) co-resident graph store. If
you're new, read in this order: [`README.md`](README.md) → [`spec/tridb_spec_v0.1.0.md`](spec/tridb_spec_v0.1.0.md)
→ [`docs/decisions/`](docs/decisions/) (the ADRs) → [`docs/STATUS.md`](docs/STATUS.md) (per-issue state)
→ [`advisor-plans/`](advisor-plans/) + [`plans/`](plans/) (scoped, self-contained improvement plans).

The current **strategic roadmap** is [`docs/tridb_productization_roadmap_v0.1.0.md`](docs/tridb_productization_roadmap_v0.1.0.md)
(the D1→D2→D3 spike→product ladder, with Addenda A1/A2/A3 recording the Gate A/B/CSR verdicts). That
is the direction-setting document; `plans/` and `advisor-plans/` are the independently-numbered batches
of scoped implementation plans that execute against it — don't confuse a plan number with a roadmap phase.

## The non-negotiable invariants (read before proposing changes)

These are the project's load-bearing design rules (full text in [`CLAUDE.md`](CLAUDE.md)). A change that
violates one will be rejected on design grounds, not style:

1. **TR-1 — no blocking operators.** Every operator is a Volcano `Open/Next/Close` iterator with early
   termination. No full sort-before-emit, no hash-build-before-probe, no materialize-all on the
   canonical path.
2. **One Postgres process, one transaction manager, one WAL.** The graph store is a Postgres access
   method, not a sidecar. No second WAL, no cross-system 2PC.
3. **Graph topology is a native adjacency-list access method**, never relational join tables.
4. **One canonical query for v1** — SQL/PGQ `GRAPH_TABLE(...)` + pgvector `<->`, no new query language.
5. **Three stores only** (vector / graph / relational). The BM25 seam is architected but closed for v1.

## The two build layers

**Hardware-independent layer (Python tooling, harnesses, design) — runs on any x86_64/ARM64 box:**

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
# (if python3-venv is unavailable, `uv venv .venv && uv pip install -r requirements.txt` also works)
# Optional: `pip install -r requirements-vdbb.txt` for the VectorDBBench adapter (bench/vdbb_tridb.py) —
# not needed for make test/make lint.
make test      # pytest — fast, no Docker
make lint      # ruff check + format --check
```

**Engine layer (the MSVBASE fork + native graph store) — needs the Docker image:**

```bash
scripts/x86build.sh --docker   # builds tridb/msvbase:dev (the x86 dev/CI engine image)
make test-all                  # test + lint + smoke-test + graph-test (full verify)
make graph-test                # the native-AM engine suites (PGXS-build src/ in the image)
```

The engine test harnesses PGXS-build `src/graph_store` and `src/planner` from your working tree **inside
the image at test time**, so your C changes are compiled and run by `make graph-test` — no image rebuild
needed for first-party `src/` changes.

### Hardware-gating reality (important for honest reporting)

The native C **builds and runs on the x86 engine image**. Two things are **GX10-only** (the target is a
DGX Spark / GB10, ARM64 + CUDA, 128 GB): the **ARM64 build sign-off** and the **128 GB headline
benchmark**. If you write GX10-gated C or a benchmark that needs the target, mark it clearly as
**UNBUILT-HERE** and do **not** claim it "builds" or "passes" off-target. "Shipped" means tested + tagged,
not merely on `master`.

## Conventions

- **Commits:** `type(scope): summary` (e.g. `feat(graph-store): …`, `fix(planner): …`, `docs(adr): …`),
  present-tense, scoped. One logical change per commit.
- **Branches:** `feat/…`, `fix/…`, `chore/…`, `docs/…`, `spec/…` (or `dustin/dev-NNNN` matching Linear).
- **Decisions that lock in structure get an ADR** in `docs/decisions/NNNN-*.md` (numbered).
- **Specs evolve by addendum / version bump**, not silent rewrite.
- **Python:** `ruff` for lint + format, `pytest` for tests, `requirements.txt` (no `setup.py`).
- **Extension versioning (plan 100):** from 0.2.0 on, released surface changes to
  `graph_store_am` / `tjs_pg` ship as `--X--Y.sql` **upgrade scripts** (`ALTER EXTENSION ...
  UPDATE`); editing the base `--X.Y.Z.sql` in place is only allowed pre-release within a
  version. Bump `default_version` in the `.control`, add the upgrade script to the extension
  `Makefile`'s `DATA`, and keep `scripts/extension_upgrade_test.sh` green (it installs the
  vendored previous-release fixtures from `test/fixtures/upgrade/` and proves data survives
  the upgrade).
- **C:** targets both the **PostgreSQL 13.4 fork** and **stock PostgreSQL 16/17** access-method APIs
  (zero measured PG 13→17 drift, ADR-0015 E2). The graph AM is BLCKSZ-capability, not fixed
  (`gph_page.h`: `BLCKSZ >= 8192` — 8KB works on stock PG, 32KB is the high-degree performance target
  on the fork). MSVBASE fork edits ship as patches under `scripts/patches/` (vendored source is
  re-cloned), wired idempotently into `scripts/lib/msvbase_patches.sh` with a `verify_patches` sentinel.

## Submitting a change

1. Open an issue first for anything non-trivial (or comment on an existing one) so the design fits the
   invariants above.
2. Branch, make the change, and ensure **`make test` and `make lint` pass**; if you touched the engine
   and have the image, run **`make graph-test`** (and `crash_recovery_test.sh` / `txn_atomicity_test.sh`
   for anything touching the graph store's write path — FR-7 atomicity must hold).
3. Open a PR with a clear description and the verification output. State plainly what you ran vs. what is
   GX10-gated/unverified.

## Good places to start

- [`advisor-plans/README.md`](advisor-plans/README.md) and [`plans/README.md`](plans/README.md) list
  scoped, self-contained improvement plans with verification gates.
- Documentation reconciliation (the ADRs and `STATUS.md` occasionally drift as findings land) is always
  welcome and low-risk.
- Reproducing a benchmark on real public data (HotpotQA / SIFT — see the `make bench-*` targets) and
  reporting honestly is high-value (and the credibility gap the project most wants closed).

## Security

Please report vulnerabilities privately — see [`SECURITY.md`](SECURITY.md). Note in particular that the
`tjs()` operator's SQL-fragment arguments are **internal-only** (fed from controlled query lowering) and
must not be exposed to untrusted input.
