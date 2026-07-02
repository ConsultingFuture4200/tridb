# Plan 015: Make the Python toolchain and build-mutation layer deterministic (lockfile, pinned ruff + config, venv-consistent Makefile, sed post-conditions, .env.example)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `advisor-plans/README.md` — unless a reviewer dispatched you and told you
> they maintain the index.
>
> **Drift check (run first)**: `git diff --stat 408e852..HEAD -- requirements.txt Makefile .gitignore scripts/lib/msvbase_patches.sh .github/workflows/ci.yml`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (coordinate with plan 011 if both touch `ci.yml` — 011 adds permissions/SHA pins; this plan only changes the pip-install line)
- **Category**: dx / deps
- **Planned at**: commit `408e852`, 2026-07-01

## Why this matters

The C fork is pinned to a 40-char commit with sentinel-verified patches; the Python side has no
equivalent discipline: every dependency is an unbounded `>=` range with no lockfile, ruff spans
0.6→0.15 with **zero config file** (no `pyproject.toml` exists), and `make lint` calls bare
`ruff` (fails without an activated venv) while `make test` auto-detects `.venv`. A fresh CI run
and a dev box can silently resolve different versions of everything — a breaking dep release
turns green history red with no code change, and a ruff format revision breaks `format --check`
on untouched files. Separately, the Dockerfile-hardening `sed`s in the patch-application layer
have no fail-loud post-condition: if upstream drifts, the plan-007 download integrity checks
silently vanish from the image while the build stays green. Finally, required env vars are
documented nowhere and `.gitignore`'s `.env.*` glob would silently swallow a future
`.env.example`.

## Current state

- `requirements.txt` (14 lines, all floors): `numpy>=1.26`, `pytest>=8.0`, `ruff>=0.6`,
  `neo4j>=5.20`, `pymilvus>=2.4`, `psycopg[binary]>=3.1`, `fastembed>=0.8`, `hnswlib>=0.8`,
  `h5py>=3.10`. No lockfile of any kind in the repo; no `pyproject.toml` / `ruff.toml` /
  `setup.cfg` (verified absent).
- `Makefile:6` `PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)`;
  `test:` uses `$(PY) -m pytest tests/ -q`; `lint:` is:
  ```make
  lint:
  	ruff check . && ruff format --check .
  ```
- `.github/workflows/ci.yml:18-20` — `pip install -r requirements.txt` then `pytest tests/ -q`
  then `ruff check . && ruff format --check .` (system env, resolves fresh every run).
- `scripts/lib/msvbase_patches.sh` — two mutation mechanisms:
  - `.patch` files applied with fail-loud `|| die` + end-state sentinels in `verify_patches()`
    (e.g. `grep -q 'TRIDB: real scalar L2 distance' "$root/src/operator.cpp" || die ...`).
  - `sed -i` edits in `patch_upstream_dockerfile()` / `harden_dockerfile_downloads()` (verified
    excerpt):
    ```bash
    if grep -q 'boost_1_81_0.tar.gz" -q -O -' "$df"; then
      log "hardening Boost download (sha256sum -c before extract)"
      sed -i 's#boost_1_81_0.tar.gz" -q -O - \\#boost_1_81_0.tar.gz" -q -O boost.tgz \&\& \\#' "$df"
      ...
    ```
    The `grep -q` guards make them idempotent but also mean **upstream drift = silent no-op**:
    no post-condition asserts `sha256sum -c` landed. Contrast the existing post-condition pattern
    at the end of `patch_cmake_arm_isa_flags()`:
    ```bash
    if grep -qE 'msse4\.2|maes|mavx2|mmwaitx' "$top" "$tp" 2>/dev/null; then
      die "patch_cmake_arm_isa_flags: x86 ISA flag still present after patch (upstream drift?) — inspect ..."
    fi
    ```
- `.gitignore:24-25`:
  ```
  .env
  .env.*
  ```
  (`.env.*` matches `.env.example`). Env vars actually consumed (verified sample):
  `PGPORT/PGHOST/PGUSER/PGPASSWORD/PGDATABASE` (`baseline/harness.py`), `BASELINE_PG_PORT`
  (`baseline/docker-compose.yml`), `ANTHROPIC_API_KEY` (`bench/graphrag_report.py`,
  `scripts/bench_graphrag.sh`), `NEO4J_AUTH` (compose). Known quirk: `make sm2` needs
  `PGPORT=5433` on boxes where the baseline Postgres maps to 5433.
- Repo Python conventions: 3.12, `uv`/pip, ruff, pytest (CLAUDE.md).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `make test` | exit 0 |
| Lint | `make lint` | exit 0 (must work with NO venv activated) |
| Lock generation | `.venv/bin/python -m pip freeze > requirements.lock` (or `uv pip compile` if uv present) | file created |
| Shell syntax | `bash -n scripts/lib/msvbase_patches.sh` | exit 0 |
| Patch layer still works | `bash scripts/ci_check_patches.sh` | exit 0 |

## Scope

**In scope**:
- `requirements.txt` (comment pointing at the lock; keep floors)
- `requirements.lock` (create — pinned versions)
- `pyproject.toml` (create — `[tool.ruff]` config only; no packaging metadata needed)
- `Makefile` (`lint:` target)
- `.github/workflows/ci.yml` (install from lock)
- `scripts/lib/msvbase_patches.sh` (post-condition asserts)
- `.gitignore` (negation) + `.env.example` (create)
- `README.md` (two lines: lockfile install + PGPORT note near `make sm2`)
- `advisor-plans/README.md` (status row)

**Out of scope**:
- Upgrading/downgrading any dependency — the lock pins WHATEVER the current `.venv` has (it is
  the validated state; 143 tests pass on it).
- `baseline/docker-compose.yml` image digests (recorded as a separate low-priority finding).
- Pre-commit hooks (considered; CI already gates — not worth doing now).
- CONTRIBUTING.md's pyproject sentence (plan 013 owns doc text; coordinate if both run).

## Git workflow

- Branch: `advisor/015-toolchain-determinism` from `origin/master`
- Commits: `build(python): lockfile + pinned ruff config (advisor plan 015)` etc.
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Generate and wire the lockfile

`.venv/bin/python -m pip freeze --exclude-editable > requirements.lock` (strip any local-path
lines if present). Add a header comment to `requirements.txt`: `# Floors only. Reproducible
installs: pip install -r requirements.lock (regenerate with make lock)`. Add a `lock:` Makefile
target that regenerates it. Change `ci.yml:18` to `pip install -r requirements.lock`.

**Verify**: `wc -l requirements.lock` → >9 lines; `grep -n "requirements.lock" .github/workflows/ci.yml` → the install line.

### Step 2: Pin ruff behavior with a pyproject

Create `pyproject.toml` containing only:
```toml
[tool.ruff]
target-version = "py312"
line-length = 88
```
`line-length = 88` is ruff's default and is exactly what this codebase already conforms to
(`.venv/bin/ruff format --check .` exits 0 with no reformats at 88 — verified 2026-07-02). Setting
it explicitly pins the value so a future ruff default change can't silently reformat. After adding
the file, confirm `.venv/bin/ruff format --check .` still exits 0 with zero diffs; if it does NOT
(i.e. the codebase has drifted off 88 since this plan was written), STOP and report the reformat
count rather than picking a new width. In `requirements.txt`, tighten `ruff>=0.6` to the installed
minor (e.g. `ruff~=0.15.18` — read the actual version from `.venv/bin/ruff --version`).

**Verify**: `make lint` → exit 0 with zero reformat output.

### Step 3: Make `make lint` venv-consistent

```make
lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check .
```

**Verify**: `env -i HOME=$HOME PATH=/usr/bin:/bin make lint` (no venv on PATH) → exit 0.

### Step 4: sed post-conditions

At the end of `harden_dockerfile_downloads()`, add the fail-loud asserts (mirror the
`patch_cmake_arm_isa_flags` post-condition style exactly, including the "upstream drift?" hint):

```bash
# post-condition (plan 015): the hardening MUST have landed — a silent sed no-op here would
# ship an image with unverified downloads (the exact hole plan 007 closed).
grep -q -- '--no-check-certificate' "$df" && \
  die "harden_dockerfile_downloads: --no-check-certificate still present (upstream drift?) — inspect $df"
if grep -q 'boost_1_81_0.tar.gz' "$df" && ! grep -q 'sha256sum -c' "$df"; then
  die "harden_dockerfile_downloads: Boost/CMake download present without sha256sum -c (upstream drift?) — inspect $df"
fi
```

**Verify**: `bash -n scripts/lib/msvbase_patches.sh` → exit 0; `bash scripts/ci_check_patches.sh`
→ exit 0 (proves the asserts pass against the pinned upstream today).

### Step 5: .env.example + gitignore negation + README notes

- `.gitignore`: after `.env.*`, add `!.env.example`.
- Create `.env.example` documenting (values empty or safe defaults, NO real values):
  `PGHOST`, `PGPORT` (comment: "baseline Postgres maps to 5433 on some boxes — make sm2 needs
  PGPORT=5433 there"), `PGUSER`, `PGPASSWORD`, `PGDATABASE`, `BASELINE_PG_PORT`,
  `ANTHROPIC_API_KEY` (comment: "optional — enables the LLM reader in make graphrag"),
  `NEO4J_AUTH`.
- README: one line in the setup section pointing at `.env.example` + `requirements.lock`; one
  line next to the `make sm2` quickstart mentioning the PGPORT quirk.

**Verify**: `git check-ignore .env.example` → exit 1 (NOT ignored); `git status` shows it as
addable.

## Test plan

- `make test && make lint` → exit 0 before and after (same pass count).
- Step 3's no-venv lint run is the regression test for the PATH bug.
- `bash scripts/ci_check_patches.sh` after Step 4 proves the post-conditions hold on the pinned
  upstream (a true drift will now fail loud — desired).

## Done criteria

- [ ] `requirements.lock` committed; CI installs from it; `make lock` regenerates it
- [ ] `pyproject.toml` with `[tool.ruff]` pins target-version + line-length; ruff pinned to a
      single minor in requirements.txt
- [ ] `make lint` passes with no venv on PATH
- [ ] `harden_dockerfile_downloads` dies loud when its seds no-op; `ci_check_patches.sh` still
      exits 0
- [ ] `.env.example` committed and not gitignored; README notes added
- [ ] `make test && make lint` exit 0; `git status` clean outside scope
- [ ] `advisor-plans/README.md` status row updated

## STOP conditions

- `ruff format --check` at line-length 88 is NOT clean (the codebase drifted since this plan was
  written — report the reformat count; do not reformat the codebase or pick another width) (mixed widths — report file
  counts per width; do not reformat the codebase).
- `pip freeze` emits editable/local-path entries you can't cleanly strip.
- `ci_check_patches.sh` fails AFTER Step 4 — the post-condition found real drift today; report
  it (that's a genuine plan-007 regression), don't loosen the assert.
- Plan 011 already modified `ci.yml` in a way that conflicts with the install-line change —
  rebase, don't duplicate.

## Maintenance notes

- Dep bumps now happen deliberately: edit `requirements.txt` floor → `make lock` → commit both.
  Reviewer should reject PRs that change `.venv` behavior without touching the lock.
- The ruff pin means ruff upgrades are explicit; expect a small reformat diff when bumping minors
  — do it in a dedicated commit.
- Deferred: compose-image digest pinning (baseline-only blast radius), pre-commit hooks
  (CI-gated already).
