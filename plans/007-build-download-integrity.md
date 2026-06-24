# Plan 007: Verify the integrity of build-time downloads (Boost, CMake)

> **Executor instructions**: Follow step by step; run every verification command. On a STOP
> condition, stop and report. Update this plan's row in `plans/README.md` when done.
>
> **Drift check (run first)**: `git -C /home/bob/code/tridb diff --stat cb097db..HEAD -- scripts/x86build.sh scripts/gx10build.sh`
> If changed, re-read before editing; mismatch with excerpts = STOP.

## Status
- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none (composes with 004 if the shared lib exists)
- **Category**: security (supply chain)
- **Planned at**: commit `cb097db`, 2026-06-24

## Why this matters
The build downloads third-party source/binaries with **no checksum or signature verification**,
producing a ~9.5 GB database image. A network interception or a compromised mirror could inject
malicious code into the image with no detection. Two downloads:
1. **Boost 1.81.0** — `scripts/x86build.sh`'s `patch_upstream_dockerfile` rewrites the dead
   Boost URL to `https://archives.boost.io/...`, but the Dockerfile's `wget` keeps
   `--no-check-certificate` (TLS verification disabled) and there is no hash check.
2. **CMake** (ARM build) — `scripts/gx10build.sh:57` `curl -fsSL …/cmake-…-linux-aarch64.tar.gz`
   downloaded and added to `PATH` with no verification. CMake runs arbitrary code at configure
   time, so a tampered cmake compromises the whole build.

## Current state
- `scripts/x86build.sh:53-73` `patch_upstream_dockerfile()` — the sed that swaps the Boost URL.
  The Dockerfile line it targets uses `wget … --no-check-certificate`. (Confirm:
  `grep -n 'boost_1_81_0\|no-check-certificate' vendor/MSVBASE/Dockerfile`.)
- `scripts/gx10build.sh:55-60` `ensure_cmake()`:
  ```bash
  local ver="3.27.9" tgz="cmake-${ver}-linux-aarch64.tar.gz"
  curl -fsSL "https://github.com/Kitware/CMake/releases/download/v${ver}/${tgz}" -o "/tmp/${tgz}"
  tar -C /tmp -xzf "/tmp/${tgz}"
  export PATH="/tmp/cmake-${ver}-linux-aarch64/bin:${PATH}"
  ```
- Known-good checksums (the executor must confirm these against the official sources, do not
  trust this plan's memory):
  - Boost 1.81.0 source tarball SHA256: from https://www.boost.org/users/history/version_1_81_0.html
    (or the `.sha256`/release notes). Record the value you obtain.
  - CMake 3.27.9 linux-aarch64 SHA256: from the
    `cmake-3.27.9-SHA-256.txt` asset on the Kitware GitHub release.

## Commands you will need
| Purpose | Command | Expected |
|---|---|---|
| Find Boost wget line | `grep -n 'boost_1_81_0\|no-check-certificate' vendor/MSVBASE/Dockerfile` | the line(s) |
| Bash syntax | `bash -n scripts/x86build.sh && bash -n scripts/gx10build.sh` | exit 0 |
| Verify a checksum locally | `echo "<sha256>  <file>" \| sha256sum -c` | `<file>: OK` |

## Scope
**In scope**: `scripts/x86build.sh` (the Boost-URL patch — also remove `--no-check-certificate`
and add a checksum step), `scripts/gx10build.sh` (cmake download — add checksum), and the shared
`scripts/lib/msvbase_patches.sh` if plan 004 has landed. **Out of scope**: pinning the MSVBASE
commit (plan 002, separate supply-chain axis), the patch-application verification (plan 003).

## Git workflow
- Branch `advisor/007-download-integrity`; commit `security(build): verify Boost + CMake download checksums`.

## Steps

### Step 1: Obtain and record the official checksums
From the official sources (Boost release page, Kitware release `SHA-256.txt`), obtain the SHA256
for Boost 1.81.0 source and CMake 3.27.9 linux-aarch64. Record them as constants with a comment
citing where they came from.
**Verify**: you have two SHA256 strings, each with a source URL noted.

### Step 2: Harden the Boost download in the Dockerfile patch
Extend `patch_upstream_dockerfile()` so that, in addition to swapping the URL, it (a) removes
`--no-check-certificate` from the Boost `wget`, and (b) adds a `sha256sum -c` check on the
downloaded tarball before extraction. Because the download happens inside the Dockerfile, the
patch must inject the checksum step into the Dockerfile's Boost `RUN` block (download → verify →
extract). Keep it idempotent (grep-guarded).
**Verify**: run the patch against a *copy* of `vendor/MSVBASE/Dockerfile`; the Boost RUN block
now has no `--no-check-certificate` and includes a `sha256sum -c`. Running the patch twice does
not double-inject.

### Step 3: Harden the CMake download in gx10build.sh
In `ensure_cmake()`, after the `curl` download and before `tar`, verify the tarball:
```bash
echo "${CMAKE_AARCH64_SHA256}  /tmp/${tgz}" | sha256sum -c - \
  || die "cmake tarball checksum mismatch — refusing to use a tampered cmake"
```
Add a `trap` (or explicit cleanup) so `/tmp/${tgz}` and the extracted dir are removed on exit
(addresses the world-writable `/tmp` reuse risk).
**Verify**: `bash -n scripts/gx10build.sh`; with the wrong checksum the function would `die`
(you can dry-run the `sha256sum -c` line against a local file with a deliberately wrong hash to
see it fail).

### Step 4: Document
Add a one-line note to `docs/BUILD_NOTES.md` that build downloads are checksum-verified and
where the pinned hashes live.
**Verify**: `grep -n 'sha256\|checksum' docs/BUILD_NOTES.md` → match.

## Test plan
- `bash -n` both scripts.
- Patch idempotency: run `patch_upstream_dockerfile` twice against a Dockerfile copy; the result
  is identical and valid.
- Negative: feed `sha256sum -c` a wrong hash for a local test file → it reports FAILED and the
  guarded code path `die`s.
- Dev box (heavy, optional): a full `scripts/x86build.sh --docker` still succeeds (the real
  Boost tarball matches the recorded hash).

## Done criteria
- [ ] Boost download: `--no-check-certificate` removed and a `sha256sum -c` added before extract.
- [ ] CMake download in gx10build verified by `sha256sum -c`; `/tmp` artifacts cleaned on exit.
- [ ] Both checksums recorded with their source URLs.
- [ ] Idempotency + negative checks pass; `bash -n` passes.
- [ ] `docs/BUILD_NOTES.md` notes the verification.
- [ ] `plans/README.md` status row updated.

## STOP conditions
- The Boost `RUN` block in the Dockerfile is structured so a checksum step cannot be injected
  idempotently with sed (too complex to patch reliably). STOP and report — propose vendoring a
  small wrapper script the Dockerfile calls, instead of sed-injecting.
- The official checksum cannot be obtained from an authoritative source (only mirrors). STOP —
  do not invent a hash; report so the team can decide on a trust anchor.

## Maintenance notes
- When Boost or CMake versions change (or MSVBASE is re-pinned), update the recorded checksums.
- Reviewer: confirm the hashes came from official sources, not copied from this plan.
- Related: plan 002 (commit pin) closes the *source* supply-chain axis; this plan closes the
  *download* axis — both matter.
