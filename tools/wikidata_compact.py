"""One parallel scan of a Wikidata dump -> a compact per-entity sidecar (fast path).

Plan 060 / ADR-0018 — the scale companion to tools/wikidata_ingest.py. The pure ingest
re-parses the full `latest-all.json.gz` (~1.5 TB raw, ~110M lines) once per BFS hop plus
twice more (present + emit); at Python json speeds that is days. This tool amortizes ALL
of that json work into ONE parallel scan, producing a sidecar ~100x smaller that
`wikidata_ingest --compact` then consumes parse-free (see closure_ids_compact /
present_ids_compact / prefix_ids_compact there).

FORMAT — `<out>/compact.tsv.gz`, one line per Q-id entity IN DUMP ORDER:

    qid<TAB>usable<TAB>p:dst,p:dst,...

  qid    = the integer Q-number (entities whose id is not a Q-id — properties,
           lexemes — are OMITTED: every compact consumer keys on qid_to_int, which
           skips them anyway).
  usable = 1 iff parse_entity(obj, lang) is not None (the vector-row test).
  edges  = exactly entity_edges(obj["claims"]) — truthy, property-sorted, de-duped —
           empty third field when the entity has no entity-valued truthy statements.

The rank/usability logic is IMPORTED from wikidata_ingest, never reimplemented, so the
two paths cannot drift. `usable` is lang-dependent: the sidecar records `lang` in
`compact.meta.json` and ingest refuses a mismatched sidecar. The pair is published
ATOMICALLY (same-directory temp files, fsync, os.replace data-then-meta) and the
metadata carries the compact file's byte count + SHA-256; ingest verifies both before
consuming a single row, so a crashed or tampered publication can never be consumed.

The gzip output is MULTI-MEMBER (each worker batch is compressed independently with
mtime=0); that is a valid gzip stream for gzip.open and pigz alike, keeps the writer
pure-IO, and makes worker output deterministic.

PARALLELISM. A reader (pigz -dc subprocess when available, else gzip/bz2/plain) feeds
~8 MB line batches to a multiprocessing Pool; workers do the json parsing (orjson when
importable, stdlib json fallback) and return compressed compact blobs IN ORDER
(Pool.imap). Memory ceiling: one line batch per worker in flight plus the in-order
result queue — tens of MB, independent of dump size.

CLI:
    python -m tools.wikidata_compact --dump latest-all.json.gz --out <dir> [--lang en]
    python -m tools.wikidata_compact --dump <dump> --out <dir> --limit 100000   # smoke
"""

from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from functools import partial
from multiprocessing import Pool
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from tools.wikidata_ingest import (
    DEFAULT_LANG,
    entity_edges,
    parse_entity,
    qid_to_int,
)

COMPACT_VERSION = "0.2.0"  # 0.2.0: atomic publication + compact_size/compact_sha256
COMPACT_NAME = "compact.tsv.gz"
META_NAME = "compact.meta.json"
DEFAULT_BATCH_BYTES = 8 * 1024 * 1024
PROGRESS_EVERY = 1_000_000

try:  # optional fast parser; wheels exist for aarch64/x86_64. Tests never require it.
    from orjson import loads as _loads
except ImportError:  # pragma: no cover - exercised implicitly where orjson is absent
    _loads = json.loads


def compact_batch(lang: str, lines: list[bytes]) -> tuple[bytes, int, int, int]:
    """Turn one batch of raw dump lines into a gzip-member of compact lines.

    Pure: output depends only on (lang, lines). Line handling mirrors
    wikidata_ingest.iter_entities exactly (decode, strip, skip brackets/blank, drop
    trailing comma, skip non-json / non-dict), then keeps only Q-id entities.
    Returns (gzip_blob, entities, usable, edges) for the batch; blob is b"" when the
    batch yields no entities.
    """
    out: list[str] = []
    entities = usable = edges_total = 0
    for raw in lines:
        line = raw.decode("utf-8").strip()
        if not line or line in ("[", "]"):
            continue
        if line.endswith(","):
            line = line[:-1]
        try:
            obj = _loads(line)
        except ValueError:  # covers json.JSONDecodeError and orjson.JSONDecodeError
            continue
        if not isinstance(obj, dict):
            continue
        qid = qid_to_int(obj.get("id", ""))
        if qid is None:
            continue
        u = 1 if parse_entity(obj, lang) is not None else 0
        edges = entity_edges(obj.get("claims") or {})
        out.append(f"{qid}\t{u}\t{','.join(f'{p}:{d}' for p, d in edges)}\n")
        entities += 1
        usable += u
        edges_total += len(edges)
    if not out:
        return b"", 0, 0, 0
    blob = gzip.compress("".join(out).encode("utf-8"), compresslevel=1, mtime=0)
    return blob, entities, usable, edges_total


def _trim_blob(blob: bytes, keep: int) -> tuple[bytes, int, int, int]:
    """Trim a compact gzip-member to its first `keep` entity lines (--limit crossing)."""
    lines = gzip.decompress(blob).splitlines(keepends=True)[:keep]
    usable = edges = 0
    for ln in lines:
        parts = ln.rstrip(b"\n").split(b"\t")
        usable += parts[1] == b"1"
        edges += (parts[2].count(b",") + 1) if parts[2] else 0
    if not lines:
        return b"", 0, 0, 0
    blob = gzip.compress(b"".join(lines), compresslevel=1, mtime=0)
    return blob, len(lines), usable, edges


def _open_dump_stream(
    path: Path,
) -> tuple[BinaryIO, Callable[[], tuple[int, int]], Callable[[bool], None]]:
    """Binary line stream over the dump, preferring a `pigz -dc` subprocess for .gz.

    Returns (fh, progress, cleanup): progress() -> (compressed_bytes_done, total) for
    rate/ETA reporting; cleanup(check) tears the stream down, and with check=True
    raises if the decompressor failed (a truncated/corrupt dump must not pass silently).
    The pigz child reads a dup of our unbuffered fd, so the shared file offset IS the
    progress counter — no feeder thread.
    """
    total = path.stat().st_size
    if path.suffix == ".gz" and shutil.which("pigz"):
        raw = open(path, "rb", buffering=0)
        proc = subprocess.Popen(
            ["pigz", "-dc"], stdin=raw, stdout=subprocess.PIPE, bufsize=-1
        )

        def progress() -> tuple[int, int]:
            return os.lseek(raw.fileno(), 0, os.SEEK_CUR), total

        def cleanup(check: bool) -> None:
            proc.stdout.close()  # EPIPEs the child if we stopped early (--limit)
            rc = proc.wait()
            raw.close()
            if check and rc != 0:
                raise RuntimeError(f"pigz -dc {path} exited {rc}")

        return proc.stdout, progress, cleanup

    raw = open(path, "rb")
    fh: BinaryIO
    if path.suffix == ".gz":
        fh = gzip.GzipFile(fileobj=raw)
    elif path.suffix == ".bz2":
        fh = bz2.BZ2File(raw)
    else:
        fh = raw

    def progress() -> tuple[int, int]:
        return raw.tell(), total

    def cleanup(check: bool) -> None:
        if fh is not raw:
            fh.close()
        raw.close()

    return fh, progress, cleanup


def _batches(fh: BinaryIO, hint: int) -> Iterator[list[bytes]]:
    """Yield line batches of ~`hint` bytes (C-level readlines batching)."""
    while True:
        lines = fh.readlines(hint)
        if not lines:
            return
        yield lines


def _replace(tmp: Path, final: Path) -> None:
    """Publication seam: atomic same-directory rename (monkeypatchable in tests)."""
    os.replace(tmp, final)


def _fsync_dir(path: Path) -> None:
    """fsync a directory so the just-published renames survive a crash (best effort)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - some filesystems refuse directory fsync
        pass
    finally:
        os.close(fd)


def build_compact(
    dump: Path,
    out: Path,
    *,
    lang: str = DEFAULT_LANG,
    workers: int | None = None,
    limit: int | None = None,
    batch_bytes: int = DEFAULT_BATCH_BYTES,
    progress_every: int = PROGRESS_EVERY,
) -> dict:
    """Run the parallel scan; write compact.tsv.gz + compact.meta.json. Returns meta.

    ATOMIC PUBLICATION (plan 078). Data and metadata are streamed to unique temp
    files in `out` (same filesystem), fsynced, then published data-first via
    os.replace. Metadata records the compact file's exact compressed byte count and
    SHA-256 (updated incrementally as members are copied), so a crash at ANY point —
    including between the two replaces — leaves either the wholly-old pair, the
    wholly-new pair, or a mismatched pair that wikidata_ingest rejects fail-closed.
    A pre-existing final pair is never opened for in-place writing.
    """
    out.mkdir(parents=True, exist_ok=True)
    cpath = out / COMPACT_NAME
    workers = workers or os.cpu_count() or 1
    t0 = time.monotonic()
    entities = usable = edges = 0
    next_report = progress_every
    stopped_early = False
    sha = hashlib.sha256()
    nbytes = 0
    tmp_data: Path | None = None
    tmp_meta: Path | None = None
    try:
        fh, progress, cleanup = _open_dump_stream(dump)
        try:
            dfd, dname = tempfile.mkstemp(
                dir=out, prefix=COMPACT_NAME + ".", suffix=".tmp"
            )
            tmp_data = Path(dname)
            with os.fdopen(dfd, "wb") as cf, Pool(workers) as pool:
                work = pool.imap(
                    partial(compact_batch, lang), _batches(fh, batch_bytes)
                )
                for blob, n, u, e in work:
                    if limit is not None and entities + n >= limit:
                        blob, n, u, e = _trim_blob(blob, limit - entities)
                        stopped_early = True
                    if blob:
                        cf.write(blob)
                        sha.update(blob)
                        nbytes += len(blob)
                    entities += n
                    usable += u
                    edges += e
                    if stopped_early:
                        break
                    while entities >= next_report:
                        done, total = progress()
                        elapsed = time.monotonic() - t0
                        rate = entities / elapsed if elapsed else 0.0
                        eta = elapsed * (total - done) / done if done else float("inf")
                        print(
                            f"[wikidata_compact] {entities:,} entities  "
                            f"{rate:,.0f}/s  {100.0 * done / total:.1f}% of dump  "
                            f"ETA {eta / 60:.0f} min",
                            flush=True,
                        )
                        next_report += progress_every
                cf.flush()
                os.fsync(cf.fileno())
        finally:
            cleanup(not stopped_early)
        st = dump.stat()
        meta = {
            "tool": "tools/wikidata_compact.py",
            "version": COMPACT_VERSION,
            "created": datetime.now(timezone.utc).isoformat(),
            "dump": dump.name,
            "dump_path": str(dump),
            "dump_size": st.st_size,
            "dump_mtime": st.st_mtime,
            "lang": lang,
            "limit": limit,
            "workers": workers,
            "entities": entities,
            "usable": usable,
            "edges": edges,
            "compact_size": nbytes,
            "compact_sha256": sha.hexdigest(),
            "elapsed_s": round(time.monotonic() - t0, 3),
        }
        mfd, mname = tempfile.mkstemp(dir=out, prefix=META_NAME + ".", suffix=".tmp")
        tmp_meta = Path(mname)
        with os.fdopen(mfd, "w", encoding="utf-8") as mf:
            json.dump(meta, mf, ensure_ascii=False, indent=2)
            mf.flush()
            os.fsync(mf.fileno())
        # data first, then the metadata that names it; old-meta + new-data is
        # detectable (size/sha mismatch), new-meta + old-data can never occur
        _replace(tmp_data, cpath)
        _replace(tmp_meta, out / META_NAME)
        _fsync_dir(out)
        return meta
    finally:
        # clean only THIS run's temp paths; a successful replace already consumed
        # them (missing_ok) and pre-existing finals are never touched on failure
        for tmp in (tmp_data, tmp_meta):
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="One parallel dump scan -> compact.tsv.gz sidecar for "
        "wikidata_ingest --compact."
    )
    ap.add_argument(
        "--dump", type=Path, required=True, help="path to latest-all.json[.gz|.bz2]"
    )
    ap.add_argument(
        "--out", type=Path, required=True, help="output directory for the sidecar"
    )
    ap.add_argument(
        "--lang",
        type=str,
        default=DEFAULT_LANG,
        help="label/description language for the usable flag (must match ingest --lang)",
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="smoke run: stop after N entities"
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=None,
        help="parse worker processes (default: all cores)",
    )
    args = ap.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        ap.error("--limit must be positive")
    if args.workers is not None and args.workers <= 0:
        ap.error("--workers must be positive")
    meta = build_compact(
        args.dump,
        args.out,
        lang=args.lang,
        workers=args.workers,
        limit=args.limit,
    )
    print(
        f"[wikidata_compact] {meta['entities']} entities ({meta['usable']} usable), "
        f"{meta['edges']} edges in {meta['elapsed_s']}s -> {args.out}/{COMPACT_NAME}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
