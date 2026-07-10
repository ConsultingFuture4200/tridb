"""Stream a MediaWiki 'pages-articles.xml.bz2' dump into a portable corpus manifest.

PHASE 0 of the full-Wikipedia benchmark (docs/wiki_scale_benchmark_spec_v0.1.0.md):
the hardware-independent extraction that is *also* the personal offline-wiki
foundation. It mirrors the semantic shape of tools/build_wiki_graph.py — directed
article->article edges, a title->id map, redirect-resolved links — but at streaming
scale: the full enwiki dump is ~90 GB extracted, so nothing is ever materialized in
RAM. We stream the bz2 twice:

    pass 1 : build {normalized title -> article id} for every ns0 NON-redirect page,
             plus a {normalized title -> normalized target} redirect map.
    pass 2 : re-stream, and for each article emit its plain text + metadata and its
             hyperlink out-edges, resolving each [[wikilink]] through the redirect
             map to a real article id (dropping red-links and non-ns0 targets).

Only ns0 (main/article) pages are kept. Redirect pages are recorded in the redirect
map, NOT emitted as corpus articles. Links whose resolved target is not an existing
ns0 article are dropped (red-links, File:/Category:/Template: targets, etc.).

The two maps built in pass 1 are the only things held in RAM (the documented
two-pass tradeoff, spec §"Phase 0"); article TEXT is streamed straight to disk.

OUTPUT (--out DIR), all COPY-friendly and sharded (see manifest.json for the exact
contract the downstream loader consumes without reading this code):
    articles-NNNNN.jsonl   one JSON object/line: {"id","title","text","ts"}
    edges-NNNNN.tsv        "src_id\\tdst_id" — the redirect-resolved hyperlink graph
    categories-NNNNN.tsv   "article_id\\tcategory" — normalized category membership
    redirects.tsv          "source_title\\ttarget_title" — the (normalized) redirect map
    manifest.json          provenance + per-shard {path,rows,schema} + total counts

CLI:
    python -m tools.wiki_extract --dump <dump.xml.bz2> --out <dir> [--max-articles N]

--max-articles bounds pass 1 (stops after N articles are indexed) and pass 2 (stops
after N articles are emitted) to the SAME first-N articles, so ids are consistent.
For a slice, a link through a redirect that only appears LATER in the dump than the
Nth article cannot resolve (that redirect was never seen) — an accepted slice caveat.
"""

from __future__ import annotations

import argparse
import bz2
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator
from xml.etree import ElementTree as ET

import mwparserfromhell

EXTRACTOR_VERSION = "0.1.0"
EDGE_SOURCE = "wikilink"
DEFAULT_SHARD_SIZE = 100_000
_MAX_REDIRECT_HOPS = 8
_WS = re.compile(r"\s+")
# Link/category namespace prefixes are matched case-insensitively on the part
# before the first ':'. We only need Category here (harvested separately); every
# other non-ns0 target simply fails the title_to_id lookup and is dropped.
_CATEGORY_PREFIXES = ("category",)


def normalize_title(title: str) -> str:
    """MediaWiki-faithful title key: strip, '_'->' ', collapse ws, upper-case first.

    enwiki has $wgCapitalLinks=true, so the first character of a title is
    case-insensitive; everything else is significant. Page titles and link targets
    are normalized identically so a [[united_states]] link keys to the "United
    States" article. This is a real hyperlink key, not the lossy token key that
    tools/build_wiki_graph.py uses for its text-mention proxy.
    """
    t = _WS.sub(" ", title.replace("_", " ")).strip()
    if not t:
        return ""
    return t[0].upper() + t[1:]


def resolve_link_target(raw: str) -> str:
    """Normalize a raw [[wikilink]] target to a title key.

    Drops a leading ':' (an escaped-namespace link like [[:Category:X]] points AT
    the page rather than transcluding it) and any '#section' anchor, then applies
    normalize_title. Returns "" for an empty/anchor-only target.
    """
    t = raw.strip()
    if t.startswith(":"):
        t = t[1:]
    t = t.split("#", 1)[0]
    return normalize_title(t)


def _category_name(raw: str) -> str | None:
    """If `raw` is a [[Category:X]] membership link, return normalized 'X', else None.

    A leading ':' (i.e. [[:Category:X]]) is a *link* to the category page, not a
    membership, so it is not a category here.
    """
    t = raw.strip()
    if t.startswith(":"):
        return None
    head, sep, tail = t.partition(":")
    if not sep or head.strip().lower() not in _CATEGORY_PREFIXES:
        return None
    name = tail.split("#", 1)[0]
    return normalize_title(name) or None


def _copy_text_escape(field: str) -> str:
    """Escape a text field so it round-trips through `COPY ... (FORMAT text)`.

    In PG text-format COPY, backslash is the escape metacharacter and tab/newline are
    the field/row terminators. `normalize_title` collapses whitespace so tab/newline
    should not reach here, but backslash is a legal title char ($wgLegalTitleChars)
    and MUST be doubled — otherwise PG drops it (e.g. `C\\C++` -> `CC++`) and a
    trailing backslash before the row terminator can abort the shard's COPY. Backslash
    is escaped first so the tab/newline escapes it emits are not re-escaped.
    """
    return (
        field.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


@dataclass
class RawPage:
    title: str
    ns: int
    is_redirect: bool
    redirect_target: str  # normalized; "" unless is_redirect
    timestamp: str
    text: str


def _open_dump(path: Path) -> BinaryIO:
    """Open a dump as a binary stream, transparently bz2-decompressing '*.bz2'.

    A plain '.xml' path opens raw — convenient for the in-repo test fixture, which
    exercises the identical pass code without needing a compressed file.
    """
    if path.suffix == ".bz2":
        return bz2.open(path, "rb")
    return open(path, "rb")


def iter_pages(fh: BinaryIO) -> Iterator[RawPage]:
    """Stream <page> elements, yielding a RawPage each, with bounded memory.

    Uses iterparse and clears both the finished <page> and the root's accumulated
    (already-emitted) children every page, so peak memory is one page — never the
    whole dump.
    """
    context = ET.iterparse(fh, events=("start", "end"))
    _, root = next(context)  # the <mediawiki> root, on its start event
    ns_uri = root.tag[: root.tag.index("}") + 1] if "}" in root.tag else ""

    def q(tag: str) -> str:
        return f"{ns_uri}{tag}"

    page_tag = q("page")
    for event, elem in context:
        if event != "end" or elem.tag != page_tag:
            continue
        title_el = elem.find(q("title"))
        ns_el = elem.find(q("ns"))
        title = title_el.text or "" if title_el is not None else ""
        try:
            ns = int(ns_el.text) if ns_el is not None and ns_el.text else -1
        except ValueError:
            ns = -1
        redir_el = elem.find(q("redirect"))
        is_redirect = redir_el is not None
        redirect_target = (
            normalize_title(redir_el.get("title", "")) if is_redirect else ""
        )
        ts_el = elem.find(f"{q('revision')}/{q('timestamp')}")
        text_el = elem.find(f"{q('revision')}/{q('text')}")
        yield RawPage(
            title=title,
            ns=ns,
            is_redirect=is_redirect,
            redirect_target=redirect_target,
            timestamp=(ts_el.text or "") if ts_el is not None else "",
            text=(text_el.text or "") if text_el is not None else "",
        )
        elem.clear()
        root.clear()


def first_pass(
    dump: Path, max_articles: int | None
) -> tuple[dict[str, int], dict[str, str], dict]:
    """Build (title_to_id, redirects, stats) over the dump's ns0 pages.

    ns0 non-redirect pages get sequential ids in encounter order; ns0 redirect
    pages populate the redirect map. Stops once `max_articles` ids are assigned.
    """
    title_to_id: dict[str, int] = {}
    redirects: dict[str, str] = {}
    pages_scanned = 0
    with _open_dump(dump) as fh:
        for page in iter_pages(fh):
            pages_scanned += 1
            if page.ns != 0:
                continue
            key = normalize_title(page.title)
            if not key:
                continue
            if page.is_redirect:
                if page.redirect_target and key not in redirects:
                    redirects[key] = page.redirect_target
                continue
            if key in title_to_id:
                continue
            title_to_id[key] = len(title_to_id)
            if max_articles is not None and len(title_to_id) >= max_articles:
                break
    stats = {
        "pages_scanned_pass1": pages_scanned,
        "articles_indexed": len(title_to_id),
        "redirects": len(redirects),
    }
    return title_to_id, redirects, stats


def resolve_edge(
    key: str, title_to_id: dict[str, int], redirects: dict[str, str]
) -> int | None:
    """Resolve a normalized link target to an article id, following redirects.

    A direct article hit wins immediately; otherwise follow the redirect chain (up
    to _MAX_REDIRECT_HOPS, cycle-guarded) until it lands on a real article. Returns
    None for a red-link / non-ns0 / dangling-redirect target.
    """
    cur = key
    seen: set[str] = set()
    for _ in range(_MAX_REDIRECT_HOPS):
        hit = title_to_id.get(cur)
        if hit is not None:
            return hit
        nxt = redirects.get(cur)
        if nxt is None or cur in seen:
            return None
        seen.add(cur)
        cur = nxt
    return None


def parse_article(text: str) -> tuple[str, list[str], list[str]]:
    """Return (plain_text, link_targets, categories) for one article's wikitext.

    Links and categories are the RAW wikilink titles (resolution/normalization is
    the caller's job); plain_text is mwparserfromhell's strip_code output.
    """
    code = mwparserfromhell.parse(text)
    link_targets: list[str] = []
    categories: list[str] = []
    for link in code.filter_wikilinks():
        raw = str(link.title)
        cat = _category_name(raw)
        if cat is not None:
            categories.append(cat)
            # A category tag is metadata, not prose — drop it so its "Category:X"
            # text does not leak into the stripped article body.
            try:
                code.remove(link)
            except ValueError:
                pass
        else:
            link_targets.append(raw)
    plain = code.strip_code(normalize=True, collapse=True).strip()
    return plain, link_targets, categories


class _ShardWriter:
    """Rotates articles/edges/categories shard files every `shard_size` articles.

    Article i and the edges/categories emitted for it always land in shard
    i // shard_size, so the three streams stay index-aligned.

    Each shard_idx is opened AT MOST ONCE for the life of a writer: `_rotate`
    exclusive-creates the shard files ("x" mode) and records which indices have
    been opened. A non-monotonic shard progression (write() computing a
    shard_idx that was already opened+closed) previously reopened that shard's
    files in truncate ("w") mode, silently wiping prior content while the
    manifest kept the earlier (now-stale) descriptor around too. That is now a
    hard error: this can only happen from a bug upstream (e.g. duplicate/
    out-of-order article ids), and silent data loss is worse than a loud crash.
    """

    def __init__(self, out: Path, shard_size: int):
        self.out = out
        self.shard_size = shard_size
        self.articles_shards: list[dict] = []
        self.edges_shards: list[dict] = []
        self.categories_shards: list[dict] = []
        self._idx = -1
        self._af = self._ef = self._cf = None
        self._arows = self._erows = self._crows = 0
        self._opened_shards: set[int] = set()

    def _rotate(self, shard_idx: int) -> None:
        if shard_idx in self._opened_shards:
            raise ValueError(
                f"non-monotonic shard progression: shard {shard_idx} was already "
                f"opened and closed (currently on shard {self._idx}); reopening it "
                "would truncate its existing content. This indicates article ids "
                "were not emitted in non-decreasing shard order upstream — fix the "
                "id assignment rather than reopening the shard."
            )
        self._close_current()
        self._idx = shard_idx
        self._opened_shards.add(shard_idx)
        ap = self.out / f"articles-{shard_idx:05d}.jsonl"
        ep = self.out / f"edges-{shard_idx:05d}.tsv"
        cp = self.out / f"categories-{shard_idx:05d}.tsv"
        self._af = ap.open("x", encoding="utf-8")
        self._ef = ep.open("x", encoding="utf-8")
        self._cf = cp.open("x", encoding="utf-8")
        self._arows = self._erows = self._crows = 0
        self.articles_shards.append({"path": ap.name, "rows": 0})
        self.edges_shards.append({"path": ep.name, "rows": 0})
        self.categories_shards.append({"path": cp.name, "rows": 0})

    def _close_current(self) -> None:
        if self._af is None:
            return
        self._af.close()
        self._ef.close()
        self._cf.close()
        self.articles_shards[-1]["rows"] = self._arows
        self.edges_shards[-1]["rows"] = self._erows
        self.categories_shards[-1]["rows"] = self._crows

    def write(
        self,
        article: dict,
        edges: list[tuple[int, int]],
        categories: list[str],
    ) -> None:
        shard_idx = article["id"] // self.shard_size
        if shard_idx != self._idx:
            self._rotate(shard_idx)
        self._af.write(json.dumps(article, ensure_ascii=False) + "\n")
        self._arows += 1
        for src, dst in edges:
            self._ef.write(f"{src}\t{dst}\n")
            self._erows += 1
        aid = article["id"]
        for cat in categories:
            # categories-*.tsv is a `COPY ... (FORMAT text)` target: escape the text
            # column so backslash-bearing category names round-trip verbatim.
            self._cf.write(f"{aid}\t{_copy_text_escape(cat)}\n")
            self._crows += 1

    def close(self) -> None:
        self._close_current()

    def totals(self) -> tuple[int, int, int]:
        a = sum(s["rows"] for s in self.articles_shards)
        e = sum(s["rows"] for s in self.edges_shards)
        c = sum(s["rows"] for s in self.categories_shards)
        return a, e, c


def second_pass(
    dump: Path,
    out: Path,
    title_to_id: dict[str, int],
    redirects: dict[str, str],
    max_articles: int | None,
    shard_size: int,
) -> dict:
    """Stream the dump again, emitting article/edge/category shards; return metadata."""
    writer = _ShardWriter(out, shard_size)
    emitted = 0
    with _open_dump(dump) as fh:
        for page in iter_pages(fh):
            if page.ns != 0 or page.is_redirect:
                continue
            key = normalize_title(page.title)
            aid = title_to_id.get(key)
            if aid is None:
                continue  # not among the (possibly sliced) indexed articles
            plain, raw_links, raw_cats = parse_article(page.text)
            seen: set[int] = set()
            edges: list[tuple[int, int]] = []
            for raw in raw_links:
                tkey = resolve_link_target(raw)
                if not tkey:
                    continue
                dst = resolve_edge(tkey, title_to_id, redirects)
                if dst is None or dst == aid or dst in seen:
                    continue
                seen.add(dst)
                edges.append((aid, dst))
            cats = sorted(set(raw_cats))
            writer.write(
                {"id": aid, "title": page.title, "text": plain, "ts": page.timestamp},
                edges,
                cats,
            )
            emitted += 1
            if max_articles is not None and emitted >= max_articles:
                break
    writer.close()
    return {
        "articles_shards": writer.articles_shards,
        "edges_shards": writer.edges_shards,
        "categories_shards": writer.categories_shards,
        "totals": writer.totals(),
    }


def write_redirects(out: Path, redirects: dict[str, str]) -> dict:
    """Write the single redirects.tsv shard; return its {path,rows} descriptor."""
    path = out / "redirects.tsv"
    rows = 0
    with path.open("w", encoding="utf-8") as f:
        for src, dst in redirects.items():
            f.write(f"{src}\t{dst}\n")
            rows += 1
    return {"path": path.name, "rows": rows}


def build_manifest(
    dump: Path,
    max_articles: int | None,
    shard_size: int,
    pass1_stats: dict,
    pass2: dict,
    redirect_shard: dict,
) -> dict:
    """Assemble manifest.json: provenance + per-shard schema + reconciled counts."""
    n_articles, n_edges, n_categories = pass2["totals"]
    return {
        "source": "mediawiki-pages-articles",
        "dump": dump.name,
        "dump_path": str(dump),
        "extractor": "tools/wiki_extract.py",
        "extractor_version": EXTRACTOR_VERSION,
        "wikitext_stripper": f"mwparserfromhell {mwparserfromhell.__version__}",
        "created": datetime.now(timezone.utc).isoformat(),
        "edge_source": EDGE_SOURCE,
        "namespace_filter": "ns0",
        "title_normalization": "strip; '_'->' '; collapse whitespace; upper-case first char",
        "max_articles": max_articles,
        "shard_size": shard_size,
        "counts": {
            "articles": n_articles,
            "edges": n_edges,
            "categories": n_categories,
            "redirects": redirect_shard["rows"],
            "pages_scanned_pass1": pass1_stats["pages_scanned_pass1"],
        },
        "shards": {
            "articles": {
                "schema": "jsonl; one object/line: "
                '{"id": int, "title": str, "text": str, "ts": str (ISO8601)}',
                "files": pass2["articles_shards"],
            },
            "edges": {
                "schema": "tsv; no header; columns: src_id\\tdst_id "
                "(both int article ids; directed src->dst hyperlink)",
                "files": pass2["edges_shards"],
            },
            "categories": {
                "schema": "tsv; no header; columns: article_id\\tcategory "
                "(int id, normalized category title). The category text column is "
                "PG COPY FORMAT-text escaped (backslash/tab/newline) so it round-trips "
                "verbatim into `COPY article_categories FROM STDIN (FORMAT text)`.",
                "files": pass2["categories_shards"],
            },
            "redirects": {
                "schema": "tsv; no header; columns: source_title\\ttarget_title "
                "(both normalized title keys). PLAIN tsv (NOT a FORMAT-text COPY "
                "target): provenance / query-time, read back as plain tsv by "
                "tools/wiki_hotpot_link.load_title_index.",
                "files": [redirect_shard],
            },
        },
    }


def extract(
    dump: Path,
    out: Path,
    *,
    max_articles: int | None = None,
    shard_size: int = DEFAULT_SHARD_SIZE,
) -> dict:
    """Run the full two-pass extraction and write the manifest. Returns the manifest."""
    out.mkdir(parents=True, exist_ok=True)
    title_to_id, redirects, pass1_stats = first_pass(dump, max_articles)
    pass2 = second_pass(dump, out, title_to_id, redirects, max_articles, shard_size)
    redirect_shard = write_redirects(out, redirects)
    manifest = build_manifest(
        dump, max_articles, shard_size, pass1_stats, pass2, redirect_shard
    )
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Stream a MediaWiki pages-articles dump into a portable corpus."
    )
    ap.add_argument(
        "--dump", type=Path, required=True, help="path to *-pages-articles.xml[.bz2]"
    )
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    ap.add_argument(
        "--max-articles",
        type=int,
        default=None,
        help="bound work: index+emit only the first N articles (slice/testing)",
    )
    ap.add_argument(
        "--shard-size",
        type=int,
        default=DEFAULT_SHARD_SIZE,
        help=f"articles per shard file (default {DEFAULT_SHARD_SIZE})",
    )
    args = ap.parse_args(argv)
    if args.max_articles is not None and args.max_articles <= 0:
        ap.error("--max-articles must be positive")
    if args.shard_size <= 0:
        ap.error("--shard-size must be positive")

    manifest = extract(
        args.dump,
        args.out,
        max_articles=args.max_articles,
        shard_size=args.shard_size,
    )
    c = manifest["counts"]
    print(
        f"[wiki_extract] {c['articles']} articles, {c['edges']} edges, "
        f"{c['redirects']} redirects, {c['categories']} category rows "
        f"-> {args.out}/manifest.json"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
