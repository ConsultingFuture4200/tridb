.PHONY: test lint graph-test smoke-test test-all baseline-up baseline-down seed bench bench-live sweep sm2 fetch-dataset bench-public fetch-hotpot graphrag graphrag-live bench-filtered ablation recall-decay tjs-open-ref graphrag-h2h clean

PUBLIC_DATASET ?= gist-960-euclidean

# Prefer the repo venv (has numpy/fastembed/baseline clients); fall back to python3.
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

IMAGE ?= tridb/msvbase:dev
ENGINE_TESTS := test/graph_store_test.sql test/trimodal_compose.sql \
                test/trimodal_early_term.sql test/fork_distance_probe.sql \
                test/vector_relaxed_mono_test.sql test/canonical_e2e_test.sql \
                test/parse_canonical.sql

test:
	$(PY) -m pytest tests/ -q

lint:
	ruff check . && ruff format --check .

# Native-AM harnesses (DEV-1164/1165/1166) — each PGXS-builds src/graph_store in the image and
# FAILS LOUD on any error (build output -> log, nonzero make aborts; no piping to tail).
AM_TESTS := scripts/graph_am_test.sh \
            scripts/txn_atomicity_test.sh \
            scripts/crash_recovery_test.sh \
            scripts/graph_concurrency_test.sh \
            scripts/graph_edge_count_test.sh \
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

# Fetch the PINNED recognized public ANN dataset for the public benchmark (GTM make-or-break).
# NETWORK-GATED: this downloads (~hundreds of MB) and verifies the SHA256 — it is NOT run by tests
# or CI. Default gist-960-euclidean (dim 960, L2 — the 768+ headline set). See tools/fetch_dataset.py
# for the pinned URL/checksum + the first-fetch --pin flow. Override the set with PUBLIC_DATASET=...
fetch-dataset:
	python3 -m tools.fetch_dataset --dataset $(PUBLIC_DATASET)

# LIVE benchmark on a RECOGNIZED PUBLIC dataset (the GTM make-or-break, docs/benchmark_public_v0.1.0.md).
# Runs the canonical tjs() query on the LIVE forked-MSVBASE engine over a topical graph synthesized on
# REAL public embeddings, grading recall@k against an exact numpy oracle. Sibling of bench-live/sweep:
# it guards on BOTH the dataset being present (else: make fetch-dataset) AND the engine image (the live
# run is GX10/stack-gated). The recall oracle is computed host-side on the real embeddings (no engine);
# only the live tjs()/latency measurement is gated. One-command repro: make fetch-dataset && make bench-public.
bench-public:
	@test -f data/public/$(PUBLIC_DATASET).hdf5 || \
	  { echo "dataset data/public/$(PUBLIC_DATASET).hdf5 missing — run: make fetch-dataset"; exit 1; }
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || \
	  { echo "image $(IMAGE) not built (live run is ENGINE-GATED) — run scripts/x86build.sh --docker / gx10build.sh"; exit 1; }
	PUBLIC_DATASET=$(PUBLIC_DATASET) bash scripts/bench_public.sh $(IMAGE)

# GraphRAG QA-accuracy benchmark (Plan 015) — the "is the answer right?" artifact.
# REAL multi-hop QA (HotpotQA), a REAL embedding-independent graph (title-mention
# proxy for Wikipedia hyperlinks), graded on evidence recall + downstream answer
# EM/F1: graph-constrained tjs() retrieval vs a vector-only ablation. ACCURACY is
# host-side (no engine, like tools/real_corpus.py recall); the live tjs() latency
# and the full retrieve-from-all-Wikipedia fullwiki run are GX10-gated (graphrag-live).
HOTPOT_Q ?= 500
GRAPHRAG_READER ?= extractive   # 'anthropic' for the LLM EM/F1 headline (needs ANTHROPIC_API_KEY)

# Network-gated: pulls the HotpotQA dev slice from the HF mirror (CMU host is down).
# NOT run by tests/CI, same policy as fetch-dataset.
fetch-hotpot:
	$(PY) -m tools.fetch_hotpot --questions $(HOTPOT_Q) --out data/hotpot/dev_slice.json

# Host-side accuracy (buildable here). Needs the dev slice (make fetch-hotpot) and
# the embedder (fastembed). Builds the real graph + BGE-768 embeddings, then grades.
graphrag:
	@test -f data/hotpot/dev_slice.json || { echo "no dev slice — run: make fetch-hotpot"; exit 1; }
	$(PY) -m tools.hotpot_corpus --slice data/hotpot/dev_slice.json --k 10
	$(PY) -m bench.graphrag_report --reader $(GRAPHRAG_READER)

# LIVE engine head-to-head (GX10/engine-gated): canonical tjs() + live latency vs the
# multi-store baseline. Guards on the image like bench-public; UNBUILT-HERE off-target.
graphrag-live:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || \
	  { echo "image $(IMAGE) not built — graphrag-live is ENGINE-GATED (UNBUILT-HERE)"; exit 1; }
	bash scripts/bench_graphrag.sh $(IMAGE)

# Filtered vector search (VectorDBBench IntFilter methodology) on the live engine:
# recall@k + latency vs filter SELECTIVITY on real SIFT-128. ENGINE-gated; keep
# FILT_LIMIT small on the standin. GX10 headline: FILT_LIMIT=1000000 (NEON HNSW).
bench-filtered:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || \
	  { echo "image $(IMAGE) not built — bench-filtered is ENGINE-GATED"; exit 1; }
	bash scripts/bench_filtered.sh $(IMAGE)

# 4-way tri-modal FUSION ABLATION on MultiHopRAG (vector / graph / relational /
# fusion), recall@k — the thesis-falsification test. Host-side (no engine); needs
# the embedder (fastembed) + HF reachable. Add --reuse-embeddings to skip re-embed.
MHRAG_Q ?= 300
ablation:
	$(PY) -m tools.multihoprag_corpus --questions $(MHRAG_Q) --k 10
	$(PY) -m bench.ablation_report --k 10

# Vector recall decay under upsert/delete churn on hnswlib (the engine's own vector
# lib), real SIFT-128, with a rebuild reference. Host-side; the at-scale (1M+) decay
# curve is the GX10 follow-up. DECAY_LIMIT scales the base set.
DECAY_LIMIT ?= 20000
recall-decay:
	@test -f data/public/sift-128-euclidean.hdf5 || \
	  { echo "dataset missing — run: make fetch-dataset PUBLIC_DATASET=sift-128-euclidean"; exit 1; }
	$(PY) -m bench.recall_decay --limit $(DECAY_LIMIT)

# tjs_open (B) host reference (Plan 007): bounded-push PPR ranking + NRA/FR-bound
# termination + RRF fusion, the executable spec for the GX10/engine-gated realization (B).
# Host-only (no engine, no LLM); recall@k vs gold_ids like v2a_open. The full HotpotQA run
# is DATA-gated (needs data/hotpot/manifest.json from `make fetch-hotpot`); the unit tests
# (tests/test_tjs_open_ref.py) run anywhere via `make test`.
HOTPOT_MANIFEST ?= data/hotpot/manifest.json
tjs-open-ref:
	@test -f $(HOTPOT_MANIFEST) || \
	  { echo "manifest $(HOTPOT_MANIFEST) missing — DATA-GATED; build it with: make fetch-hotpot"; exit 1; }
	$(PY) -m bench.tjs_open_ref --manifest $(HOTPOT_MANIFEST)

# Real-workload head-to-head (GTM #1): canonical tjs() on the live engine vs the
# tuned multi-store baseline (Milvus+Neo4j+rerank), same HotpotQA corpus+queries+k,
# recall@k + end-to-end latency. Needs the engine image + baseline stack up.
graphrag-h2h:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || { echo "image $(IMAGE) not built (ENGINE-GATED)"; exit 1; }
	bash scripts/bench_graphrag_h2h.sh $(IMAGE)

baseline-up:
	docker compose -f baseline/docker-compose.yml up -d

baseline-down:
	docker compose -f baseline/docker-compose.yml down -v

clean:
	rm -rf data/ bench/out/ .pytest_cache/
