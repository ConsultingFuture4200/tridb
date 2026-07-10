"""Stream a Wikidata truthy-JSON dump slice into a portable tri-modal corpus manifest.

Plan 060 / ADR-0018 — the hardware-independent Wikidata ingest, the analogue of
tools/wiki_extract.py for the Wikidata proving ground. Where wiki_extract turns a
MediaWiki dump into (articles, hyperlink-edges, categories), this turns the Wikidata
`latest-all.json.gz` dump into the three modal streams a tri-modal load consumes:

    entities-NNNNN.jsonl   {"id","label","description"}  — the VECTOR embedding source
    edges-NNNNN.tsv        "src_id\\tp_id\\tdst_id"        — the TYPED GRAPH (statements)
    claims-NNNNN.jsonl     {"id","P31":[...], ...}         — the RELATIONAL filter columns
    manifest.json          provenance + per-shard {path,rows,schema} + reconciled counts

IDS ARE EXT IDS, NOT DENSE VIDS (ADR-0018 (c)). An entity `Qn` is emitted as the integer
`n`; a property `Pm` as `m`. The engine assigns dense vids at load via gph_upsert_vertex
(ADR-0013) and maps P-ids through register_edge_type (ADR-0016) — the ingest tool never
runs a parallel id scheme that could drift from the engine's.

TRUTHY. The full JSON dump carries every statement rank; we keep only the *truthy* set
(the best rank present per property, deprecated dropped) — the edge set a benchmark wants,
with no deprecated/duplicate-rank noise. This reproduces the `latest-truthy` dump from the
richer `latest-all` dump so a single stream serves both the graph and the claims legs.

SLICE MODES (ADR-0018 (b)). The dump is a JSON array streamed one entity per line, so it
is not random-access; the slice is built with bounded memory (only an id `set[int]` in RAM):

  --limit N            PREFIX slice: the first N ns-item entities in dump order. Deterministic
                       and cheap, but Q-id dump order scatters neighbours, so the induced graph
                       is largely DISCONNECTED — fine for a tooling dry-run, NOT for a traversal
                       measurement.
  --seeds Q..,Q.. \\    BFS slice: a breadth-first closure from a seed entity set over truthy
    --target N         out-edges to N entities, so the induced graph is CONNECTED (the measured
                       slice). Multi-pass streaming: one dump scan per BFS hop to expand the
                       frontier id set, then one emit scan. Memory ceiling = the frontier set.

Both modes drop edges whose dst is outside the kept set (the dangling-statement analogue of
wiki_extract's red-link drop), so the emitted graph is closed over the slice.

CLI:
    python -m tools.wikidata_ingest --dump <latest-all.json.gz> --out <dir> --limit 100000
    python -m tools.wikidata_ingest --dump <dump> --out <dir> --seeds Q11173 --target 1000000
"""

from __future__ import annotations

import argparse
import bz2
import gzip
import json
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

INGEST_VERSION = "0.1.0"
EDGE_SOURCE = "wikidata-statement"
DEFAULT_SHARD_SIZE = 100_000
DEFAULT_LANG = "en"
# P31 = "instance of" — the entity-type constraint the filter-first physical path (ADR-0018 (e))
# uses as its selective relational leg. Always harvested; extra literal props are opt-in.
TYPE_PROP = "P31"
_QID = re.compile(r"^Q([1-9][0-9]*)$")
_PID = re.compile(r"^P([1-9][0-9]*)$")


def qid_to_int(qid: str) -> int | None:
    """'Q42' -> 42; anything that is not a positive item id (P.., L.., '', 'Q0') -> None."""
    m = _QID.match(qid or "")
    return int(m.group(1)) if m else None


def pid_to_int(pid: str) -> int | None:
    """'P31' -> 31; a non-property key -> None."""
    m = _PID.match(pid or "")
    return int(m.group(1)) if m else None


# Statement ranks, worst-to-best. Truthy = keep every statement at the single highest
# rank PRESENT for a property; drop the rest (so a 'deprecated'-only property still yields
# its deprecated statements — matching Wikidata's truthy dump, which emits best-available).
_RANK_ORDER = {"deprecated": 0, "normal": 1, "preferred": 2}


def best_rank_statements(statements: list[dict]) -> list[dict]:
    """Filter one property's statement list to its truthy (best-rank-present) subset."""
    if not statements:
        return []
    best = max(_RANK_ORDER.get(s.get("rank", "normal"), 1) for s in statements)
    return [
        s for s in statements if _RANK_ORDER.get(s.get("rank", "normal"), 1) == best
    ]


def _mainsnak_entity_target(statement: dict) -> int | None:
    """If a statement's mainsnak is a wikibase-entityid VALUE, return the target Q int."""
    snak = statement.get("mainsnak") or {}
    if snak.get("snaktype") != "value":
        return None  # 'somevalue'/'novalue' snaks carry no edge target
    dv = snak.get("datavalue") or {}
    if dv.get("type") != "wikibase-entityid":
        return None
    val = dv.get("value") or {}
    # 'id' is authoritative; 'numeric-id' is a convenience mirror. Prefer id so a future
    # non-item entity type (property/lexeme value) is rejected by qid_to_int, not mis-kept.
    return qid_to_int(val.get("id", "")) if val.get("id") else val.get("numeric-id")


def entity_edges(claims: dict) -> list[tuple[int, int]]:
    """Return [(p_id, dst_id)] for every truthy entity-valued statement (subject implicit).

    The subject is the entity owning `claims`; the caller pairs each (p_id, dst_id) with it
    to form the directed out-edge src->dst (ADR-0018 (c): statements are out-edges).
    Order is deterministic: properties sorted by id, targets in dump order, de-duplicated.
    """
    edges: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for prop in sorted(claims, key=lambda p: pid_to_int(p) or 0):
        pid = pid_to_int(prop)
        if pid is None:
            continue
        for st in best_rank_statements(claims[prop]):
            dst = _mainsnak_entity_target(st)
            if dst is None:
                continue
            key = (pid, dst)
            if key in seen:
                continue
            seen.add(key)
            edges.append(key)
    return edges


def entity_types(claims: dict) -> list[int]:
    """Return the truthy P31 'instance of' target ids — the filter-first type constraint."""
    out: list[int] = []
    for st in best_rank_statements(claims.get(TYPE_PROP, [])):
        dst = _mainsnak_entity_target(st)
        if dst is not None and dst not in out:
            out.append(dst)
    return out


def _literal_value(statement: dict):
    """Best-effort literal value of a non-entity truthy statement, for a relational column.

    time -> the ISO-ish time string; quantity -> the amount string; string/external-id ->
    the raw string; monolingual text -> its value. Returns None for anything else (leaving
    the column absent rather than guessing a shape).
    """
    snak = statement.get("mainsnak") or {}
    if snak.get("snaktype") != "value":
        return None
    dv = snak.get("datavalue") or {}
    val = dv.get("value")
    t = dv.get("type")
    if t == "time" and isinstance(val, dict):
        return val.get("time")
    if t == "quantity" and isinstance(val, dict):
        return val.get("amount")
    if t == "monolingualtext" and isinstance(val, dict):
        return val.get("text")
    if t in ("string", "external-id") and isinstance(val, str):
        return val
    return None


def entity_claims(claims: dict, literal_props: Iterable[str]) -> dict:
    """Build the relational claims row: P31 type ids + each requested literal property.

    P31 is always present (as an int list, possibly empty). Each literal prop maps to a
    list of its truthy literal values (dates/quantities/strings), omitted if it yields none.
    """
    row: dict = {TYPE_PROP: entity_types(claims)}
    for prop in literal_props:
        if prop == TYPE_PROP:
            continue
        vals = [
            v
            for st in best_rank_statements(claims.get(prop, []))
            if (v := _literal_value(st)) is not None
        ]
        if vals:
            row[prop] = vals
    return row


@dataclass
class ParsedEntity:
    id: int
    label: str
    description: str
    edges: list[tuple[int, int]]  # (p_id, dst_id), subject = id
    claims: dict
    # dump-order edges retained even when dst is outside a slice are dropped by the writer,
    # not here — parse is slice-agnostic so the pure helper is testable in isolation.


def _lang_value(section: dict, lang: str) -> str:
    """Pull section[lang]['value'] (labels/descriptions shape); '' if absent."""
    entry = (section or {}).get(lang)
    return entry.get("value", "") if isinstance(entry, dict) else ""


def parse_entity(
    obj: dict, lang: str = DEFAULT_LANG, literal_props: Iterable[str] = ()
) -> ParsedEntity | None:
    """Parse one Wikidata entity object into a ParsedEntity, or None if it is not usable.

    Rejected (return None): non-item entities (properties/lexemes — no dense-vertex role
    in the item graph), and items with neither a label nor a description in `lang` (nothing
    to embed — still a valid graph target for others' edges, just not a vector row here).
    """
    if obj.get("type") != "item":
        return None
    eid = qid_to_int(obj.get("id", ""))
    if eid is None:
        return None
    label = _lang_value(obj.get("labels"), lang)
    description = _lang_value(obj.get("descriptions"), lang)
    if not label and not description:
        return None
    claims = obj.get("claims") or {}
    return ParsedEntity(
        id=eid,
        label=label,
        description=description,
        edges=entity_edges(claims),
        claims=entity_claims(claims, literal_props),
    )


def _open_dump(path: Path) -> BinaryIO:
    """Open a dump as a binary stream, transparently de-gzip/bz2 by suffix.

    A plain '.json'/'.ndjson' opens raw — convenient for the in-repo test fixture, which
    exercises the identical streaming path without a compressed file.
    """
    if path.suffix == ".gz":
        return gzip.open(path, "rb")
    if path.suffix == ".bz2":
        return bz2.open(path, "rb")
    return open(path, "rb")


def iter_entities(fh: BinaryIO) -> Iterator[dict]:
    """Stream entity objects from a Wikidata JSON-array dump, one per line, bounded memory.

    The dump is `[` / one-entity-per-line-with-trailing-comma / `]`. We strip the array
    punctuation and a trailing comma and json.loads each entity line. A bare-object-per-line
    (ndjson) fixture also parses, since the array-bracket lines are simply skipped. A line
    that is not valid JSON after stripping is skipped (the last entity has no trailing comma;
    blank lines and the brackets are non-entities).
    """
    for raw in fh:
        line = raw.decode("utf-8").strip()
        if not line or line in ("[", "]"):
            continue
        if line.endswith(","):
            line = line[:-1]
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def prefix_ids(dump: Path, limit: int, lang: str) -> set[int]:
    """PREFIX slice: the ids of the first `limit` usable items in dump order."""
    kept: set[int] = set()
    with _open_dump(dump) as fh:
        for obj in iter_entities(fh):
            ent = parse_entity(obj, lang)
            if ent is None:
                continue
            kept.add(ent.id)
            if len(kept) >= limit:
                break
    return kept


def bfs_closure(
    seeds: Iterable[int], adjacency: dict[int, list[int]], target: int
) -> set[int]:
    """Pure BFS closure: expand `seeds` over `adjacency` until `target` ids are reached.

    Deterministic (frontier is a FIFO; neighbours consumed in adjacency order). Stops as
    soon as the kept set hits `target` OR the frontier drains. This is the in-memory core
    the streaming driver reproduces one dump-scan per hop; unit-tested directly.
    """
    kept: set[int] = set()
    q: deque[int] = deque()
    for s in seeds:
        if s not in kept:
            kept.add(s)
            q.append(s)
            if len(kept) >= target:
                return kept
    while q:
        cur = q.popleft()
        for nxt in adjacency.get(cur, ()):  # noqa: SIM118 - dict.get default is a tuple
            if nxt not in kept:
                kept.add(nxt)
                q.append(nxt)
                if len(kept) >= target:
                    return kept
    return kept


def closure_ids(
    dump: Path, seeds: Iterable[int], target: int, lang: str
) -> tuple[set[int], int]:
    """Streaming BFS closure: re-scan the dump once per hop until `target` ids are kept.

    Returns (kept_ids, hops_scanned). Memory ceiling is the kept set + the current frontier
    (documented in the report). Each hop scans the whole dump collecting the out-neighbours
    of the current frontier; a hop that adds nothing (frontier fully expanded) terminates.
    """
    kept: set[int] = {s for s in seeds}
    frontier: set[int] = set(kept)
    hops = 0
    while frontier and len(kept) < target:
        next_frontier: set[int] = set()
        with _open_dump(dump) as fh:
            for obj in iter_entities(fh):
                eid = qid_to_int(obj.get("id", ""))
                if eid is None or eid not in frontier:
                    continue
                for _pid, dst in entity_edges(obj.get("claims") or {}):
                    if dst not in kept:
                        next_frontier.add(dst)
        hops += 1
        # honour target even mid-frontier: keep in a stable (sorted) order for determinism
        for dst in sorted(next_frontier):
            if len(kept) >= target:
                break
            kept.add(dst)
        frontier = {d for d in next_frontier if d in kept}
    return kept, hops


class _ShardWriter:
    """Rotates entities/edges/claims shard files every `shard_size` emitted entities.

    Same monotonic-shard discipline as tools/wiki_extract._ShardWriter: a shard index is
    opened AT MOST ONCE ('x' mode); a backward jump to an already-closed shard is a hard
    error rather than a silent truncate. Shards are keyed by EMISSION ORDINAL (a dense
    0..N-1 counter), NOT the sparse Q-id, so the three streams stay index-aligned and shard
    progression is strictly monotonic regardless of Q-id gaps in the slice.
    """

    def __init__(self, out: Path, shard_size: int):
        self.out = out
        self.shard_size = shard_size
        self.entities_shards: list[dict] = []
        self.edges_shards: list[dict] = []
        self.claims_shards: list[dict] = []
        self._idx = -1
        self._ef = self._gf = self._cf = None
        self._erows = self._grows = self._crows = 0
        self._opened_shards: set[int] = set()

    def _rotate(self, shard_idx: int) -> None:
        if shard_idx in self._opened_shards:
            raise ValueError(
                f"non-monotonic shard progression: shard {shard_idx} was already opened "
                f"and closed (currently on shard {self._idx}); reopening it would truncate "
                "its content. Entities must be emitted in non-decreasing shard order."
            )
        self._close_current()
        self._idx = shard_idx
        self._opened_shards.add(shard_idx)
        ep = self.out / f"entities-{shard_idx:05d}.jsonl"
        gp = self.out / f"edges-{shard_idx:05d}.tsv"
        cp = self.out / f"claims-{shard_idx:05d}.jsonl"
        self._ef = ep.open("x", encoding="utf-8")
        self._gf = gp.open("x", encoding="utf-8")
        self._cf = cp.open("x", encoding="utf-8")
        self._erows = self._grows = self._crows = 0
        self.entities_shards.append({"path": ep.name, "rows": 0})
        self.edges_shards.append({"path": gp.name, "rows": 0})
        self.claims_shards.append({"path": cp.name, "rows": 0})

    def _close_current(self) -> None:
        if self._ef is None:
            return
        self._ef.close()
        self._gf.close()
        self._cf.close()
        self.entities_shards[-1]["rows"] = self._erows
        self.edges_shards[-1]["rows"] = self._grows
        self.claims_shards[-1]["rows"] = self._crows

    def write(
        self,
        ordinal: int,
        entity: dict,
        edges: list[tuple[int, int, int]],
        claims: dict,
    ) -> None:
        """Emit one entity (+ its edges/claims) at dense emission `ordinal`."""
        shard_idx = ordinal // self.shard_size
        if shard_idx != self._idx:
            self._rotate(shard_idx)
        self._ef.write(json.dumps(entity, ensure_ascii=False) + "\n")
        self._erows += 1
        for src, pid, dst in edges:
            self._gf.write(f"{src}\t{pid}\t{dst}\n")
            self._grows += 1
        self._cf.write(json.dumps(claims, ensure_ascii=False) + "\n")
        self._crows += 1

    def close(self) -> None:
        self._close_current()

    def totals(self) -> tuple[int, int, int]:
        e = sum(s["rows"] for s in self.entities_shards)
        g = sum(s["rows"] for s in self.edges_shards)
        c = sum(s["rows"] for s in self.claims_shards)
        return e, g, c


def present_ids(dump: Path, kept: set[int], lang: str) -> set[int]:
    """The subset of `kept` that is actually an embeddable item in the dump.

    A BFS closure adds statement TARGETS to `kept`, some of which are absent from the dump
    (phantom ids) or are items without a label/description (not a vector row). Only these
    present ids become vertices; edges to anything else are dangling and must be dropped. A
    prefix slice's `kept` is already exactly its present set, so this scan is a no-op there.
    """
    present: set[int] = set()
    with _open_dump(dump) as fh:
        for obj in iter_entities(fh):
            eid = qid_to_int(obj.get("id", ""))
            if eid is None or eid not in kept:
                continue
            if parse_entity(obj, lang) is not None:
                present.add(eid)
    return present


def emit_slice(
    dump: Path,
    out: Path,
    kept: set[int],
    lang: str,
    literal_props: list[str],
    shard_size: int,
) -> dict:
    """Emit pass: stream the dump, write shards for every present entity in `kept`.

    Edges are filtered to intra-slice targets that are themselves present vertices — the
    dangling-statement drop (a target absent from the slice, a phantom closure id, or a
    no-vector-row item). Emission ordinal is a dense counter over emitted entities in dump
    order (id-aligned shards).
    """
    present = present_ids(dump, kept, lang)
    writer = _ShardWriter(out, shard_size)
    ordinal = 0
    dropped_edges = 0
    with _open_dump(dump) as fh:
        for obj in iter_entities(fh):
            eid = qid_to_int(obj.get("id", ""))
            if eid is None or eid not in present:
                continue
            ent = parse_entity(obj, lang, literal_props)
            if (
                ent is None
            ):  # unreachable given present_ids, but keep the writer total honest
                continue
            edges: list[tuple[int, int, int]] = []
            for pid, dst in ent.edges:
                if dst in present:
                    edges.append((ent.id, pid, dst))
                else:
                    dropped_edges += 1
            writer.write(
                ordinal,
                {"id": ent.id, "label": ent.label, "description": ent.description},
                edges,
                {"id": ent.id, **ent.claims},
            )
            ordinal += 1
    writer.close()
    return {
        "entities_shards": writer.entities_shards,
        "edges_shards": writer.edges_shards,
        "claims_shards": writer.claims_shards,
        "totals": writer.totals(),
        "dropped_edges_dangling": dropped_edges,
        "present": len(present),
    }


def build_manifest(
    dump: Path,
    slice_meta: dict,
    lang: str,
    literal_props: list[str],
    shard_size: int,
    emit: dict,
) -> dict:
    """Assemble manifest.json: provenance + per-shard schema + reconciled counts."""
    n_entities, n_edges, n_claims = emit["totals"]
    return {
        "source": "wikidata-truthy-json",
        "dump": dump.name,
        "dump_path": str(dump),
        "extractor": "tools/wikidata_ingest.py",
        "extractor_version": INGEST_VERSION,
        "created": datetime.now(timezone.utc).isoformat(),
        "edge_source": EDGE_SOURCE,
        "language": lang,
        "slice": slice_meta,
        "id_scheme": "ext ids: entity id = Q-number, edge p_id = P-number; engine assigns "
        "dense vids at load via gph_upsert_vertex (ADR-0013), P-ids via register_edge_type "
        "(ADR-0016)",
        "type_prop": TYPE_PROP,
        "literal_props": literal_props,
        "shard_size": shard_size,
        "counts": {
            "entities": n_entities,
            "edges": n_edges,
            "claims": n_claims,
            "dropped_edges_dangling": emit["dropped_edges_dangling"],
        },
        "shards": {
            "entities": {
                "schema": 'jsonl; one object/line: {"id": int (Q-number), "label": str, '
                '"description": str}. The VECTOR embedding source (label + " — " + '
                "description, normalize-at-write per ADR-0017).",
                "files": emit["entities_shards"],
            },
            "edges": {
                "schema": "tsv; no header; columns: src_id\\tp_id\\tdst_id (all int ext "
                "ids; directed truthy statement src->dst; p_id is the P-number, mapped to "
                "a typed-edge dict id at load).",
                "files": emit["edges_shards"],
            },
            "claims": {
                "schema": 'jsonl; one object/line: {"id": int, "P31": [int type ids], '
                "<literal Pxx>: [values]}. The RELATIONAL filter columns; P31 is the "
                "entity-type constraint the filter-first path selects on.",
                "files": emit["claims_shards"],
            },
        },
    }


@dataclass
class SliceSpec:
    limit: int | None = None
    seeds: list[int] = field(default_factory=list)
    target: int | None = None


def ingest(
    dump: Path,
    out: Path,
    spec: SliceSpec,
    *,
    lang: str = DEFAULT_LANG,
    literal_props: Iterable[str] = (),
    shard_size: int = DEFAULT_SHARD_SIZE,
) -> dict:
    """Run the full slice + emit and write the manifest. Returns the manifest."""
    out.mkdir(parents=True, exist_ok=True)
    literal_props = [p for p in literal_props if p != TYPE_PROP]
    if spec.seeds:
        target = spec.target or (spec.limit or 0) or len(spec.seeds)
        kept, hops = closure_ids(dump, spec.seeds, target, lang)
        slice_meta = {
            "mode": "bfs_closure",
            "seeds": [f"Q{s}" for s in spec.seeds],
            "target": target,
            "hops_scanned": hops,
            "kept": len(kept),
            "note": "connected induced subgraph; memory ceiling = kept id set",
        }
    else:
        if not spec.limit:
            raise ValueError("prefix slice requires a positive --limit")
        kept = prefix_ids(dump, spec.limit, lang)
        slice_meta = {
            "mode": "prefix",
            "limit": spec.limit,
            "kept": len(kept),
            "note": "first-N in dump order; induced graph is largely DISCONNECTED "
            "(tooling dry-run, not a traversal measurement)",
        }
    emit = emit_slice(dump, out, kept, lang, literal_props, shard_size)
    manifest = build_manifest(dump, slice_meta, lang, literal_props, shard_size, emit)
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _parse_seeds(raw: str) -> list[int]:
    """Parse --seeds: a comma list of Q-ids, or @path to a file of one Q-id per line."""
    if raw.startswith("@"):
        text = Path(raw[1:]).read_text(encoding="utf-8")
        tokens = text.split()
    else:
        tokens = [t.strip() for t in raw.split(",")]
    seeds: list[int] = []
    for tok in tokens:
        s = qid_to_int(tok)
        if s is not None and s not in seeds:
            seeds.append(s)
    return seeds


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Stream a Wikidata truthy-JSON dump slice into a tri-modal corpus."
    )
    ap.add_argument(
        "--dump", type=Path, required=True, help="path to latest-all.json[.gz|.bz2]"
    )
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="PREFIX slice: first N items in dump order",
    )
    ap.add_argument(
        "--seeds",
        type=str,
        default=None,
        help="BFS slice: comma Q-ids or @file; closes over out-edges to --target entities",
    )
    ap.add_argument(
        "--target",
        type=int,
        default=None,
        help="BFS target entity count (with --seeds)",
    )
    ap.add_argument(
        "--lang", type=str, default=DEFAULT_LANG, help="label/description language"
    )
    ap.add_argument(
        "--claim-props",
        type=str,
        default="",
        help="extra literal properties for the claims row (comma P-ids; P31 always included)",
    )
    ap.add_argument(
        "--shard-size",
        type=int,
        default=DEFAULT_SHARD_SIZE,
        help=f"entities per shard file (default {DEFAULT_SHARD_SIZE})",
    )
    args = ap.parse_args(argv)
    if args.shard_size <= 0:
        ap.error("--shard-size must be positive")
    if args.limit is not None and args.limit <= 0:
        ap.error("--limit must be positive")
    seeds = _parse_seeds(args.seeds) if args.seeds else []
    if not seeds and not args.limit:
        ap.error("provide --limit (prefix slice) or --seeds (bfs slice)")
    literal_props = [p.strip() for p in args.claim_props.split(",") if p.strip()]

    manifest = ingest(
        args.dump,
        args.out,
        SliceSpec(limit=args.limit, seeds=seeds, target=args.target),
        lang=args.lang,
        literal_props=literal_props,
        shard_size=args.shard_size,
    )
    c = manifest["counts"]
    print(
        f"[wikidata_ingest] {c['entities']} entities, {c['edges']} edges, "
        f"{c['claims']} claims rows ({c['dropped_edges_dangling']} dangling edges dropped) "
        f"-> {args.out}/manifest.json"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
