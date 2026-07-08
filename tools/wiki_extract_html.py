"""Stream the Wikimedia Enterprise HTML dump into a FORMATTING-PRESERVING corpus.

This is the structure-preserving successor to tools/wiki_extract.py. That extractor
consumes the wikitext `pages-articles.xml.bz2` dump and flattens each article to plain
`{id,title,text,ts}` — every heading, list, table, infobox and image is thrown away.
Downstream (the reader, GraphRAG) increasingly wants the real article STRUCTURE, which
only exists as rendered HTML. The Wikimedia *Enterprise HTML* dump ships pre-rendered
Parsoid HTML per article (real infoboxes/tables/figures) — the only source that yields
them without a Parsoid-grade template expander.

SOURCE (--source): the Enterprise dump `enwiki-NS0-<DATE>-ENTERPRISE-HTML.json.tar.gz`,
a gzipped tar of a handful of `enwiki_namespace_0_N.ndjson` members. Each NDJSON line is
one article object: {name, identifier, namespace:{identifier:0}, date_modified,
article_body:{html, wikitext}, categories:[{name,url}], ...}. Redirect *pages* are NOT
in this dump and there is no incoming-redirect field, so a redirect map cannot be
harvested here — pass one in via --redirects (reuse the proven wikitext-derived
redirects.tsv) to keep edge resolution honest; without it, links whose target is a
redirect simply drop (documented loss).

We stream the tar TWICE (never materializing a member in RAM, bounded to one line):
    pass 1 : assign every ns0 article a sequential id in encounter order, building
             {normalized title -> id}. (Mirrors wiki_extract.py's dense-id contract so
             existing id-indexed consumers keep working.) Persisted to _work/ so a
             restart skips it.
    pass 2 : re-stream; for each article sanitize its HTML (keep structural tags, strip
             head/script/style/nav/edit cruft), derive a plain-text fallback, resolve
             its in-article <a href="./Target"> links through title_to_id (+ redirects)
             into directed id->id edges, and harvest categories.

RESUMABILITY / FIX #4 BY CONSTRUCTION: pass 2 writes ONE shard-set per input member and
records the member as complete in a checkpoint only after it is fully written. A
completed member's shard files are NEVER reopened — the old sharded writer's bug (it
reopened articles-00028/49/71 in truncate mode and clobbered ~289k articles) is
structurally impossible here: every shard is written exactly once, by exactly one
member, in one open. A crashed member's partial shard is simply rewritten from scratch
on resume. The runner then runs tools/wiki_manifest_verify.py as an independent
files-vs-manifest gate.

OUTPUT (--out DIR), COPY-friendly, same shapes as tools/wiki_extract.py plus `html`:
    articles-NNNNN.jsonl   {"id","title","html","text","ts"} — html = sanitized
                           structural HTML (NEW); text = plain-text fallback so the
                           existing embed/index paths keep working unchanged.
    edges-NNNNN.tsv        "src_id\\tdst_id" — redirect-resolved directed hyperlinks.
    categories-NNNNN.tsv   "article_id\\tcategory" — PG FORMAT-text escaped.
    redirects.tsv          "source_title\\ttarget_title" — the --redirects map, copied
                           in verbatim for provenance (empty if none supplied).
    manifest.json          provenance (source dump + date + params) + per-shard schema.

NNNNN is the member's stream position, so shard files are not id-contiguous (verify /
the loader glob-and-count every shard, so this is irrelevant to them).

CLI:
    python -m tools.wiki_extract_html --source <dump.tar.gz> --out <dir> \
        [--redirects redirects.tsv] [--max-articles N]
"""

from __future__ import annotations

import argparse
import json
import re
import tarfile
import urllib.parse
from datetime import datetime, timezone
from html import escape as _hesc
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

EXTRACTOR_VERSION = "0.1.0"
EDGE_SOURCE = "wikilink-html"
_MAX_REDIRECT_HOPS = 8
_WS = re.compile(r"\s+")
_MULTINL = re.compile(r"\n{3,}")


# --- title / edge helpers (copied from tools.wiki_extract to stay stdlib-only; that
# --- module imports mwparserfromhell at top level and we deliberately avoid the dep) --

def normalize_title(title: str) -> str:
    """MediaWiki-faithful title key: strip, '_'->' ', collapse ws, upper-case first."""
    t = _WS.sub(" ", title.replace("_", " ")).strip()
    if not t:
        return ""
    return t[0].upper() + t[1:]


def _copy_text_escape(field: str) -> str:
    """Escape a text field so it round-trips through `COPY ... (FORMAT text)`."""
    return (
        field.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _category_key(raw: str) -> str | None:
    """Normalize an Enterprise category `name` ('Category:Foo') to 'Foo' key, else None."""
    head, sep, tail = raw.strip().partition(":")
    if not sep or head.strip().lower() != "category":
        return None
    return normalize_title(tail.split("#", 1)[0]) or None


def resolve_edge(
    key: str, title_to_id: dict[str, int], redirects: dict[str, str]
) -> int | None:
    """Resolve a normalized link target to an article id, following redirects."""
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


# --------------------------------- HTML sanitizer --------------------------------------
# Emit a small structural subset; drop everything else. Unknown tags are TRANSPARENT
# (tag dropped, children kept) so Parsoid's <section>/<div> wrappers unwrap cleanly.

_VOID = {"img", "br", "hr", "col", "wbr"}
_ALLOWED = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "b", "strong", "i", "em", "blockquote", "figure", "figcaption", "img", "a",
    "sup", "sub", "br", "hr", "abbr", "code", "pre", "cite", "span", "mark", "small",
    "u", "s", "time", "ins", "del", "kbd", "samp", "var",
}
# Drop the element AND its subtree entirely.
_DROP = {
    "head", "script", "style", "link", "meta", "title", "base", "noscript",
    "nav", "aside", "form", "button", "input", "iframe", "object", "embed",
    "audio", "video", "map", "svg",
}
# Elements whose class marks them as chrome/maintenance cruft -> drop subtree.
_CRUFT_CLASS = (
    "mw-editsection", "navbox", "noprint", "ambox", "metadata", "mbox-",
    "mw-empty-elt", "mw-indicators", "shortdescription", "printfooter",
    "catlinks", "mw-jump-link", "vector-", "sistersitebox", "navbar",
)
# Tags we preserve a (single) class attribute on — lets the reader style infoboxes,
# wikitables and the references list without us keeping Parsoid's id/data-mw noise.
_KEEP_CLASS = {
    "table", "td", "th", "tr", "caption", "figure", "figcaption",
    "span", "ol", "ul", "blockquote", "sup", "div",
}
# block tags that get a newline in the plain-text projection
_TEXT_BLOCK = {
    "p", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "table", "caption", "figcaption", "dd", "dt", "div", "br", "hr",
}


class _Sanitizer(HTMLParser):
    """Parse a full Parsoid HTML doc; emit a sanitized <body> fragment + link targets."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.text: list[str] = []
        self.links: list[str] = []
        self.in_body = False
        self.skip = 0  # nesting depth inside a dropped subtree (>0 => suppress)

    # -- decide fate of a start tag --------------------------------------------------
    def _is_cruft(self, attrs_d: dict[str, str]) -> bool:
        cls = attrs_d.get("class", "")
        return bool(cls) and any(c in cls for c in _CRUFT_CLASS)

    def _filter_attrs(self, tag: str, attrs_d: dict[str, str]) -> str:
        out: list[tuple[str, str]] = []
        if tag == "a":
            href = attrs_d.get("href", "") or ""
            if href.startswith("./"):
                t = urllib.parse.unquote(href[2:]).split("#", 1)[0].replace("_", " ")
                t = t.strip()
                if t:
                    out.append(("data-wiki-title", t))
                    self.links.append(t)
        elif tag == "img":
            src = attrs_d.get("src") or attrs_d.get("data-src")
            if src:
                out.append(("data-src", src))
            for a in ("alt", "width", "height"):
                v = attrs_d.get(a)
                if v:
                    out.append(("data-" + a, v))
        else:
            if tag in _KEEP_CLASS and attrs_d.get("class"):
                out.append(("class", attrs_d["class"]))
            for a in ("colspan", "rowspan"):
                if attrs_d.get(a):
                    out.append((a, attrs_d[a]))
        return "".join(f' {k}="{_hesc(v, quote=True)}"' for k, v in out)

    def _emit_start(self, tag: str, attrs, self_close: bool) -> None:
        d = {k: (v or "") for k, v in attrs}
        # already inside a dropped subtree: just track nesting
        if self.skip:
            if tag not in _VOID and not self_close:
                self.skip += 1
            return
        if not self.in_body:
            if tag == "body":
                self.in_body = True
            return
        if tag in _DROP or self._is_cruft(d):
            if tag not in _VOID and not self_close:
                self.skip = 1
            return
        if tag in _ALLOWED:
            frag = "<" + tag + self._filter_attrs(tag, d)
            if tag in _VOID or self_close:
                self.out.append(frag + "/>")
            else:
                self.out.append(frag + ">")
            if tag in _TEXT_BLOCK:
                self.text.append("\n")
        # else: transparent (drop tag, keep children)

    def handle_starttag(self, tag, attrs):
        self._emit_start(tag, attrs, self_close=False)

    def handle_startendtag(self, tag, attrs):
        self._emit_start(tag, attrs, self_close=True)

    def handle_endtag(self, tag):
        if self.skip:
            if tag not in _VOID:
                self.skip -= 1
            return
        if not self.in_body:
            return
        if tag == "body":
            self.in_body = False
            return
        if tag in _ALLOWED and tag not in _VOID:
            self.out.append("</" + tag + ">")
            if tag in _TEXT_BLOCK:
                self.text.append("\n")

    def handle_data(self, data):
        if self.in_body and not self.skip:
            self.out.append(_hesc(data, quote=False))
            self.text.append(data)


def sanitize(html: str) -> tuple[str, str, list[str]]:
    """Return (sanitized_html_fragment, plain_text, link_target_titles)."""
    p = _Sanitizer()
    try:
        p.feed(html)
        p.close()
    except Exception:
        # malformed tail: keep whatever was emitted before the error
        pass
    frag = "".join(p.out).strip()
    text = _MULTINL.sub("\n\n", "".join(p.text)).strip()
    return frag, text, p.links


# ------------------------------- dump streaming ----------------------------------------

def _iter_members(
    source: Path, warnings: list | None = None
) -> Iterator[tuple[int, str, "tarfile.ExFileObject | None"]]:
    """Yield (member_index, member_name, fileobj) for each ns0 NDJSON member, in order.

    Streaming tar (r|gz): members arrive in archive order, memory bounded to one member's
    header. A member the caller does not read is skipped by the tar reader on the next
    step. member_index (stream position over NDJSON members) is the stable shard id.

    A truncated/corrupt archive tail (short download) is caught and stops iteration
    cleanly, appending "truncated" to `warnings` so callers can flag it — the runner is
    what guarantees a complete download (Content-Length check); here we fail soft, not
    with an opaque crash.
    """
    mode = "r|gz" if source.suffix == ".gz" or ".tar.gz" in source.name else "r|"
    tar = tarfile.open(str(source), mode=mode)
    idx = 0
    try:
        while True:
            try:
                member = tar.next()
            except (tarfile.ReadError, EOFError):
                if warnings is not None:
                    warnings.append("truncated")
                break
            if member is None:
                break
            if not member.isfile() or not member.name.endswith(".ndjson"):
                continue
            yield idx, member.name, tar.extractfile(member)
            idx += 1
    finally:
        tar.close()


def _iter_json_lines(fh, warnings: list | None = None) -> Iterator[dict]:
    """Yield parsed JSON objects from an NDJSON member, tolerating a truncated tail.

    Both a partial final JSON line and a truncated member body (a short download raises
    tarfile.ReadError/EOFError as the ExFileObject runs off the end) stop the member
    cleanly; `warnings` records the hard-truncation case so the manifest can flag it.
    """
    if fh is None:
        return
    it = iter(fh)
    while True:
        try:
            line = next(it)
        except StopIteration:
            break
        except (tarfile.ReadError, EOFError, OSError):
            if warnings is not None:
                warnings.append("truncated")
            break
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # truncated final line (partial download / cut member): stop this member
            if warnings is not None:
                warnings.append("truncated")
            break


def _is_ns0_article(obj: dict) -> bool:
    ns = obj.get("namespace")
    return isinstance(ns, dict) and ns.get("identifier") == 0 and bool(obj.get("name"))


# ------------------------------------ passes -------------------------------------------

def load_redirects(path: Path | None) -> dict[str, str]:
    """Load a source_title\\ttarget_title redirects.tsv into a normalized dict."""
    redirects: dict[str, str] = {}
    if path is None:
        return redirects
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 2:
                continue
            src, dst = normalize_title(parts[0]), normalize_title(parts[1])
            if src and dst:
                redirects[src] = dst
    return redirects


def first_pass(source: Path, work: Path, max_articles: int | None) -> dict[str, int]:
    """Assign dense sequential ids to ns0 articles in encounter order; persist the map.

    Resumable: if _work/pass1.done exists, load and return the persisted map instead of
    re-streaming the (large) dump.
    """
    done = work / "pass1.done"
    map_path = work / "title_to_id.tsv"
    if done.exists() and map_path.exists():
        title_to_id: dict[str, int] = {}
        with map_path.open("r", encoding="utf-8") as f:
            for line in f:
                t, i = line.rstrip("\n").split("\t")
                title_to_id[t] = int(i)
        return title_to_id

    work.mkdir(parents=True, exist_ok=True)
    title_to_id = {}
    for _idx, _name, fh in _iter_members(source, warnings=[]):
        for obj in _iter_json_lines(fh):
            if not _is_ns0_article(obj):
                continue
            key = normalize_title(obj["name"])
            if not key or key in title_to_id:
                continue
            title_to_id[key] = len(title_to_id)
            if max_articles is not None and len(title_to_id) >= max_articles:
                break
        if max_articles is not None and len(title_to_id) >= max_articles:
            break

    tmp = map_path.with_suffix(".tsv.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for t, i in title_to_id.items():
            f.write(f"{t}\t{i}\n")
    tmp.replace(map_path)
    done.write_text(datetime.now(timezone.utc).isoformat())
    return title_to_id


def _load_checkpoint(work: Path) -> dict:
    cp = work / "pass2_checkpoint.json"
    if cp.exists():
        return json.loads(cp.read_text())
    return {"members": {}}


def _save_checkpoint(work: Path, ckpt: dict) -> None:
    cp = work / "pass2_checkpoint.json"
    tmp = cp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ckpt, ensure_ascii=False))
    tmp.replace(cp)


def second_pass(
    source: Path,
    out: Path,
    work: Path,
    title_to_id: dict[str, int],
    redirects: dict[str, str],
    max_articles: int | None,
) -> dict:
    """Stream the dump again; write one shard-set per member; checkpoint per member.

    Fix #4 by construction: a member's three shard files are opened once, written, then
    the member is recorded complete. Completed members are skipped on resume and their
    shards are never touched again. A partially-written (crashed) member is simply
    re-processed, overwriting its own shards from scratch.
    """
    out.mkdir(parents=True, exist_ok=True)
    ckpt = _load_checkpoint(work)
    emitted_total = 0
    warnings: list = []

    for idx, name, fh in _iter_members(source, warnings=warnings):
        key = str(idx)
        if key in ckpt["members"]:
            # already done in a previous run; tar reader skips the member body
            continue
        ap = out / f"articles-{idx:05d}.jsonl"
        ep = out / f"edges-{idx:05d}.tsv"
        cp = out / f"categories-{idx:05d}.tsv"
        arows = erows = crows = 0
        with ap.open("w", encoding="utf-8") as af, \
             ep.open("w", encoding="utf-8") as ef, \
             cp.open("w", encoding="utf-8") as cf:
            for obj in _iter_json_lines(fh, warnings):
                if not _is_ns0_article(obj):
                    continue
                title = obj["name"]
                aid = title_to_id.get(normalize_title(title))
                if aid is None:
                    continue  # not indexed (sliced run)
                body = obj.get("article_body") or {}
                html_frag, text, link_titles = sanitize(body.get("html", "") or "")
                # edges: resolve in-article links -> ids, dedup, no self-loop
                seen: set[int] = set()
                for lt in link_titles:
                    dst = resolve_edge(normalize_title(lt), title_to_id, redirects)
                    if dst is None or dst == aid or dst in seen:
                        continue
                    seen.add(dst)
                    ef.write(f"{aid}\t{dst}\n")
                    erows += 1
                # categories
                for c in obj.get("categories") or []:
                    ck = _category_key(c.get("name", "") if isinstance(c, dict) else "")
                    if ck:
                        cf.write(f"{aid}\t{_copy_text_escape(ck)}\n")
                        crows += 1
                af.write(json.dumps(
                    {
                        "id": aid,
                        "title": title,
                        "html": html_frag,
                        "text": text,
                        "ts": obj.get("date_modified", "") or "",
                    },
                    ensure_ascii=False,
                ) + "\n")
                arows += 1
                emitted_total += 1
                if max_articles is not None and emitted_total >= max_articles:
                    break
        member_complete = not (max_articles is not None and emitted_total >= max_articles)
        if member_complete:
            ckpt["members"][key] = {
                "name": name, "articles": arows, "edges": erows, "categories": crows,
            }
            _save_checkpoint(work, ckpt)
        if max_articles is not None and emitted_total >= max_articles:
            # partial member on a sliced/test run: still surface its counts for manifest
            ckpt["members"].setdefault(key, {
                "name": name, "articles": arows, "edges": erows, "categories": crows,
            })
            break

    ckpt["source_truncated"] = "truncated" in warnings
    _save_checkpoint(work, ckpt)
    return ckpt


def write_redirects(out: Path, redirects: dict[str, str]) -> dict:
    """Copy the (reused) redirect map into the corpus for provenance/self-containment."""
    path = out / "redirects.tsv"
    rows = 0
    with path.open("w", encoding="utf-8") as f:
        for src, dst in redirects.items():
            f.write(f"{src}\t{dst}\n")
            rows += 1
    return {"path": path.name, "rows": rows}


def build_manifest(
    source: Path,
    max_articles: int | None,
    redirects_src: Path | None,
    ckpt: dict,
    redirect_shard: dict,
) -> dict:
    """Assemble manifest.json: provenance + per-shard schema (from the checkpoint)."""
    members = sorted(ckpt["members"].items(), key=lambda kv: int(kv[0]))
    art_files = [{"path": f"articles-{int(k):05d}.jsonl", "rows": v["articles"]}
                 for k, v in members]
    edge_files = [{"path": f"edges-{int(k):05d}.tsv", "rows": v["edges"]}
                  for k, v in members]
    cat_files = [{"path": f"categories-{int(k):05d}.tsv", "rows": v["categories"]}
                 for k, v in members]
    n_art = sum(f["rows"] for f in art_files)
    n_edge = sum(f["rows"] for f in edge_files)
    n_cat = sum(f["rows"] for f in cat_files)
    # source date from the enterprise filename enwiki-NS0-<DATE>-ENTERPRISE-HTML...
    m = re.search(r"-NS0-(\d{8})-", source.name)
    return {
        "source": "wikimedia-enterprise-html",
        "dump": source.name,
        "dump_path": str(source),
        "dump_date": m.group(1) if m else None,
        "extractor": "tools/wiki_extract_html.py",
        "extractor_version": EXTRACTOR_VERSION,
        "html_sanitizer": "stdlib html.parser structural allowlist",
        "created": datetime.now(timezone.utc).isoformat(),
        "edge_source": EDGE_SOURCE,
        "namespace_filter": "ns0",
        "title_normalization": "strip; '_'->' '; collapse whitespace; upper-case first char",
        "redirects_source": str(redirects_src) if redirects_src else None,
        "redirects_note": (
            "Enterprise HTML has no redirect pages/field; this map was reused from the "
            "wikitext-derived redirects.tsv to resolve link targets. Without it, links "
            "to redirect titles drop." if redirects_src else
            "NO redirect map supplied: links whose target is a redirect are dropped."
        ),
        "max_articles": max_articles,
        "source_truncated": bool(ckpt.get("source_truncated")),
        "members_processed": len(ckpt.get("members", {})),
        "shard_scheme": "one shard-set per input NDJSON member (NNNNN = member index)",
        "counts": {
            "articles": n_art,
            "edges": n_edge,
            "categories": n_cat,
            "redirects": redirect_shard["rows"],
        },
        "shards": {
            "articles": {
                "schema": "jsonl; one object/line: "
                '{"id": int, "title": str, "html": str (sanitized structural HTML), '
                '"text": str (plain-text fallback), "ts": str (ISO8601)}',
                "files": art_files,
            },
            "edges": {
                "schema": "tsv; no header; columns: src_id\\tdst_id "
                "(both int article ids; directed src->dst hyperlink)",
                "files": edge_files,
            },
            "categories": {
                "schema": "tsv; no header; columns: article_id\\tcategory "
                "(int id, normalized category title). Category text column is PG COPY "
                "FORMAT-text escaped (backslash/tab/newline) so it round-trips verbatim.",
                "files": cat_files,
            },
            "redirects": {
                "schema": "tsv; no header; columns: source_title\\ttarget_title "
                "(both normalized title keys). PLAIN tsv (NOT a FORMAT-text COPY target).",
                "files": [redirect_shard],
            },
        },
    }


def extract(
    source: Path,
    out: Path,
    *,
    redirects_path: Path | None = None,
    max_articles: int | None = None,
    work: Path | None = None,
) -> dict:
    """Run the full two-pass extraction and write the manifest. Returns the manifest."""
    out.mkdir(parents=True, exist_ok=True)
    work = work or (out / "_work")
    work.mkdir(parents=True, exist_ok=True)

    redirects = load_redirects(redirects_path)
    title_to_id = first_pass(source, work, max_articles)
    ckpt = second_pass(source, out, work, title_to_id, redirects, max_articles)
    redirect_shard = write_redirects(out, redirects)
    manifest = build_manifest(source, max_articles, redirects_path, ckpt, redirect_shard)
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Stream the Wikimedia Enterprise HTML dump into a formatting-"
        "preserving, resumable corpus (structural HTML + plain-text fallback)."
    )
    ap.add_argument(
        "--source", type=Path, required=True,
        help="enwiki-NS0-<DATE>-ENTERPRISE-HTML.json.tar.gz (streamed, never RAM-loaded)",
    )
    ap.add_argument("--out", type=Path, required=True, help="output corpus directory")
    ap.add_argument(
        "--redirects", type=Path, default=None,
        help="reuse a source->target redirects.tsv for edge resolution "
        "(Enterprise HTML has none of its own)",
    )
    ap.add_argument(
        "--max-articles", type=int, default=None,
        help="bound work: index+emit only the first N ns0 articles (slice/testing)",
    )
    ap.add_argument(
        "--work", type=Path, default=None,
        help="scratch dir for pass-1 map + resume checkpoint (default <out>/_work)",
    )
    args = ap.parse_args(argv)
    if args.max_articles is not None and args.max_articles <= 0:
        ap.error("--max-articles must be positive")

    manifest = extract(
        args.source,
        args.out,
        redirects_path=args.redirects,
        max_articles=args.max_articles,
        work=args.work,
    )
    c = manifest["counts"]
    print(
        f"[wiki_extract_html] {c['articles']} articles, {c['edges']} edges, "
        f"{c['categories']} category rows, {c['redirects']} redirects "
        f"-> {args.out}/manifest.json"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
