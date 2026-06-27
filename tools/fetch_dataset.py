"""Fetch a PINNED, recognized public ANN dataset for the public benchmark (GTM).

WHY THIS EXISTS
---------------
docs/gtm_opensource_v0.1.0.md names the launch make-or-break as a reproducible
benchmark on a dataset *strangers recognize*, with a one-command repro and pinned
data. tools/real_corpus.py already consumes an ann-benchmarks `.hdf5` ('train'
matrix) end to end — it just needs the file on disk. This module is the missing
"get the file on disk, verifiably" half: it downloads ONE pinned, recognized
public dataset to data/public/ and checks its SHA256 before anything trusts it.

WHY THIS DATASET (gist-960-euclidean, the default)
--------------------------------------------------
The GTM headline wants **real embeddings, dim 768+**, and the TriDB canonical
query ranks by L2 (`<->`, engine `distmethod=l2_distance`). gist-960-euclidean
is the ann-benchmarks set that fits BOTH constraints at once:

  * dim **960** (>= the 768+ the GTM headline asks for — real GIST descriptors),
  * **Euclidean / L2** distance (matches the canonical `<->` ordering and the
    HNSW index `distmethod=l2_distance`; an *angular* set like glove-100 would
    not — cosine ranking would disagree with the L2 oracle), and
  * it is a **canonical ann-benchmarks dataset** (GIST1M is a standard ANN
    benchmark corpus; ann-benchmarks ships it as a named HDF5), so "recognized"
    is defensible to a hostile reader.

A smaller, also-recognized, also-L2 alternative is pinned too: sift-128-euclidean
(dim 128). It is BELOW the 768+ headline target — use it only for a fast local
smoke of the pipeline, and say so. The 768+ headline run uses GIST.

SUPPLY-CHAIN DISCIPLINE (mirrors scripts/lib/msvbase_patches.sh)
----------------------------------------------------------------
Every build-time download in this repo is PINNED to a URL + a SHA256 constant and
verified before use (see msvbase_patches.sh BOOST_*/CMAKE_* + harden_dockerfile_
downloads). We mirror that here: each dataset has a fixed mirror URL and a SHA256
slot. The checksum is verified after download; a mismatch is a hard failure and
the partial file is removed.

A NOTE ON THE PINNED HASHES, STATED HONESTLY: the SHA256 constants below are
SENTINELS (``_PENDING``) until someone runs a real fetch ONCE and pins the
observed digest. This file was authored on a no-network box; fabricating a hash
would be worse than admitting it is unset. The first fetch on a networked box
must run with ``--pin`` (prints the digest to paste in here) or ``--allow-unpinned``
(one-time escape that warns loudly). After the constant is set, every subsequent
fetch verifies against it with no escape. This is intentionally the SAME "pin it
once, verify forever" contract msvbase_patches.sh uses for Boost/CMake.

NOT RUN HERE: this module performs NETWORK I/O and is never invoked by the test
suite or by CI. tests/test_fetch_dataset.py exercises only the offline pieces
(the registry shape + the checksum/verification helpers against an in-test file).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

# Sentinel for an unpinned checksum (see module docstring). A real pinned dataset
# replaces this with its lowercase hex SHA256 digest.
_PENDING = "PENDING_FIRST_FETCH"

# Default cache dir for downloaded datasets (gitignored via /data/ in .gitignore).
DEFAULT_CACHE = Path("data/public")


@dataclass(frozen=True)
class Dataset:
    """A pinned, recognized public ANN dataset in ann-benchmarks HDF5 format.

    `url` is the canonical ann-benchmarks public mirror. `sha256` pins the file
    (``_PENDING`` until pinned on a first real fetch — see module docstring).
    `dim` / `distance` document what the file is so a caller can assert the
    L2 / dim-768+ requirements WITHOUT opening the file.
    """

    name: str
    url: str
    sha256: str
    dim: int
    distance: str  # "euclidean" (L2) | "angular" (cosine)
    note: str


# Canonical ann-benchmarks HDF5 mirror. These URLs are the long-standing public
# distribution point for the named datasets (the same files the ann-benchmarks
# project's data loader downloads).
_BASE = "http://ann-benchmarks.com"

REGISTRY: dict[str, Dataset] = {
    # DEFAULT — dim 960 (>= 768 headline target), L2 (matches the canonical <-> /
    # engine distmethod=l2_distance). The recognized 768+ set for the headline.
    "gist-960-euclidean": Dataset(
        name="gist-960-euclidean",
        url=f"{_BASE}/gist-960-euclidean.hdf5",
        sha256=_PENDING,
        dim=960,
        distance="euclidean",
        note="GIST1M descriptors; dim 960 (>=768 headline), L2 — the headline set.",
    ),
    # Smaller L2 alternative for a fast local pipeline smoke. BELOW the 768+ target.
    "sift-128-euclidean": Dataset(
        name="sift-128-euclidean",
        url=f"{_BASE}/sift-128-euclidean.hdf5",
        sha256="dd6f0a6ed6b7ebb8934680f861a33ed01ff33991eaee4fd60914d854a0ca5984",
        dim=128,
        distance="euclidean",
        note="SIFT1M; dim 128, L2. Fast pipeline smoke only — below the 768+ headline.",
    ),
}

DEFAULT_DATASET = "gist-960-euclidean"


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """Streaming SHA256 of a file -> lowercase hex digest (memory-bounded)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def verify_checksum(path: Path, expected: str) -> None:
    """Verify `path`'s SHA256 equals `expected` (lowercase hex). Raise on mismatch.

    Mirrors `sha256sum -c` in harden_dockerfile_downloads: a tampered/corrupt
    file is a hard failure, never a silent pass. `_PENDING` is rejected here — a
    fetch path must resolve the pin policy (--pin / --allow-unpinned) BEFORE
    calling this; this function only ever compares against a real digest.
    """
    if expected == _PENDING:
        raise ValueError(
            "verify_checksum called with an unpinned (_PENDING) checksum — the "
            "fetch path must resolve --pin/--allow-unpinned before verifying"
        )
    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        raise ValueError(
            f"checksum MISMATCH for {path}\n  expected {expected}\n  actual   {actual}\n"
            "Refusing to use a file that does not match the pin (supply-chain "
            "integrity). Delete it and re-fetch, or re-pin if the upstream changed."
        )


def _download(url: str, dest: Path) -> None:
    """Stream `url` -> `dest` (download to a .part then rename, so a crashed
    download never leaves a truncated file that looks complete)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    print(f"[fetch_dataset] downloading {url}")
    # The ann-benchmarks mirror 403s the default "Python-urllib" User-Agent; send a non-default UA
    # (the SHA256 pin, not the transport, is the integrity guarantee — see verify_checksum).
    req = Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; tridb-fetch_dataset/1.0)"}
    )
    with urlopen(req) as resp, open(part, "wb") as out:  # noqa: S310 (pinned mirror + checksum-verified)
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            out.write(block)
    part.rename(dest)


def fetch(
    name: str = DEFAULT_DATASET,
    *,
    cache: Path = DEFAULT_CACHE,
    allow_unpinned: bool = False,
    pin: bool = False,
    force: bool = False,
) -> Path:
    """Fetch a pinned dataset to `cache`, verifying its SHA256. Return its path.

    NETWORK I/O — never called by the tests. Behaviour:
      * if the file already exists and verifies (and not --force): no download;
      * download to a .part then atomically rename;
      * pin policy:
          - checksum pinned (not _PENDING)  -> verify; mismatch is fatal;
          - _PENDING + --pin                -> print the observed digest to paste
                                               into REGISTRY, then succeed;
          - _PENDING + --allow-unpinned     -> warn loudly, skip verification;
          - _PENDING otherwise              -> refuse (no silent unpinned trust).
    """
    if name not in REGISTRY:
        raise KeyError(
            f"unknown dataset {name!r}; known: {', '.join(sorted(REGISTRY))}"
        )
    ds = REGISTRY[name]
    dest = cache / f"{ds.name}.hdf5"

    pinned = ds.sha256 != _PENDING

    # Resolve the pin policy BEFORE any network I/O: an unpinned dataset with no
    # escape must refuse WITHOUT downloading (no unverified bytes ever touch disk).
    if not pinned and not pin and not allow_unpinned:
        raise SystemExit(
            f"{name} has an UNPINNED checksum (_PENDING) and neither --pin nor "
            "--allow-unpinned was given. Refusing to fetch an unverifiable download. "
            "Run once with --pin to record the digest, then commit it."
        )

    # Fast path: present + pinned + verifies -> reuse.
    if dest.exists() and not force:
        if pinned:
            verify_checksum(dest, ds.sha256)
            print(f"[fetch_dataset] {dest} already present and verified")
            return dest
        print(f"[fetch_dataset] {dest} already present (checksum UNPINNED — see --pin)")
        if pin:
            print(f"[fetch_dataset] observed sha256 = {sha256_file(dest)}")
        return dest

    _download(ds.url, dest)

    if pinned:
        verify_checksum(dest, ds.sha256)
        print(f"[fetch_dataset] verified {dest} against pinned sha256")
    elif pin:
        digest = sha256_file(dest)
        print(
            f"[fetch_dataset] PIN THIS in tools/fetch_dataset.py REGISTRY[{name!r}]:\n"
            f'    sha256="{digest}",'
        )
    elif allow_unpinned:
        print(
            f"[fetch_dataset] WARNING: {name} checksum is UNPINNED and verification "
            "was SKIPPED (--allow-unpinned). Re-run with --pin and paste the digest "
            "into the REGISTRY so future fetches are verified."
        )
    print(f"[fetch_dataset] dataset ready: {dest}")
    return dest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        choices=sorted(REGISTRY),
        help=f"recognized public ANN dataset to fetch (default: {DEFAULT_DATASET})",
    )
    p.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"cache dir (default: {DEFAULT_CACHE}, gitignored)",
    )
    p.add_argument(
        "--pin",
        action="store_true",
        help="print the observed SHA256 to pin into the REGISTRY (first-fetch flow)",
    )
    p.add_argument(
        "--allow-unpinned",
        action="store_true",
        help="one-time escape: download an unpinned dataset, SKIPPING verification "
        "(warns loudly). Prefer --pin.",
    )
    p.add_argument("--force", action="store_true", help="re-download even if cached")
    p.add_argument("--list", action="store_true", help="list known datasets and exit")
    args = p.parse_args(argv)

    if args.list:
        for name in sorted(REGISTRY):
            ds = REGISTRY[name]
            pin = "pinned" if ds.sha256 != _PENDING else "UNPINNED"
            star = " (default)" if name == DEFAULT_DATASET else ""
            print(f"{name}{star}: dim={ds.dim} dist={ds.distance} [{pin}] — {ds.note}")
        return 0

    fetch(
        args.dataset,
        cache=args.cache,
        allow_unpinned=args.allow_unpinned,
        pin=args.pin,
        force=args.force,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
