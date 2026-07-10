# Plan 058: Split requirements.lock core vs optional extras

> **Executor instructions**: Python deps only; no secrets. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- requirements.txt requirements.lock Makefile .github/workflows/ci.yml pyproject.toml`

## Status
- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: dependencies / dx
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

`requirements.txt` lists floors (numpy, pytest, ruff, neo4j, pymilvus, …). `requirements.lock` freezes
extra exploratory packages from a dirty venv (`streamlit`, `vectordb-bench`, `scikit-learn`, `polars`,
AWS/OSS SDKs, …). CI installs the **whole** lock (`.github/workflows/ci.yml`). That bloats CI,
widens supply chain, and makes `make lock` re-bake one-off experiments into “reproducible” installs.

## Current state

```
# requirements.txt — floors only (header says use lock for repro)
# requirements.lock — includes streamlit==1.58.0, vectordb-bench==1.0.22, ...
# CI: pip install -r requirements.lock
```

- `bench/vdbb_tridb.py` needs vectordb-bench optionally
- `make lock` freezes current `.venv`

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Lock regen | clean venv + `make lock` | lock matches floors (+ declared extras policy) |
| CI local | `pip install -r requirements.lock && make test && make lint` | green |
| Audit optional | `rg 'streamlit|vectordb' requirements.lock` | only in extras file if split |

## Scope

**In scope:**
- `requirements.txt` (core floors)
- optional `requirements-extras.txt` or `requirements-vdbb.txt` for VectorDBBench/streamlit/etc.
- Regenerate **core** `requirements.lock` from clean venv of floors only
- CI stays on core lock
- README/CONTRIBUTING install blurb
- `make lock` docs: refuse or warn if extras imported? keep simple: document two files

**Out of scope:** pinning Docker base images (separate); running pip-audit fixes unless trivial.

## Git workflow
- Branch: `advisor/058-lockfile-split`
- Commit: `chore(deps): core lock without exploratory extras (advisor 058)`

## Steps

### Step 1: Inventory lock-only imports

```bash
rg -n "streamlit|vectordb_bench|sklearn|polars|pyarrow" --type py bench tools
```

Map each to extras file or delete dead code path.

### Step 2: Clean venv lock

```bash
python3 -m venv /tmp/tridb-lock && /tmp/tridb-lock/bin/pip install -r requirements.txt
# freeze → requirements.lock with make lock adapted to VIRTUAL_ENV
```

### Step 3: Extras file

```
# requirements-vdbb.txt
-r requirements.txt
vectordb-bench==...
```

Document: `pip install -r requirements-vdbb.txt` for VDBB only.

### Step 4: CI + docs

CI continues `pip install -r requirements.lock` (core). README: lock for repro; extras optional.

**Verify**: `make test && make lint` on core install; vdbb import fails without extras (expected) or guarded.

## Test plan
- Full pytest on core lock.
- Optional: note in `bench/vdbb_tridb.py` docstring if import missing.

## Done criteria
- [ ] Core lock has no streamlit/vdbb/AWS SDK unless a **core** module imports them
- [ ] CI uses core lock and stays green
- [ ] Extras documented
- [ ] Index DONE

## STOP conditions
- A first-party test imports an extras-only package — move test to optional marker or declare dep in core intentionally.
- Lock generation tools unavailable — use `pip freeze` from clean venv as Makefile already allows.

## Maintenance notes
- Never run `make lock` from a kitchen-sink venv again without reviewing the diff.
