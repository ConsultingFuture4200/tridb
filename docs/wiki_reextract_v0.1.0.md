# Formatting-Preserving Wikipedia Re-Extract — v0.1.0

TL;DR — A new streaming extractor (`tools/wiki_extract_html.py`) + 2 AM runner
(`scripts/wiki_reextract_run.sh`) rebuild the enwiki corpus from the **Wikimedia
Enterprise HTML dump**, keeping real article structure (headings, bold/italic, lists,
tables, infoboxes, images, references) instead of flattening to plain text. Output goes
to a **new** dir `data/wiki/enwiki_html/` — the live `data/wiki/enwiki/` corpus is never
touched. It also fixes deferred bug #4 (the old sharded writer clobbered 3 shards,
losing ~289k articles) *by construction* and gates on the files-vs-manifest check.

## Why

`tools/wiki_extract.py` consumes the wikitext `pages-articles.xml.bz2` dump and reduces
each article to `{id, title, text, ts}` — every heading, list, table and infobox is
discarded by `mwparserfromhell.strip_code`. The reader / GraphRAG paths increasingly
want the real structure, which only survives as rendered HTML.

## Source chosen: Wikimedia Enterprise HTML dump (PRIMARY)

`dumps.wikimedia.org/other/enterprise_html/runs/<DATE>/enwiki-NS0-<DATE>-ENTERPRISE-HTML.json.tar.gz`
— a gzipped tar of a handful of `enwiki_namespace_0_N.ndjson` members; each line is one
article object carrying **pre-rendered Parsoid HTML** (`article_body.html`). This is the
only source that yields real infoboxes/tables/images without a Parsoid-grade template
expander. The runner resolves the latest run programmatically (latest ≈ 140 GB
compressed as of run `20250320`).

Fallback (wikitext + `mwparserfromhell`) was **not** used — the Enterprise dump was
reachable and complete. That fallback would preserve headings/bold/italic/lists/simple
tables but **not** infoboxes/complex templates; if the primary ever fails, that loss
must be documented at that time. This build used the primary.

### What the sanitizer preserves (and strips)

Kept as a structural allowlist (stdlib `html.parser`, no new deps): `h1–h6, p, ul/ol/li,
dl/dt/dd, table/thead/tbody/tfoot/tr/th/td/caption/col, b/strong/i/em, blockquote,
figure/figcaption, img, a, sup/sub` (references), plus inline `abbr/code/pre/cite/span/…`.
- `table` keeps `class` (so `infobox`/`wikitable`/references are recognizable) + `colspan/rowspan`.
- `img` is emitted **inert** as `<img data-src=… data-alt=… data-width/height=…>` (no live `src`).
- `a` is emitted as `<a data-wiki-title="Target">text</a>` (live `href` stripped; the
  reader does its own linking).
- Unknown tags (Parsoid `<section>`/`<div>` wrappers) are **transparent** — dropped,
  children kept.
- **Stripped entirely:** the whole `<head>`, `script`, `style`, `link`, `meta`, `nav`,
  `aside`, `svg`, edit-section links, navboxes, maintenance/ambox banners, `noprint`,
  hidden short-descriptions, and other chrome (by tag or `class` match).

Each article also keeps a plain-text projection in `text` (derived from the sanitized
HTML) so the existing embed/index paths keep working unchanged.

## Output contract (`data/wiki/enwiki_html/`)

Mirrors `tools/wiki_extract.py` so downstream loaders work, plus the new `html` field:

| File | Shape |
|---|---|
| `articles-NNNNN.jsonl` | `{"id","title","html","text","ts"}` — `html`=sanitized structural HTML, `text`=plain-text fallback |
| `edges-NNNNN.tsv` | `src_id\tdst_id` — redirect-resolved directed hyperlinks |
| `categories-NNNNN.tsv` | `article_id\tcategory` — PG `COPY (FORMAT text)` escaped |
| `redirects.tsv` | `source_title\ttarget_title` — the reused map (provenance) |
| `manifest.json` | provenance (source dump + date + params) + per-shard schema + counts |

`NNNNN` = the input member's stream position (one shard-set per NDJSON member), so shard
files are **not** id-contiguous — irrelevant to the loader / verifier, which glob and
count every shard. Article ids are dense sequential (encounter order), matching the
existing corpus's id contract.

### Redirects caveat

The Enterprise HTML dump contains **no redirect pages and no incoming-redirect field**,
so a redirect map cannot be harvested from it. The runner passes `--redirects
data/wiki/enwiki/redirects.tsv` (the proven wikitext-derived map, read-only) so link
targets that are redirects still resolve to the canonical article id. Provenance is
recorded in `manifest.json` (`redirects_source`, `redirects_note`). Without a redirect
map, links to redirect titles are simply dropped.

## Fix for deferred bug #4 (shard clobber) — by construction

The old writer rotated shards by `id // shard_size` and, on the full run, **reopened**
`articles-00028/49/71` in truncate mode, clobbering ~289k articles while the manifest
still claimed them. Here each input member owns exactly one shard-set, opened once,
written once, then recorded complete in a checkpoint. A completed member's shards are
**never reopened** — the clobber is structurally impossible. A crashed member's partial
shard is rewritten wholesale on resume (verified: resume leaves a completed shard
byte-for-byte identical). The runner then runs `tools/wiki_manifest_verify.py` as an
independent files-vs-manifest gate and **fails loudly (nonzero exit)** on any divergence,
plus a `source_truncated` flag and a ≥6.0M-article sanity floor.

## Resumability

- **Download:** `curl -C -` resumes a partial `.tar.gz`; size is checked against the
  server `Content-Length` before extraction (a short download fails, does not proceed).
- **Pass 1** (title→id map): persisted to `<scratch>/work/`; a restart loads it and skips
  the re-stream.
- **Pass 2:** per-member checkpoint (`pass2_checkpoint.json`); completed members are
  skipped on restart.
- A fully-verified, non-truncated corpus short-circuits the whole run.

## Expected full-run time & size (estimate — NOT yet measured)

Source ~140 GB compressed. Two streaming passes (pass 1 is cheap field reads; pass 2 does
the HTML sanitize over ~6.9M articles, the bottleneck). **Rough estimate: 6–12 h**
wall on Spark, dominated by pass-2 Python HTML parsing. Output corpus **~100–150 GB**
(sanitized HTML is much larger than the old plain-text corpus). Peak scratch ≈ 140 GB
(source) + corpus, well within Spark's 2.9 TB free. These are engineering estimates; the
2 AM run produces the real numbers (written to `/tmp/wiki_reextract.done`).

## How the reader will consume `html` later (follow-on — NOT this task)

The reader currently renders the plain `text` field. A follow-on reader rebuild will
render the `html` field (already sanitized to a safe structural subset), resolve
`<a data-wiki-title>` into internal navigation, and lazy-load `<img data-src>`. Because
`text` is retained, embed/index/serve paths keep working before that rebuild lands.

## Runbook

- Cron trigger `scripts/cron_reextract_trigger.sh` (already installed) fires once at 2 AM,
  checks the runner is present+executable on Spark, then launches
  `scripts/wiki_reextract_run.sh` under `nohup`.
- Progress log: `spark:/tmp/wiki_reextract.log`. Completion sentinel + summary:
  `spark:/tmp/wiki_reextract.done` (`status=OK|FAILED` + counts).
- Re-arm the one-shot: `rm ~/.wiki_reextract_triggered`.
