# Wiki enrichment batch — infobox structured-facts KB + attribute-gap suggestions (v0.1.0)

Companion to `wiki_reextract_v0.1.0.md`. This is the "add the others to the 2 AM job"
stage: a **non-fatal appendix** that runs AFTER the re-extract produces structural HTML
and its manifest-verify gate passes. It is a **mechanical parse — no corpus-wide LLM** —
so it fits the overnight batch (LLM prose enrichment stays on-demand in the reader).

Tool: `tools/wiki_enrich_batch.py`. Wired into `scripts/wiki_reextract_run.sh`.

## What it produces

Both outputs land in the SHARED overlay DB `data/wiki/enrich_overlay.db`, in tables the
reader does NOT own (the reader owns `facts`; we own the ones below and only
`CREATE ... IF NOT EXISTS`, never touching another owner's tables):

1. **Infobox → structured facts.** Stream every `articles-*.jsonl` shard, parse each
   `<table class="infobox…">` in the sanitized `html` field into
   `infobox_facts(subject_id, property, value, shard)`. This is the KB foundation.

2. **Infobox attribute-gap suggestions (cross-reference, mechanical).**
   `attr_suggestions(subject_id, property, value, source_id, confidence)`, all **PENDING
   review — never auto-applied**. For every RECIPROCAL-symmetric infobox value link
   (neighbour B's infobox says `P → A`) where A's own infobox lacks property `P`, we
   suggest `A.P = <B's title>`, sourced to B.

Support tables: `article_titles(id,title,norm_title,shard)`, `value_links(target_norm,
property,source_id,shard)`, `enrich_meta(key,value)` (checkpoint).

## Precision guards (why it isn't a flood of noise)

- **Exact linked-subject match**, not fuzzy text: the value's `data-wiki-title` hyperlink
  must resolve (via the extractor's own `normalize_title`) to a real article id. That
  hyperlink IS a graph edge — the extractor derives `edges-*.tsv` from exactly these
  in-article links — so we get the "graph-linked neighbour" signal without a second scan
  of the 224M-row edge shards.
- **Reciprocal properties only.** Cross-referencing is restricted to symmetric person
  relations (`spouse / sibling / partner / relative / …`), because only for those does
  "B's value about A" soundly imply "A's value about B". Asymmetric properties (political
  party, employer, occupation, parent/child, birthplace) would each spawn a garbage
  reciprocal for every article they point at, so they are intentionally NOT suggested.
  `value_links` is likewise recorded only for reciprocal properties, to keep the shared
  DB lean. Confidence is a fixed `0.85` (mechanical, exact-link).

## Resumable / bounded RAM

State lives in SQLite, not a giant Python dict. Each shard is processed in one
transaction and marked done in `enrich_meta` only after commit; an un-done shard is
re-processed after first DELETE-ing its own rows (keyed by the `shard` column), so a
crash mid-shard is idempotent. Phase B (suggestions) is one set-based SQL statement,
re-runnable in full. A completion sentinel `/tmp/wiki_enrich.done` (status + counts) is
written at the end.

## Non-fatal wiring into the 2 AM runner

`scripts/wiki_reextract_run.sh` calls `run_enrichment()` **only after** the corpus exists
and its manifest-verify + truncation + article-floor gate passes (both on the main build
path and on the already-complete short-circuit, since the enrichment is resumable). The
call is wrapped so **any enrichment error is logged loudly and swallowed** — the corpus
is the primary deliverable, so the overall run still exits reflecting the successful
re-extract. A broken enrichment can never cost us the corpus.

## Sample test (real sanitized HTML)

Verified end-to-end on Spark against a 3-article sample whose infobox HTML was produced
by the extractor's own `sanitize()` (byte-identical to the corpus). Michelle Obama's
infobox `Spouse → [[Barack Obama]]` + Barack's infobox lacking spouse yields exactly:
`attr_suggestions = (subject_id=0 "Barack Obama", property="spouse", value="Michelle
Obama", source_id=1, confidence=0.85)`, with 5 `infobox_facts` over 2 subjects. The
asymmetric "political party" link produces NO suggestion (precision guard working).

## Estimated full-corpus runtime

Streams all ~6.9M articles but parses HTML only for those whose body contains "infobox"
(early substring skip), then one indexed SQL pass for suggestions. **Rough estimate
1–3 h** on Spark, well under the re-extract's own 6–12 h. NOT yet measured at scale — the
2 AM run against the real corpus produces the true numbers (`/tmp/wiki_enrich.done`).
This stage is infobox/structured parse ONLY; it cannot be full-tested until the real
corpus exists.
