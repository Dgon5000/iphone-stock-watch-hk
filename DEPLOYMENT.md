# Рабочее окружение

Репозиторий: https://github.com/Dgon5000/iphone-stock-watch-hk, ветка `main`.

Каждый участник проверяет Apple один раз в минуту по синхронизированным часам:

| Секунда минуты | Участник | Режим |
| --- | --- | --- |
| `:00` | GitHub Actions | `--probe feed --every 60 --offset 0 --minutes 330` |
| `:20` | `VPS2_IP` | `--watch --every 60 --offset 20 --source secondary --standby-of main --participate` |
| `:40` | `VPS1_IP` | `--watch --every 60 --offset 40` |

Оба VPS используют `--inbox /var/spool/iphone-stock-watch-hk`. Основной VPS
обрабатывает результаты всех трёх участников, ведёт историю и отправляет
уведомления. Telegram-команды обслуживает только его `iphone-stock-watch-hk-bot.service`.

В GitHub работает длительный процесс, который сам ждёт своей секунды минуты.
Цепочка запусков и `watchdog.yml` восстанавливают его после завершения.
Ожидание свободного runner и перезапуск workflow могут давать пропуски; GitHub
не гарантирует непрерывную работу. Ошибки сети также могут задержать запросы.
При отказах Apple сохраняются защитные паузы на 2, 4, 8 и максимум 15 минут.

Дополнительный VPS передаёт свои проверки через уже настроенный ограниченный
SSH-доступ `stockfeed` и `--sync`. При временном обрыве связи сохраняет результаты
в локальной очереди. После трёх минут недоступности основного обработчика он
берёт обработку уведомлений на себя; после восстановления передаёт историю и
состояние основному и продолжает регулярные проверки на `:20`. Он также может
подменять проверки недоступного участника в его минутном слоте. Это не применяется
при отказах Apple на другом узле.

## Доступ для обслуживания

SSH-логин на обоих VPS — `root`, порт — `22`. Отдельный ключ на компьютере владельца:
`~/.ssh/ADMIN_KEY`. Он не хранится в репозитории или GitHub Secrets.
Публичный ключ имеет комментарий `ADMIN_KEY_COMMENT`; прежние ключи сохранены.

```sh
ssh -i ~/.ssh/ADMIN_KEY -o IdentitiesOnly=yes root@VPS1_IP
ssh -i ~/.ssh/ADMIN_KEY -o IdentitiesOnly=yes root@VPS2_IP
```

Для GitHub используется существующая авторизация `gh` аккаунта `Dgon5000`.
Секреты `FEED_HOST`, `FEED_HOST_BACKUP`, `FEED_SSH_KEY`, `FEED_KNOWN_HOSTS`,
`TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` уже настроены.

## Границы изменений и проверка

На VPS изменяются только файлы проекта и его служба:

- `/opt/iphone-stock-watch-hk/check_stock.py` — программа службы;
- `/usr/local/lib/iphone-stock-watch-hk/check_stock.py` — копия для ограниченных SSH-команд;
- `/etc/systemd/system/iphone-stock-watch-hk.service` — расписание службы;
- при добавлении ключа — только новая строка `ADMIN_KEY_COMMENT` в `/root/.ssh/authorized_keys`.

При обновлении существующей установки бот, секреты, история, состояние и ключи
обмена остаются на месте. Переустанавливать службу через `vps_install.sh` для
смены одного расписания не требуется. Для новой установки его значения по
умолчанию соответствуют таблице; `PARTICIPATE=0` сохраняет старый пассивный режим.

```sh
systemctl status iphone-stock-watch-hk.service --no-pager
journalctl -u iphone-stock-watch-hk.service -n 30 --no-pager -o cat
```

Резервные копии перед сменой расписания:
`/var/backups/iphone-stock-watch-hk/three-slots-20261005/` на каждом VPS.
Резервная копия ключей до добавления доступа:
`/var/backups/iphone-stock-watch-hk/admin-access-20261005/authorized_keys`.
Для отката программы и расписания восстановить из первой папки `check_stock.py`,
`feed-check_stock.py` и `iphone-stock-watch-hk.service` на прежние пути, затем
выполнить `systemctl daemon-reload` и перезапустить только службу проверки.
GitHub-расписание при откате также нужно согласовать с восстановленными слотами.

Локальная проверка: `python3 -m unittest discover -s tests -q`.
Сценарии моделируют Apple, Telegram, SSH и время без внешних запросов.
