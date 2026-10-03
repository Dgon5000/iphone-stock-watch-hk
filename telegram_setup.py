#!/usr/bin/env python3
"""Find the Telegram chat ID for stock alerts and send a confirmation message.

1. In Telegram, create a bot with @BotFather (/newbot) and copy its token.
2. Open the new bot and press Start.
3. Run: python3 telegram_setup.py
"""
import getpass
import os
import re
import sys

from check_stock import CONFIG_FILE, TELEGRAM_TOKEN_RE, telegram_api


def normalize_token(raw):
    """Undo common copy-paste accidents: spaces, invisible characters, quotes, a "bot" prefix."""
    token = "".join(ch for ch in raw if ch.isprintable() and not ch.isspace())
    token = token.strip("'\"`<>«»")
    return re.sub(r"^bot(?=[0-9])", "", token, flags=re.IGNORECASE)


def token_problem(token):
    """Say what is wrong with a malformed token without printing its secret part."""
    if TELEGRAM_TOKEN_RE.fullmatch(token):
        return None
    bot_id, colon, secret = token.partition(":")
    if not colon:
        return f"в нём нет двоеточия (введено символов: {len(token)})"
    if not re.fullmatch(r"[0-9]+", bot_id):
        return f"до двоеточия должен быть только номер бота из цифр, а введено «{bot_id}»"
    return "после двоеточия есть лишние символы: допустимы только латинские буквы, цифры, «-» и «_»"


def explain(exc, secret):
    text = str(exc)
    if "HTTP 401" in text:
        hint = " Похоже, он скопирован не полностью." if len(secret) < 30 else ""
        return (
            f"Telegram не принял токен (401 Unauthorized): он неверный или отозван.{hint}\n"
            "Актуальный токен: @BotFather → /mybots → ваш бот → API Token."
        )
    if "HTTP 404" in text:
        return "Telegram не узнал токен (404 Not Found): в нём лишние или недостающие символы. Скопируйте его заново."
    return f"Ошибка: {text}"


def save_config(token, chat_id):
    """Keep the Telegram settings for runs on this computer, readable only by the user."""
    CONFIG_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    keep = []
    if CONFIG_FILE.exists():
        keep = [
            line
            for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines()
            if not line.startswith(("TELEGRAM_BOT_TOKEN=", "TELEGRAM_CHAT_ID="))
        ]
    text = "\n".join(keep + [f"TELEGRAM_BOT_TOKEN={token}", f"TELEGRAM_CHAT_ID={chat_id}"]) + "\n"
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(CONFIG_FILE, 0o600)


def chat_name(chat):
    if chat.get("title"):
        return chat["title"]
    name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
    return name or (f'@{chat["username"]}' if chat.get("username") else "")


def find_chats(token):
    chats = {}
    for update in telegram_api(token, "getUpdates") or []:
        for kind in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(kind) or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat
    return chats


def main():
    token = normalize_token(getpass.getpass("Токен бота от @BotFather (ввод скрыт, вставьте Cmd+V и нажмите Enter): "))
    if not token:
        sys.exit("Токен не введён.")
    problem = token_problem(token)
    if problem:
        sys.exit(
            f"Это не похоже на токен бота: {problem}.\n"
            "Скопируйте токен из сообщения @BotFather целиком (вид 123456789:AAH…), запустите скрипт заново, "
            "вставьте токен одним нажатием Cmd+V и сразу нажмите Enter."
        )
    bot_id, _, secret = token.partition(":")
    print(f"Токен по формату в порядке: бот {bot_id}, секретная часть — {len(secret)} символов.")

    try:
        bot = telegram_api(token, "getMe")
        print(f"Бот: @{bot['username']}")
        chats = find_chats(token)
        while not chats:
            input(f"Боту ещё никто не писал. Откройте https://t.me/{bot['username']}, нажмите Start и затем Enter здесь… ")
            chats = find_chats(token)

        print("\nЧаты, которые написали боту:")
        for chat_id, chat in chats.items():
            print(f"  {chat_id}  ({chat.get('type')}) {chat_name(chat)}")

        if len(chats) == 1:
            chat_id = next(iter(chats))
            telegram_api(
                token,
                "sendMessage",
                {"chat_id": chat_id, "text": "✅ Этот чат подключён к iPhone Stock Watch HK."},
            )
            print(f"\nОтправил подтверждение в чат {chat_id}.")
            chat_hint = str(chat_id)
        else:
            chat_hint = "ID нужного чата из списка выше (можно несколько через запятую)"
    except RuntimeError as exc:
        sys.exit(explain(exc, secret))

    print("\nДля GitHub (Settings → Secrets and variables → Actions → New repository secret):")
    print("  TELEGRAM_BOT_TOKEN = токен, который вы ввели")
    print(f"  TELEGRAM_CHAT_ID   = {chat_hint}")

    if len(chats) == 1:
        answer = input(f"\nСохранить токен и chat ID для запуска на этом компьютере ({CONFIG_FILE})? [Д/н] ")
        if answer.strip().lower() in ("", "д", "да", "y", "yes"):
            save_config(token, chat_hint)
            print(f"Сохранено в {CONFIG_FILE}, доступ только у вашей учётной записи.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)
