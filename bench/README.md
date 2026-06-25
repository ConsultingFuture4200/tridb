# Bench — TriDB benchmark harness + report (DEV-1172 / DEV-1173)

Drives the **one canonical query** (spec §5) on an identical corpus against
**both** TriDB and the multi-system baseline (`baseline/`), captures the five
success metrics (spec §7) per-query and in aggregate, and renders a read-once
HTML report comparing the two against their targets.

| Metric | Target (spec §7) | Measured on |
|---|---|---|
| SM-1 | ≥5× intermediate-result reduction vs. baseline | TriDB vs baseline peak intermediate rows |
| SM-2 | lower latency on ≥80% of queries | per-query TriDB vs baseline latency |
| SM-3 | <25% of corpus examined for k=5 | TriDB (worst-case query) |
| SM-4 | ≥99% answer-set parity with baseline | per-query Jaccard, averaged |
| SM-5 | 100% transaction atomicity | TriDB (one txn manager, one WAL) |

## Layout

| File | Role |
|---|---|
| `metrics.py` | Typed metric schema. `QuerySample` (per-query raw obs), `MetricResult` / `BenchmarkReport` (derived SM verdicts vs targets), JSON (de)serialization. Single source of truth for the SM targets. |
| `driver.py` | Engine abstraction. `EngineDriver` interface; `StubDriver` (deterministic, no engine — runs anywhere); `LiveDriver` (GX10/engine-gated, **UNBUILT-HERE**). |
| `harness.py` | Loads the corpus, runs the canonical query vs TriDB (driver) and vs the in-process baseline model, derives SM-1..SM-5. CLI entry. |
| `report.py` | Renders the self-contained HTML report (DEV-1173) incl. the v2-recommendation section. |

## Engine gating

The live TriDB run needs the forked-MSVBASE engine + native graph access method,
which build **only on the GX10** (see `CLAUDE.md` "Hardware reality"). The engine
is therefore behind `EngineDriver`:

- **`StubDriver` (default, `--engine stub`)** — computes the canonical-query
  answer set directly from the corpus (the ground truth a correct TJS plan must
  return, so SM-4 parity is exact) and reports the *bounded, early-terminating*
  intermediate sizes / corpus-examined counts the TR-1 plan would produce. Fully
  runnable and unit-tested off-target.
- **`LiveDriver` (`--engine live`)** — GX10/engine-gated. Connects to a running
  TriDB and runs the one canonical SQL/PGQ query, reading real peak intermediate
  rows + rows-examined from the TJS custom-scan node via `EXPLAIN (ANALYZE)`.
  Raises off-target; the query text + instrumentation contract are fixed so the
  on-target implementer drops in psycopg + EXPLAIN parsing against a known surface.

The baseline side mirrors the split: an in-process model (`--baseline inprocess`,
default, runs anywhere) replays `baseline/harness.py`'s materialize-transfer-prune
cost; `--baseline live` drives the real docker-compose stack (stack-gated).

## Run (stub — works anywhere)

```bash
make bench
# or explicitly:
python -m bench.harness --seed-dir data/seed --k 5 --engine stub \
  --out bench/out/bench_metrics.json --html bench/out/report.html
```

Output:

```
[bench] engine=stub k=5 corpus=1000 queries=10
[bench] SM-1 PASS: baseline materialized N intermediate rows vs TriDB M (...x reduction; target >= 5x)
[bench] SM-2 PASS: TriDB faster on 10/10 queries ...
[bench] SM-3 PASS: worst-case ...% of corpus examined ...
[bench] SM-4 PASS: mean answer-set parity 100.0% ...
[bench] SM-5 PASS: 10/10 queries atomic ...
```

The harness exits non-zero if any metric fails (CI-friendly).

## Run (live — GX10 / engine-gated)

On the GX10, with the forked-MSVBASE TriDB up and the corpus loaded:

```bash
python -m bench.harness --seed-dir data/seed --k 5 --engine live --dsn "$TRIDB_DSN" \
  --baseline live --out bench/out/bench_metrics.json --html bench/out/report.html
```

> Off-target this raises `NotImplementedError` by design — the stub numbers are a
> deterministic model, **not** the live engine. Re-run live before quoting
> SM-1/SM-2/SM-3 latencies as real.

## Re-render the report from saved metrics

```bash
python -m bench.report --metrics bench/out/bench_metrics.json --out bench/out/report.html
```

## Output

`bench/out/` (gitignored): `bench_metrics.json` (full `BenchmarkReport`) and
`report.html` (read-once comparison + SM scoreboard + v2 recommendations).
