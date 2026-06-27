.PHONY: test lint graph-test smoke-test test-all baseline-up baseline-down seed bench bench-live sweep sm2 clean

IMAGE ?= tridb/msvbase:dev
ENGINE_TESTS := test/graph_store_test.sql test/trimodal_compose.sql \
                test/trimodal_early_term.sql test/fork_distance_probe.sql \
                test/vector_relaxed_mono_test.sql test/canonical_e2e_test.sql \
                test/parse_canonical.sql

test:
	pytest tests/ -q

lint:
	ruff check . && ruff format --check .

# Native-AM harnesses (DEV-1164/1165/1166) — each PGXS-builds src/graph_store in the image and
# FAILS LOUD on any error (build output -> log, nonzero make aborts; no piping to tail).
AM_TESTS := scripts/graph_am_test.sh \
            scripts/txn_atomicity_test.sh \
            scripts/crash_recovery_test.sh \
            scripts/graph_concurrency_test.sh \
            scripts/join_order_test.sh

# Engine test suites — require the tridb/msvbase:dev image (scripts/x86build.sh --docker).
graph-test:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || \
	  { echo "image $(IMAGE) not built — run scripts/x86build.sh --docker"; exit 1; }
	@for t in $(ENGINE_TESTS); do \
	  echo "=== $$t ==="; bash scripts/graph_test.sh $(IMAGE) $$t || exit 1; done
	@for h in $(AM_TESTS); do \
	  echo "=== $$h ==="; bash $$h $(IMAGE) || exit 1; done

smoke-test:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || \
	  { echo "image $(IMAGE) not built — run scripts/x86build.sh --docker"; exit 1; }
	bash scripts/smoke_test.sh

# Full verification: fast Python+lint layer, then the engine (smoke + graph) layer.
test-all: test lint smoke-test graph-test

seed:
	python3 tools/seed_corpus.py --entities 1000 --dim 768 --out data/seed/

# TriDB benchmark (DEV-1172 harness + DEV-1173 report), deterministic STUB engine
# (runs anywhere). Seeds a small corpus if data/seed is missing, then drives the
# canonical query vs the in-process baseline and renders bench/out/report.html.
# The live engine run (--engine live) is GX10/engine-gated; do not run off-target.
bench:
	@test -f data/seed/entities.csv || \
	  python3 tools/seed_corpus.py --entities 200 --dim 32 --out data/seed/
	python3 -m bench.harness --seed-dir data/seed --k 5 --engine stub \
	  --out bench/out/bench_metrics.json --html bench/out/report.html

# LIVE TriDB Phase-3 benchmark (DEV-1172/1173): drives the canonical query on the
# REAL forked-MSVBASE engine (tridb/msvbase:dev) over a real corpus across many
# queries, captures the actual TriDB-side numbers (tjs answer set + parity oracle,
# tjs_candidates_examined -> SM-3, EXPLAIN ANALYZE latency), derives SM-1..SM-5 vs
# the in-process baseline model, and renders bench/results/report_live.html.
# Needs the image (scripts/x86build.sh --docker). The TriDB side is live-measured;
# SM-2 head-to-head + the 128 GB headline are GX10-/stack-gated (see the report).
bench-live:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || \
	  { echo "image $(IMAGE) not built — run scripts/x86build.sh --docker"; exit 1; }
	bash scripts/bench_live.sh $(IMAGE)

# LIVE HNSW index-quality x term_cond sweep on the NEON+reloptions engine (DEV-1286). One-command
# repro of bench/results/neon_sweep_* — the GTM launch gate (docs/gtm_opensource_v0.1.0.md). Sweeps
# each index config (m/ef_construction reloptions) x term_cond, grading recall@k vs an exact numpy
# oracle plus examined-% and EXPLAIN ANALYZE latency. Defaults reproduce the committed 20k/128 run;
# the headline is the same script at scale: SWEEP_ENTITIES=100000 SWEEP_DIM=768 make sweep.
# GX10/engine-gated — needs the image (scripts/x86build.sh --docker / gx10build.sh).
sweep:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || \
	  { echo "image $(IMAGE) not built — run scripts/x86build.sh --docker / gx10build.sh"; exit 1; }
	bash scripts/bench_gx10_sweep.sh $(IMAGE)

# FAIR SM-2 head-to-head (DEV-1171): LIVE TriDB vs the LIVE multi-system baseline
# (Milvus+Neo4j+Postgres). Both sides run the IDENTICAL corpus + queries + k from
# one deterministic generator, and both are measured the SAME way (client-side
# end-to-end wall-clock per query, warm connections, median of N runs, load/index
# excluded). Emits bench/results/sm2_metrics.json + docs/benchmark_sm2_v0.1.0.md.
# Needs the engine image (scripts/x86build.sh --docker) AND the baseline stack up
# (make baseline-up) AND the repo .venv with pymilvus/neo4j/psycopg.
sm2:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || \
	  { echo "image $(IMAGE) not built — run scripts/x86build.sh --docker"; exit 1; }
	@docker ps --filter name=tridb-baseline --format '{{.Names}}' | grep -q tridb-baseline || \
	  { echo "baseline stack not up — run make baseline-up"; exit 1; }
	bash scripts/bench_sm2.sh $(IMAGE)

baseline-up:
	docker compose -f baseline/docker-compose.yml up -d

baseline-down:
	docker compose -f baseline/docker-compose.yml down -v

clean:
	rm -rf data/ bench/out/ .pytest_cache/
