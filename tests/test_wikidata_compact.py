"""Tests for the plan-060 Wikidata fast path — compact sidecar + ingest --compact.

No network, no large data, no engine. Drives tools/wikidata_compact.py over a tiny
real-dump-format fixture (array brackets, trailing commas, `{"type":...,"id":...`
key order, compact separators) and proves:

  (a) the compact sidecar is exactly (qid, parse_entity-usable, entity_edges) per
      Q-id entity in dump order;
  (b) EQUIVALENCE — ingest with and without --compact produces byte-identical shard
      files and identical manifest counts, in both prefix and BFS modes, including
      unusable entities, dangling edges, and a decoy entity whose raw line contains
      the literal bytes '"id":"Q999"' (the emit guard may only skip on a POSITIVE
      head-of-line id match, so the decoy cannot cause a wrong skip);
  (c) the parse worker is pure and batch-shaped (same input -> same bytes, brackets/
      junk/non-items skipped, counts correct).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import wikidata_compact  # noqa: E402
from tools.wikidata_compact import (  # noqa: E402
    COMPACT_NAME,
    META_NAME,
    build_compact,
    compact_batch,
)
from tools.wikidata_compact import main as compact_main  # noqa: E402
from tools.wikidata_ingest import (  # noqa: E402
    _HEAD_QID,
    _iter_kept_entities,
    CompactSidecarError,
    SliceSpec,
    closure_ids,
    closure_ids_compact,
    ingest,
    prefix_ids,
    prefix_ids_compact,
    present_ids,
    present_ids_compact,
    verify_compact_sidecar,
)
from tools.wikidata_ingest import main as ingest_main  # noqa: E402


def _stmt(prop, target=None, *, rank="normal", time=None):
    snak = {"snaktype": "value", "property": prop}
    if target is not None:
        snak["datavalue"] = {
            "type": "wikibase-entityid",
            "value": {
                "entity-type": "item",
                "numeric-id": int(target[1:]),
                "id": target,
            },
        }
    elif time is not None:
        snak["datavalue"] = {"type": "time", "value": {"time": time}}
    return {"mainsnak": snak, "type": "statement", "rank": rank}


def _item(qid, label=None, desc=None, claims=None):
    obj = {
        "type": "item",
        "id": qid,
        "labels": {},
        "descriptions": {},
        "claims": claims or {},
    }
    if label is not None:
        obj["labels"]["en"] = {"language": "en", "value": label}
    if desc is not None:
        obj["descriptions"]["en"] = {"language": "en", "value": desc}
    return obj


# Q1 Universe -> P31 Q5, P279 Q2, P361 Q999 (DANGLING), P527 Q7 (reaches the decoy).
# Q2 Galaxy   -> P31 Q5.
# Q3 Milky Way-> P31 [deprecated Q9, normal Q2] (truthy keeps Q2), P569 date literal.
# Q4 Star     -> P31 Q5 — DISCONNECTED from Q1 (the BFS emit guard must skip its line).
# Q5 class    -> no claims (empty edge field).
# Q6          -> NO label/desc (usable=0), P31 Q5 — an edge target, never a vector row.
# Q7 Decoy    -> label contains '"id":"Q999"' AND a P361->Q999 statement, so the RAW
#                LINE contains the literal bytes '"id":"Q999"' twice; Q999 is not in
#                the dump, so a sloppy search-anywhere guard would wrongly skip Q7.
# "noise"     -> a valid-JSON non-dict array element, skipped everywhere.
# P100        -> a PROPERTY entity, omitted from the sidecar entirely.
FIXTURE = [
    _item(
        "Q1",
        "Universe",
        "all of space and time",
        {
            "P31": [_stmt("P31", "Q5")],
            "P279": [_stmt("P279", "Q2")],
            "P361": [_stmt("P361", "Q999")],
            "P527": [_stmt("P527", "Q7")],
        },
    ),
    _item(
        "Q2", "Galaxy", "gravitationally bound system", {"P31": [_stmt("P31", "Q5")]}
    ),
    _item(
        "Q3",
        "Milky Way",
        "the galaxy containing the Solar System",
        {
            "P31": [_stmt("P31", "Q9", rank="deprecated"), _stmt("P31", "Q2")],
            "P569": [_stmt("P569", time="+1610-01-07T00:00:00Z")],
        },
    ),
    _item("Q4", "Star", "astronomical object", {"P31": [_stmt("P31", "Q5")]}),
    _item("Q5", "class", "a metaclass", {}),
    _item("Q6", None, None, {"P31": [_stmt("P31", "Q5")]}),
    _item(
        "Q7",
        'decoy with "id":"Q999" in the label',
        "part of the universe",
        {"P361": [_stmt("P361", "Q999")]},
    ),
    "noise",
    {
        "type": "property",
        "id": "P100",
        "labels": {"en": {"language": "en", "value": "prop"}},
    },
]

# the sidecar the fixture must compact to (lang=en, dump order)
EXPECTED_COMPACT = [
    "1\t1\t31:5,279:2,361:999,527:7",
    "2\t1\t31:5",
    "3\t1\t31:2",
    "4\t1\t31:5",
    "5\t1\t",
    "6\t0\t31:5",
    "7\t1\t361:999",
]


def _write_dump(tmp_path: Path, *, compressed: bool = False) -> Path:
    # REAL dump line shape: compact separators, "type" then "id" first, one entity
    # per line with a trailing comma inside array brackets.
    lines = [json.dumps(o, separators=(",", ":")) for o in FIXTURE]
    body = "[\n" + ",\n".join(lines) + "\n]\n"
    suffix = ".json.gz" if compressed else ".json"
    dump = tmp_path / f"wikidata-fast{suffix}"
    if compressed:
        dump.write_bytes(gzip.compress(body.encode("utf-8")))
    else:
        dump.write_text(body, encoding="utf-8")
    return dump


def _build_sidecar(tmp_path: Path, dump: Path, **kw) -> Path:
    cdir = tmp_path / "sidecar"
    build_compact(dump, cdir, workers=2, **kw)
    return cdir / "compact.tsv.gz"


def _read_compact(cpath: Path) -> list[str]:
    with gzip.open(cpath, "rt", encoding="utf-8") as fh:
        return [ln.rstrip("\n") for ln in fh]


def _assert_identical_outputs(pure: Path, fast: Path) -> None:
    names_pure = sorted(p.name for p in pure.iterdir())
    names_fast = sorted(p.name for p in fast.iterdir())
    assert names_pure == names_fast
    for name in names_pure:
        if name == "manifest.json":
            mp = json.loads((pure / name).read_text())
            mf = json.loads((fast / name).read_text())
            for key in ("counts", "slice", "shards", "shard_size", "language"):
                assert mp[key] == mf[key], key
        else:
            assert (pure / name).read_bytes() == (fast / name).read_bytes(), name


# --------------------------------------------------------------------------- #
# (a) compact sidecar correctness
# --------------------------------------------------------------------------- #
def test_compact_file_matches_parse_entity_and_entity_edges(tmp_path):
    dump = _write_dump(tmp_path)
    cpath = _build_sidecar(tmp_path, dump)
    assert _read_compact(cpath) == EXPECTED_COMPACT
    meta = json.loads((cpath.parent / "compact.meta.json").read_text())
    assert meta["entities"] == 7
    assert meta["usable"] == 6  # Q6 has no label/desc
    assert meta["edges"] == 9
    assert meta["lang"] == "en"
    assert meta["limit"] is None


def test_compact_gz_dump_and_pigz_path(tmp_path):
    # a .gz dump takes the pigz subprocess reader when pigz is installed, the
    # gzip.GzipFile fallback otherwise — either way the sidecar is identical
    dump = _write_dump(tmp_path, compressed=True)
    cpath = _build_sidecar(tmp_path, dump)
    assert _read_compact(cpath) == EXPECTED_COMPACT


def test_compact_lang_flips_usable(tmp_path):
    dump = _write_dump(tmp_path)
    cpath = _build_sidecar(tmp_path, dump, lang="de")
    # no entity has a German label/description: every usable flag is 0
    assert all(ln.split("\t")[1] == "0" for ln in _read_compact(cpath))


def test_compact_limit_trims_exactly(tmp_path):
    dump = _write_dump(tmp_path)
    cpath = _build_sidecar(tmp_path, dump, limit=3)
    assert _read_compact(cpath) == EXPECTED_COMPACT[:3]
    meta = json.loads((cpath.parent / "compact.meta.json").read_text())
    assert meta["entities"] == 3 and meta["limit"] == 3
    assert meta["usable"] == 3
    assert meta["edges"] == 6  # 4 + 1 + 1


# --------------------------------------------------------------------------- #
# (b) equivalence — pure path vs --compact, byte-identical
# --------------------------------------------------------------------------- #
def test_prefix_ingest_equivalence(tmp_path):
    dump = _write_dump(tmp_path)
    cpath = _build_sidecar(tmp_path, dump)
    out_pure, out_fast = tmp_path / "pure", tmp_path / "fast"
    kw = dict(literal_props=["P569"], shard_size=2)  # multiple shards too
    ingest(dump, out_pure, SliceSpec(limit=100), **kw)
    ingest(dump, out_fast, SliceSpec(limit=100), compact=cpath, **kw)
    _assert_identical_outputs(out_pure, out_fast)


def test_bfs_ingest_equivalence_with_decoy_and_dangling(tmp_path):
    dump = _write_dump(tmp_path)
    # the trap must be real: Q7's raw dump line carries the literal decoy bytes
    raw_q7 = next(
        ln
        for ln in dump.read_bytes().splitlines()
        if ln.startswith(b'{"type":"item","id":"Q7"')
    )
    # the statement datavalue embeds the exact bytes; the label decoy is present in
    # its JSON-escaped form (quotes inside a string value are always escaped)
    assert raw_q7.count(b'"id":"Q999"') == 1
    assert raw_q7.count(rb"\"id\":\"Q999\"") == 1
    cpath = _build_sidecar(tmp_path, dump)
    out_pure, out_fast = tmp_path / "pure", tmp_path / "fast"
    m_pure = ingest(dump, out_pure, SliceSpec(seeds=[1], target=100))
    m_fast = ingest(dump, out_fast, SliceSpec(seeds=[1], target=100), compact=cpath)
    _assert_identical_outputs(out_pure, out_fast)
    # the decoy entity IS in the slice (guard did not wrongly skip it) and the
    # dangling Q999 edges were dropped identically
    ents = (out_fast / "entities-00000.jsonl").read_text()
    assert '"id": 7' in ents
    assert m_pure["counts"] == m_fast["counts"]
    assert m_fast["counts"]["dropped_edges_dangling"] == 2  # Q1->Q999, Q7->Q999


def test_bfs_target_cut_equivalence(tmp_path):
    # the sorted() mid-frontier target cut must be reproduced exactly
    dump = _write_dump(tmp_path)
    cpath = _build_sidecar(tmp_path, dump)
    kept_pure, hops_pure = closure_ids(dump, seeds=[1], target=3, lang="en")
    kept_fast, hops_fast = closure_ids_compact(cpath, seeds=[1], target=3)
    assert kept_pure == kept_fast == {1, 2, 5}
    assert hops_pure == hops_fast
    out_pure, out_fast = tmp_path / "pure", tmp_path / "fast"
    ingest(dump, out_pure, SliceSpec(seeds=[1], target=3))
    ingest(dump, out_fast, SliceSpec(seeds=[1], target=3), compact=cpath)
    _assert_identical_outputs(out_pure, out_fast)


def test_compact_helpers_match_pure_helpers(tmp_path):
    dump = _write_dump(tmp_path)
    cpath = _build_sidecar(tmp_path, dump)
    assert prefix_ids_compact(cpath, 100) == prefix_ids(dump, 100, "en")
    assert prefix_ids_compact(cpath, 2) == prefix_ids(dump, 2, "en")
    kept = {1, 2, 5, 6, 7, 999}  # incl. an unusable item and a phantom id
    assert present_ids_compact(cpath, kept) == present_ids(dump, kept, "en")
    assert closure_ids_compact(cpath, [1], 100) == closure_ids(dump, [1], 100, "en")


def test_cli_equivalence_and_meta_guards(tmp_path):
    dump = _write_dump(tmp_path)
    cdir = tmp_path / "sidecar"
    assert (
        compact_main(["--dump", str(dump), "--out", str(cdir), "--workers", "2"]) == 0
    )
    cpath = cdir / "compact.tsv.gz"
    out_pure, out_fast = tmp_path / "pure", tmp_path / "fast"
    base = ["--dump", str(dump), "--seeds", "Q1", "--target", "100"]
    assert ingest_main([*base, "--out", str(out_pure)]) == 0
    assert ingest_main([*base, "--out", str(out_fast), "--compact", str(cpath)]) == 0
    _assert_identical_outputs(out_pure, out_fast)
    # a sidecar built for another language is refused
    with pytest.raises(SystemExit):
        ingest_main(
            [
                *base,
                "--out",
                str(tmp_path / "x1"),
                "--compact",
                str(cpath),
                "--lang",
                "de",
            ]
        )
    # a --limit (smoke prefix) sidecar is refused
    lim_dir = tmp_path / "sidecar-lim"
    compact_main(["--dump", str(dump), "--out", str(lim_dir), "--limit", "3"])
    with pytest.raises(SystemExit):
        ingest_main(
            [
                *base,
                "--out",
                str(tmp_path / "x2"),
                "--compact",
                str(lim_dir / "compact.tsv.gz"),
            ]
        )


# --------------------------------------------------------------------------- #
# (d) atomic publication — plan 078: a failed run must never damage the
#     previously published (data, meta) pair, and must leave no temp files
# --------------------------------------------------------------------------- #
def test_copy_failure_preserves_old_finals_and_cleans_temps(tmp_path, monkeypatch):
    dump = _write_dump(tmp_path)
    cdir = tmp_path / "sidecar"
    build_compact(dump, cdir, workers=2)
    cpath, mpath = cdir / COMPACT_NAME, cdir / META_NAME
    old_data, old_meta = cpath.read_bytes(), mpath.read_bytes()

    def boom(blob, keep):
        raise RuntimeError("injected: failure while copying member blobs")

    # deterministic parent-process failure mid copy loop (the --limit trim step)
    monkeypatch.setattr(wikidata_compact, "_trim_blob", boom)
    with pytest.raises(RuntimeError, match="injected"):
        build_compact(dump, cdir, workers=2, limit=3)
    # the previously published pair is untouched and no temp files remain
    assert cpath.read_bytes() == old_data
    assert mpath.read_bytes() == old_meta
    assert {p.name for p in cdir.iterdir()} == {COMPACT_NAME, META_NAME}
    # the wholly-old pair is still accepted
    verify_compact_sidecar(cpath, dump, "en")


def _gzip_member_count(data: bytes) -> int:
    """Count concatenated gzip members in `data` (each must decompress cleanly)."""
    n = 0
    while data:
        d = zlib.decompressobj(wbits=31)
        d.decompress(data)
        assert d.eof
        data = d.unused_data
        n += 1
    return n


def test_metadata_records_compact_identity(tmp_path):
    dump = _write_dump(tmp_path)
    cpath = _build_sidecar(tmp_path, dump)
    meta = json.loads((cpath.parent / META_NAME).read_text())
    assert meta["compact_size"] == cpath.stat().st_size
    assert meta["compact_sha256"] == hashlib.sha256(cpath.read_bytes()).hexdigest()
    # a successful run leaves exactly the published pair — no temp files
    assert {p.name for p in cpath.parent.iterdir()} == {COMPACT_NAME, META_NAME}
    # the valid pair verifies and hands back its metadata
    assert verify_compact_sidecar(cpath, dump, "en")["entities"] == 7


def test_multi_member_output_preserved_with_identity(tmp_path):
    # 1-byte batch hint -> one gzip member per fixture line; the digest must cover
    # the exact concatenated multi-member bytes
    dump = _write_dump(tmp_path)
    cdir = tmp_path / "sidecar"
    build_compact(dump, cdir, workers=2, batch_bytes=1)
    cpath = cdir / COMPACT_NAME
    data = cpath.read_bytes()
    assert _gzip_member_count(data) >= 2
    assert _read_compact(cpath) == EXPECTED_COMPACT
    meta = json.loads((cdir / META_NAME).read_text())
    assert meta["compact_size"] == len(data)
    assert meta["compact_sha256"] == hashlib.sha256(data).hexdigest()
    verify_compact_sidecar(cpath, dump, "en")


def test_publication_failure_between_replaces_fails_closed(tmp_path, monkeypatch):
    dump = _write_dump(tmp_path)
    cdir = tmp_path / "sidecar"
    build_compact(dump, cdir, workers=2, lang="de")  # the old, valid pair
    cpath, mpath = cdir / COMPACT_NAME, cdir / META_NAME
    old_meta = mpath.read_bytes()
    ref = tmp_path / "ref"
    build_compact(dump, ref, workers=2)  # what the new data bytes will be (lang=en)
    real_replace = wikidata_compact._replace
    calls = {"n": 0}

    def crash_between(tmp, final):
        calls["n"] += 1
        if calls["n"] == 2:  # data replaced, meta not — the crash window
            raise RuntimeError("injected: crash between data and meta replace")
        real_replace(tmp, final)

    monkeypatch.setattr(wikidata_compact, "_replace", crash_between)
    with pytest.raises(RuntimeError, match="injected"):
        build_compact(dump, cdir, workers=2)
    # final data is wholly NEW, final meta is wholly OLD, no temp files remain ...
    assert cpath.read_bytes() == (ref / COMPACT_NAME).read_bytes()
    assert mpath.read_bytes() == old_meta
    assert {p.name for p in cdir.iterdir()} == {COMPACT_NAME, META_NAME}
    # ... and ingest REJECTS the mismatched pair instead of consuming it
    with pytest.raises(CompactSidecarError):
        ingest(dump, tmp_path / "out", SliceSpec(limit=100), compact=cpath)


# --------------------------------------------------------------------------- #
# (e) ingest identity verification — plan 078: fail-closed sidecar reuse
# --------------------------------------------------------------------------- #
def test_ingest_rejects_missing_and_malformed_metadata(tmp_path):
    dump = _write_dump(tmp_path)
    cpath = _build_sidecar(tmp_path, dump)
    meta_path = cpath.parent / META_NAME
    good_meta = meta_path.read_text()
    spec = SliceSpec(limit=100)
    meta_path.unlink()
    with pytest.raises(CompactSidecarError, match="metadata missing"):
        ingest(dump, tmp_path / "o1", spec, compact=cpath)
    # the CLI surfaces the same rejection as an argparse error
    with pytest.raises(SystemExit):
        ingest_main(
            [
                "--dump",
                str(dump),
                "--out",
                str(tmp_path / "o2"),
                "--limit",
                "100",
                "--compact",
                str(cpath),
            ]
        )
    meta_path.write_text("not json{")
    with pytest.raises(CompactSidecarError, match="malformed"):
        ingest(dump, tmp_path / "o3", spec, compact=cpath)
    meta_path.write_text("[1, 2]")
    with pytest.raises(CompactSidecarError, match="malformed"):
        ingest(dump, tmp_path / "o4", spec, compact=cpath)
    stripped = json.loads(good_meta)
    del stripped["compact_sha256"]  # a pre-plan-078 sidecar shape
    meta_path.write_text(json.dumps(stripped))
    with pytest.raises(CompactSidecarError, match="compact_sha256"):
        ingest(dump, tmp_path / "o5", spec, compact=cpath)


def test_ingest_rejects_tampered_sidecar_bytes(tmp_path):
    dump = _write_dump(tmp_path)
    cpath = _build_sidecar(tmp_path, dump)
    good = cpath.read_bytes()
    spec = SliceSpec(limit=100)
    # truncation that is still a VALID gzip stream (fewer rows, reads cleanly)
    prefix = gzip.compress(
        "".join(f"{ln}\n" for ln in EXPECTED_COMPACT[:3]).encode(), mtime=0
    )
    cpath.write_bytes(prefix)
    with pytest.raises(CompactSidecarError, match="size mismatch"):
        ingest(dump, tmp_path / "t1", spec, compact=cpath)
    # an appended extra member (valid multi-member gzip, extra rows)
    cpath.write_bytes(good + gzip.compress(b"8\t1\t\n", mtime=0))
    with pytest.raises(CompactSidecarError, match="size mismatch"):
        ingest(dump, tmp_path / "t2", spec, compact=cpath)
    # a same-size byte flip -> sha256 mismatch
    flipped = bytearray(good)
    flipped[-1] ^= 0xFF
    cpath.write_bytes(bytes(flipped))
    with pytest.raises(CompactSidecarError, match="sha256 mismatch"):
        ingest(dump, tmp_path / "t3", spec, compact=cpath)
    # the pristine bytes are accepted again (successful reuse)
    cpath.write_bytes(good)
    ingest(dump, tmp_path / "t4", spec, compact=cpath)


def test_ingest_rejects_stale_metadata(tmp_path):
    # new data published without its metadata (the shape a crash between the two
    # replaces leaves behind): old meta names bytes the file no longer has
    dump = _write_dump(tmp_path)
    cpath = _build_sidecar(tmp_path, dump)
    other = tmp_path / "other"
    build_compact(dump, other, workers=2, lang="de")  # same dump, different bytes
    cpath.write_bytes((other / COMPACT_NAME).read_bytes())
    with pytest.raises(CompactSidecarError, match="mismatch"):
        ingest(dump, tmp_path / "o", SliceSpec(limit=100), compact=cpath)


# --------------------------------------------------------------------------- #
# (c) worker-batch purity + the emit guard
# --------------------------------------------------------------------------- #
def test_compact_batch_pure_and_skips_non_entities():
    lines = [json.dumps(o, separators=(",", ":")).encode() + b",\n" for o in FIXTURE]
    batch = [b"[\n", *lines[:3], b"this is not json\n", *lines[3:], b"]\n"]
    blob1, entities, usable, edges = compact_batch("en", batch)
    blob2 = compact_batch("en", batch)[0]
    assert blob1 == blob2  # deterministic bytes (gzip mtime pinned)
    got = gzip.decompress(blob1).decode().splitlines()
    assert got == EXPECTED_COMPACT
    assert (entities, usable, edges) == (7, 6, 9)
    assert compact_batch("en", [b"[\n", b"]\n"]) == (b"", 0, 0, 0)


def test_emit_guard_skips_only_on_positive_head_match():
    lines = [json.dumps(o, separators=(",", ":")) + ",\n" for o in FIXTURE]
    # reorder Q4's keys so its id is NOT at the structural head: the guard must
    # MISS and fall back to a full parse rather than guess
    q4 = json.loads(lines[3].rstrip().rstrip(","))
    reordered = {
        "labels": q4["labels"],
        "id": q4["id"],
        "type": q4["type"],
        "descriptions": q4["descriptions"],
        "claims": q4["claims"],
    }
    lines[3] = json.dumps(reordered, separators=(",", ":")) + ",\n"
    body = b"[\n" + "".join(lines).encode() + b"]\n"
    present = {4, 7}  # Q7 is the decoy line; Q4 has the reordered head
    got = [obj["id"] for obj in _iter_kept_entities(io.BytesIO(body), present)]
    # Q1/Q2/Q3/Q5/Q6: positive head match, not present -> skipped without parsing.
    # Q4: guard MISS (reordered head) -> full parse, yielded. Q7: positive match,
    # present -> yielded. P100: guard miss ('P100' is not a Q-id head) -> full
    # parse, yielded (the caller's eid filter drops it, exactly like iter_entities).
    assert got == ["Q4", "Q7", "P100"]
    # and the head regex itself reads the true id off the decoy line's head
    decoy_line = lines[6].encode()  # Q7, contains '"id":"Q999"' twice mid-line
    m = _HEAD_QID.match(decoy_line)
    assert m is not None and m.group(1) == b"7"
    assert _HEAD_QID.match(b'{"labels":{},"id":"Q4","type":"item"}') is None
    assert _HEAD_QID.match(b'{"type":"property","id":"P100"}') is None
