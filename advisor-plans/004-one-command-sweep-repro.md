# Plan 004: One-command sweep repro — `scripts/bench_gx10_sweep.sh` + `make sweep`

> **Executor instructions**: Author the script + Makefile target + doc here; the *run* is GX10/engine-gated.
> `bash -n` and `shellcheck` the script locally; do not claim a live run on an x86 box. Follow steps in
> order; STOP and report on any STOP condition. Update this plan's row in `advisor-plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 7bf3dca..HEAD -- Makefile scripts/bench_live.sh tools/sweep_corpus.py docs/benchmark_neon_sweep_v0.1.0.md scripts/lib/msvbase_patches.sh`

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx (reproducibility)
- **Planned at**: commit `7bf3dca`, 2026-06-26

## Why this matters

`docs/gtm_opensource_v0.1.0.md` names a **one-command repro** as the launch make-or-break item. The NEON
index-quality × term_cond sweep that produced `bench/results/neon_sweep_*` was run via a hand-built,
uncommitted recipe (a manual in-image rebuild + an ad-hoc runner). The benchmark *data* is committed but
the *reproduction* is not. This codifies the proven recipe into `scripts/bench_gx10_sweep.sh` + a
`make sweep` target so the result (and the 100k/768 headline, same script with bigger args) is
reproducible from the repo. It mirrors the existing `scripts/bench_live.sh` / `make bench-live` pattern.

## Current state

- `Makefile` targets today: `test, lint, graph-test, smoke-test, test-all, seed, bench, bench-live,
  sm2, baseline-up, baseline-down, clean`. **No `sweep` target.** `bench-live` is the closest model —
  it guards on the image, generates corpus SQL with a Python tool, runs it in the image, and parses.
- `scripts/bench_live.sh` is the structural template: `docker image inspect` guard → generate SQL via
  `tools/bench_corpus.py` → `docker run --rm --entrypoint bash ... -c '<build ext, initdb, psql -f>'` →
  parse with a Python reporter → write `bench/results/`.
- `tools/sweep_corpus.py` already: emits the sweep SQL + numpy oracle manifest (`build`), and grades a
  captured transcript (`--report <raw> --manifest <json>` → JSON table). This is the Python half; only
  the engine-side runner is missing.
- The proven engine recipe (what the script must encode), run inside the `tridb/msvbase:*` image:
  1. ensure the engine has the NEON + reloptions patches — the **committed patch chain**
     (`scripts/lib/msvbase_patches.sh`) is the canonical way to get them; prefer rebuilding/clarifying
     against that over copying loose source files. On the `gx10` image the MSVBASE build tree persists at
     `/tmp/vectordb` (build dir `/tmp/vectordb/build`), enabling an incremental `make` of `vectordb.so`.
  2. build + install `graph_store_ext` (the `gx10` image ships only `vectordb`, not `graph_store`) — same
     `make PG_CONFIG=$PGC [install]` pattern `scripts/crash_recovery_test.sh` / `bench_live.sh` use.
  3. `initdb`, start PG, `psql -q -f` the generated sweep SQL, capture `#SWEEP` + `Time:` +
     `Execution Time:` lines.
  4. host-side: `python3 -m tools.sweep_corpus --report <raw> --manifest <manifest>` → write
     `bench/results/neon_sweep_metrics.json` + keep `neon_sweep_raw.txt`.
- `docs/benchmark_neon_sweep_v0.1.0.md` "Reproduce" section currently describes step 2 in prose ("on the
  GX10 rebuild vectordb.so ... run sweep.sql") with no committed script.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Syntax-check the script | `bash -n scripts/bench_gx10_sweep.sh` | exit 0 |
| Lint (if shellcheck present) | `shellcheck scripts/bench_gx10_sweep.sh` | no errors |
| Python layer unaffected | `make test && make lint` | pass / clean |
| Run (GX10/engine-gated) | `make sweep` | writes `bench/results/neon_sweep_*` |

## Scope

**In scope:** `scripts/bench_gx10_sweep.sh` (create), `Makefile` (add `sweep` target),
`docs/benchmark_neon_sweep_v0.1.0.md` (replace the prose step 2 with the script invocation).
**Out of scope:** `tools/sweep_corpus.py` (already does its half — do not change it here; if you find a bug
defer to plan 001); the public-*dataset* path (`make bench-public` + dataset choice) — that is a separate
GTM product decision, not this plan; `vendor/`.

## Steps

### Step 1: Write `scripts/bench_gx10_sweep.sh`

Model on `scripts/bench_live.sh`. Parameterize via env (`SWEEP_ENTITIES`, `SWEEP_DIM`, `SWEEP_HUBS`,
`SWEEP_FANOUT`, `SWEEP_QUERIES`, `SWEEP_K`, `SWEEP_INDEX_CONFIGS`, `SWEEP_TERMCONDS`, `SWEEP_SEED`) with
the defaults that produced the committed result (20000 / 128 / 16 / 200 / 8 / 10 / "16:200,32:400" /
"20,50,200,1000" / 42), plus a documented "headline" example (`SWEEP_ENTITIES=100000 SWEEP_DIM=768`).
The script: (a) guards on the image, (b) generates SQL+manifest with `tools/sweep_corpus.py`, (c) runs the
engine recipe above in one container, capturing the transcript, (d) grades it with
`tools/sweep_corpus.py --report` into `bench/results/`. Keep the engine side reusing the committed patch
chain where possible; if it must rebuild `vectordb.so` in-image, document why in a comment.

**Verify**: `bash -n scripts/bench_gx10_sweep.sh` → exit 0; `chmod +x`; `shellcheck` clean if available.

### Step 2: Add the `make sweep` target

Mirror `bench-live`'s image guard and structure:
```make
# LIVE index-quality x term_cond sweep on the NEON+reloptions engine (DEV-1286). GX10/engine-gated.
sweep:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || \
	  { echo "image $(IMAGE) not built — run scripts/x86build.sh --docker / gx10build.sh"; exit 1; }
	bash scripts/bench_gx10_sweep.sh $(IMAGE)
```
Add `sweep` to `.PHONY`.

**Verify**: `make -n sweep` prints the recipe; `make test && make lint` still pass/clean.

### Step 3: Update the repro doc

In `docs/benchmark_neon_sweep_v0.1.0.md`, replace the prose "step 2" with the committed command
(`make sweep` and the headline `SWEEP_ENTITIES=100000 SWEEP_DIM=768 make sweep`), so the doc's repro
matches the repo.

**Verify**: `grep -n "make sweep" docs/benchmark_neon_sweep_v0.1.0.md` → present.

## Test plan

No unit tests for shell glue. Gates: `bash -n` + `shellcheck` clean; `make test`/`make lint` unaffected;
`make -n sweep` shows the recipe. A real run is GX10/engine-gated and is the maintainer's acceptance step
(it should reproduce `bench/results/neon_sweep_metrics.json` within noise for the default args).

## Done criteria

- [ ] `scripts/bench_gx10_sweep.sh` exists, executable, `bash -n` exit 0, shellcheck clean (if available).
- [ ] `make sweep` target exists, image-guarded, in `.PHONY`; `make -n sweep` works.
- [ ] `docs/benchmark_neon_sweep_v0.1.0.md` repro references `make sweep`.
- [ ] `make test` + `make lint` still pass/clean (no Python touched).
- [ ] `advisor-plans/README.md` row updated.

## STOP conditions

- The in-image patch state can't be reconciled with `scripts/lib/msvbase_patches.sh` (i.e. the only way to
  get NEON+reloptions into the running engine is copying loose source, diverging from the canonical patch
  chain) — report; the script should prefer the patch chain so it doesn't rot.
- `graph_store_ext` won't build/install in the image with the documented `PG_CONFIG` invocation — report.
- The script would need to download a dataset or hit the network — out of scope (that's `bench-public`).

## Maintenance notes

- The 100k/768 headline run (the GTM gate, Linear DEV-1286) is this exact script with the headline env
  vars on a quiet GX10 — keep the script scale-agnostic.
- This is the sibling of a future `make bench-public` (public real dataset via `tools/real_corpus.py`);
  keep the engine-recipe portion factored so both can share it.
- Reviewer: confirm the script reuses the committed patch chain rather than a parallel ad-hoc rebuild.
