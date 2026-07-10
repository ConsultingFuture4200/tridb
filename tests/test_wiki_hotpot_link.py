"""Tests for the HotpotQA -> full-wiki title linker — no network, no engine.

Covers the pure resolution logic (direct hit / redirect chain / miss), the
question-linking + fully_resolved coverage accounting, and a manifest round-trip
(extract a tiny in-repo dump, then rebuild the title index from its shards and link
a synthetic question against it).
"""

from __future__ import annotations

import bz2
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.wiki_hotpot_link import (  # noqa: E402
    coverage,
    link_questions,
    load_title_index,
    resolve_title,
)

# Alpha(0) links to Beta(1); Gamma(2) exists; "Gamma Redirect" -> Gamma.
FIXTURE = """<?xml version="1.0"?>
<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/" version="0.11">
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page><title>Alpha</title><ns>0</ns><id>1</id>
    <revision><timestamp>2020-01-01T00:00:00Z</timestamp>
    <text>Alpha links to [[Beta]].</text></revision></page>
  <page><title>Beta</title><ns>0</ns><id>2</id>
    <revision><timestamp>2020-02-02T00:00:00Z</timestamp>
    <text>Beta.</text></revision></page>
  <page><title>Gamma</title><ns>0</ns><id>3</id>
    <revision><timestamp>2020-03-03T00:00:00Z</timestamp>
    <text>Gamma.</text></revision></page>
  <page><title>Gamma Redirect</title><ns>0</ns><id>4</id>
    <redirect title="Gamma" />
    <revision><timestamp>2020-04-04T00:00:00Z</timestamp>
    <text>#REDIRECT [[Gamma]]</text></revision></page>
</mediawiki>
"""


# --------------------------------------------------------------------------- #
# pure resolution logic
# --------------------------------------------------------------------------- #
def test_resolve_title_direct_redirect_and_miss():
    title_to_id = {"Alpha": 0, "Beta": 1, "Gamma": 2}
    # redirect keys are normalize_title output (only the first char is upper-cased).
    redirects = {"Gamma Redirect": "Gamma", "Old alpha": "Alpha"}
    # direct hit (case-insensitive first char via normalize_title)
    assert resolve_title("alpha", title_to_id, redirects) == 0
    # through a single redirect
    assert resolve_title("Gamma Redirect", title_to_id, redirects) == 2
    # underscores + whitespace normalize to the same key
    assert resolve_title("old_alpha", title_to_id, redirects) == 0
    # absent title -> None
    assert resolve_title("Nonexistent", title_to_id, redirects) is None
    # empty -> None
    assert resolve_title("", title_to_id, redirects) is None


def test_link_questions_fully_resolved_flag():
    title_to_id = {"Alpha": 0, "Beta": 1, "Gamma": 2}
    redirects = {"Gamma Redirect": "Gamma"}
    questions = [
        # both gold titles present -> fully resolved, gold_ids in wiki space
        {
            "id": "q1",
            "question": "?",
            "answer": "a",
            "type": "bridge",
            "supporting_facts": [["Alpha", 0], ["Gamma Redirect", 1]],
        },
        # one gold title missing -> NOT fully resolved
        {
            "id": "q2",
            "question": "?",
            "answer": "b",
            "type": "comparison",
            "supporting_facts": [["Beta", 0], ["Missing Title", 0]],
        },
    ]
    linked = link_questions(questions, title_to_id, redirects)
    q1, q2 = linked
    assert q1["fully_resolved"] is True
    assert sorted(q1["gold_ids"]) == [0, 2]  # Alpha + Gamma (via redirect)
    assert q1["n_gold"] == 2 and q1["n_gold_resolved"] == 2
    assert q2["fully_resolved"] is False
    assert q2["gold_ids"] == [1]  # only Beta
    assert q2["qid"] == 1


def test_coverage_accounting():
    linked = [
        {"fully_resolved": True, "gold_ids": [0, 2], "n_gold": 2, "n_gold_resolved": 2},
        {"fully_resolved": False, "gold_ids": [1], "n_gold": 2, "n_gold_resolved": 1},
        {"fully_resolved": False, "gold_ids": [], "n_gold": 2, "n_gold_resolved": 0},
    ]
    cov = coverage(linked)
    assert cov["n_questions"] == 3
    assert cov["n_fully_resolved"] == 1
    assert cov["n_partially_resolved"] == 2  # any gold hit
    assert cov["gold_titles_total"] == 6
    assert cov["gold_titles_resolved"] == 3
    assert abs(cov["frac_gold_titles_resolved"] - 0.5) < 1e-9


# --------------------------------------------------------------------------- #
# manifest round-trip (extractor -> title index -> link)
# --------------------------------------------------------------------------- #
def test_load_title_index_roundtrip(tmp_path):
    from tools.wiki_extract import extract

    dump = tmp_path / "testwiki-pages-articles.xml.bz2"
    dump.write_bytes(bz2.compress(FIXTURE.encode("utf-8")))
    out = tmp_path / "corpus"
    extract(dump, out)

    title_to_id, redirects = load_title_index(out)
    assert title_to_id == {"Alpha": 0, "Beta": 1, "Gamma": 2}
    assert redirects == {"Gamma Redirect": "Gamma"}

    # a question whose gold titles are Alpha + (Gamma via redirect) fully resolves.
    questions = [
        {
            "id": "q",
            "question": "?",
            "answer": "a",
            "type": "bridge",
            "supporting_facts": [["Alpha", 0], ["Gamma Redirect", 2]],
        }
    ]
    linked = link_questions(questions, title_to_id, redirects)
    assert linked[0]["fully_resolved"] is True
    assert sorted(linked[0]["gold_ids"]) == [0, 2]


def test_load_title_index_matches_manifest_json(tmp_path):
    """The rebuilt id space equals the ids the extractor wrote in the shards."""
    from tools.wiki_extract import extract

    dump = tmp_path / "testwiki-pages-articles.xml"
    dump.write_text(FIXTURE, encoding="utf-8")
    out = tmp_path / "corpus"
    extract(dump, out)

    title_to_id, _ = load_title_index(out)
    manifest = json.loads((out / "manifest.json").read_text())
    written = {}
    for shard in manifest["shards"]["articles"]["files"]:
        for line in (out / shard["path"]).read_text().splitlines():
            rec = json.loads(line)
            written[rec["title"]] = rec["id"]
    # every article title maps to the same id it was written with
    assert title_to_id["Alpha"] == written["Alpha"]
    assert title_to_id["Gamma"] == written["Gamma"]
