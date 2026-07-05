"""Tests for the Phase-0 wiki extractor — no network, no large data, no engine.

Drives tools/wiki_extract.extract over a tiny in-repo MediaWiki XML fixture that
exercises every rule the fullwiki path depends on: ns0-only filtering, redirect
pages recorded (not emitted), link resolution THROUGH a redirect, red-link drops,
category harvesting, and a manifest whose counts reconcile with the shard files.
"""

from __future__ import annotations

import bz2
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.wiki_extract import (  # noqa: E402
    _copy_text_escape,
    extract,
    normalize_title,
    resolve_edge,
    resolve_link_target,
)

# A namespaced dump (like the real export-0.11 schema) so the ns-stripping path is
# exercised. Pages, in id-assignment order: Alpha(0), Beta(1), Gamma(2); then a
# redirect page, a Talk page (ns1), and a Template page (ns10) — all dropped from
# the corpus. Alpha links to: Beta (direct), "Gamma Redirect" (through a redirect
# -> Gamma), a red-link, a Category (membership, not an edge), a Template (non-ns0,
# dropped), and itself (self-loop, dropped).
FIXTURE = """<?xml version="1.0"?>
<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/" version="0.11">
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page>
    <title>Alpha</title>
    <ns>0</ns>
    <id>1</id>
    <revision>
      <timestamp>2020-01-01T00:00:00Z</timestamp>
      <text>Alpha links to [[Beta]] and [[Gamma Redirect|gamma]] and
      [[Nonexistent Page]] and itself [[Alpha]]. [[Template:Foo]].
      [[Category:Test]]</text>
    </revision>
  </page>
  <page>
    <title>Beta</title>
    <ns>0</ns>
    <id>2</id>
    <revision>
      <timestamp>2020-02-02T00:00:00Z</timestamp>
      <text>Beta refers back to [[Alpha]].</text>
    </revision>
  </page>
  <page>
    <title>Gamma</title>
    <ns>0</ns>
    <id>3</id>
    <revision>
      <timestamp>2020-03-03T00:00:00Z</timestamp>
      <text>Gamma mentions [[Beta]].</text>
    </revision>
  </page>
  <page>
    <title>Gamma Redirect</title>
    <ns>0</ns>
    <id>4</id>
    <redirect title="Gamma" />
    <revision>
      <timestamp>2020-04-04T00:00:00Z</timestamp>
      <text>#REDIRECT [[Gamma]]</text>
    </revision>
  </page>
  <page>
    <title>Talk:Alpha</title>
    <ns>1</ns>
    <id>5</id>
    <revision>
      <timestamp>2020-05-05T00:00:00Z</timestamp>
      <text>A talk page linking [[Beta]] that must be dropped.</text>
    </revision>
  </page>
  <page>
    <title>Template:Foo</title>
    <ns>10</ns>
    <id>6</id>
    <revision>
      <timestamp>2020-06-06T00:00:00Z</timestamp>
      <text>A template linking [[Alpha]] that must be dropped.</text>
    </revision>
  </page>
</mediawiki>
"""


def _write_fixture(tmp_path: Path, *, compressed: bool = False) -> Path:
    suffix = ".xml.bz2" if compressed else ".xml"
    dump = tmp_path / f"testwiki-pages-articles{suffix}"
    if compressed:
        dump.write_bytes(bz2.compress(FIXTURE.encode("utf-8")))
    else:
        dump.write_text(FIXTURE, encoding="utf-8")
    return dump


def _load(out: Path):
    manifest = json.loads((out / "manifest.json").read_text())
    articles = []
    for s in manifest["shards"]["articles"]["files"]:
        for line in (out / s["path"]).read_text().splitlines():
            articles.append(json.loads(line))
    edges = []
    for s in manifest["shards"]["edges"]["files"]:
        for line in (out / s["path"]).read_text().splitlines():
            src, dst = line.split("\t")
            edges.append((int(src), int(dst)))
    cats = []
    for s in manifest["shards"]["categories"]["files"]:
        for line in (out / s["path"]).read_text().splitlines():
            aid, cat = line.split("\t")
            cats.append((int(aid), cat))
    redirects = {}
    for line in (out / "redirects.tsv").read_text().splitlines():
        src, dst = line.split("\t")
        redirects[src] = dst
    return manifest, articles, edges, cats, redirects


def test_article_count_and_namespace_filter(tmp_path):
    out = tmp_path / "corpus"
    extract(_write_fixture(tmp_path), out)
    _, articles, _, _, _ = _load(out)
    titles = {a["title"] for a in articles}
    # Only the three ns0 non-redirect pages survive.
    assert titles == {"Alpha", "Beta", "Gamma"}
    # Non-ns0 and redirect pages are NOT corpus articles.
    assert "Talk:Alpha" not in titles
    assert "Template:Foo" not in titles
    assert "Gamma Redirect" not in titles


def test_redirect_recorded_not_emitted(tmp_path):
    out = tmp_path / "corpus"
    extract(_write_fixture(tmp_path), out)
    _, _, _, _, redirects = _load(out)
    assert redirects == {"Gamma Redirect": "Gamma"}


def test_edges_resolve_through_redirect_and_drop_redlinks(tmp_path):
    out = tmp_path / "corpus"
    extract(_write_fixture(tmp_path), out)
    _, articles, edges, _, _ = _load(out)
    tid = {a["title"]: a["id"] for a in articles}
    edge_titles = {
        (
            next(t for t, i in tid.items() if i == s),
            next(t for t, i in tid.items() if i == d),
        )
        for s, d in edges
    }
    # Alpha->Beta (direct) and Alpha->Gamma (THROUGH the "Gamma Redirect" redirect).
    assert (tid["Alpha"], tid["Beta"]) in edges
    assert (tid["Alpha"], tid["Gamma"]) in edges
    assert (tid["Beta"], tid["Alpha"]) in edges
    assert (tid["Gamma"], tid["Beta"]) in edges
    # Exactly those four: red-link, self-loop, category, and Template: are all dropped.
    assert len(edges) == 4
    assert ("Alpha", "Alpha") not in edge_titles  # self-loop dropped


def test_categories_harvested(tmp_path):
    out = tmp_path / "corpus"
    extract(_write_fixture(tmp_path), out)
    _, articles, _, cats, _ = _load(out)
    tid = {a["title"]: a["id"] for a in articles}
    assert (tid["Alpha"], "Test") in cats
    # A category is not also an edge target.
    assert len(cats) == 1


def test_manifest_counts_match_shards(tmp_path):
    out = tmp_path / "corpus"
    extract(_write_fixture(tmp_path), out)
    manifest, articles, edges, cats, redirects = _load(out)
    c = manifest["counts"]
    assert c["articles"] == len(articles) == 3
    assert c["edges"] == len(edges)
    assert c["categories"] == len(cats)
    assert c["redirects"] == len(redirects)
    # per-shard rows sum to the declared totals
    assert (
        sum(s["rows"] for s in manifest["shards"]["articles"]["files"]) == c["articles"]
    )
    assert sum(s["rows"] for s in manifest["shards"]["edges"]["files"]) == c["edges"]
    assert manifest["edge_source"] == "wikilink"
    assert manifest["namespace_filter"] == "ns0"


def test_plain_text_strips_wikitext(tmp_path):
    out = tmp_path / "corpus"
    extract(_write_fixture(tmp_path), out)
    _, articles, _, _, _ = _load(out)
    alpha = next(a for a in articles if a["title"] == "Alpha")
    # wikilink/category markup gone; link display text remains.
    assert "[[" not in alpha["text"]
    assert "Category:Test" not in alpha["text"]
    assert "Beta" in alpha["text"]


def test_bz2_streaming(tmp_path):
    out = tmp_path / "corpus"
    extract(_write_fixture(tmp_path, compressed=True), out)
    _, articles, edges, _, _ = _load(out)
    assert len(articles) == 3
    assert len(edges) == 4


def test_max_articles_bounds_output(tmp_path):
    out = tmp_path / "corpus"
    extract(_write_fixture(tmp_path), out, max_articles=2)
    manifest, articles, _, _, _ = _load(out)
    assert manifest["counts"]["articles"] == 2
    assert {a["title"] for a in articles} == {"Alpha", "Beta"}


def test_sharding_splits_files(tmp_path):
    out = tmp_path / "corpus"
    extract(_write_fixture(tmp_path), out, shard_size=2)
    manifest, articles, _, _, _ = _load(out)
    # 3 articles, shard_size 2 -> two article shard files (rows 2 and 1).
    files = manifest["shards"]["articles"]["files"]
    assert len(files) == 2
    assert sorted(s["rows"] for s in files) == [1, 2]
    assert len(articles) == 3


# --------------------------------------------------------------------------- #
# unit-level helpers
# --------------------------------------------------------------------------- #
def test_normalize_title():
    assert normalize_title("united_states") == "United states"
    assert normalize_title("  spaced   out ") == "Spaced out"
    assert normalize_title("") == ""


def test_resolve_link_target_strips_anchor_and_colon():
    assert resolve_link_target("Foo#Section") == "Foo"
    assert resolve_link_target(":Category:Bar") == "Category:Bar"


def test_resolve_edge_follows_chain():
    tid = {"Target": 7}
    redirs = {"R1": "R2", "R2": "Target"}
    assert resolve_edge("R1", tid, redirs) == 7
    assert resolve_edge("Missing", tid, redirs) is None


def test_copy_text_escape_roundtrips_backslash():
    # PG COPY FORMAT-text semantics: backslash is the escape metachar. A raw
    # backslash must be doubled or PG drops it / aborts the stream.
    assert _copy_text_escape("C\\C++") == "C\\\\C++"
    assert _copy_text_escape("a\tb\nc") == "a\\tb\\nc"
    assert _copy_text_escape("plain") == "plain"


# A backslash-bearing category (legal MediaWiki title char) must be written to
# categories-*.tsv in escaped form so a `COPY ... (FORMAT text)` load round-trips it
# verbatim instead of silently corrupting it.
_BACKSLASH_FIXTURE = """<?xml version="1.0"?>
<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/" version="0.11">
  <page>
    <title>Alpha</title>
    <ns>0</ns>
    <id>1</id>
    <revision>
      <timestamp>2020-01-01T00:00:00Z</timestamp>
      <text>Body. [[Category:C\\C++ stuff]]</text>
    </revision>
  </page>
</mediawiki>
"""


def _pg_text_unescape(field: str) -> str:
    """Minimal PG COPY FORMAT-text field decoder (backslash escapes) for the test."""
    out, it = [], iter(field)
    for ch in it:
        if ch != "\\":
            out.append(ch)
            continue
        nxt = next(it)
        out.append({"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}.get(nxt, nxt))
    return "".join(out)


def test_category_backslash_escaped_for_copy(tmp_path):
    dump = tmp_path / "bs-pages-articles.xml"
    dump.write_text(_BACKSLASH_FIXTURE, encoding="utf-8")
    out = tmp_path / "corpus"
    extract(dump, out)
    raw_lines = (out / "categories-00000.tsv").read_text().splitlines()
    assert len(raw_lines) == 1
    aid, escaped_cat = raw_lines[0].split("\t")
    # stored escaped (doubled backslash), but decodes back to the true category name
    assert "\\\\" in escaped_cat
    assert _pg_text_unescape(escaped_cat) == "C\\C++ stuff"
