# Plan 030: Benchmark credibility + reproducibility batch — tail latencies, multi-client, pinned baselines/datasets, dependency hygiene

> **Executor instructions**: Follow step by step; verify each step. On any STOP condition, stop and
> report. Update your row in `advisor-plans/README.md` when done (unless a reviewer maintains it).
>
> **Drift check (run first)**: `git diff --stat e345998..HEAD -- bench/ baseline/ tools/fetch_dataset.py requirements* Makefile docs/benchmark_sm2_1m_v0.2.0.md baseline/TUNING.md`
> Additive drift from 025/027/029 is expected; excerpt mismatches are a STOP.

## Status

- **Priority**: P2
- **Effort**: M-L
- **Risk**: LOW-MED (harness + packaging; one lockfile regeneration)
- **Depends on**: soft on 025 (a v1-measured headline should exist before anything is published);
  no hard code dependency
- **Category**: tests / deps / docs (launch credibility)
- **Planned at**: commit `e345998`, 2026-07-03

## Why this matters

The internal methodology (exact-oracle scoring, committed baseline tuning, curve-not-point recall,
honesty boxes) already exceeds vendor norm — but the public-facing gaps are exactly the ones that
get benchmark reports shredded in 2026: median-only single-client latency (no p95/p99/QPS), no
Milvus-HNSW row (IVF-only baseline invites "you picked the slow index"), baseline versions not
pinned in the report, the DEFAULT public dataset fetchable only unpinned over plain HTTP while SIFT
has a committed SHA256, and a dependency story where the floors have drifted a major version below
the validated lock, the pymilvus 2.6 client drives a Milvus 2.4.5 server, and a single optional
adapter (`bench/vdbb_tridb.py`) drags streamlit/aliyun/s3fs into every install. Fix the enumerable
list; each item is small.

## Current state

- `bench/sm2_compare.py` — per-query it computes `statistics.median(samples_ms)` for both sides
  (docstring: "MEDIAN of N runs"); samples ARE retained in the metrics JSON (`tridb_samples_ms`,
  `baseline_samples_ms`) but no percentiles are derived; SM-2 = fraction of queries where TriDB
  median < baseline median.
- `baseline/sm2.py` — `MILVUS_INDEX = {"index_type": "IVF_FLAT", ... nlist}` +
  `MILVUS_SEARCH_PARAM` (nprobe), env-overridable via `BASELINE_NLIST`/`BASELINE_NPROBE`/
  `BASELINE_ANN_FANOUT`; single client, sequential queries, `--runs N` per query; `--no-load` mode
  reuses a loaded stack.
- `tools/bench_sm2_corpus.py` — emits the TriDB-side timed psql script (single connection,
  sequential `\timing` statements) + manifest; recently gained `--join-order`.
- `tools/fetch_dataset.py:90` `_BASE = "http://ann-benchmarks.com"`; `gist-960-euclidean` (the
  DEFAULT/headline set) has `sha256=_PENDING`; `sift-128-euclidean` has a real digest
  (`dd6f0a6ed6...`); unpinned sets are fetchable only via `--allow-unpinned` which SKIPS verification.
- `requirements.txt` floors: `numpy>=1.26`, `pytest>=8.0`, `neo4j>=5.20`, `pymilvus>=2.4` … vs
  `requirements.lock`: `numpy==2.5.0`, `pytest==9.1.1`, `neo4j==6.2.0`, `pymilvus==2.6.16`.
  `bench/vdbb_tridb.py` imports `vectordb_bench.*` (lock-only, undeclared in floors; owns the
  streamlit/altair/aliyun/s3fs subtree). `baseline/docker-compose.yml:85` pins `milvusdb/milvus:v2.4.5`.
- `baseline/TUNING.md` — the committed-config manifesto; already documents the ~4·√N nlist rule and
  env overrides; versions of the baseline SERVERS are in compose but not stated in TUNING.md.
- `bench/results/bench_public_manifest.json` — 36 MB committed artifact (verify it is an output,
  not a fixture: `grep -rn "bench_public_manifest" --include=*.py --include=*.sh .`).
- GX10/spark ops (only if live reruns are attempted): baseline stack `tridb-baseline-*` still up
  with the 1M corpus; scp artifacts BACK before any rsync toward spark.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Python | `make test && make lint` | green |
| Lock regen | edit floors → `. .venv/bin/activate && pip install <changed pins> && make lock` | lock updated, `make test` still green |
| SM-2 tiny e2e (if local stack) | `make baseline-up && bash scripts/bench_sm2.sh tridb/msvbase:dev` (PGPORT=5433 quirk on this box) | `#SM2 DONE`, report emitted |
| Dataset pin | `python -m tools.fetch_dataset --dataset gist-960-euclidean --pin` (network) | digest printed |

## Scope

**In scope:** `bench/sm2_compare.py` (+ its tests) for p50/p95/p99 + QPS derivation;
`baseline/sm2.py` (+`tools/bench_sm2_corpus.py`) multi-client mode + `BASELINE_INDEX=HNSW` option;
`baseline/TUNING.md` + `docs/benchmark_sm2_1m_v0.2.0.md`-successor notes (pinned versions section);
`tools/fetch_dataset.py` (https + gist pin); `requirements.txt`/`requirements.lock` (+ a
`requirements-vdbb.txt` optional extra); `.gitignore`/removal for regenerable `bench/results`
artifacts; README row.

**Out of scope:** rerunning the 1M headline (025 owns the next headline run — this plan makes the
HARNESS credible); changing any committed benchmark NUMBERS or their docs beyond adding
version-pin/percentile notes; LDBC integration (deferred, see maintenance notes).

## Git workflow
Branch `advisor/030-bench-credibility`; `feat(bench):`/`build(python):` commits; do NOT push.

## Steps

### Step 1: Percentiles + QPS in the compare layer
Extend `bench/sm2_compare.py`: per query emit p50/p95/p99 from the retained samples (`statistics.quantiles`
n=100 or manual; with runs=7 note in the output that p95/p99 need `--runs >= 20` to be meaningful —
emit them as `null` below a sample-size threshold rather than fabricating); aggregate section gains
`p95_ratio`, `p99_ratio`, and a `qps_singleclient` derivation. Keep the JSON backward-compatible
(only additive keys). Update its unit tests (`tests/` has sm2 parser tests — extend the same file).
**Verify**: `make test` green; running the compare on `bench/results/sm2_1m_metrics.json` inputs (the
committed 1M artifacts include full samples) prints the new fields.

### Step 2: Multi-client measurement mode
Add `--clients N` to `baseline/sm2.py` (thread pool, each with its own connections, queries
round-robined, per-query wall clock recorded as today) and a `SM2_CLIENTS=N` mode to the TriDB side:
in `tools/bench_sm2_corpus.py` add `--clients N` emitting N psql scripts (query subsets) that
`scripts/bench_sm2.sh` launches concurrently against the same cluster (document that TriDB-side
concurrency shares one PG instance — that's the point). Default remains 1 (all existing recipes
unchanged).
**Verify**: tiny-scale run with `--clients 4` completes both sides; per-client transcripts parse;
`make test` (new unit coverage for the round-robin split — no dropped/duplicated qids).

### Step 3: Milvus-HNSW baseline row
`BASELINE_INDEX={IVF_FLAT|HNSW}` env in `baseline/sm2.py` (HNSW: `{"index_type":"HNSW","params":{"M":16,"efConstruction":200}}`,
search `{"ef": env BASELINE_EF or 128}`). Record the chosen config verbatim into the output JSON
(`baseline_index_config` key) so reports self-document. TUNING.md gains the HNSW row + rationale +
the server/client versions table (Milvus v2.4.5, neo4j 5.20 image, postgres 16, pymilvus/neo4j/psycopg
lock versions).
**Verify**: `make test`; if a local/spark stack is available, a tiny `--no-load`-style HNSW-vs-IVF
sanity run; otherwise mark the live check deferred in the README row (config plumbing is testable
without a server via a captured-args fake, as in `tests/test_baseline_sm2_load.py` if plan 027 landed).

### Step 4: Dataset + transport pinning
`tools/fetch_dataset.py`: switch `_BASE` to `https://ann-benchmarks.com`; fetch gist with `--pin`
(network) and commit the observed digest into the REGISTRY replacing `_PENDING`. If the environment
has no network, STOP-note this single step (leave a TODO with the exact command) and continue.
**Verify**: `python -m tools.fetch_dataset --dataset gist-960-euclidean` (no flags) proceeds past the
pin check (or the STOP note is recorded).

### Step 5: Dependency hygiene
(a) Raise floors to the lock's major line (`numpy>=2.5`, `pytest>=9`, `neo4j>=6`, keep `ruff~=`);
(b) pymilvus: pin the CLIENT to the server line — floors `pymilvus>=2.4,<2.5` — then reinstall +
`make lock`; run the baseline import smoke (`python -c "import baseline.sm2"`) and the sm2 unit
tests; document in TUNING.md that client/server track together; (c) move `vectordb-bench` out of
the core install: `requirements-vdbb.txt` (+ note in `bench/vdbb_tridb.py` header + README one-liner),
regenerate the core lock WITHOUT it, verify the streamlit/aliyun/s3fs subtree left the lock;
(d) `bench/results` hygiene: if the 36MB `bench_public_manifest.json` is an output (Step's grep),
`git rm` it + gitignore `bench/results/*_raw.txt` EXCEPT files referenced by committed docs
(`grep -rn "raw.txt" docs/` first; keep those).
**Verify**: `make test && make lint` green under the regenerated lock; `grep -c streamlit requirements.lock` → 0;
`git ls-files bench/results | xargs du -ch | tail -1` shows the shrink.

## Test plan
Unit tests per step (compare fields, client split, config capture); tiny-scale e2e where a stack
exists; everything else is verified-by-command above.

## Done criteria
- [ ] p50/p95/p99/QPS in compare output (null-guarded for small N) + tests
- [ ] `--clients` on both sides, default-1 backward compatible + tests
- [ ] `BASELINE_INDEX=HNSW` plumbed + config self-documented in output + TUNING.md versions table
- [ ] gist digest pinned (or STOP-noted with exact command); `_BASE` https
- [ ] Floors match lock majors; pymilvus client/server aligned; vdbb split out; lock regenerated; results dir slimmed
- [ ] `make test && make lint` green; README row updated

## STOP conditions
- Lock regeneration breaks any test after two attempts (dependency solver conflict) — report the
  conflicting pins.
- `bench_public_manifest.json` turns out to be a pinned INPUT fixture — do not delete; switch to the
  fetch-flow note instead and report.
- pymilvus <2.5 lacks an API `baseline/*.py` uses — report the API list; the fallback decision
  (bump the server image instead) is the maintainer's.

## Maintenance notes
After 025's v1 rerun, regenerate the headline docs WITH the new percentile fields and the versions
table — that combination is the publishable format. LDBC SNB exposure and the public "TriBench"
packaging are deliberate follow-ups (see docs/landscape_review_v0.1.0.md §2.3), sequenced after the
v1-measured headline exists.
