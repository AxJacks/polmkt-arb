#!/usr/bin/env bash
# polmkt-arb one-shot VM setup. Run from the project dir ON THE SERVER:
#   cd ~/polmkt-arb && sudo bash deploy/setup.sh
# Installs Python deps into a venv and registers auto-restarting systemd services
# for: sports detector, Telegram control bot, dashboard (localhost), and a nightly
# daily-summary timer. Idempotent — safe to re-run after a code update.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"
PY="$DIR/.venv/bin/python"

echo "==> install dir: $DIR   service user: $RUN_USER"

# 1. system deps + venv
apt-get update -y
apt-get install -y python3-venv python3-pip rsync
if [ ! -x "$PY" ]; then
  sudo -u "$RUN_USER" python3 -m venv "$DIR/.venv"
fi
sudo -u "$RUN_USER" "$DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

# 2. sanity: secrets present
if [ ! -f "$DIR/.env" ]; then
  echo "!! $DIR/.env missing — copy it from your Mac before starting (see DEPLOY.md). Continuing setup."
fi

# 3. generate systemd units
gen() { # name  description  execargs
  cat > "/etc/systemd/system/$1.service" <<UNIT
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$PY $3
Restart=always
RestartSec=5
# don't thrash on a hard config error
StartLimitIntervalSec=60
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
UNIT
}

gen polmkt-detector "polmkt-arb detector (sports + macro, paper detection)" "-m src.detector --sports all,macro --log-level INFO"
gen polmkt-bot      "polmkt-arb Telegram control bot (/status /halt …)"            "-m src.notify listen"
gen polmkt-dashboard "polmkt-arb dashboard (token-gated, public:8787)"             "-m src.dashboard --db $DIR/arb.db --host 0.0.0.0 --port 8787"

# nightly daily-summary (oneshot + timer)
cat > /etc/systemd/system/polmkt-daily.service <<UNIT
[Unit]
Description=polmkt-arb nightly Telegram summary
[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$PY -m src.notify daily
UNIT
cat > /etc/systemd/system/polmkt-daily.timer <<UNIT
[Unit]
Description=run polmkt daily summary nightly
[Timer]
OnCalendar=*-*-* 04:30:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT

# nightly slate refresh: restart the detector each morning (before games) so it
# rolls to the new day's games automatically and prunes finished ones.
cat > /etc/systemd/system/polmkt-refresh.service <<UNIT
[Unit]
Description=polmkt-arb refresh sports slate (restart detector)
[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart polmkt-detector
UNIT
cat > /etc/systemd/system/polmkt-refresh.timer <<UNIT
[Unit]
Description=refresh polmkt sports slate daily
[Timer]
OnCalendar=*-*-* 13:00:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT

# 4. enable + (re)start
systemctl daemon-reload
systemctl enable --now polmkt-detector polmkt-bot polmkt-dashboard polmkt-daily.timer polmkt-refresh.timer
systemctl restart polmkt-detector polmkt-bot polmkt-dashboard

echo
echo "==> services:"
systemctl --no-pager --no-legend status polmkt-detector polmkt-bot polmkt-dashboard | grep -E "polmkt|Active" || true
echo
echo "Done. Logs:   journalctl -u polmkt-detector -f"
echo "Halt all:     touch $DIR/HALT     (or text /halt to your bot)"
echo "Dashboard:    ssh -L 8787:127.0.0.1:8787 <user>@<vm-ip>  then open http://127.0.0.1:8787"
