#!/usr/bin/env bash
# One-shot, self-guarding 2 AM trigger that launches the formatting-preserving
# Wikipedia re-extract on the Spark box (gx10). Installed as a cron on this
# workstation. Runs ONCE: a sentinel file disables subsequent 2 AM firings.
#
#   crontab:  0 2 * * *  /home/bob/code/tridb/scripts/cron_reextract_trigger.sh
#
# Re-arm for another run:  rm ~/.wiki_reextract_triggered
set -uo pipefail

SENTINEL="$HOME/.wiki_reextract_triggered"
LOG="$HOME/code/tridb/logs/wiki_reextract_trigger.log"
RUNNER="scripts/wiki_reextract_run.sh"     # path on spark, relative to ~/code/tridb
mkdir -p "$(dirname "$LOG")"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

if [ -f "$SENTINEL" ]; then
  echo "$(ts) already triggered (sentinel present) — skipping" >>"$LOG"
  exit 0
fi

echo "$(ts) 2 AM trigger firing" >>"$LOG"

# Do NOT burn the one-shot sentinel if the Spark runner isn't in place yet —
# log loudly and let the next 2 AM cron retry.
if ! ssh -o BatchMode=yes spark "test -x ~/code/tridb/$RUNNER" 2>>"$LOG"; then
  echo "$(ts) ERROR: ~/code/tridb/$RUNNER missing/not-executable on spark — NOT triggering; will retry next cron" >>"$LOG"
  exit 1
fi

touch "$SENTINEL"
echo "$(ts) launching re-extract on spark (detached under nohup)" >>"$LOG"
ssh -o BatchMode=yes spark \
  "cd ~/code/tridb && nohup bash $RUNNER >/tmp/wiki_reextract.log 2>&1 & echo spark_pid=\$!" \
  >>"$LOG" 2>&1
echo "$(ts) dispatched — follow progress in spark:/tmp/wiki_reextract.log" >>"$LOG"
