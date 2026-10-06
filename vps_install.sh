#!/bin/bash
# Install the Apple Store Hong Kong stock watch on a Linux server with systemd
# (Ubuntu, Debian and similar) as a service that checks every 90 seconds and sends a
# quiet status every 10 minutes, plus a second service that answers /iphone and /report in
# Telegram. GitHub, VPS 2, VPS 1, VPS 3, VPS 4 and VPS 5 check 15 seconds apart:
# one state and a 90-second cycle. See vps_probe_install.sh for VPS 3, 4 and 5.
# The second server also takes over if the main watcher stops working.
# The services start again after reboots or crashes.
#
#   sudo bash vps_install.sh             install or update (asks for the Telegram token on the first run)
#   sudo EVERY=90 OFFSET=30 STATUS_MINUTES=10 bash vps_install.sh   other check times (seconds) and status period (minutes, 0 = none)
#   sudo bash vps_install.sh feed-key 'ssh-ed25519 AAAA… github'   let GitHub hand in its checks with this SSH key
#   sudo bash vps_install.sh standby MAIN_IP    make this server the standby of the main one (prints its key);
#                                               MAIN_HOST_KEY='ssh-ed25519 AAAA…' gives the main server's host key
#                                               PARTICIPATE=0 keeps the legacy passive standby mode
#   sudo bash vps_install.sh sync-key 'ssh-ed25519 AAAA… standby'  on the main server: let the standby ask how it is
#   sudo bash vps_install.sh probe-key 'ssh-ed25519 AAAA… vps4' fourth  let VPS 4 hand in its checks (default: third)
#   sudo bash vps_install.sh status      send what is in stock now to Telegram
#   sudo bash vps_install.sh report      when which configuration was in stock (history report)
#   sudo bash vps_install.sh uninstall   stop and remove the services and the SSH access (files and settings stay)
#
# Logs: journalctl -u iphone-stock-watch-hk -f   (checks, GitHub's too)
#       journalctl -u iphone-stock-watch-hk-bot -f   (/iphone, /report)
set -euo pipefail

APP="${APP:-/opt/iphone-stock-watch-hk}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
EVERY="${EVERY:-90}"
OFFSET="${OFFSET:-}"
PARTICIPATE="${PARTICIPATE:-1}"
STATUS_MINUTES="${STATUS_MINUTES:-10}"
NAME="iphone-stock-watch-hk"
RUN_USER="stockwatch"
# GitHub (and a standby server) log in as FEED_USER, whose keys may only run check_stock.py
# --ingest or --sync (a root-owned copy in FEED_LIB) with INBOX, a folder only it and the
# watcher can use.
FEED_USER="stockfeed"
FEED_HOME="${FEED_HOME:-/var/lib/stockfeed}"
FEED_LIB="${FEED_LIB:-/usr/local/lib/iphone-stock-watch-hk}"
INBOX="${INBOX:-/var/spool/iphone-stock-watch-hk}"
ROLE_FILE="$FEED_LIB/role"  # "standby MAIN_HOST" on a standby server; root's, unlike $APP
SRC="$(cd "$(dirname "$0")" && pwd)"
KEY_RE='^ssh-ed25519 [A-Za-z0-9+/]+={0,3}( [A-Za-z0-9@._:+-]+)?$'
HOST_RE='^[A-Za-z0-9.:-]+$'

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

# Python runs as the service user, never as root: it can change the files in $APP. Only the
# known settings come from config.env.
as_service_user() {
  (
    while IFS='=' read -r key value; do
      case "$key" in
        TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|PART_NUMBERS) export "$key=$value" ;;
      esac
    done < "$APP/config.env"
    cd "$APP"
    runuser -u "$RUN_USER" -- "$@"
  )
}

ROLE="main"
MAIN_HOST=""
if [ -f "$ROLE_FILE" ]; then
  read -r ROLE MAIN_HOST < "$ROLE_FILE" || true
  if [ "$ROLE" != standby ] || ! [[ "$MAIN_HOST" =~ $HOST_RE ]]; then
    echo "Непонятная роль в $ROLE_FILE; удалите файл или запустите: sudo bash $0 standby MAIN_IP" >&2
    exit 1
  fi
fi

FEED_KEY=""
SYNC_KEY=""
PROBE_KEY=""
PROBE_SOURCE=third
case "${1:-}" in
  uninstall)
    remove_units
    rm -f "$FEED_HOME/.ssh/authorized_keys"
    echo "Службы удалены, доступ по SSH для GitHub и запасного сервера закрыт. Файлы и настройки остались в $APP."
    exit 0
    ;;
  report)
    shift
    as_service_user python3 "$APP/stock_report.py" "$APP/stock_history.jsonl" "$@"
    exit $?
    ;;
  status)
    as_service_user python3 "$APP/check_stock.py" --status
    exit $?
    ;;
  feed-key)
    FEED_KEY="${2:-}"
    if ! [[ "$FEED_KEY" =~ $KEY_RE ]]; then
      echo "Нужен открытый ключ ssh-ed25519 в кавычках: sudo bash $0 feed-key 'ssh-ed25519 AAAA… github'" >&2
      exit 1
    fi
    ;;
  sync-key)
    SYNC_KEY="${2:-}"
    if ! [[ "$SYNC_KEY" =~ $KEY_RE ]]; then
      echo "Нужен открытый ключ ssh-ed25519 запасного сервера в кавычках: sudo bash $0 sync-key 'ssh-ed25519 AAAA… standby'" >&2
      exit 1
    fi
    if [ "$ROLE" = standby ]; then
      echo "Это запасной сервер; sync-key ставится на основной." >&2
      exit 1
    fi
    ;;
  probe-key)
    PROBE_KEY="${2:-}"
    PROBE_SOURCE="${3:-third}"
    if ! [[ "$PROBE_KEY" =~ $KEY_RE ]]; then
      echo "Нужен открытый ключ ssh-ed25519 проверяющего VPS в кавычках." >&2
      exit 1
    fi
    if ! [[ "$PROBE_SOURCE" =~ ^(third|fourth|fifth)$ ]]; then
      echo "Источник probe-key — third, fourth или fifth." >&2
      exit 1
    fi
    ;;
  standby)
    MAIN_HOST="${2:-}"
    if ! [[ "$MAIN_HOST" =~ $HOST_RE ]]; then
      echo "Нужен адрес основного сервера: sudo bash $0 standby MAIN_IP" >&2
      exit 1
    fi
    if [ -n "${MAIN_HOST_KEY:-}" ] && ! [[ "$MAIN_HOST_KEY" =~ ^ssh-ed25519\ [A-Za-z0-9+/]+={0,3}$ ]]; then
      echo "MAIN_HOST_KEY — ключ основного сервера вида 'ssh-ed25519 AAAA…' (из /etc/ssh/ssh_host_ed25519_key.pub)." >&2
      exit 1
    fi
    ROLE=standby
    ;;
  "") ;;
  *)
    echo "Неизвестная команда: $1 (есть feed-key, standby, sync-key, probe-key, status, report, uninstall)" >&2
    exit 1
    ;;
esac

if [ -z "$OFFSET" ]; then
  if [ "$ROLE" = standby ]; then OFFSET=15; else OFFSET=30; fi
fi
if ! [[ "$PARTICIPATE" =~ ^[01]$ ]]; then
  echo "PARTICIPATE — 1 (дополнительный сервер проверяет постоянно) или 0 (только резервирование)." >&2
  exit 1
fi
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
if [ "$ROLE" = main ] && [ ! -f "$APP/stock_state.json" ] && [ -f "$SRC/stock_state.json" ]; then
  install -m 644 -o "$RUN_USER" -g "$RUN_USER" "$SRC/stock_state.json" "$APP/stock_state.json"
fi

if [ ! -f "$APP/config.env" ]; then
  read -rsp "Токен бота от @BotFather (ввод скрыт): " token
  echo
  read -rp "Chat ID: " chat
  (umask 077 && printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$token" "$chat" > "$APP/config.env")
  chown "$RUN_USER:$RUN_USER" "$APP/config.env"

  echo "Проверяю Apple и Telegram — в Telegram придёт тестовое сообщение…"
  if ! as_service_user "$PYTHON" "$APP/check_stock.py" --test-telegram; then
    rm -f "$APP/config.env"
    echo "Telegram не принял токен или chat ID. Запустите установку ещё раз и введите их заново." >&2
    exit 1
  fi
fi
chown "$RUN_USER:$RUN_USER" "$APP/config.env"  # also when copied here from the main server
chmod 600 "$APP/config.env"

# The way in for GitHub and a standby. Its home, keys and program belong to root, so a key can
# do nothing but its one command, even if it leaks.
id "$FEED_USER" >/dev/null 2>&1 || useradd --system --user-group --home-dir "$FEED_HOME" --shell /bin/sh "$FEED_USER"
install -d -m 755 -o root -g root "$FEED_HOME" "$FEED_HOME/.ssh" "$FEED_LIB"
install -m 644 -o root -g root "$SRC/check_stock.py" "$FEED_LIB/check_stock.py"
install -d -m 2770 -o "$FEED_USER" -g "$RUN_USER" "$INBOX"
if [ -n "$FEED_KEY" ]; then
  printf '%s\n' "$FEED_KEY" > "$FEED_HOME/.ssh/feed_key.pub"
fi
if [ -n "$SYNC_KEY" ]; then
  printf '%s\n' "$SYNC_KEY" > "$FEED_HOME/.ssh/sync_key.pub"
fi
if [ -n "$PROBE_KEY" ]; then
  if [ "$PROBE_SOURCE" = third ]; then PROBE_FILE=probe_key.pub; else PROBE_FILE="probe_${PROBE_SOURCE}_key.pub"; fi
  printf '%s\n' "$PROBE_KEY" > "$FEED_HOME/.ssh/$PROBE_FILE"
fi
{
  if [ -f "$FEED_HOME/.ssh/feed_key.pub" ]; then
    printf 'restrict,command="%s -I %s/check_stock.py --ingest %s --source github" %s\n' \
      "$PYTHON" "$FEED_LIB" "$INBOX" "$(head -n 1 "$FEED_HOME/.ssh/feed_key.pub")"
  fi
  if [ "$ROLE" = main ] && [ -f "$FEED_HOME/.ssh/sync_key.pub" ]; then
    printf 'restrict,command="%s -I %s/check_stock.py --sync %s" %s\n' \
      "$PYTHON" "$FEED_LIB" "$INBOX" "$(head -n 1 "$FEED_HOME/.ssh/sync_key.pub")"
  fi
  for probe_source in third fourth fifth; do
    if [ "$probe_source" = third ]; then probe_file=probe_key.pub; else probe_file="probe_${probe_source}_key.pub"; fi
    if [ -f "$FEED_HOME/.ssh/$probe_file" ]; then
      printf 'restrict,command="%s -I %s/check_stock.py --ingest %s --source %s" %s\n' \
        "$PYTHON" "$FEED_LIB" "$INBOX" "$probe_source" "$(head -n 1 "$FEED_HOME/.ssh/$probe_file")"
    fi
  done
} > "$FEED_HOME/.ssh/authorized_keys.new"
if [ -s "$FEED_HOME/.ssh/authorized_keys.new" ]; then
  chmod 644 "$FEED_HOME/.ssh/authorized_keys.new" "$FEED_HOME"/.ssh/*.pub
  mv "$FEED_HOME/.ssh/authorized_keys.new" "$FEED_HOME/.ssh/authorized_keys"
else
  rm -f "$FEED_HOME/.ssh/authorized_keys.new" "$FEED_HOME/.ssh/authorized_keys"
fi

# A standby asks the main server how it is with its own key, as the service user.
if [ "$ROLE" = standby ]; then
  printf 'standby %s\n' "$MAIN_HOST" > "$ROLE_FILE"
  chmod 644 "$ROLE_FILE"
  install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$APP/.ssh"
  if [ ! -f "$APP/.ssh/sync_key" ]; then
    ssh-keygen -q -t ed25519 -N '' -C "standby-$(hostname -s)" -f "$APP/.ssh/sync_key"
  fi
  if [ -n "${MAIN_HOST_KEY:-}" ]; then
    printf '%s %s\n' "$MAIN_HOST" "$MAIN_HOST_KEY" > "$APP/.ssh/known_hosts"
  elif [ ! -s "$APP/.ssh/known_hosts" ]; then
    ssh-keyscan -t ed25519 "$MAIN_HOST" 2>/dev/null > "$APP/.ssh/known_hosts" || true
  fi
  cat > "$APP/.ssh/config" <<EOF
Host main
  HostName $MAIN_HOST
  User $FEED_USER
  IdentityFile $APP/.ssh/sync_key
  IdentitiesOnly yes
  UserKnownHostsFile $APP/.ssh/known_hosts
  StrictHostKeyChecking yes
  BatchMode yes
  ConnectTimeout 15
  ServerAliveInterval 15
  ServerAliveCountMax 2
  ControlMaster auto
  ControlPath $APP/.ssh/cm-%C
  ControlPersist 10m
EOF
  chown "$RUN_USER:$RUN_USER" "$APP/.ssh/sync_key" "$APP/.ssh/sync_key.pub" "$APP/.ssh/known_hosts" "$APP/.ssh/config"
  chmod 600 "$APP/.ssh/sync_key" "$APP/.ssh/config"
  chmod 644 "$APP/.ssh/sync_key.pub" "$APP/.ssh/known_hosts"
  WATCH="--watch --every $EVERY --offset $OFFSET --status-minutes $STATUS_MINUTES --inbox $INBOX --source backup --standby-of main"
  if [ "$PARTICIPATE" = 1 ]; then
    WATCH="--watch --every $EVERY --offset $OFFSET --status-minutes $STATUS_MINUTES --inbox $INBOX --source secondary --standby-of main --participate"
  fi
else
  WATCH="--watch --every $EVERY --offset $OFFSET --status-minutes $STATUS_MINUTES --inbox $INBOX"
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
ExecStart=$PYTHON -u $APP/check_stock.py $WATCH
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

# The bot answers on the main server only: Telegram lets one computer read the bot's messages.
UNITS=("$NAME.service")
if [ "$ROLE" = main ]; then
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
  UNITS+=("$NAME-bot.service")
fi

systemctl daemon-reload
systemctl enable --now "${UNITS[@]}"
sleep 5
journalctl -u "$NAME.service" -u "$NAME-bot.service" -n 6 --no-pager -o cat || true
if [ "$ROLE" = standby ]; then
  if [ "$PARTICIPATE" = 1 ]; then
    echo "Готово: дополнительный сервер проверяет каждые $EVERY с (сдвиг $OFFSET с) и передаёт результаты $MAIN_HOST; при сбое основного на 3 мин сам отправляет уведомления."
  else
    echo "Готово: запасной сервер для $MAIN_HOST; проверяет сам, если основной не работает 3 мин."
  fi
  if runuser -u "$RUN_USER" -- ssh -F "$APP/.ssh/config" -T main < /dev/null > /dev/null 2>&1; then
    echo "Связь с основным сервером есть."
  else
    echo "Основной сервер пока не отвечает этому: выполните там команду ниже. Пока связи не было ни разу, запасной не подменяет основной."
  fi
  echo "Ключ этого сервера для основного (выполнить там): sudo bash vps_install.sh sync-key '$(cat "$APP/.ssh/sync_key.pub")'"
else
  echo "Готово: сервер проверяет каждые $EVERY с (сдвиг $OFFSET с), сводка раз в $STATUS_MINUTES мин; бот отвечает на /iphone и /report. Всё запускается и после перезагрузки."
fi
if [ -f "$FEED_HOME/.ssh/feed_key.pub" ]; then
  echo "GitHub может передавать свои проверки (ключ установлен)."
else
  echo "Чтобы GitHub передавал свои проверки: sudo bash $0 feed-key 'ssh-ed25519 AAAA… github'"
fi
if [ "$ROLE" = main ] && [ -f "$FEED_HOME/.ssh/sync_key.pub" ]; then
  echo "Запасной сервер может узнавать, работает ли этот (ключ установлен)."
fi
echo "Логи: journalctl -u $NAME -f$([ "$ROLE" = main ] && echo "  и  journalctl -u $NAME-bot -f")"
