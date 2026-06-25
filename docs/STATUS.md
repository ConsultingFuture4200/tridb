# TriDB Build Status — per-issue gating

Updated: 2026-06-23. Legend: 🟢 unblocked here · 🟡 partial (design here,
build on GX10) · 🔴 GX10-gated (needs live MSVBASE build).

> **RE-GATED 2026-06-23:** the dev workstation was proven a viable **x86_64 standin** —
> `scripts/x86build.sh --docker` builds the MSVBASE fork and `scripts/smoke_test.sh`
> passes (vectordb + HNSW + early-terminating ANN scan). See `docs/BUILD_NOTES.md`.
> Consequence: the native C work (DEV-1164–1170) is now **developed and smoke-tested on this
> standin** (proven: graph_store v0 in `src/graph_store_ext/`, `scripts/graph_test.sh` green)
> rather than blocked on hardware. The 🔴 markers below therefore mean **final acceptance is
> on-target** — the issue builds here but is signed off on the GX10; they are not "cannot build
> off-target." What stays strictly GX10-only: the ARM64 build sign-off (DEV-1160 as written)
> and the 128 GB headline benchmark.

> **PHASE-1 PROGRESS 2026-06-23 (v0, tested on the fork):** `src/graph_store_ext/` — a
> native graph-store extension. `scripts/graph_test.sh` is green:
> DEV-1165 traversal iterator (Open/Next/Close, lazy emission), DEV-1166 FR-7 (cross-store
> atomic rollback+commit). `test/trimodal_compose.sql` composes all three legs (graph +
> relational + vector) in one query. Linus loop caught + fixed a use-after-free and
> tempered overclaims. **v0 is heap-backed**, not the custom 32KB-page AM — honest scope
> and per-issue TODOs in `docs/graph_store_v0_limitations.md`.

> **EARLY-TERMINATING COMPOSITION 2026-06-23 (DEV-1167/1169 functional shape):**
> `test/trimodal_early_term.sql` — the canonical-shaped pipeline driven by the HNSW ANN
> index scan (early-terminating), graph traversal + relational filter per candidate. Plan
> is `Limit -> NestLoop(NestLoop(IndexScan hnsw, FuncScan neighbors), IndexScan d)`; ANN
> scan emitted 8 of 2000 sources. Linus-verified (3 lenses + re-run); filter proof made
> deterministic. Two fork constraints found → `docs/fork_findings.md`: FROM-SRFs
> materialize (production iterator must be custom-scan), and scalar `<->`/`l2_distance`
> return 0 (exact top-k must be the DEV-1168 C operator, not a SQL re-rank;
> `test/fork_distance_probe.sql` confirms).

| Issue | Title | Phase | Gating | Autonomous deliverable this repo |
| -- | -- | -- | -- | -- |
| DEV-1160 | SPIKE MSVBASE build on GX10 | 0 | 🔴 GX10 | Desk-spike findings already captured in issue; live build is GX10-only |
| DEV-1161 | Reproducible GX10 build script | 0 | 🟡 | `scripts/gx10build.sh` authored from spike deltas (runs on GX10) |
| DEV-1162 | Seed corpus + rel/vec smoke test | 0 | 🟡 | `tools/seed_corpus.py` (runs anywhere); SQL smoke test needs the build |
| DEV-1163 | Design adjacency-list layout | 1 | 🟢 | `docs/graph_store_layout_v0.1.0.md` + ADR-0002 |
| DEV-1164 | Adjacency-list access method | 1 | 🟡 | **v1 core built + tested on the x86 standin** (`src/graph_store/graph_am.c`, `scripts/graph_am_test.sh`): 32KB pages via shared buffer mgr + GenericXLog, incremental iterator, FR-7 abort + restart-persistence. Full TableAM vtable / secondary indexes / GX10 benchmark deferred (ADR-0003) |
| DEV-1165 | Graph traversal iterator | 1 | 🔴 GX10 | Iterator contract documented in layout spec; stub |
| DEV-1166 | Verify shared txn manager (FR-7) | 1 | 🔴 GX10 | Test plan in layout spec; runs on the build |
| DEV-1167 | SQL/PGQ surface → logical plan | 2 | 🟡 | `docs/sqlpgq_logical_plan_v0.1.0.md` design |
| DEV-1168 | HNSW relaxed-monotonicity iterator | 2 | 🔴 GX10 | Contract documented; wraps MSVBASE code |
| DEV-1169 | TJS operator | 2 | 🔴 GX10 | Design in plan-mapping doc; stub |
| DEV-1170 | Cross-modal join-order heuristic | 2 | 🟡 | `docs/join_order_heuristic_v0.1.0.md` + py reference model + unit test |
| DEV-1171 | Multi-system baseline harness | 3 | 🟢 | `baseline/` docker-compose + harness |
| DEV-1172 | TriDB benchmark harness | 3 | 🟡 | `bench/` harness skeleton; corpus + metric capture buildable, run needs TriDB |
| DEV-1173 | Benchmark results report | 3 | 🟡 | `bench/report.py` template + metric schema |

## What "done autonomously" means here

The 🟢 / 🟡 items are produced and (where runnable) tested on this dev box. The 🔴 items
get a precise interface skeleton + a written contract so that, the moment the GX10 build
exists, an implementer drops in C against a known surface rather than designing from zero.

## Handoff to GX10

1. Run `scripts/gx10build.sh` on the GX10 → confirms marker #1 live, produces the fork.
2. Implement `src/graph_store/` against `docs/graph_store_layout_v0.1.0.md`.
3. Wire TJS per `docs/sqlpgq_logical_plan_v0.1.0.md`; port `docs/join_order_heuristic`'s
   reference model into `src/planner/join_order.c`.
4. Run `bench/` against the `baseline/` harness on identical corpus → SM-1..SM-5.
