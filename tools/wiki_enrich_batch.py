"""BATCH structured-facts + infobox attribute-gap enrichment over the re-extract corpus.

This is the overnight "add the others to the 2 AM job" stage. It runs AFTER
tools/wiki_extract_html.py has produced data/wiki/enwiki_html/ (structural HTML per
article, incl. `<table class="infobox ...">`) and its manifest-verify gate has passed.
It is a purely MECHANICAL parse — no corpus-wide LLM — so it fits an unattended batch:

  1. Infobox -> structured facts. Stream every articles-NNNNN.jsonl shard, parse each
     `<table class="infobox...">` in the sanitized `html` field into
     (subject_id, property, value) rows. This is the KB foundation.

  2. Infobox attribute-gap suggestions (cross-reference, mechanical). Wikipedia
     infoboxes carry directed links: Michelle Obama's infobox row "Spouse -> [[Barack
     Obama]]" is a fact B holds ABOUT A. If A's own infobox has no such property, that
     is a gap a neighbour already fills. We emit it as a PENDING suggestion for review
     (never auto-applied): attr_suggestions(subject_id=A, property, value=<B's title>,
     source_id=B, confidence).

     The neighbour signal is the infobox value's own `data-wiki-title` hyperlink — which
     IS a graph edge (the extractor derives edges-*.tsv from exactly these in-article
     links). Requiring an *exact* linked subject match (not fuzzy text) is what keeps
     this precise/conservative, and lets us avoid a second scan of the 224M-row edge
     shards: an infobox value that links to A is, by construction, a B->A edge.

     Two conservatism guards keep this precise instead of a flood of nonsense: (a) exact
     linked-subject match only, and (b) we only cross-reference RECIPROCAL-SYMMETRIC
     person relations (spouse / sibling / partner / relatives / ...), because only for
     those does "B's value about A" soundly imply "A's value about B". Asymmetric
     properties (political party, employer, occupation, birthplace, parent/child) would
     each spawn a garbage reciprocal for every article they point at, so they are
     intentionally NOT suggested. Everything is PENDING review — never auto-applied.

Both outputs go to the SHARED overlay DB data/wiki/enrich_overlay.db in tables the reader
does NOT use (the reader owns `facts`; we own infobox_facts / attr_suggestions / the
support tables article_titles / value_links / enrich_meta). We only CREATE ... IF NOT
EXISTS — we never touch another owner's tables.

RESUMABLE / BOUNDED RAM: state lives in SQLite, not in a giant Python dict. Each shard is
processed in one transaction and marked done in enrich_meta only after commit; a shard
that is not marked done is re-processed after first DELETE-ing its own rows (keyed by a
`shard` column), so a crash mid-shard is idempotent. The cross-reference pass (phase B)
is one set-based SQL statement, re-runnable in full. A completion sentinel with counts is
written at the end.

CLI:
    python -m tools.wiki_enrich_batch --corpus data/wiki/enwiki_html \
        --overlay data/wiki/enrich_overlay.db [--sentinel /tmp/wiki_enrich.done]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

# Reuse the extractor's title key so our link resolution matches its edge resolution.
from tools.wiki_extract_html import normalize_title

ENRICH_VERSION = "0.1.0"
_WS = re.compile(r"\s+")
_INFOBOX_TABLE = re.compile(r"\binfobox")  # class token on a <table>: "infobox[...]"

# Reciprocal-symmetric relations: B's infobox value "P = A" soundly implies "A: P = B".
# Cross-referencing is RESTRICTED to these — asymmetric properties (party, employer,
# occupation, parent/child, ...) would emit a garbage reciprocal for every link, so we
# never suggest them. Confidence 0.85 (mechanical, exact-link, pending review).
_RECIPROCAL_PROPS = frozenset(
    {
        "spouse",
        "spouse(s)",
        "sibling",
        "siblings",
        "sibling(s)",
        "partner",
        "partner(s)",
        "relative",
        "relatives",
        "domestic partner",
    }
)
_RECIPROCAL_CONF = 0.85

_MAX_PROP_LEN = 60
_MAX_VALUE_LEN = 1000


# --------------------------------- infobox parsing -------------------------------------


class _InfoboxParser(HTMLParser):
    """Extract (label, value_text, value_link_titles) rows from every infobox table.

    Operates on the sanitizer's output (tools/wiki_extract_html.sanitize): tables/rows/
    cells keep their `class`, and links are `<a data-wiki-title="Target">`. We treat any
    <table> whose class matches /\\binfobox/ as an infobox and collect its th/td rows.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self._table_depth = 0
        self._infobox_at: int | None = None  # table_depth where the infobox opened
        self._row: list[dict] | None = None
        self._cell: dict | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        d = {k: (v or "") for k, v in attrs}
        if tag == "table":
            self._table_depth += 1
            if self._infobox_at is None and _INFOBOX_TABLE.search(d.get("class", "")):
                self._infobox_at = self._table_depth
            return
        if self._infobox_at is None:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("th", "td") and self._row is not None:
            self._cell = {
                "tag": tag,
                "class": d.get("class", ""),
                "text": [],
                "links": [],
            }
        elif tag == "a" and self._cell is not None:
            t = d.get("data-wiki-title")
            if t:
                self._cell["links"].append(t)

    def handle_startendtag(self, tag: str, attrs) -> None:
        # self-closing <a/> is unlikely, but treat void/self-close uniformly
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("th", "td") and self._cell is not None:
            self._row.append(self._cell)  # type: ignore[union-attr]
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            if self._infobox_at is not None and self._table_depth == self._infobox_at:
                self._infobox_at = None
            self._table_depth -= 1


def _clean(text: str, cap: int) -> str:
    return _WS.sub(" ", text).strip()[:cap]


def _normalize_prop(label: str) -> str | None:
    p = _WS.sub(" ", label).strip().rstrip(":").strip().lower()
    if not p or len(p) > _MAX_PROP_LEN:
        return None
    return p


def parse_infobox_facts(html: str) -> Iterator[tuple[str, str, list[str]]]:
    """Yield (property, value, link_titles) for each label/value infobox row.

    A row emits a fact only when it has a label cell (a <th>, or a cell whose class marks
    it a label) AND at least one value cell — so section-header rows (label only) and the
    title/above row drop out. Robust to both th+td and td.infobox-label/td.infobox-data.
    """
    if "infobox" not in html:
        return
    p = _InfoboxParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass  # malformed tail: keep whatever rows were completed
    for row in p.rows:
        label_idx = next(
            (i for i, c in enumerate(row) if c["tag"] == "th" or "label" in c["class"]),
            None,
        )
        if label_idx is None:
            continue
        prop = _normalize_prop("".join(row[label_idx]["text"]))
        if not prop:
            continue
        vcells = [
            c
            for j, c in enumerate(row)
            if j != label_idx and (c["tag"] == "td" or "data" in c["class"])
        ]
        if not vcells:
            continue
        value = _clean(" ".join("".join(c["text"]) for c in vcells), _MAX_VALUE_LEN)
        links = [lt for c in vcells for lt in c["links"]]
        if not value and not links:
            continue
        yield prop, value, links


# ------------------------------------- storage -----------------------------------------


def _connect(overlay: Path) -> sqlite3.Connection:
    overlay.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(overlay))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS infobox_facts (
            subject_id INTEGER NOT NULL,
            property   TEXT    NOT NULL,
            value      TEXT,
            shard      INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS article_titles (
            id         INTEGER PRIMARY KEY,
            title      TEXT,
            norm_title TEXT,
            shard      INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS value_links (
            target_norm TEXT    NOT NULL,
            property    TEXT    NOT NULL,
            source_id   INTEGER NOT NULL,
            shard       INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attr_suggestions (
            subject_id INTEGER NOT NULL,
            property   TEXT    NOT NULL,
            value      TEXT,
            source_id  INTEGER NOT NULL,
            confidence REAL,
            UNIQUE (subject_id, property, source_id)
        );
        CREATE TABLE IF NOT EXISTS enrich_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()
    return conn


def _shard_done(conn: sqlite3.Connection, shard: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM enrich_meta WHERE key = ?", (f"shard:{shard}",)
    ).fetchone()
    return row is not None


def _iter_articles(shard_path: Path) -> Iterator[dict]:
    with shard_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _shard_index(name: str) -> int:
    m = re.search(r"articles-(\d+)\.jsonl$", name)
    return int(m.group(1)) if m else -1


def process_shard(
    conn: sqlite3.Connection, shard_path: Path, shard: int
) -> tuple[int, int, int]:
    """Parse one article shard into infobox_facts / article_titles / value_links.

    Idempotent: clears this shard's own rows first (a crashed prior attempt), then
    re-inserts and marks the shard done in one transaction. Returns
    (n_articles, n_facts, n_value_links).
    """
    conn.execute("DELETE FROM infobox_facts WHERE shard = ?", (shard,))
    conn.execute("DELETE FROM article_titles WHERE shard = ?", (shard,))
    conn.execute("DELETE FROM value_links WHERE shard = ?", (shard,))

    titles: list[tuple] = []
    facts: list[tuple] = []
    vlinks: list[tuple] = []
    n_art = n_fact = n_vl = 0

    def flush() -> None:
        if titles:
            conn.executemany(
                "INSERT OR REPLACE INTO article_titles(id,title,norm_title,shard) "
                "VALUES (?,?,?,?)",
                titles,
            )
            titles.clear()
        if facts:
            conn.executemany(
                "INSERT INTO infobox_facts(subject_id,property,value,shard) "
                "VALUES (?,?,?,?)",
                facts,
            )
            facts.clear()
        if vlinks:
            conn.executemany(
                "INSERT INTO value_links(target_norm,property,source_id,shard) "
                "VALUES (?,?,?,?)",
                vlinks,
            )
            vlinks.clear()

    for obj in _iter_articles(shard_path):
        aid = obj.get("id")
        if aid is None:
            continue
        title = obj.get("title", "") or ""
        titles.append((aid, title, normalize_title(title), shard))
        n_art += 1
        for prop, value, links in parse_infobox_facts(obj.get("html", "") or ""):
            facts.append((aid, prop, value, shard))
            n_fact += 1
            # value_links only feeds reciprocal-property gap suggestions; recording links
            # for asymmetric properties would bloat the shared DB with unused rows.
            if prop in _RECIPROCAL_PROPS:
                for lt in links:
                    tn = normalize_title(lt)
                    if tn:
                        vlinks.append((tn, prop, aid, shard))
                        n_vl += 1
        if len(titles) + len(facts) + len(vlinks) >= 20000:
            flush()

    flush()
    conn.execute(
        "INSERT OR REPLACE INTO enrich_meta(key,value) VALUES (?,?)",
        (
            f"shard:{shard}",
            json.dumps({"articles": n_art, "facts": n_fact, "value_links": n_vl}),
        ),
    )
    conn.commit()
    return n_art, n_fact, n_vl


def build_suggestions(conn: sqlite3.Connection) -> int:
    """Phase B: mechanical infobox attribute-gap suggestions (one set-based pass).

    For every RECIPROCAL-symmetric infobox value link (B, property P, target -> A) where
    A's own infobox has NO property P, emit a PENDING suggestion A.P = <B's title>,
    sourced to B. Exact linked subject match only; asymmetric properties are excluded.
    Full re-run each invocation (idempotent).
    """
    conn.execute("CREATE INDEX IF NOT EXISTS ix_at_norm ON article_titles(norm_title)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_vl_target ON value_links(target_norm)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_if_subj_prop ON infobox_facts(subject_id,property)"
    )
    conn.execute("DELETE FROM attr_suggestions")
    conn.execute(
        """
        INSERT OR IGNORE INTO attr_suggestions(subject_id,property,value,source_id,confidence)
        SELECT at_a.id, vl.property, at_b.title, vl.source_id, ?
        FROM value_links vl
        JOIN article_titles at_a ON at_a.norm_title = vl.target_norm
        JOIN article_titles at_b ON at_b.id = vl.source_id
        WHERE at_a.id != vl.source_id
          AND vl.property IN ({rec})
          AND NOT EXISTS (
              SELECT 1 FROM infobox_facts f
              WHERE f.subject_id = at_a.id AND f.property = vl.property
          )
        """.format(rec=",".join("?" * len(_RECIPROCAL_PROPS))),
        (_RECIPROCAL_CONF, *sorted(_RECIPROCAL_PROPS)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO enrich_meta(key,value) VALUES ('phaseB','done')"
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM attr_suggestions").fetchone()[0]


# --------------------------------------- main ------------------------------------------


def run(corpus: Path, overlay: Path, sentinel: Path | None, log) -> dict:
    shards = sorted(corpus.glob("articles-*.jsonl"), key=lambda p: _shard_index(p.name))
    if not shards:
        raise SystemExit(f"no articles-*.jsonl shards under {corpus}")
    conn = _connect(overlay)
    t0 = time.time()
    tot_art = tot_fact = tot_vl = done = skipped = 0
    for sp in shards:
        shard = _shard_index(sp.name)
        if _shard_done(conn, shard):
            skipped += 1
            continue
        a, f, v = process_shard(conn, sp, shard)
        tot_art += a
        tot_fact += f
        tot_vl += v
        done += 1
        log(f"  shard {shard:05d}: {a} articles, {f} infobox facts, {v} value-links")
    log(
        f"phase A done: {done} shards processed, {skipped} already-done; "
        f"{tot_fact} facts, {tot_vl} value-links over {tot_art} articles"
    )

    log("phase B: computing infobox attribute-gap suggestions")
    n_sugg = build_suggestions(conn)

    n_facts = conn.execute("SELECT COUNT(*) FROM infobox_facts").fetchone()[0]
    n_subj = conn.execute(
        "SELECT COUNT(DISTINCT subject_id) FROM infobox_facts"
    ).fetchone()[0]
    n_art_tot = conn.execute("SELECT COUNT(*) FROM article_titles").fetchone()[0]
    conn.close()
    dur = round(time.time() - t0, 1)

    summary = {
        "status": "OK",
        "version": ENRICH_VERSION,
        "overlay": str(overlay),
        "articles": n_art_tot,
        "infobox_facts": n_facts,
        "subjects_with_infobox": n_subj,
        "attr_suggestions": n_sugg,
        "shards_processed": done,
        "shards_skipped": skipped,
        "duration_s": dur,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    log(
        f"=== enrichment DONE: {n_facts} infobox facts over {n_subj} subjects, "
        f"{n_sugg} attr-gap suggestions in {dur}s ==="
    )
    if sentinel is not None:
        sentinel.write_text(
            "".join(f"{k}={v}\n" for k, v in summary.items()), encoding="utf-8"
        )
        log(f"sentinel written: {sentinel}")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Batch infobox structured-facts KB + mechanical attribute-gap "
        "suggestions over the enwiki_html re-extract corpus (no LLM)."
    )
    ap.add_argument(
        "--corpus",
        type=Path,
        required=True,
        help="re-extract corpus dir (data/wiki/enwiki_html) with articles-*.jsonl",
    )
    ap.add_argument(
        "--overlay",
        type=Path,
        required=True,
        help="shared overlay sqlite DB (data/wiki/enrich_overlay.db)",
    )
    ap.add_argument(
        "--sentinel",
        type=Path,
        default=None,
        help="completion sentinel to write with counts (e.g. /tmp/wiki_enrich.done)",
    )
    args = ap.parse_args(argv)

    def log(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] [wiki_enrich] {msg}", flush=True)

    run(args.corpus, args.overlay, args.sentinel, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
