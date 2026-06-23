.PHONY: test lint baseline-up baseline-down seed clean

test:
	pytest tests/ -q

lint:
	ruff check . && ruff format --check .

seed:
	python3 tools/seed_corpus.py --entities 1000 --dim 768 --out data/seed/

baseline-up:
	docker compose -f baseline/docker-compose.yml up -d

baseline-down:
	docker compose -f baseline/docker-compose.yml down -v

clean:
	rm -rf data/ bench/out/ .pytest_cache/
