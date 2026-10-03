#!/bin/bash
# Install the Apple Store Hong Kong stock watch on a Linux server with systemd
# (Ubuntu, Debian and similar) as a service that checks all the time, pausing
# 1, 2 and 3 minutes in turn (a quiet status every 3 rounds), plus a second service
# that answers /iphone in Telegram.
# Both start again after reboots or crashes.
#
#   sudo bash vps_install.sh             install or update (asks for the Telegram token on the first run)
#   sudo SCHEDULE=5,3,4 STATUS_ROUNDS=1 bash vps_install.sh   other pauses (minutes, in turn) and status rounds (0 = none)
#   sudo bash vps_install.sh status      send what is in stock now to Telegram
#   sudo bash vps_install.sh report      when which configuration was in stock (history report)
#   sudo bash vps_install.sh uninstall   stop and remove the service (files and settings stay)
#
# Logs: journalctl -u iphone-stock-watch-hk -f   (checks)
#       journalctl -u iphone-stock-watch-hk-bot -f   (/iphone)
set -euo pipefail

APP="${APP:-/opt/iphone-stock-watch-hk}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SCHEDULE="${SCHEDULE:-1,2,3}"
STATUS_ROUNDS="${STATUS_ROUNDS:-3}"
NAME="iphone-stock-watch-hk"
RUN_USER="stockwatch"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo: sudo bash $0" >&2
  exit 1
fi

remove_units() {
  for unit in "$NAME.timer" "$NAME.service" "$NAME-bot.service"; do
    systemctl disable --now "$unit" 2>/dev/null || true
  done
  rm -f "$UNIT_DIR/$NAME.service" "$UNIT_DIR/$NAME.timer" "$UNIT_DIR/$NAME-bot.service"
  systemctl daemon-reload
}

if [ "${1:-}" = "uninstall" ]; then
  remove_units
  echo "Службы удалены. Файлы и настройки остались в $APP."
  exit 0
fi

if [ "${1:-}" = "report" ]; then
  shift
  exec python3 "$APP/stock_report.py" "$APP/stock_history.jsonl" "$@"
fi

if [ "${1:-}" = "status" ]; then
  (
    while IFS='=' read -r key value; do
      if [ -n "$key" ]; then export "$key=$value"; fi
    done < "$APP/config.env"
    python3 "$APP/check_stock.py" --status
  )
  exit $?
fi

if ! [[ "$SCHEDULE" =~ ^[0-9]+(\.[0-9]+)?(,[0-9]+(\.[0-9]+)?)*$ ]]; then
  echo "SCHEDULE — минуты через запятую, например 1,2,3." >&2
  exit 1
fi
if ! [[ "$STATUS_ROUNDS" =~ ^[0-9]+$ ]]; then
  echo "STATUS_ROUNDS — целое число кругов между сводками, 0 — без сводок." >&2
  exit 1
fi

if ! command -v python3 >/dev/null; then
  if command -v apt-get >/dev/null; then
    apt-get update -q && apt-get install -y -q python3
  elif command -v dnf >/dev/null; then
    dnf install -y python3
  else
    echo "Установите python3 и запустите скрипт снова." >&2
    exit 1
  fi
fi
PYTHON="$(command -v python3)"

id "$RUN_USER" >/dev/null 2>&1 || useradd --system --user-group --home-dir "$APP" --shell /usr/sbin/nologin "$RUN_USER"
install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$APP"
install -m 644 -o "$RUN_USER" -g "$RUN_USER" "$SRC/check_stock.py" "$APP/check_stock.py"
install -m 644 -o "$RUN_USER" -g "$RUN_USER" "$SRC/stock_report.py" "$APP/stock_report.py"
if [ ! -f "$APP/stock_state.json" ] && [ -f "$SRC/stock_state.json" ]; then
  install -m 644 -o "$RUN_USER" -g "$RUN_USER" "$SRC/stock_state.json" "$APP/stock_state.json"
fi

if [ ! -f "$APP/config.env" ]; then
  read -rsp "Токен бота от @BotFather (ввод скрыт): " token
  echo
  read -rp "Chat ID: " chat
  (umask 077 && printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$token" "$chat" > "$APP/config.env")
  chown "$RUN_USER:$RUN_USER" "$APP/config.env"

  echo "Проверяю Apple и Telegram — в Telegram придёт тестовое сообщение…"
  if ! (
    while IFS='=' read -r key value; do
      if [ -n "$key" ]; then export "$key=$value"; fi
    done < "$APP/config.env"
    "$PYTHON" "$APP/check_stock.py" --test-telegram
  ); then
    rm -f "$APP/config.env"
    echo "Telegram не принял токен или chat ID. Запустите установку ещё раз и введите их заново." >&2
    exit 1
  fi
fi

remove_units
cat > "$UNIT_DIR/$NAME.service" <<EOF
[Unit]
Description=Apple Store Hong Kong iPhone stock watch
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP
EnvironmentFile=$APP/config.env
ExecStart=$PYTHON -u $APP/check_stock.py --watch $SCHEDULE --status-every $STATUS_ROUNDS
Restart=always
RestartSec=30
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=$APP

[Install]
WantedBy=multi-user.target
EOF

cat > "$UNIT_DIR/$NAME-bot.service" <<EOF
[Unit]
Description=Telegram /iphone command for the Apple Store Hong Kong stock watch
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP
EnvironmentFile=$APP/config.env
ExecStart=$PYTHON -u $APP/check_stock.py --bot
Restart=always
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$NAME.service" "$NAME-bot.service"
sleep 5
journalctl -u "$NAME.service" -u "$NAME-bot.service" -n 6 --no-pager -o cat || true
echo "Готово: проверка идёт постоянно, паузы $SCHEDULE мин по кругу, сводка раз в $STATUS_ROUNDS круга; бот отвечает на /iphone. Всё запускается и после перезагрузки."
echo "Логи: journalctl -u $NAME -f  и  journalctl -u $NAME-bot -f"
