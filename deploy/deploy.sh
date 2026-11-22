#!/usr/bin/env bash
# Деплой кода SNF на VPS (запускать из корня проекта на локальной машине, Git Bash).
# Состояние (queue.db, backfill_state.json, .env) не трогает.
set -euo pipefail

# Адрес сервера и ключ задаются через переменные окружения:
#   DEPLOY_HOST=user@1.2.3.4 SSH_KEY=~/.ssh/deploy_key bash deploy/deploy.sh
DEPLOY_HOST="${DEPLOY_HOST:?задайте DEPLOY_HOST=user@host}"
SSH_KEY="${SSH_KEY:-~/.ssh/id_rsa}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $DEPLOY_HOST"

# Код + зависимости + unit
tar czf - app certs deploy requirements.txt | $SSH "cd /opt/snf && tar xzf -"

$SSH "cd /opt/snf && .venv/bin/pip install -q -r requirements.txt \
  && cp deploy/snf-bot.service /etc/systemd/system/snf-bot.service \
  && systemctl daemon-reload && systemctl restart snf-bot \
  && sleep 3 && systemctl --no-pager --full status snf-bot | head -15"
