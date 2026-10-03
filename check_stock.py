#!/usr/bin/env python3
"""Apple Store Hong Kong pickup stock watcher.

Checks in-store pickup availability of the watched iPhone part numbers at every
Apple Store in Hong Kong and sends one Telegram alert when a configuration comes
into stock at any of them.
"""
import argparse
import itertools
import json
import os
import re
import ssl
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# iPhone 18 Pro Max 512GB, 1TB and 2TB in every colour Apple Hong Kong sells.
# Hong Kong part numbers end in ZA/A; UK ones (…QN/A) are not sold here.
# Override with the PART_NUMBERS environment variable (comma separated).
DEFAULT_PART_NUMBERS = [
    "MJXU4ZA/A",  # 512GB Silver
    "MJXT4ZA/A",  # 512GB Black
    "MJXW4ZA/A",  # 512GB Glacier
    "MJXV4ZA/A",  # 512GB Burgundy
    "MJXY4ZA/A",  # 1TB Silver
    "MJXX4ZA/A",  # 1TB Black
    "MJY14ZA/A",  # 1TB Glacier
    "MJY04ZA/A",  # 1TB Burgundy
    "MJY34ZA/A",  # 2TB Silver
    "MJY24ZA/A",  # 2TB Black
    "MJY54ZA/A",  # 2TB Glacier
    "MJY44ZA/A",  # 2TB Burgundy
]

# Hong Kong has no postcodes. Apple accepts a district name and returns all six
# Hong Kong stores for it; the location only changes the reported distances.
LOCATION = "Central"
# Apple silently drops every part number after the 20th in one request.
MAX_PARTS_PER_REQUEST = 20

APPLE_ENDPOINT = "https://www.apple.com/hk/shop/retail/pickup-message"
PRODUCT_URL = "https://www.apple.com/hk/shop/product/{part}"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_TOKEN_RE = re.compile(r"[0-9]+:[A-Za-z0-9_-]+")  # bot number, colon, secret
TELEGRAM_MAX_LENGTH = 4000  # Telegram allows 4096 characters per message.
# Report a broken watcher to Telegram only once it has been failing this long.
PROBLEM_ALERT_AFTER = timedelta(minutes=30)
# While Apple releases stock its answers flicker ("in stock", nothing, "in stock"
# seconds apart), so a sell-out is reported only if a second look this much later agrees.
CONFIRM_DELAY_SECONDS = 20
# Pauses between checks in --watch mode, in minutes, used in turn, and after how many
# full rounds of them a quiet status of everything watched is sent (0: never).
DEFAULT_SCHEDULE = "1,2,3"
DEFAULT_STATUS_ROUNDS = 3
# A request that fails on the network (timeout, reset) is tried once more after this pause.
NETWORK_RETRY_SECONDS = 5
# Telegram bot (--bot): commands in the bot's menu, commands older than this are ignored
# (sent while the bot was down), and repeated /iphone within this time reuse the last answer.
BOT_COMMANDS = [
    {"command": "iphone", "description": "Что сейчас в наличии"},
    {"command": "report", "description": "Статистика наличия"},
]
BOT_HELP = (
    "/iphone — что сейчас есть в наличии в Apple Store Hong Kong.\n"
    "/report — когда что появлялось и сколько держалось, с таблицей для Excel.\n\n"
    "Об изменениях наличия я пишу сам."
)
STALE_COMMAND_SECONDS = 120
STATUS_CACHE_SECONDS = 20
# Apple's product titles look like "iPhone 18 Pro Max 512GB Silver".
TITLE_RE = re.compile(r"(?P<model>.+?)\s+(?P<storage>\d+\s?[GT]B)\s+(?P<color>.+)")
COLOR_EMOJI = {
    "silver": "🩶", "black": "🖤", "glacier": "🩵", "burgundy": "❤️",
    "white": "🤍", "gold": "💛", "blue": "💙", "teal": "🩵", "pink": "🩷",
    "green": "💚", "sage": "💚", "lavender": "💜", "purple": "💜", "orange": "🧡", "red": "❤️",
}
STATE_FILE = Path(__file__).resolve().with_name("stock_state.json")
# One JSON line per check, for stock_report.py: when which configuration was in which store.
HISTORY_FILE = Path(__file__).resolve().with_name("stock_history.jsonl")
# Telegram settings for runs on a Mac or a server, kept outside the project folder
# so they cannot be uploaded to GitHub with it. Real environment variables win.
CONFIG_FILE = Path.home() / ".config" / "iphone-stock-watch-hk" / "config.env"
HKT = timezone(timedelta(hours=8))  # Hong Kong has no daylight saving time.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0 Safari/537.36"
)


@dataclass
class CheckResult:
    store_count: int
    products: dict  # part number -> product title reported by Apple
    available: list  # one dict per (store, part) with pickup stock
    missing_parts: list  # watched parts Apple returned no record for
    checked_at: object = None  # Apple's own time of the answer (aware datetime), if it sent one


def now_hkt():
    return datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S HKT")


def ssl_context():
    context = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    if not paths.cafile and not paths.capath and os.path.exists("/etc/ssl/cert.pem"):
        # python.org builds on macOS ship without CA certificates; use the system bundle.
        context.load_verify_locations("/etc/ssl/cert.pem")
    return context


def log(message, error=False):
    print(f"[{now_hkt()}] {message}", file=sys.stderr if error else sys.stdout, flush=True)


def clean(text):
    # Apple puts non-breaking spaces (U+00A0) in product titles.
    return " ".join(str(text or "").split())


def load_config():
    """Read KEY=VALUE lines from CONFIG_FILE into the environment, unless already set."""
    try:
        lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def configured_parts():
    raw = os.environ.get("PART_NUMBERS", "")
    parts = [p.upper() for p in raw.replace(",", " ").split()]
    return list(dict.fromkeys(parts)) or list(DEFAULT_PART_NUMBERS)


def env_int(name, default, minimum):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"ERROR: {name} must be a whole number, got {raw!r}.")
    if value < minimum:
        raise SystemExit(f"ERROR: {name} must be at least {minimum}, got {value}.")
    return value


def describe(title):
    """Split "iPhone 18 Pro Max 512GB Silver" into model, storage and colour."""
    match = TITLE_RE.fullmatch(title)
    return (match["model"], match["storage"].replace(" ", ""), match["color"]) if match else (title, "", "")


def color_emoji(color):
    # The last known word wins: "Space Black" -> black, "Light Gold" -> gold.
    for word in reversed(color.lower().split()):
        if word in COLOR_EMOJI:
            return COLOR_EMOJI[word]
    return "🎨"


def read_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def write_state(state):
    """Write the state file only when something besides the timestamp changed (so the
    GitHub workflow commits real changes only), and atomically: a service can be
    stopped at any moment."""
    state = {k: v for k, v in state.items() if k != "updated_hkt" and v is not None}
    previous = read_state()
    previous.pop("updated_hkt", None)
    if previous == state:
        return
    state["updated_hkt"] = now_hkt()
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, STATE_FILE)


def parse_utc(text):
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def known_stock(state, parts):
    """Watched configurations that were in stock at the last check, keyed by part number."""
    return {
        item["partNumber"]: item
        for item in state.get("available") or []
        if isinstance(item, dict) and item.get("partNumber") in parts
    }


def stock_snapshot(items):
    """One entry per part number, however many stores have it."""
    snapshot = {item["partNumber"]: {k: item.get(k, "") for k in ("partNumber", "product")} for item in items}
    return [snapshot[part] for part in sorted(snapshot)]


def parse_schedule(text):
    """Turn "1,2,3" (minutes) into pauses in seconds: [60, 120, 180]."""
    try:
        minutes = [float(x) for x in text.replace(" ", "").split(",") if x]
    except ValueError:
        minutes = []
    if not minutes or min(minutes) < 0.5:
        raise SystemExit(f"ERROR: the schedule must be minutes separated by commas, each at least 0.5; got {text!r}.")
    return [round(m * 60) for m in minutes]


def request_stores(parts):
    """Return Apple's Hong Kong store list with pickup availability for up to 20 parts."""
    params = {"pl": "true", "location": LOCATION}
    params.update({f"parts.{i}": part for i, part in enumerate(parts)})
    req = Request(
        f"{APPLE_ENDPOINT}?{urlencode(params)}",
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-HK,en;q=0.9",
            "User-Agent": USER_AGENT,
        },
    )

    for attempt in (1, 2):
        try:
            with urlopen(req, timeout=20, context=ssl_context()) as response:
                status = getattr(response, "status", 200)
                answered_at = apple_time(response)
                raw = response.read()
            break
        except HTTPError as exc:
            raise RuntimeError(f"Apple returned HTTP {exc.code}; not treating this as out of stock.") from exc
        except OSError as exc:  # URLError, timeouts, connection resets: usually gone a moment later
            reason = getattr(exc, "reason", exc)
            if attempt == 1:
                log(f"Apple did not answer ({reason}); trying again in {NETWORK_RETRY_SECONDS} s.")
                time.sleep(NETWORK_RETRY_SECONDS)
                continue
            raise RuntimeError(f"Could not reach Apple: {reason}; not treating this as out of stock.") from exc

    if status != 200:
        raise RuntimeError(f"Apple returned HTTP {status}; not treating this as out of stock.")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Apple returned a non-JSON response; not treating this as out of stock.") from exc

    body = payload.get("body") if isinstance(payload, dict) else None
    body = body if isinstance(body, dict) else {}
    stores = body.get("stores")
    if not isinstance(stores, list) or not stores:
        detail = f' Apple said: "{body["errorMessage"]}"' if body.get("errorMessage") else ""
        raise RuntimeError(
            "Apple response did not contain any stores; the endpoint may have changed or "
            f"no watched part number is a valid Hong Kong part.{detail}"
        )
    return stores, answered_at


def apple_time(response):
    """Apple's clock from the HTTP Date header: right even when this computer's clock is not."""
    try:
        moment = parsedate_to_datetime(response.headers["Date"])
    except Exception:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def fetch_stock(parts):
    store_numbers = set()
    products = {}
    available = []
    answered_at = None
    for start in range(0, len(parts), MAX_PARTS_PER_REQUEST):
        batch = parts[start:start + MAX_PARTS_PER_REQUEST]
        stores, answered_at = request_stores(batch)
        for store in stores:
            if not store.get("storeNumber"):
                continue
            store_numbers.add(store["storeNumber"])
            parts_availability = store.get("partsAvailability") or {}
            for part in batch:
                record = parts_availability.get(part)
                if not isinstance(record, dict):
                    continue

                # storeSelectionEnabled lives under messageTypes.regular, not at the top level.
                regular = (record.get("messageTypes") or {}).get("regular") or {}
                products.setdefault(part, clean(regular.get("storePickupProductTitle")) or part)

                pickup_display = str(record.get("pickupDisplay") or "").strip().lower()
                if pickup_display != "available" and regular.get("storeSelectionEnabled") is not True:
                    continue

                available.append(
                    {
                        "storeNumber": store["storeNumber"],
                        "storeName": clean(store.get("storeName")) or "Unknown store",
                        "city": clean(store.get("city")),
                        "partNumber": part,
                        "product": products[part],
                        "pickup": clean(
                            record.get("pickupSearchQuote")
                            or regular.get("storePickupQuote")
                            or "Available"
                        ),
                    }
                )

    missing = [part for part in parts if part not in products]
    return CheckResult(len(store_numbers), products, available, missing, answered_at)


def telegram_api(token, method, payload=None, timeout=30, document=None):
    """Call a Telegram Bot API method and return its result. `document` is a
    (filename, bytes) file to upload, e.g. for sendDocument."""
    if document:
        boundary = uuid.uuid4().hex
        fields = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
            for name, value in (payload or {}).items()
        ]
        filename, content = document
        fields.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8") + content + b"\r\n"
        )
        data = b"".join(fields) + f"--{boundary}--\r\n".encode("utf-8")
        content_type = f"multipart/form-data; boundary={boundary}"
    else:
        data = json.dumps(payload or {}).encode("utf-8")
        content_type = "application/json"
    request = Request(TELEGRAM_API.format(token=token, method=method), data=data, headers={"Content-Type": content_type})
    for attempt in (1, 2):
        try:
            with urlopen(request, timeout=timeout, context=ssl_context()) as response:
                reply = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                reply = json.loads(exc.read().decode("utf-8"))
            except Exception:
                reply = {}
            retry_after = (reply.get("parameters") or {}).get("retry_after")
            if exc.code == 429 and attempt == 1 and isinstance(retry_after, int) and retry_after <= 30:
                time.sleep(retry_after)
                continue
            # "from None": the chained exception would carry the URL, which contains the bot token.
            raise RuntimeError(f'Telegram {method} failed: HTTP {exc.code} {reply.get("description", "")}'.strip()) from None
        except OSError as exc:
            raise RuntimeError(f"Could not reach Telegram: {getattr(exc, 'reason', exc)}") from None
        except Exception as exc:  # e.g. http.client.InvalidURL, whose message would contain the token
            raise RuntimeError(f"Telegram {method} request failed: {type(exc).__name__}") from None
        if not reply.get("ok"):
            raise RuntimeError(f'Telegram {method} failed: {reply.get("description", "unknown error")}')
        return reply.get("result")


def split_message(text):
    """Split into messages within Telegram's length limit. Breaks go between blocks
    (blank lines) where possible and never inside a line, so HTML tags stay intact."""
    chunks = []
    current = ""

    def add(piece, separator):
        nonlocal current
        if current and len(current) + len(separator) + len(piece) > TELEGRAM_MAX_LENGTH:
            chunks.append(current)
            current = ""
        current = f"{current}{separator}{piece}" if current else piece

    for block in text.split("\n\n"):
        if len(block) <= TELEGRAM_MAX_LENGTH:
            add(block, "\n\n")
        else:
            lines = block.split("\n")
            add(lines[0], "\n\n")
            for line in lines[1:]:
                add(line, "\n")
    if current:
        chunks.append(current)
    return chunks


def telegram_settings():
    """The bot token and the chats from TELEGRAM_CHAT_ID, checked."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = os.environ.get("TELEGRAM_CHAT_ID", "").replace(",", " ").split()
    if not token or not chat_ids:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
    if not TELEGRAM_TOKEN_RE.fullmatch(token):
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is malformed: expected the bot number, a colon and the secret "
            "(123456789:AAH…) with no extra characters."
        )
    return token, chat_ids


def send_telegram(text, silent=False, chat_ids=None):
    """Send an HTML message to every chat in TELEGRAM_CHAT_ID (or to chat_ids); silent ones
    arrive without a sound."""
    token, configured = telegram_settings()
    for chat_id in chat_ids or configured:
        for chunk in split_message(text):
            telegram_api(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                    "disable_notification": silent,
                },
            )


def config_blocks(items, status=None, title="📱"):
    """Per model a bold "<title> <model>" header, then one line per configuration
    (colour emoji, storage, colour and status(item) if given) with a blank line
    between storage sizes."""
    groups = {}
    for item in items:
        model, storage, color = describe(item["product"])
        details = " · ".join(escape(x) for x in (storage, color) if x)
        suffix = status(item) if status else ""
        if details:
            line = f"{color_emoji(color)} {details}" + (f" — {suffix}" if suffix else "")
        else:
            line = suffix or escape(item["partNumber"])
        groups.setdefault(model, {}).setdefault(storage, []).append(line)
    return [
        f"<b>{title} {escape(model)}</b>\n" + "\n\n".join("\n".join(lines) for lines in by_storage.values())
        for model, by_storage in groups.items()
    ]


def checkout_link(item):
    return f'<a href="{escape(PRODUCT_URL.format(part=item["partNumber"]))}">🛒 Оформить</a>'


def checked_at(moment=None):
    """The check time in Hong Kong; pass Apple's time when known, since server clocks drift."""
    return f"🕐 Проверено: {(moment or datetime.now(timezone.utc)).astimezone(HKT):%d.%m.%Y %H:%M} (HKT)"


def change_message(appeared, sold_out, parts, moment=None):
    order = {part: i for i, part in enumerate(parts)}

    def in_watch_order(items):
        return sorted(items, key=lambda i: order.get(i["partNumber"], len(order)))

    blocks = ["🍏 Apple Store Hong Kong"]
    if appeared:
        blocks += config_blocks(in_watch_order(appeared), checkout_link, "🟢 Появились ·")
    if sold_out:
        blocks += config_blocks(in_watch_order(sold_out), None, "🔴 Закончились ·")
    blocks.append(checked_at(moment))
    return "\n\n".join(blocks)


def unavailable_summary(missing, watched):
    """Out-of-stock configurations in one line: "512GB — все цвета; 1TB: Black, Glacier"."""
    totals, gone = {}, {}
    for item in watched:
        model, storage, _ = describe(item["product"])
        totals[(model, storage)] = totals.get((model, storage), 0) + 1
    for item in missing:
        model, storage, color = describe(item["product"])
        gone.setdefault((model, storage), []).append(color or item["partNumber"])
    several_models = len({model for model, _ in totals}) > 1
    pieces = []
    for (model, storage), total in totals.items():  # watch-list order
        colors = gone.get((model, storage))
        if not colors:
            continue
        name = escape(f"{model} {storage}".strip() if several_models or not storage else storage)
        pieces.append(f"{name} — все цвета" if len(colors) == total else f"{name}: {escape(', '.join(colors))}")
    return "; ".join(pieces)


def status_message(result, parts):
    """Everything watched: what is in stock now (with links) and, in one line, what is not."""
    in_stock = {item["partNumber"] for item in result.available}
    watched = [{"partNumber": p, "product": result.products[p]} for p in parts if p in result.products]
    available = [item for item in watched if item["partNumber"] in in_stock]
    missing = [item for item in watched if item["partNumber"] not in in_stock]
    blocks = ["📋 Наличие в Apple Store Hong Kong"]
    if available:
        blocks += config_blocks(available, checkout_link, "🟢 В наличии ·")
    else:
        blocks.append("Сейчас ничего из отслеживаемого нет в наличии.")
    if missing:
        blocks.append(f"➖ Нет в наличии: {unavailable_summary(missing, watched)}")
    blocks.append(checked_at(result.checked_at))
    return "\n\n".join(blocks)


def stores_with_stock(result, part):
    return sorted((i["storeName"] for i in result.available if i["partNumber"] == part), key=str.lower)


def summary(result, parts):
    in_stock = []
    for part in parts:
        stores = stores_with_stock(result, part)
        if stores:
            in_stock.append(f'{result.products.get(part, part)}: {", ".join(stores)}')
    checked = len(parts) - len(result.missing_parts)
    status = "; ".join(in_stock) if in_stock else "no pickup stock"
    return f"{result.store_count} stores × {checked} models checked — {status}"


def actions_run_url():
    server, repo, run_id = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else ""


def report_health(state, problem):
    """Tell Telegram once when the watcher has been broken for a while and once when it recovers."""
    known = state.get("problem") if isinstance(state.get("problem"), dict) else None
    now = datetime.now(timezone.utc)

    if problem is None:
        if known and known.get("notified"):
            try:
                send_telegram("✅ Проверка наличия в Apple Store Hong Kong снова работает.")
            except Exception as exc:
                log(f"ERROR: could not send the Telegram recovery message: {exc}", error=True)
                return  # keep the problem so the recovery message is retried
        state["problem"] = None
        write_state(state)
        return

    if known is None:
        known = {"since_utc": now.isoformat(timespec="seconds"), "notified": False}
        state["problem"] = known
    since = parse_utc(known.get("since_utc")) or now
    if not known.get("notified") and now - since >= PROBLEM_ALERT_AFTER:
        minutes = int((now - since).total_seconds() // 60)
        text = (
            f"⚠️ Проверка наличия в Apple Store Hong Kong не работает уже {minutes} мин — "
            f"уведомления о наличии сейчас не придут.\n\n{escape(problem)}"
        )
        if actions_run_url():
            text += f'\n\n<a href="{escape(actions_run_url())}">Лог запуска</a>'
        try:
            send_telegram(text)
            known["notified"] = True
        except Exception as exc:
            log(f"ERROR: could not send the Telegram problem report: {exc}", error=True)
    write_state(state)


_history_started = False


def record_history(result=None, error=None):
    """Append one check to HISTORY_FILE: the time (Hong Kong) and which configurations were
    in stock in which stores, or the error. The first line of each run lists what is watched."""
    global _history_started
    moment = (result.checked_at if result and result.checked_at else datetime.now(timezone.utc)).astimezone(HKT)
    lines = []
    if result is None:
        lines.append({"time": moment.isoformat(timespec="seconds"), "error": str(error)[:300]})
    else:
        if not _history_started:
            lines.append({"time": moment.isoformat(timespec="seconds"), "watched": list(result.products.values())})
            _history_started = True
        in_stock = {}
        for item in result.available:
            in_stock.setdefault(item["product"], []).append(item["storeName"])
        lines.append({
            "time": moment.isoformat(timespec="seconds"),
            "stores": result.store_count,
            "in_stock": {product: sorted(stores, key=str.lower) for product, stores in in_stock.items()},
        })
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines))
    except OSError as exc:
        log(f"ERROR: could not write {HISTORY_FILE.name}: {exc}", error=True)


def observe(parts, record):
    """fetch_stock, also recorded in HISTORY_FILE when `record` is true."""
    try:
        result = fetch_stock(parts)
    except Exception as exc:
        if record:
            record_history(error=exc)
        raise
    if record:
        record_history(result)
    return result


def confirm_sold_out(parts, gone, record=True):
    """Look again before reporting a sell-out: Apple's answers flicker while stock is released."""
    time.sleep(CONFIRM_DELAY_SECONDS)
    try:
        again = observe(parts, record)
    except Exception as exc:
        log(f"Could not confirm the sell-out ({exc}); will look again at the next check.", error=True)
        return []
    back = {item["partNumber"] for item in again.available}
    confirmed = [item for item in gone if item["partNumber"] not in back]
    if len(confirmed) < len(gone):
        log(f"{len(gone) - len(confirmed)} configuration(s) were back on a second look (Apple flicker); not reported.")
    return confirmed


def check_once(parts, state, dry_run, prefix=""):
    """One check: tell Telegram what came into stock or sold out, then update the state.
    Returns (Apple's answer or None, whether a change was reported, whether an alert failed or
    a part number is unknown, the problem for report_health)."""
    try:
        result = observe(parts, record=not dry_run)
    except Exception as exc:
        log(f"{prefix}ERROR: {exc}", error=True)
        return None, False, False, f"Последняя ошибка: {exc}"
    log(prefix + summary(result, parts))

    failed, problem = False, None
    if result.missing_parts:
        failed = True
        problem = f'Apple не вернул данные по артикулам {", ".join(result.missing_parts)}. Проверьте PART_NUMBERS.'
        log(
            f'ERROR: Apple returned no availability record for {", ".join(result.missing_parts)}. '
            "Hong Kong part numbers look like MJXU4ZA/A; not treating this as out of stock.",
            error=True,
        )

    known = known_stock(state, parts)
    current = {}
    for item in result.available:
        current.setdefault(item["partNumber"], item)
    appeared = [current[part] for part in parts if part in current and part not in known]
    sold_out = [known[part] for part in parts if part in known and part not in current]
    if sold_out:
        sold_out = confirm_sold_out(parts, sold_out, record=not dry_run)

    if appeared or sold_out:
        log(f"CHANGES: {len(appeared)} came into stock, {len(sold_out)} sold out.")
        message = change_message(appeared, sold_out, parts, result.checked_at)
        if dry_run:
            print(f"--- dry run, Telegram message not sent ---\n{message}\n---", flush=True)
            return result, True, failed, problem
        try:
            send_telegram(message)
        except Exception as exc:
            # Keep the old state so the next check reports the changes again.
            log(f"ERROR: could not send the Telegram alert: {exc}", error=True)
            return result, False, True, problem
        log("Telegram alert sent.")
    elif current:
        log(f"No changes: {len(current)} configuration(s) still in stock.")

    if not dry_run:
        gone = {item["partNumber"] for item in sold_out}
        still = [item for part, item in known.items() if part not in gone]
        state.update(part_numbers=parts, location=LOCATION, available=stock_snapshot(still + appeared))
        state.pop("recently_gone", None)  # used by older versions
        write_state(state)
    return result, bool(appeared or sold_out), failed, problem


def run_checks(parts, checks, pauses, dry_run, status_every=0):
    """Run `checks` checks (None: forever), pausing between them for the seconds in
    `pauses` (a number or a list used in turn). Every `status_every` full rounds of pauses
    (0: never) a quiet summary of everything watched follows. Return the process exit code."""
    pauses = list(pauses) if isinstance(pauses, (list, tuple)) else [pauses]
    schedule = itertools.cycle(pauses)
    state = read_state()
    answered = 0
    failed = False
    n = 0
    while checks is None or n < checks:
        if n:
            pause = next(schedule)
            if checks is None:
                next_at = datetime.now(HKT) + timedelta(seconds=pause)
                log(f"Next check in {pause / 60:g} min, at {next_at:%H:%M:%S} HKT.")
            time.sleep(pause)
        n += 1
        prefix = f"Check {n}/{checks}: " if checks and checks > 1 else ""
        result, reported, alert_failed, problem = check_once(parts, state, dry_run, prefix)
        answered += result is not None
        failed = failed or alert_failed
        if not dry_run:
            report_health(state, problem)

        pauses_taken = n - 1
        rounds_done = pauses_taken // len(pauses) if pauses_taken % len(pauses) == 0 else 0
        if status_every and rounds_done and rounds_done % status_every == 0 and result and not reported:
            message = status_message(result, parts)
            if dry_run:
                print(f"--- dry run, status not sent ---\n{message}\n---", flush=True)
                continue
            try:
                send_telegram(message, silent=True)
                log("Status sent to Telegram.")
            except Exception as exc:
                log(f"ERROR: could not send the status: {exc}", error=True)

    if not answered:
        log("ERROR: every check failed; Apple may be blocking requests or the endpoint changed.", error=True)
        return 1
    return 1 if failed else 0


def answer_command(token, allowed, message, parts, cache):
    """Reply to /iphone, /start or /help from the configured chats. `cache` is
    (time.monotonic(), CheckResult) of the last /iphone check; returns it updated."""
    text = (message.get("text") or "").strip()
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if command not in ("/iphone", "/report", "/start", "/help"):
        return cache
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if chat_id not in allowed:
        log(f"Ignored {command} from chat {chat_id}: it is not in TELEGRAM_CHAT_ID.")
        return cache
    if time.time() - message.get("date", 0) > STALE_COMMAND_SECONDS:
        return cache  # sent while the bot was not running

    try:
        if command == "/report":
            send_report(token, chat_id)
            return cache
        if command != "/iphone":
            send_telegram(BOT_HELP, chat_ids=[chat_id])
            return cache
        telegram_api(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
        if cache is None or time.monotonic() - cache[0] > STATUS_CACHE_SECONDS:
            try:
                cache = (time.monotonic(), fetch_stock(parts))
            except Exception as exc:
                send_telegram(
                    f"⚠️ Не удалось проверить наличие: {escape(str(exc))}\n\nПопробуйте ещё раз через минуту.",
                    chat_ids=[chat_id],
                )
                return cache
        send_telegram(status_message(cache[1], parts), chat_ids=[chat_id])
        log(f"Answered /iphone in chat {chat_id}.")
    except Exception as exc:
        log(f"ERROR: could not answer {command}: {exc}", error=True)
    return cache


def send_report(token, chat_id):
    """The history report (stock_report.py) as a message, plus the CSV of every appearance."""
    import stock_report  # imports this module, so only when needed

    try:
        watched, checks = stock_report.load([HISTORY_FILE])
    except OSError:
        watched, checks = [], []
    if not checks:
        send_telegram("Журнал наличия пока пуст: он заполняется с каждой проверкой.", chat_ids=[chat_id])
        return
    telegram_api(token, "sendChatAction", {"chat_id": chat_id, "action": "upload_document"})
    send_telegram(escape(stock_report.report(watched, checks, show_last=3, one_per_line=True)), chat_ids=[chat_id])
    telegram_api(
        token,
        "sendDocument",
        {"chat_id": chat_id, "caption": "Все появления — таблица для Excel/Numbers"},
        document=("stock_intervals.csv", stock_report.csv_text(watched, checks).encode("utf-8-sig")),
    )
    log(f"Sent the report to chat {chat_id}.")


def run_bot(parts):
    """Answer /iphone with what is in stock right now. Runs next to --watch and never
    touches its state or schedule."""
    token, allowed = telegram_settings()
    # The list for private chats overrides the default one, so set both; the Menu button
    # then shows these commands.
    for scope in ({"type": "default"}, {"type": "all_private_chats"}):
        telegram_api(token, "setMyCommands", {"commands": BOT_COMMANDS, "scope": scope})
    telegram_api(token, "setChatMenuButton", {"menu_button": {"type": "commands"}})
    log("Bot is listening for /iphone and /report.")
    offset, cache = None, None
    while True:
        poll = {"timeout": 50, "allowed_updates": ["message"]}
        if offset is not None:
            poll["offset"] = offset
        try:
            updates = telegram_api(token, "getUpdates", poll, timeout=65) or []
        except RuntimeError as exc:
            log(f"ERROR: {exc}; trying again in 10 s.", error=True)
            time.sleep(10)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            cache = answer_command(token, allowed, update.get("message") or {}, parts, cache)


def send_test_message(parts):
    moment = None
    try:
        result = fetch_stock(parts)
    except Exception as exc:
        status = [f"⚠️ Не удалось проверить наличие: {escape(str(exc))}"]
    else:
        moment = result.checked_at
        in_stock = {item["partNumber"] for item in result.available}

        def availability(item):
            if item["partNumber"] in result.missing_parts:
                return "⚠️ нет данных, проверьте артикул"
            return "✅ в наличии" if item["partNumber"] in in_stock else "➖ нет"

        items = [{"partNumber": part, "product": result.products.get(part, part)} for part in parts]
        status = ["Сейчас:", *config_blocks(items, availability)]
    send_telegram(
        "\n\n".join(["🧪 Тест: уведомления о наличии в Apple Store Hong Kong будут приходить сюда.", *status, checked_at(moment)])
    )
    log("Test Telegram message sent.")


def main():
    load_config()
    parser = argparse.ArgumentParser(description="Apple Store Hong Kong pickup stock watcher.")
    parser.add_argument(
        "--checks",
        type=int,
        default=env_int("CHECKS_PER_RUN", 1, 1),
        help="number of checks in this run (default: CHECKS_PER_RUN or 1)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=env_int("CHECK_INTERVAL_SECONDS", 60, 10),
        help="seconds between checks (default: CHECK_INTERVAL_SECONDS or 60)",
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        const="",
        metavar="MINUTES",
        help=f"check forever, pausing these minutes in turn (default: CHECK_SCHEDULE_MINUTES or {DEFAULT_SCHEDULE})",
    )
    parser.add_argument(
        "--status-every",
        type=int,
        default=DEFAULT_STATUS_ROUNDS,
        metavar="ROUNDS",
        help=f"with --watch: a quiet status after every ROUNDS full rounds of pauses, 0 = never (default {DEFAULT_STATUS_ROUNDS})",
    )
    parser.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them; keep the state file")
    parser.add_argument("--test-telegram", action="store_true", help="send a test Telegram message and exit")
    parser.add_argument("--status", action="store_true", help="send what is in stock now to Telegram and exit")
    parser.add_argument("--bot", action="store_true", help="answer the /iphone command in Telegram (runs until stopped)")
    args = parser.parse_args()

    parts = configured_parts()
    if args.test_telegram:
        send_test_message(parts)
        return 0
    if args.bot:
        return run_bot(parts)
    if args.status:
        send_telegram(status_message(fetch_stock(parts), parts))
        log("Status sent to Telegram.")
        return 0
    if args.watch is not None:
        if args.status_every < 0:
            parser.error("--status-every must be 0 or more")
        pauses = parse_schedule(args.watch or os.environ.get("CHECK_SCHEDULE_MINUTES") or DEFAULT_SCHEDULE)
        minutes = ", ".join(f"{p / 60:g}" for p in pauses)
        status = "no status messages."
        if args.status_every:
            status = f"a quiet status every {args.status_every} round(s), {sum(pauses) * args.status_every / 60:g} min."
        log(f"Watching {len(parts)} configurations; pauses of {minutes} min in turn; {status}")
        return run_checks(parts, None, pauses, args.dry_run, args.status_every)
    if args.checks < 1 or args.interval < 10:
        parser.error("--checks must be at least 1 and --interval at least 10 seconds")
    return run_checks(parts, args.checks, args.interval, args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
