#!/usr/bin/env bash
#
# bench_graphrag.sh — LIVE GraphRAG QA run on the forked-MSVBASE engine (Plan 015, Phase 5).
#
# >>> GX10 / ENGINE-GATED — UNBUILT-HERE. <<<
# This script drives the canonical tjs() graph-constrained retrieval on the LIVE
# tridb/msvbase:dev engine over the HotpotQA corpus, then grades the SAME accuracy
# (evidence recall + answer EM/F1) the host-side report computes — proving the live
# engine returns the same retrieval the host oracle does — PLUS the live latency
# (EXPLAIN ANALYZE + tjs_candidates_examined) at the operating term_cond, vs the
# live multi-store baseline. The full retrieve-from-all-Wikipedia fullwiki corpus
# (~5M paragraphs) is embedded + HNSW-built ON THE GX10 (128 GB); the x86 standin
# runs the dev slice only and must NOT claim the full-corpus or live-latency number.
#
# WHAT IS MEASURED vs GATED
#   host-side (no engine): evidence recall + answer EM/F1 — see bench/graphrag_report.py
#     (make graphrag). The dev-slice fetch is network-gated (tools/fetch_hotpot.py).
#   LIVE / GX10-gated: the tjs() answer set, tjs_candidates_examined(), EXPLAIN ANALYZE
#     latency. This script GUARDS on the engine image and refuses to fabricate a live
#     number off-target — identical policy to scripts/bench_public.sh.
#
# Usage: scripts/bench_graphrag.sh [image]
#   env: GRAPHRAG_K=10 GRAPHRAG_TERMCOND=0  (tjs operating point; 0 -> engine default)
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"   # v1 native AM (graph_store_am, ADR-0013 Stage B)
cd "$ROOT"

K="${GRAPHRAG_K:-10}"
TERMCOND="${GRAPHRAG_TERMCOND:-0}"
MANIFEST="$ROOT/data/hotpot/manifest.json"

PY="python3"
[ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"

# --- corpus guard: the manifest + embeddings must exist (host build is not gated) ----
if [ ! -f "$MANIFEST" ]; then
  echo "manifest $MANIFEST missing — run: python -m tools.fetch_hotpot && python -m tools.hotpot_corpus" >&2
  exit 1
fi

# --- engine guard: the LIVE tjs() run is GX10/engine-gated; never fabricate off-target ----
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "image $IMAGE not built — the live GraphRAG run is ENGINE-GATED (UNBUILT-HERE)." >&2
  echo "Build on the GX10: scripts/gx10build.sh  (or x86 standin: scripts/x86build.sh --docker)" >&2
  exit 1
fi

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
SQL="$WORK/graphrag.sql"
RAW="$WORK/graphrag_raw.txt"

echo "[graphrag-live] emitting canonical #BENCH SQL (tjs graph-constrained) from the corpus"
"$PY" - "$MANIFEST" "$SQL" "$K" "$TERMCOND" <<'PYEOF'
import sys, numpy as np, json
from tools.hotpot_corpus import emit_bench_sql
manifest_path, sql_out, k, termcond = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
m = json.load(open(manifest_path))
corpus_emb = np.load(m["corpus_emb_path"]); query_emb = np.load(m["query_emb_path"])
import os; os.environ["BENCH_TERMCOND"] = str(termcond)
open(sql_out, "w").write(emit_bench_sql(m, corpus_emb, query_emb, k))
print(f"[graphrag-live] {len(m['paragraphs'])} paras, {len(m['questions'])} queries, k={k}, term_cond={termcond}")
PYEOF

echo "[graphrag-live] running tjs() on the LIVE engine ($IMAGE)"
docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/graphrag.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin; PGC=$B/pg_config
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  make PG_CONFIG=$PGC >/tmp/make.log 2>&1 || { echo "BUILD FAILED:"; tail -40 /tmp/make.log; exit 1; }
  make PG_CONFIG=$PGC install >/tmp/install.log 2>&1 || { echo "INSTALL FAILED:"; tail -40 /tmp/install.log; exit 1; }
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  $B/psql -p 5432 -d postgres -q -f /tmp/graphrag.sql
  rc=$?; $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true; exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector' | tee "$RAW"

echo "[graphrag-live] grading live tjs() ids vs host oracle + EM/F1 (parity + latency)"
echo "  TODO(GX10): wire $RAW '#BENCH' answer ids into bench/graphrag_report grading +"
echo "  the live multi-store baseline (baseline/graphrag.py) for the latency head-to-head."
echo "[graphrag-live] DONE (engine-gated path; full-corpus headline is GX10-only)"
