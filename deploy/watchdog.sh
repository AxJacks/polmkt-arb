#!/usr/bin/env bash
# polmkt-arb watchdog: self-heal + Telegram alert. Run by polmkt-watchdog.timer
# every 10 minutes. Quiet when healthy — it only texts when something needed
# fixing or needs a human.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$DIR/.venv/bin/python"
alerts=""

# 1. Disk: the June incident was a 21GB arb.db filling the disk. Retention now
#    bounds the db, but alert well before full so nothing wedges again.
use=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "${use:-0}" -ge 85 ] && alerts+="disk ${use}% full. "

# 2. arb.db runaway guard. Steady state with 24h retention is ~2-3GB (SQLite
#    reuses freed pages; the file doesn't shrink) — alert only well above that.
sz=$(du -m "$DIR/arb.db" 2>/dev/null | cut -f1)
[ "${sz:-0}" -ge 6000 ] && alerts+="arb.db ${sz}MB — retention may be broken. "

# 3. Trim a runaway text log (INFO logging is chatty).
if [ -f "$DIR/arb.log" ] && [ "$(du -m "$DIR/arb.log" | cut -f1)" -ge 200 ]; then
  : > "$DIR/arb.log"
fi

# 4. Services: systemd restarts crashes, but a unit can land in "failed" after
#    StartLimitBurst. Reset + restart it, and say whether that worked.
for s in polmkt-detector polmkt-bot polmkt-dashboard; do
  if ! systemctl is-active --quiet "$s"; then
    systemctl reset-failed "$s" 2>/dev/null
    systemctl restart "$s"
    sleep 3
    if systemctl is-active --quiet "$s"; then
      alerts+="$s was down — restarted OK. "
    else
      alerts+="$s DOWN and restart FAILED — needs a look. "
    fi
  fi
done

# 4b. Live runner: it legitimately exits (halt / caps reached) so we never
#     auto-restart it here — the daily refresh timer does that. But repeated
#     CRASHES park it in "failed", which deserves a text, not silence.
if systemctl list-unit-files polmkt-livearb.service --no-legend 2>/dev/null | grep -q livearb; then
  if systemctl is-failed --quiet polmkt-livearb; then
    alerts+="polmkt-livearb crashed repeatedly (failed state) — check journalctl -u polmkt-livearb. "
  fi
fi

# 5. Liveness: a running detector that logged nothing for 30+ min is wedged
#    (feeds die silently). Restart it once and say so.
if systemctl is-active --quiet polmkt-detector; then
  last=$(journalctl -u polmkt-detector --since "-30 min" -o cat -q | wc -l)
  if [ "$last" -eq 0 ]; then
    systemctl restart polmkt-detector
    alerts+="detector silent 30min — restarted. "
  fi
fi

if [ -n "$alerts" ]; then
  echo "watchdog: $alerts"
  # Repeat-suppression: the same persistent condition (e.g. db size) must not
  # text every 10 minutes — once per 6h per identical message is plenty.
  STATE="/tmp/.polmkt_watchdog_last"
  sig=$(echo "$alerts" | md5sum | cut -d" " -f1)
  now=$(date +%s)
  if [ -f "$STATE" ]; then
    read -r last_sig last_ts < "$STATE"
    if [ "$sig" = "$last_sig" ] && [ $((now - last_ts)) -lt 21600 ]; then
      exit 0
    fi
  fi
  echo "$sig $now" > "$STATE"
  cd "$DIR" && "$PY" -c "
import sys
from config import load_config
from src.notify import Notifier
Notifier.from_config(load_config()).send('🛠 droplet watchdog: ' + sys.argv[1])
" "$alerts" 2>/dev/null || true
fi
exit 0
