# Plan 021: Close the residual upstream Dockerfile/image supply-chain gaps (git TLS off, floating base image, unconditional SPTAG fetch, superuser entrypoint)

> **Executor instructions**: Follow step by step; run every verification command. Stop and report
> on any "STOP condition". Update `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `cd vendor/MSVBASE && git show HEAD:Dockerfile | grep -n "sslverify\|^FROM "` — confirm `RUN git config --global http.sslverify false` and `FROM gcc:12.3.0` still exist at the pin. If gone, STOP.

## Status

- **Priority**: P2
- **Effort**: S (per item; M if apt pinning is included — it is NOT, see scope)
- **Risk**: LOW
- **Depends on**: coordinate with plan 015 if both touch `msvbase_patches.sh` hardening block
- **Category**: security / dependencies
- **Planned at**: commit `408e852`, 2026-07-01
- **Upstream**: microsoft/MSVBASE `Dockerfile`, `scripts/pg_scripts/docker-entrypoint.sh`, submodule fetch in TriDB build scripts

## Why this matters

TriDB's plan-007 hardening restored TLS + checksums on the Boost/CMake tarball downloads (verified
complete), but four residual supply-chain/reproducibility gaps in the inherited build survive it:

1. **`RUN git config --global http.sslverify false`** (`vendor/MSVBASE/Dockerfile:66`) disables
   TLS cert verification for ALL git traffic, baked into the image and persisting at runtime.
   TriDB's `harden_dockerfile_downloads` only strips `--no-check-certificate` from the two `wget`
   lines — a grep for `sslverify` in `scripts/lib/msvbase_patches.sh` returns 0. Any
   clone/fetch/submodule op accepts a forged cert → the MITM path the tarball hardening closed,
   reopened for git. (UP-BUILD-01)
2. **`FROM gcc:12.3.0`** (`Dockerfile:2`) is a floating tag, not digest-pinned — two builds months
   apart can pull different base layers, defeating the reproducibility the `PIN_COMMIT` + checksums
   aim for. (UP-BUILD-02)
3. **SPTAG is still fetched unconditionally**: `scripts/x86build.sh` and `scripts/gx10build.sh` run
   `git submodule update --init --recursive`, pulling SPTAG (+ its Git-LFS objects) even though
   `WITH_SPTAG` defaults OFF (DEV-1228) and `verify_patches` treats it optional. Upstream issue #18
   ("submodule update blocked" on the SPTAG commit) can still block a fresh clone build; upstream's
   own CI sets `GIT_LFS_SKIP_SMUDGE=1`, TriDB's scripts don't. (UP-BUILD-05)
4. **The image entrypoint provisions a SUPERUSER open to `0.0.0.0/0`**
   (`scripts/pg_scripts/docker-entrypoint.sh:27,63,97`), shipped in the TriDB-built image. TriDB
   doesn't use upstream's `dockerrun.sh` (which supplies a weak default password), but any published
   TriDB image inherits the superuser-on-all-interfaces posture. (UP-BUILD-04)

## Current state

- TriDB's Dockerfile mutations live in `scripts/lib/msvbase_patches.sh`:
  `patch_upstream_dockerfile()` (~line 389) and `harden_dockerfile_downloads()` (~line 416) — `sed`
  edits guarded by `grep -q`. The pristine upstream Dockerfile is read via
  `git show HEAD:Dockerfile`; the on-disk one is already TriDB-mutated.
- The Boost/CMake hardening (the completed part) is the pattern to mirror: `grep -q '<exact string>'`
  then `sed -i` (see plan 015 which adds post-conditions to these).
- Submodule fetch: `scripts/x86build.sh` (~line 69,93) and `scripts/gx10build.sh` (~line 78) call
  `git submodule update --init --recursive`.
- SPTAG-optional facts: `verify_patches` only checks the spann sentinel when the SPTAG tree exists
  (`msvbase_patches.sh:49-52`); `WITH_SPTAG` default OFF (`sptag_optional_build.patch`).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Patch layer syntax | `bash -n scripts/lib/msvbase_patches.sh scripts/x86build.sh scripts/gx10build.sh` | exit 0 |
| Patch chain still applies | `bash scripts/ci_check_patches.sh` | exit 0 |
| Confirm sslverify removed post-mutation | (inside ci flow) `grep -c "sslverify false" <mutated Dockerfile>` | 0 |
| Python layer | `make test && make lint` | exit 0 |
| Engine build (gated) | `scripts/x86build.sh --docker` | image builds |

## Scope

**In scope**:
- `scripts/lib/msvbase_patches.sh` (`patch_upstream_dockerfile` / `harden_dockerfile_downloads`:
  add sslverify removal + base-image digest pin)
- `scripts/x86build.sh`, `scripts/gx10build.sh` (scoped submodule init + `GIT_LFS_SKIP_SMUDGE=1`)
- `docs/BUILD_NOTES.md` or `SECURITY.md` (one note: published images must not ship the
  SUPERUSER/`0.0.0.0/0` default entrypoint; scope `pg_hba` / rotate `PGPASSWORD` before publishing)
- `advisor-plans/README.md` (status row)

**Out of scope**:
- `apt-get` version pinning (UP-BUILD-03) — MED effort, reproducibility-only (apt verifies its own
  repo signatures); record as accepted gap, don't do it here.
- Rewriting the upstream entrypoint C/shell — the note + a publish-time checklist is the fix; do not
  re-architect the entrypoint.
- The sed post-conditions themselves (plan 015 adds those).

## Git workflow

- Branch: `advisor/021-upstream-build-hardening` from `origin/master`
- Commits per item, e.g. `security(build): disable git sslverify=false in image (advisor plan 021)`
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Remove the global git TLS-off setting

In `harden_dockerfile_downloads()` (or `patch_upstream_dockerfile`), add a `grep -q`-guarded `sed`
that deletes the `RUN git config --global http.sslverify false` line (or rewrites `false`→`true`).
Mirror the existing Boost/CMake `grep`+`sed` idiom.

**Verify**: run the mutation over a copy of `git show HEAD:Dockerfile`; `grep -c "sslverify false"`
→ 0; `bash scripts/ci_check_patches.sh` → exit 0.

### Step 2: Digest-pin the base image

Resolve the current digest for `gcc:12.3.0`
(`docker buildx imagetools inspect gcc:12.3.0 --format '{{.Manifest.Digest}}'`, or
`docker inspect --format='{{index .RepoDigests 0}}' gcc:12.3.0` after a pull). Add a
`grep -q '^FROM gcc:12.3.0'`-guarded `sed` rewriting it to `FROM gcc:12.3.0@sha256:<digest>`. Record
the digest next to the checksum constants in `msvbase_patches.sh` (near `BOOST_1_81_0_SHA256`) with
a dated comment. If the digest cannot be resolved (no docker/network), STOP and report — do not
invent one.

**Verify**: mutated Dockerfile line matches `FROM gcc:12.3.0@sha256:`; `bash -n` clean.

### Step 3: Scope the submodule fetch

In `x86build.sh` and `gx10build.sh`, replace `git submodule update --init --recursive` with an
explicit list of the submodules a default (SPTAG-OFF) build needs —
`git submodule update --init thirdparty/Postgres thirdparty/hnsw` — and export
`GIT_LFS_SKIP_SMUDGE=1` before it (matching upstream `azure-pipelines.yml`). Guard behind the same
`WITH_SPTAG` logic if the scripts already branch on it; if a `-DWITH_SPTAG=ON` path exists, keep the
recursive form there.

**Verify**: `bash -n` both scripts; `grep -n "GIT_LFS_SKIP_SMUDGE\|submodule update --init thirdparty" scripts/x86build.sh scripts/gx10build.sh` → present.

### Step 4: Document the entrypoint publish-time hardening

Add to `SECURITY.md` (near the existing baseline-credentials note) a short paragraph: the inherited
MSVBASE image entrypoint (`scripts/pg_scripts/docker-entrypoint.sh`) creates a SUPERUSER listening
on `0.0.0.0/0`; **any TriDB image published beyond a local dev box must** override the entrypoint to
scope `pg_hba` to the container network, drop SUPERUSER for the app role, and set a rotated
non-default `PGPASSWORD`. This is a checklist item, not a code change here.

**Verify**: `grep -n "0.0.0.0/0\|entrypoint" SECURITY.md` → the new note.

## Test plan

- Steps 1-3 verify via `bash -n`, the mutation greps, and `ci_check_patches.sh`.
- A full `scripts/x86build.sh --docker` (gated) confirms the digest-pinned base + scoped submodules
  still build; if unavailable, mark "engine-gated: unbuilt here".
- `make test && make lint` unchanged.

## Done criteria

- [ ] `sslverify false` removed by the hardening (grep on the mutated Dockerfile → 0)
- [ ] Base image digest-pinned; digest recorded with a dated comment
- [ ] Submodule init scoped to Postgres+hnsw with `GIT_LFS_SKIP_SMUDGE=1` (both build scripts)
- [ ] SECURITY.md entrypoint publish-time note added
- [ ] `bash -n` all touched scripts; `bash scripts/ci_check_patches.sh` exits 0
- [ ] `make test && make lint` exit 0; `git status` clean outside scope
- [ ] `advisor-plans/README.md` row updated

## STOP conditions

- The `gcc:12.3.0` digest can't be resolved (no docker/network) — do Steps 1,3,4; report Step 2
  blocked.
- Scoping the submodule init breaks a build that actually needed a recursive submodule you didn't
  list (report the missing path from the build error; add it).
- Plan 015 already restructured `harden_dockerfile_downloads` — rebase onto its version, don't
  duplicate the block.

## Maintenance notes

- The base-image digest must be refreshed intentionally (like the checksum constants) — note that
  next to it.
- Reviewer: confirm the scoped submodule init still yields a green `make graph-test` (the AM tests
  need Postgres+hnsw, not SPTAG).
- Accepted-and-deferred: apt version pinning (UP-BUILD-03) — reproducibility-only, recorded in the
  index.
