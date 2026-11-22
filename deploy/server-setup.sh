#!/usr/bin/env bash
# Одноразовая подготовка сервера (запускать на VPS под root).
set -euo pipefail

install -d -m 755 /opt/snf
cd /opt/snf

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

cp deploy/snf-bot.service /etc/systemd/system/snf-bot.service
systemctl daemon-reload
systemctl enable snf-bot

cat > /etc/logrotate.d/snf <<'EOF'
/opt/snf/*.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
EOF

echo "Setup done. Now: put .env into /opt/snf, copy queue.db/backfill_state.json if migrating state, then: systemctl start snf-bot"
