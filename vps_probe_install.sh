#!/bin/bash
# Install VPS 3 as a checking-only service. It sends results to the main VPS or VPS 2;
# it never takes over Telegram polling or stock alerts.
# Supply verified MAIN_HOST_KEY and BACKUP_HOST_KEY (ssh-ed25519 public host keys).
set -euo pipefail

APP="${APP:-/opt/iphone-stock-watch-hk}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
MAIN_HOST="${MAIN_HOST:-VPS1_IP}"
BACKUP_HOST="${BACKUP_HOST:-VPS2_IP}"
EVERY="${EVERY:-60}"
OFFSET="${OFFSET:-45}"
NAME=iphone-stock-watch-hk
RUN_USER=stockwatch
SRC="$(cd "$(dirname "$0")" && pwd)"
HOST_RE='^[A-Za-z0-9.:-]+$'
KEY_RE='^ssh-ed25519 [A-Za-z0-9+/]+={0,3}$'

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo." >&2
  exit 1
fi
if ! [[ "$MAIN_HOST" =~ $HOST_RE && "$BACKUP_HOST" =~ $HOST_RE \
        && "${MAIN_HOST_KEY:-}" =~ $KEY_RE && "${BACKUP_HOST_KEY:-}" =~ $KEY_RE \
        && "$EVERY" =~ ^[0-9]+$ && "$OFFSET" =~ ^[0-9]+$ ]] \
    || [ "$EVERY" -lt 10 ] || [ "$OFFSET" -ge "$EVERY" ]; then
  echo "Нужны адреса, проверенные публичные ключи обоих серверов и корректные EVERY/OFFSET." >&2
  exit 1
fi
if [ -f "$UNIT_DIR/$NAME.service" ] && ! grep -q -- '--probe feed' "$UNIT_DIR/$NAME.service"; then
  echo "На сервере уже настроена другая роль iPhone-проекта; её не меняю." >&2
  exit 1
fi
for program in python3 ssh ssh-keygen systemctl; do
  command -v "$program" >/dev/null || { echo "Установите $program." >&2; exit 1; }
done
PYTHON="$(command -v python3)"
if id "$RUN_USER" >/dev/null 2>&1; then
  if [ "$(getent passwd "$RUN_USER" | cut -d: -f6)" != "$APP" ]; then
    echo "Пользователь $RUN_USER уже принадлежит другой установке; его не меняю." >&2
    exit 1
  fi
else
  useradd --system --user-group --home-dir "$APP" --shell /usr/sbin/nologin "$RUN_USER"
fi
install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$APP" "$APP/.ssh"
if [ "$SRC/check_stock.py" != "$APP/check_stock.py" ]; then
  install -m 644 -o "$RUN_USER" -g "$RUN_USER" "$SRC/check_stock.py" "$APP/check_stock.py"
fi
if [ ! -f "$APP/config.env" ]; then
  (umask 077 && printf 'PART_NUMBERS=%s\n' "${PART_NUMBERS:-}" > "$APP/config.env")
fi
chown "$RUN_USER:$RUN_USER" "$APP/config.env"
chmod 600 "$APP/config.env"
if [ ! -f "$APP/.ssh/feed_key" ]; then
  ssh-keygen -q -t ed25519 -N '' -C iphone-stock-vps3-feed -f "$APP/.ssh/feed_key"
fi
printf '%s %s\n%s %s\n' "$MAIN_HOST" "$MAIN_HOST_KEY" "$BACKUP_HOST" "$BACKUP_HOST_KEY" > "$APP/.ssh/known_hosts"
cat > "$APP/.ssh/config" <<EOF
Host feed feed2
  User stockfeed
  IdentityFile $APP/.ssh/feed_key
  IdentitiesOnly yes
  UserKnownHostsFile $APP/.ssh/known_hosts
  StrictHostKeyChecking yes
  BatchMode yes
  ConnectTimeout 10
  ServerAliveInterval 15
  ServerAliveCountMax 2
  ControlMaster auto
  ControlPath $APP/.ssh/cm-%C
  ControlPersist 10m
Host feed
  HostName $MAIN_HOST
Host feed2
  HostName $BACKUP_HOST
EOF
chown "$RUN_USER:$RUN_USER" "$APP/.ssh/feed_key" "$APP/.ssh/feed_key.pub" "$APP/.ssh/known_hosts" "$APP/.ssh/config"
chmod 600 "$APP/.ssh/feed_key" "$APP/.ssh/config"
chmod 644 "$APP/.ssh/feed_key.pub" "$APP/.ssh/known_hosts"

cat > "$UNIT_DIR/$NAME.service" <<EOF
[Unit]
Description=VPS 3 Apple Store Hong Kong iPhone stock probe
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP
EnvironmentFile=$APP/config.env
ExecStart=$PYTHON -u $APP/check_stock.py --probe feed --backup feed2 --every $EVERY --offset $OFFSET --source third
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
systemctl daemon-reload
if [ "${START_SERVICE:-1}" = 1 ]; then
  systemctl enable "$NAME.service"
  systemctl restart "$NAME.service"
fi
echo "VPS 3: проверка каждые $EVERY с со сдвигом $OFFSET с; результаты идут основному VPS или VPS 2."
echo "Добавьте этот публичный ключ обоим получателям:"
cat "$APP/.ssh/feed_key.pub"
