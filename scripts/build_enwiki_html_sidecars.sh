#!/usr/bin/env bash
# build_enwiki_html_sidecars.sh — one-shot: embed the structured-HTML wiki corpus,
# then build reader.db + CSR sidecars so wiki_reader.py can serve data/wiki/enwiki_html.
#
# Order matters: cmd_build's build_id2row needs emb/ids.i64.npy, so EMBED must finish
# first. Resumable (--resume) so an interrupted embed picks up from its checkpoint.
# Logs to /tmp/enwiki_html_build.log; writes .done / .fail sentinels.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
C=data/wiki/enwiki_html
PY=.venv/bin/python
LOG=/tmp/enwiki_html_build.log
rm -f /tmp/enwiki_html_build.done /tmp/enwiki_html_build.fail
{
  echo "==================================================================="
  echo "[pipeline] START $(date -Is)  corpus=$C"
  nsh=$(ls "$C"/articles-*.jsonl 2>/dev/null | wc -l)
  echo "[pipeline] STEP 1/2: embed BGE-384 over $nsh shards (~2h on the GPU) ..."
  $PY -u tools/wiki_embed_hybrid.py --corpus "$C" --out "$C/emb" \
      --model BAAI/bge-small-en-v1.5 --dim 384
  rc=$?
  if [ $rc -ne 0 ]; then echo "[pipeline] EMBED FAILED rc=$rc $(date -Is)"; touch /tmp/enwiki_html_build.fail; exit $rc; fi
  echo "[pipeline] STEP 2/2: build reader.db + id2row + CSR + redirects + categories ..."
  $PY -u tools/wiki_reader.py --corpus "$C" build
  rc=$?
  if [ $rc -ne 0 ]; then echo "[pipeline] BUILD FAILED rc=$rc $(date -Is)"; touch /tmp/enwiki_html_build.fail; exit $rc; fi
  echo "[pipeline] DONE $(date -Is)"
  echo "[pipeline] serve it:  $PY tools/wiki_reader.py --corpus $C serve --host 127.0.0.1 --port 8080"
  touch /tmp/enwiki_html_build.done
} >> "$LOG" 2>&1
