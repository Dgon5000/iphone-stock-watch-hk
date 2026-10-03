#!/bin/bash
# Install the Apple Store Hong Kong stock watch on a Linux server with systemd
# (Ubuntu, Debian and similar) as a service that checks at :00 of every minute and sends a
# quiet status every 10 minutes, plus a second service that answers /iphone and /report in
# Telegram. GitHub may check at :30 and hand its checks in over SSH (see feed-key): one
# state for both, so a check every 30 seconds and still one alert per change.
# Both services start again after reboots or crashes.
#
#   sudo bash vps_install.sh             install or update (asks for the Telegram token on the first run)
#   sudo EVERY=60 OFFSET=0 STATUS_MINUTES=10 bash vps_install.sh   other check times (seconds) and status period (minutes, 0 = none)
#   sudo bash vps_install.sh feed-key 'ssh-ed25519 AAAA… github'   let GitHub hand in its checks with this SSH key
#   sudo bash vps_install.sh status      send what is in stock now to Telegram
#   sudo bash vps_install.sh report      when which configuration was in stock (history report)
#   sudo bash vps_install.sh uninstall   stop and remove the services and GitHub's access (files and settings stay)
#
# Logs: journalctl -u iphone-stock-watch-hk -f   (checks, GitHub's too)
#       journalctl -u iphone-stock-watch-hk-bot -f   (/iphone, /report)
set -euo pipefail

APP="${APP:-/opt/iphone-stock-watch-hk}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
EVERY="${EVERY:-60}"
OFFSET="${OFFSET:-0}"
STATUS_MINUTES="${STATUS_MINUTES:-10}"
NAME="iphone-stock-watch-hk"
RUN_USER="stockwatch"
# GitHub logs in as FEED_USER, whose key may only run check_stock.py --ingest (a root-owned
# copy in FEED_LIB) to drop checks into INBOX, a folder only it and the watcher can use.
FEED_USER="stockfeed"
FEED_HOME="${FEED_HOME:-/var/lib/stockfeed}"
FEED_LIB="${FEED_LIB:-/usr/local/lib/iphone-stock-watch-hk}"
INBOX="${INBOX:-/var/spool/iphone-stock-watch-hk}"
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

FEED_KEY=""
case "${1:-}" in
  uninstall)
    remove_units
    rm -f "$FEED_HOME/.ssh/authorized_keys"
    echo "Службы удалены, доступ GitHub по SSH закрыт. Файлы и настройки остались в $APP."
    exit 0
    ;;
  report)
    shift
    exec python3 "$APP/stock_report.py" "$APP/stock_history.jsonl" "$@"
    ;;
  status)
    (
      while IFS='=' read -r key value; do
        if [ -n "$key" ]; then export "$key=$value"; fi
      done < "$APP/config.env"
      python3 "$APP/check_stock.py" --status
    )
    exit $?
    ;;
  feed-key)
    FEED_KEY="${2:-}"
    key_re='^ssh-ed25519 [A-Za-z0-9+/]+={0,3}( [A-Za-z0-9@._:+-]+)?$'
    if ! [[ "$FEED_KEY" =~ $key_re ]]; then
      echo "Нужен открытый ключ ssh-ed25519 в кавычках: sudo bash $0 feed-key 'ssh-ed25519 AAAA… github'" >&2
      exit 1
    fi
    ;;
  "") ;;
  *)
    echo "Неизвестная команда: $1 (есть feed-key, status, report, uninstall)" >&2
    exit 1
    ;;
esac

if ! [[ "$EVERY" =~ ^[0-9]+$ && "$OFFSET" =~ ^[0-9]+$ && "$STATUS_MINUTES" =~ ^[0-9]+$ ]] \
    || [ "$EVERY" -lt 10 ] || [ "$OFFSET" -ge "$EVERY" ]; then
  echo "EVERY — секунды между проверками (не меньше 10), OFFSET — сдвиг в секундах (меньше EVERY), STATUS_MINUTES — минуты между сводками (0 — без сводок)." >&2
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

# GitHub's way in. Its home, key and program belong to root, so the key can do nothing but
# hand in checks, even if it leaks.
id "$FEED_USER" >/dev/null 2>&1 || useradd --system --user-group --home-dir "$FEED_HOME" --shell /bin/sh "$FEED_USER"
install -d -m 755 -o root -g root "$FEED_HOME" "$FEED_HOME/.ssh" "$FEED_LIB"
install -m 644 -o root -g root "$SRC/check_stock.py" "$FEED_LIB/check_stock.py"
install -d -m 2770 -o "$FEED_USER" -g "$RUN_USER" "$INBOX"
if [ -n "$FEED_KEY" ]; then
  printf '%s\n' "$FEED_KEY" > "$FEED_HOME/.ssh/feed_key.pub"
fi
if [ -f "$FEED_HOME/.ssh/feed_key.pub" ]; then
  printf 'restrict,command="%s -I %s/check_stock.py --ingest %s --source github" %s\n' \
    "$PYTHON" "$FEED_LIB" "$INBOX" "$(head -n 1 "$FEED_HOME/.ssh/feed_key.pub")" > "$FEED_HOME/.ssh/authorized_keys"
  chmod 644 "$FEED_HOME/.ssh/feed_key.pub" "$FEED_HOME/.ssh/authorized_keys"
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
ExecStart=$PYTHON -u $APP/check_stock.py --watch --every $EVERY --offset $OFFSET --status-minutes $STATUS_MINUTES --inbox $INBOX
Restart=always
RestartSec=30
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=$APP $INBOX

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
echo "Готово: сервер проверяет каждые $EVERY с (сдвиг $OFFSET с), сводка раз в $STATUS_MINUTES мин; бот отвечает на /iphone и /report. Всё запускается и после перезагрузки."
if [ -f "$FEED_HOME/.ssh/authorized_keys" ]; then
  echo "GitHub может передавать свои проверки (ключ установлен)."
else
  echo "Чтобы GitHub передавал свои проверки: sudo bash $0 feed-key 'ssh-ed25519 AAAA… github'"
fi
echo "Логи: journalctl -u $NAME -f  и  journalctl -u $NAME-bot -f"
