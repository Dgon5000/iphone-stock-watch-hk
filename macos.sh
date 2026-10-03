#!/bin/bash
# Run the Apple Store Hong Kong stock watch on this Mac with launchd: checks all
# the time while the Mac is awake, pausing 1, 2 and 3 minutes in turn (a quiet status every 3 rounds),
# also after a restart or login.
#
#   bash macos.sh install [minutes]   start (other pauses are optional, e.g. 5,3,4)
#   bash macos.sh status              is it running, last checks
#   bash macos.sh log                 follow the log (Ctrl+C to quit)
#   bash macos.sh uninstall           stop and remove
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.iphone-stock-watch-hk"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/iphone-stock-watch-hk.log"
CONFIG="$HOME/.config/iphone-stock-watch-hk/config.env"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
  install)
    schedule="${2:-1,2,3}"
    if ! [[ "$schedule" =~ ^[0-9]+(\.[0-9]+)?(,[0-9]+(\.[0-9]+)?)*$ ]]; then
      echo "Паузы — минуты через запятую, например 1,2,3." >&2
      exit 1
    fi
    python="$(command -v python3 || true)"
    if [ -z "$python" ]; then
      echo "Не найден python3. Установите Python 3 с https://www.python.org/downloads/" >&2
      exit 1
    fi
    if [ ! -f "$CONFIG" ]; then
      echo "Нет настроек Telegram. Сначала запустите: python3 telegram_setup.py" >&2
      exit 1
    fi

    echo "Проверяю Apple и Telegram — в Telegram придёт тестовое сообщение…"
    "$python" "$DIR/check_stock.py" --test-telegram

    mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
    "$python" - "$PLIST" "$LABEL" "$python" "$DIR" "$schedule" "$LOG" <<'PY'
import plistlib
import sys

path, label, python, folder, schedule, log = sys.argv[1:]
with open(path, "wb") as f:
    plistlib.dump(
        {
            "Label": label,
            "ProgramArguments": [python, "-u", f"{folder}/check_stock.py", "--watch", schedule],
            "WorkingDirectory": folder,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "StandardOutPath": log,
            "StandardErrorPath": log,
            "ProcessType": "Background",
        },
        f,
    )
PY
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "Готово: проверка идёт постоянно, паузы $schedule мин по кругу, пока Mac не спит. Лог: $LOG"
    ;;

  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      echo "Работает."
    else
      echo "Не запущено."
    fi
    if [ -f "$LOG" ]; then
      echo "Последние записи:"
      tail -n 6 "$LOG"
    fi
    ;;

  log)
    touch "$LOG"
    tail -f "$LOG"
    ;;

  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Остановлено и удалено. Лог остался: $LOG"
    ;;

  *)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
