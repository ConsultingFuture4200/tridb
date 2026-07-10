#!/usr/bin/env bash
# dedup_and_build_enwiki_html.sh — the enwiki_html extraction emitted ~130K duplicate-id
# records (7,144,263 rows for 7,013,642 unique articles), which trips the embed's uniqueness
# verify. The embed vectors are already computed, so instead of re-embedding we dedupe the
# embed output to one row per id (FIRST occurrence — matches build_articles' INSERT OR IGNORE),
# then run `build` (reader.db + id2row + CSR + redirects + categories).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
C=data/wiki/enwiki_html
PY=.venv/bin/python
LOG=/tmp/enwiki_html_dedup_build.log
rm -f /tmp/enwiki_html_build.done /tmp/enwiki_html_build.fail
{
  echo "==================================================================="
  echo "[pipeline] START $(date -Is)"
  # back up the small metadata before rewriting
  cp "$C/emb/ids.i64.npy" "$C/emb/ids.i64.npy.predup" 2>/dev/null || true
  cp "$C/emb/meta.json"   "$C/emb/meta.json.predup"   2>/dev/null || true
  echo "[dedup] rewriting emb to unique ids (first occurrence) ..."
  $PY - <<'PYEOF'
import numpy as np, json, os
C = 'data/wiki/enwiki_html/emb'
ids = np.load(f'{C}/ids.i64.npy')
uniq, first_idx = np.unique(ids, return_index=True)   # uniq sorted; first_idx = first-occurrence rows
meta = json.load(open(f'{C}/meta.json')); dim = int(meta['dim'])
print(f'[dedup] {len(ids):,} rows -> {len(uniq):,} unique (drop {len(ids)-len(uniq):,})', flush=True)
vecs = np.memmap(f'{C}/vectors.f32', dtype=np.float32, mode='r', shape=(len(ids), dim))
out = np.memmap(f'{C}/vectors.f32.new', dtype=np.float32, mode='w+', shape=(len(uniq), dim))
step = 200_000
for s in range(0, len(uniq), step):
    out[s:s+step] = vecs[first_idx[s:s+step]]
    print(f'[dedup]   {min(s+step,len(uniq)):,}/{len(uniq):,}', flush=True)
out.flush(); del out, vecs
os.replace(f'{C}/vectors.f32.new', f'{C}/vectors.f32')
np.save(f'{C}/ids.i64.npy', uniq.astype(np.int64))
meta['N'] = int(len(uniq)); meta['status'] = 'complete'; meta['deduped_from'] = int(len(ids))
json.dump(meta, open(f'{C}/meta.json', 'w'), indent=2)
print('[dedup] done', flush=True)
PYEOF
  rc=$?; if [ $rc -ne 0 ]; then echo "[dedup] FAILED rc=$rc $(date -Is)"; touch /tmp/enwiki_html_build.fail; exit $rc; fi
  echo "[build] reader.db + id2row + CSR + redirects + categories ..."
  $PY -u tools/wiki_reader.py --corpus "$C" build
  rc=$?; if [ $rc -ne 0 ]; then echo "[build] FAILED rc=$rc $(date -Is)"; touch /tmp/enwiki_html_build.fail; exit $rc; fi
  echo "[pipeline] DONE $(date -Is)"
  touch /tmp/enwiki_html_build.done
} >> "$LOG" 2>&1
