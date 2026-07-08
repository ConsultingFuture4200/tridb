#!/usr/bin/env bash
# Formatting-preserving Wikipedia re-extract — the 2 AM RUNNER (invoked on Spark by
# scripts/cron_reextract_trigger.sh). Resolves the latest Wikimedia Enterprise HTML
# dump, streams it through tools/wiki_extract_html.py into data/wiki/enwiki_html/
# (NEVER touching the live data/wiki/enwiki corpus), runs the files-vs-manifest gate
# (tools/wiki_manifest_verify.py — the fix for deferred bug #4), and writes a completion
# sentinel + summary. Bounded RAM, resumable, safe to launch unattended.
#
#   manual:  bash scripts/wiki_reextract_run.sh
#   cron:    dispatched by cron_reextract_trigger.sh (stdout already -> $LOG)
#
# Resumable end-to-end: the download resumes (curl -C -), pass 1's title->id map and
# pass 2's per-member checkpoint resume, and a fully-verified corpus short-circuits.
set -uo pipefail

REPO="$HOME/code/tridb"
PY="$REPO/.venv/bin/python"
SCRATCH="$HOME/wiki_reextract_scratch"          # on / (2.9 TB free on Spark)
OUT="$REPO/data/wiki/enwiki_html"               # NEW dir — never overwrite enwiki/
REDIRECTS="$REPO/data/wiki/enwiki/redirects.tsv" # reuse (read-only) for edge resolution
WORK="$SCRATCH/work"
LOG="/tmp/wiki_reextract.log"
DONE="/tmp/wiki_reextract.done"
LOCK="$SCRATCH/.run.lock"
BASE_URL="https://dumps.wikimedia.org/other/enterprise_html/runs"
MIN_ARTICLES=6000000                            # sanity floor (full enwiki ~6.9M)

mkdir -p "$SCRATCH" "$WORK" "$(dirname "$OUT")"
exec >>"$LOG" 2>&1                               # all output -> the log (append)
ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }
fail() {
  log "FAILED: $*"
  { echo "status=FAILED"; echo "reason=$*"; echo "time=$(ts)"; } >"$DONE"
  rmdir "$LOCK" 2>/dev/null
  exit 1
}

# single-instance guard (mkdir is atomic)
if ! mkdir "$LOCK" 2>/dev/null; then
  log "another run holds $LOCK — exiting"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

START=$(date +%s)
log "=== wiki re-extract starting (pid $$) ==="
[ -x "$PY" ] || fail "venv python missing at $PY"
[ -f "$REPO/tools/wiki_extract_html.py" ] || fail "extractor missing"

# ---- short-circuit if a complete, verified, non-truncated corpus already exists ----
if [ -f "$OUT/manifest.json" ]; then
  if "$PY" "$REPO/tools/wiki_manifest_verify.py" --corpus "$OUT" >/dev/null 2>&1 \
     && [ "$("$PY" -c "import json;print(json.load(open('$OUT/manifest.json')).get('source_truncated'))" 2>/dev/null)" = "False" ] \
     && [ "$("$PY" -c "import json;print(json.load(open('$OUT/manifest.json'))['counts']['articles'])" 2>/dev/null)" -ge "$MIN_ARTICLES" ]; then
    log "existing corpus at $OUT already complete + verified — nothing to do"
    { echo "status=OK (already-complete)"; echo "out=$OUT"; echo "time=$(ts)"; } >"$DONE"
    exit 0
  fi
  log "existing $OUT is incomplete/unverified — will (re)build (resumable)"
fi

# ---- resolve the latest Enterprise HTML run that actually has the enwiki NS0 file ----
log "resolving latest Enterprise HTML run under $BASE_URL/"
RUNS=$(curl -s --fail --max-time 60 "$BASE_URL/" | grep -oE '[0-9]{8}/' | tr -d '/' | sort -u)
[ -n "$RUNS" ] || fail "could not list runs (network?)"
DUMP_URL=""; DUMP_DATE=""; DUMP_NAME=""; EXPECT_LEN=""
for d in $(echo "$RUNS" | sort -r | head -5); do
  name="enwiki-NS0-${d}-ENTERPRISE-HTML.json.tar.gz"
  url="$BASE_URL/$d/$name"
  len=$(curl -sIL --max-time 60 "$url" | grep -i '^content-length:' | tail -1 | tr -dc '0-9')
  if [ -n "$len" ] && [ "$len" -gt 1000000000 ]; then   # >1 GB => real file present
    DUMP_URL="$url"; DUMP_DATE="$d"; DUMP_NAME="$name"; EXPECT_LEN="$len"
    break
  fi
  log "  run $d has no usable enwiki NS0 file yet — trying older"
done
[ -n "$DUMP_URL" ] || fail "no Enterprise HTML enwiki NS0 dump found in the latest 5 runs"
log "selected dump: $DUMP_NAME (${EXPECT_LEN} bytes, run $DUMP_DATE)"

# ---- resumable download + completeness check ----------------------------------------
SRC="$SCRATCH/$DUMP_NAME"
have=$(stat -c%s "$SRC" 2>/dev/null || echo 0)
if [ "$have" != "$EXPECT_LEN" ]; then
  log "downloading (resume from ${have} / ${EXPECT_LEN} bytes) -> $SRC"
  curl -C - --fail --retry 5 --retry-delay 30 --max-time 86400 -o "$SRC" "$DUMP_URL" \
    || fail "download error"
else
  log "download already complete ($have bytes) — skipping"
fi
got=$(stat -c%s "$SRC" 2>/dev/null || echo 0)
[ "$got" = "$EXPECT_LEN" ] || fail "size mismatch after download: got $got want $EXPECT_LEN"
log "download verified: $got bytes"

# ---- extract (streaming, resumable, formatting-preserving) --------------------------
RED_ARG=()
if [ -f "$REDIRECTS" ]; then
  RED_ARG=(--redirects "$REDIRECTS")
  log "reusing redirect map for edge resolution: $REDIRECTS"
else
  log "WARNING: $REDIRECTS absent — edges to redirect titles will be dropped"
fi
log "extracting -> $OUT (work=$WORK)"
"$PY" -m tools.wiki_extract_html --source "$SRC" --out "$OUT" --work "$WORK" \
  "${RED_ARG[@]}" || fail "extractor error"

# ---- fix-#4 gate: files-vs-manifest + truncation + sanity floor ---------------------
log "running manifest-verify gate"
"$PY" "$REPO/tools/wiki_manifest_verify.py" --corpus "$OUT" || fail "manifest-verify MISMATCH (fix-#4 gate)"
TRUNC=$("$PY" -c "import json;print(json.load(open('$OUT/manifest.json')).get('source_truncated'))")
[ "$TRUNC" = "False" ] || fail "source_truncated=$TRUNC — dump was cut; corpus incomplete"
NART=$("$PY" -c "import json;print(json.load(open('$OUT/manifest.json'))['counts']['articles'])")
[ "$NART" -ge "$MIN_ARTICLES" ] || fail "article count $NART below floor $MIN_ARTICLES — incomplete"

NEDGE=$("$PY" -c "import json;print(json.load(open('$OUT/manifest.json'))['counts']['edges'])")
NCAT=$("$PY" -c "import json;print(json.load(open('$OUT/manifest.json'))['counts']['categories'])")
DUR=$(( $(date +%s) - START ))
OUTSZ=$(du -sh "$OUT" 2>/dev/null | cut -f1)
log "=== DONE: $NART articles, $NEDGE edges, $NCAT category rows in $OUT ($OUTSZ) in ${DUR}s ==="
{
  echo "status=OK"
  echo "out=$OUT"
  echo "dump=$DUMP_NAME"
  echo "dump_date=$DUMP_DATE"
  echo "articles=$NART"
  echo "edges=$NEDGE"
  echo "categories=$NCAT"
  echo "corpus_size=$OUTSZ"
  echo "duration_s=$DUR"
  echo "verify=PASS"
  echo "time=$(ts)"
} >"$DONE"
log "sentinel written: $DONE"
exit 0
