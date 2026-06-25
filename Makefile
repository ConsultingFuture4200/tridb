.PHONY: test lint graph-test smoke-test test-all baseline-up baseline-down seed clean

IMAGE ?= tridb/msvbase:dev
ENGINE_TESTS := test/graph_store_test.sql test/trimodal_compose.sql \
                test/trimodal_early_term.sql test/fork_distance_probe.sql

test:
	pytest tests/ -q

lint:
	ruff check . && ruff format --check .

# Native-AM harnesses (DEV-1164/1165/1166) — each PGXS-builds src/graph_store in the image and
# FAILS LOUD on any error (build output -> log, nonzero make aborts; no piping to tail).
AM_TESTS := scripts/graph_am_test.sh \
            scripts/txn_atomicity_test.sh \
            scripts/crash_recovery_test.sh \
            scripts/graph_concurrency_test.sh

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

baseline-up:
	docker compose -f baseline/docker-compose.yml up -d

baseline-down:
	docker compose -f baseline/docker-compose.yml down -v

clean:
	rm -rf data/ bench/out/ .pytest_cache/
