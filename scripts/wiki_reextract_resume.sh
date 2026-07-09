#!/usr/bin/env bash
# Robust resume wrapper for the 140 GB Enterprise-HTML re-extract.
# Re-runs scripts/wiki_reextract_run.sh until it reports status=OK. Each attempt
# resumes the download (curl -C -) and the extraction checkpoints, so a network
# drop (which killed the 2 AM cron run at ~19 GB) just resumes instead of failing.
cd ~/code/tridb || exit 1
DONE=/tmp/wiki_reextract.done
for i in $(seq 1 400); do
  echo "[resume] attempt $i @ $(date '+%F %T')"
  bash scripts/wiki_reextract_run.sh
  if grep -q '^status=OK' "$DONE" 2>/dev/null; then
    echo "[resume] SUCCESS on attempt $i @ $(date '+%F %T')"
    break
  fi
  echo "[resume] attempt $i => $(cat "$DONE" 2>/dev/null | tr '\n' ' ') — retry in 20s"
  sleep 20
done
