"""Verify (and optionally rebuild) a wiki-corpus manifest against the ACTUAL files.

The Phase-0 extractor (tools/wiki_extract.py) writes a manifest.json whose per-file
`rows` counts and totals are its OWN bookkeeping — never cross-checked against the
files on disk. On the full enwiki run that bookkeeping diverged: the sharded writer
reopened 3 files (articles-00028/00049/00071) in truncate mode and clobbered them, so
the manifest claims 7,189,653 articles while the files hold 6,900,039 (289,612 lost).
The extractor's own "reconciliation" only checked manifest-internal consistency
(sum(shards.rows) == counts), so it passed while being wrong.

This tool is that missing check. `--verify` (default) counts real rows in every shard
file and reports mismatches (nonzero exit on any). `--rebuild` writes a truthful
manifest (real per-file counts, de-duplicated file lists, real totals), backing up the
original and recording the extractor's original claims under `extractor_claimed` for
provenance. Line-oriented for jsonl (articles) and tsv (edges/categories/redirects).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# manifest shard kind -> filename glob (all one-record-per-line)
KIND_GLOB = {
    "articles": "articles-*.jsonl",
    "edges": "edges-*.tsv",
    "categories": "categories-*.tsv",
}


def count_lines(path: Path) -> int:
    with path.open("rb") as fh:
        return sum(buf.count(b"\n") for buf in iter(lambda: fh.read(1 << 20), b""))


def actual_files(corpus: Path, kind: str) -> list[tuple[str, int]]:
    """[(name, real_line_count), ...] for a shard kind, sorted, de-duplicated."""
    return [(p.name, count_lines(p)) for p in sorted(corpus.glob(KIND_GLOB[kind]))]


def scan(corpus: Path) -> dict[str, list[tuple[str, int]]]:
    return {kind: actual_files(corpus, kind) for kind in KIND_GLOB}


def verify(corpus: Path, manifest: dict) -> tuple[bool, list[str]]:
    ok = True
    report: list[str] = []
    real = scan(corpus)
    for kind, files in real.items():
        real_total = sum(n for _, n in files)
        m_files = manifest.get("shards", {}).get(kind, {}).get("files", [])
        m_total = sum(int(f["rows"]) for f in m_files)
        m_count = int(manifest.get("counts", {}).get(kind, -1))
        tag = "OK" if real_total == m_total == m_count else "MISMATCH"
        if tag == "MISMATCH":
            ok = False
        report.append(
            f"{kind:11s} real={real_total:>12,}  manifest_sum={m_total:>12,}  "
            f"counts={m_count:>12,}  [{tag}]  ({len(files)} files)"
        )
        # per-file clobber detail: manifest claims for a path vs its real lines
        claimed: dict[str, int] = {}
        for f in m_files:
            claimed[f["path"]] = claimed.get(f["path"], 0) + int(f["rows"])
        for name, n in files:
            c = claimed.get(name)
            if c is not None and c != n:
                report.append(f"    {name}: real={n:,} manifest_claimed={c:,} (CLOBBERED)")
    return ok, report


def rebuild(corpus: Path, manifest: dict, shard_size: int = 100000) -> dict:
    real = scan(corpus)
    out = dict(manifest)
    out.setdefault("extractor_claimed", {
        "counts": dict(manifest.get("counts", {})),
    })
    shards = dict(manifest.get("shards", {}))
    counts = dict(manifest.get("counts", {}))
    for kind, files in real.items():
        total = sum(n for _, n in files)
        schema = shards.get(kind, {}).get("schema", "")
        shards[kind] = {
            "schema": schema,
            "files": [{"path": name, "rows": n} for name, n in files],
        }
        counts[kind] = total
    out["shards"] = shards
    out["counts"] = counts
    out["rebuilt_from_files"] = True
    out["rebuilt_note"] = (
        "per-file rows + counts recomputed from actual shard files by "
        "tools/wiki_manifest_verify.py --rebuild; original claims under extractor_claimed"
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", required=True, help="corpus dir containing manifest.json")
    ap.add_argument("--rebuild", action="store_true", help="write a corrected manifest")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    mpath = corpus / "manifest.json"
    manifest = json.loads(mpath.read_text())

    ok, report = verify(corpus, manifest)
    print(f"=== manifest verify: {corpus} ===")
    for line in report:
        print(line)

    if not args.rebuild:
        print("RESULT:", "OK" if ok else "MISMATCH (run --rebuild to correct)")
        return 0 if ok else 1

    backup = corpus / "manifest.json.pre_rebuild"
    if not backup.exists():
        backup.write_text(mpath.read_text())
    corrected = rebuild(corpus, manifest)
    mpath.write_text(json.dumps(corrected, indent=2, ensure_ascii=False))
    print(f"REBUILT {mpath} (backup: {backup.name})")
    print("new counts:", corrected["counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
