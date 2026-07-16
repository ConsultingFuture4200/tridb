# Plan 086: Isolate and lock the GB10 GPU Python environment

> **Executor instructions**: Never install GPU extras into core `.venv`. Generate the platform lock
> on the DGX Spark/GB10; do not hand-author transitive pins or claim GPU verification off-target.
> Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- scripts/spark_gpu_setup.sh requirements* Makefile .gitignore docs/spark_gpu_path_findings_v0.1.0.md tools/wiki_linkpredict_fused.py tests/`

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: dx / migration / tests
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

The GPU setup mutates core `.venv` with floating `torch` and `sentence-transformers` installs. That
can contaminate the core lock, silently change benchmark dependencies between runs, and reproduce an
untested CUDA library combination. GPU workloads need their own platform-specific environment and a
lock generated from the combination already verified on GB10.

## Current state

- `scripts/spark_gpu_setup.sh:37-55` targets `.venv`; lines 57-73 pin only `cuvs-cu13` while installing
  `torch` and `sentence-transformers` without versions.
- `Makefile:38-41` explicitly requires core `.venv` to contain only `requirements.txt` dependencies
  when generating `requirements.lock`.
- `tools/wiki_linkpredict_fused.py:81-83` imports GPU-workload packages such as pandas/scipy that are
  not guaranteed by the core manifest.
- `docs/spark_gpu_path_findings_v0.1.0.md:137-147` records a verified coexistence set including
  `torch==2.12.1`, `cuvs-cu13==26.6.0`, `sentence-transformers==5.6.0`,
  `transformers==5.13.0`, `numpy==2.5.0`, and `scipy==1.18.0`. It also records that install order
  changes shared CUDA-library versions and was reverified afterward.
- `.gitignore` ignores `.venv/` but not `.venv-gpu/`.

## Target environment contract

- Dedicated venv: `${GPU_VENV:-$ROOT/.venv-gpu}`.
- `requirements-gpu-gb10.in`: direct, exact requirements needed by GPU benchmark scripts, including
  the verified top-level versions and pandas/scipy when imported.
- `requirements-gpu-gb10.lock`: resolver-generated full ARM64/Linux closure from a clean GB10 venv.
  Prefer hashes (`uv pip compile --generate-hashes` and install with hash enforcement). If the
  platform resolver cannot produce a correct hash lock, exact full-closure pins are the minimum;
  document why and retain artifact provenance.
- Core `.venv`/`requirements.lock` must be byte-for-byte unchanged by GPU setup.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Syntax | `bash -n scripts/spark_gpu_setup.sh` | exit 0 |
| Host static | `.venv/bin/pytest tests/test_spark_gpu_setup.py -q` | all pass |
| GB10 install | `scripts/spark_gpu_setup.sh` | `ALL GPU PATHS VERIFIED`, exit 0 |
| GB10 verify | `scripts/spark_gpu_setup.sh --verify` | same marker, exit 0 |
| Core | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `scripts/spark_gpu_setup.sh`
- `requirements-gpu-gb10.in` (create)
- `requirements-gpu-gb10.lock` (create on GB10)
- `Makefile`
- `.gitignore`
- `docs/spark_gpu_path_findings_v0.1.0.md` (append reproducibility addendum)
- `tests/test_spark_gpu_setup.py` (create)

**Out of scope**:
- Core `requirements.txt`, `requirements.lock`, or `.venv` contents.
- Supporting unverified CUDA/architecture combinations under the GB10 lock name.
- Solving missing aarch64 onnxruntime-gpu wheels.
- Claiming GPU success from an x86/non-CUDA machine.

## Git workflow

Use assigned `dustin/dev-NNNN`. Commit generated input/lock and setup changes together, for example
`build(gpu): isolate locked gb10 environment`.

## Steps

### Step 1: Add a static contamination regression test

Create `tests/test_spark_gpu_setup.py`. Assert the script never references `$ROOT/.venv` as its
install target, all installs consume the GPU lock rather than package names, direct input requirements
use exact pins, `.venv-gpu/` is ignored, and the core lock target remains unchanged. Check that
off-target output includes an explicit `SKIP` and never includes the success marker.

**Verify**: the test fails on current `.venv` and floating-install lines.

### Step 2: Define exact direct GPU requirements

Create `requirements-gpu-gb10.in` from the verified versions doc and imports used by the GPU tools.
Include exact `==` pins for top-level torch, cuVS, sentence-transformers, NumPy, SciPy, pandas, and
other direct imports. Preserve the verified install-order constraint in comments only where the
resolver/input ordering matters; do not add packages based solely on the old contaminated venv.

**Verify**: a static script imports each in-scope GPU tool and maps every non-stdlib direct dependency
to the input file.

### Step 3: Generate the lock from a clean GB10 environment

On the DGX Spark, create a new empty `.venv-gpu`, resolve the input using the repo's chosen resolver,
and generate the complete ARM64/Linux lock (with hashes where viable). Install only from that lock,
run `pip check`, run the full existing torch embedding + cuVS CAGRA verification, and capture
`pip freeze`/platform/Python/CUDA metadata. Compare key versions to the documented verified set.

**Verify**: clean install returns `pip check` success and `ALL GPU PATHS VERIFIED`. A second clean
venv installed from the committed lock produces the same freeze and marker. If not, STOP; do not
commit a non-reproducible lock.

### Step 4: Point setup and Make targets at the isolated venv

Default `VENV` to `.venv-gpu`, allow `GPU_VENV` override, create it when absent with the documented
Python version, install the lock, and retain `--verify` as no-install. On hosts without `nvidia-smi`,
print a clear `SKIP: GB10/CUDA unavailable` and exit without creating/modifying any venv. Add
`gpu-setup`, `gpu-verify`, and (only if deterministic) `gpu-lock` Make targets that use this path.

**Verify**: hash/checksum core `.venv` metadata and `requirements.lock` before/after setup; no changes.

### Step 5: Document regeneration and provenance

Append exact Python/platform/CUDA prerequisites, lock-generation command, install/verify commands,
install order rationale, and the rule that only a clean GB10 run can refresh this lock. Add
`.venv-gpu/` to `.gitignore`.

**Verify**: `git diff -- requirements.lock requirements.txt` is empty; host tests/lint/syntax pass.

## Test plan

Host tests cover no core `.venv` target, no floating installs, lock-only install, off-target SKIP,
and no false success marker. GB10 gate covers two clean installs, `pip check`, imports, real CUDA
torch compute, real cuVS build/search, and exact freeze comparison. Report GB10 tests as unrun unless
actually executed there.

## Done criteria

- [ ] GPU setup creates/uses only `.venv-gpu` (or explicit `GPU_VENV`).
- [ ] Every direct GPU dependency is exact-pinned and install uses a generated full closure.
- [ ] Two clean GB10 installs reproduce and print `ALL GPU PATHS VERIFIED`; `pip check` passes.
- [ ] Core `.venv`, `requirements.txt`, and `requirements.lock` are unchanged.
- [ ] Off-target execution prints SKIP and performs no install.
- [ ] Host focused/full tests, lint, shell syntax, and diff check pass.

## STOP conditions

- No access to GB10 for lock generation; commit input/script tests only after splitting/re-approving
  scope, never a guessed lock.
- Resolver output cannot reproduce the documented working CUDA coexistence set.
- A required wheel is unavailable for the pinned Python/ARM64 platform.
- Any command modifies core `.venv` or core lock.

## Maintenance notes

Refresh the GPU lock only from a clean GB10 environment and rerun both GPU paths after every change.
Keep platform identity in the filename; a future CUDA/architecture target needs its own lock.
