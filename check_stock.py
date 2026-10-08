#!/usr/bin/env python3
"""Apple Store Hong Kong pickup stock watcher.

Checks in-store pickup availability of the watched iPhone part numbers at every
Apple Store in Hong Kong and sends one Telegram alert when a configuration comes
into stock at any of them.

Nine computers share a 135-second cycle (CYCLE_SLOTS): GitHub at offset 0 (--probe), a
participating standby at offset 15 (--watch --standby-of HOST --participate), the main
server at offset 30 (--watch --every 135 --offset 30 --inbox DIR), then VPS 3 to VPS 8 at
offsets 45 to 120 (--probe); when one is out, the others close up (see CHECK_SPACING).
All remote computers hand their
answers to the main server over SSH. Only the main server sends stock alerts and keeps
the combined history while it is healthy; the standby takes over during an outage.
"""
import argparse
import itertools
import json
import math
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
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
# seconds apart), so a sell-out is reported only if another check this much later agrees.
CONFIRM_DELAY_SECONDS = 20
# Checks by different computers at most this much apart were made at about the same time
# (VPS 1's own answer can come late, stamped with Apple's time of answering), so one that is a
# little older than the newest check acted upon still counts. Older ones only go into the
# history, and so does a computer's check that is not newer than its own last one (a repeat).
SIMULTANEOUS = timedelta(seconds=2)
# Pauses between checks in --watch mode, in minutes, used in turn (--every sets fixed times
# instead), and a quiet status of everything watched every this many minutes (0: never).
DEFAULT_SCHEDULE = "1,2,3"
DEFAULT_STATUS_MINUTES = 10
# A request that fails on the network (timeout, reset) is tried once more after this pause.
NETWORK_RETRY_SECONDS = 5
# --watch --every waits this long for its own answer from Apple (usually 1–2 s). A slower
# answer (it can take over a minute) is awaited in the background while the checks the other
# computers hand in are handled (see OwnCheck).
OWN_ANSWER_WAIT = 5
# --watch --inbox looks for the checks other computers hand in this often, so each one is
# handled, and any alert sent, within this many seconds of arriving.
INBOX_POLL_SECONDS = 0.2
# Telegram usually answers within a second, but now and then a connection hangs for a minute.
# A message is then given up after TELEGRAM_SEND_TIMEOUT and sent once more on a fresh
# connection. The quiet status is waited for STATUS_SEND_WAIT; a slower one finishes in the
# background, so it never holds up the checks and the alerts.
TELEGRAM_SEND_TIMEOUT = 10
STATUS_SEND_WAIT = 3
# When Apple refuses requests (HTTP 403, 429, or 541 — Apple's own "too many requests from
# this address"), the computer pauses its checks for 4, 8 and then at most 15 minutes
# instead of insisting. (Apple goes on refusing an address for well over 10 minutes: a retry
# after 2 minutes was refused nearly every time.)
REFUSED_CODES = (403, 429, 541)
MAX_REFUSED_PAUSE_SECONDS = 15 * 60
# Checks handed in by another computer (--probe → --ingest → --watch --inbox): at most this
# big, at most this many waiting, and read by the watcher within INBOX_STALE_SECONDS. The
# checking as a whole works while some computer had an answer from Apple this recently.
MAX_CHECK_BYTES = 64 * 1024
INBOX_LIMIT = 500
INBOX_STALE_SECONDS = 300
WORKING_WINDOW = timedelta(minutes=3)
# One set of server names for check counts, errors, recovery notices and logs.
SOURCE_NAMES = {"vps": "VPS 1", "github": "GitHub", "mac": "Mac", "backup": "VPS 2",
                "secondary": "VPS 2", "third": "VPS 3", "fourth": "VPS 4", "fifth": "VPS 5",
                "sixth": "VPS 6", "seventh": "VPS 7", "eighth": "VPS 8"}
CYCLE_SLOTS = {"github": 0, "secondary": 15, "vps": 30, "third": 45, "fourth": 60, "fifth": 75,
               "sixth": 90, "seventh": 105, "eighth": 120}
COUNT_ORDER = ("vps", "secondary", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "github")
# A standby server (--standby-of) asks the main server how it is at this many seconds past
# every minute (--sync), keeping a copy of its stock. It stands in once the main server has
# not been working for MAIN_SILENT_AFTER; back after a break that long, the main server
# waits up to RETURN_WAIT for the standby's stock before it reports changes, so that
# nothing is reported twice. Files they share sit in the inbox folder.
SYNC_OFFSET = 45
MAIN_SILENT_AFTER = timedelta(minutes=3)
RETURN_WAIT = timedelta(minutes=2)
HANDBACK_QUIET = timedelta(seconds=75)  # after a handback, until the standby has stood aside
MAX_SYNC_BYTES = 16 * 1024 * 1024
# The computers in CYCLE_SLOTS share the checking. With n of them taking part, each checks
# every max(n × CHECK_SPACING, MIN_EVERY) seconds at its own evenly spaced place (see place),
# so Apple is asked every CHECK_SPACING seconds while five or six take part, and never more
# often than every MIN_EVERY seconds from one computer, lest Apple refuse the others in turn.
# A computer that is out — nothing heard from it for out_after(), Apple refused its last check
# (it pauses, see Backoff) or it could not reach Apple UNREACHABLE_AFTER times in a row — is
# left out at once and the others close up; its next good check takes it back. The main
# server works out who takes part (Watcher.present) and says so in STATUS_SYNC and in every
# answer to --ingest and --sync; every computer takes it in only with the answer to its own
# check, and one that has heard nothing for PLAN_TTL goes back to its own place. When the
# cycle changes, a computer still waits MIN_OWN_GAP seconds after its last request, so its
# checks may then come up to a full cycle plus MIN_OWN_GAP apart: out_after() leaves room for
# that and for a slow delivery (OUT_SLACK).
CHECK_SPACING = 15
MIN_EVERY = 75
OUT_SLACK = 15
UNREACHABLE_AFTER = 2
PLAN_TTL = timedelta(minutes=5)
MIN_OWN_GAP = 30
STATUS_SYNC = "status.sync"  # the main server: alive when, last check, stock, who takes part
HANDBACK_SYNC = "handback.sync"  # the standby's stock after standing in
HISTORY_SYNC = "history-*.sync"  # the standby's checks while standing in
# Telegram bot (--bot): commands in the bot's menu, commands older than this are ignored
# (sent while the bot was down), and repeated /iphone within this time reuse the last answer.
BOT_COMMANDS = [
    {"command": "iphone", "description": "Что сейчас в наличии"},
    {"command": "report", "description": "Статистика наличия"},
]
BOT_HELP = (
    "/iphone — что сейчас есть в наличии в Apple Store Hong Kong.\n"
    "/report — статистика: по памяти — в какие часы она появляется, или всё сразу.\n\n"
    "Об изменениях наличия я пишу сам."
)
STALE_COMMAND_SECONDS = 120
STATUS_CACHE_SECONDS = 20
REPORT_ALL = "report:all"  # the data of /report's "Все" button
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
BACKOFF_FILE = Path(__file__).resolve().with_name("apple_backoff.json")
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


class AppleHTTPError(RuntimeError):
    """Apple answered with an HTTP error status (`code`)."""

    def __init__(self, code):
        super().__init__(f"Apple returned HTTP {code}; not treating this as out of stock.")
        self.code = code


def utc_now():
    return datetime.now(timezone.utc)


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


def source_name(source):
    """How a checking computer is called in logs and messages: "github" -> GitHub."""
    return SOURCE_NAMES.get(source, source)


def unreachable(error):
    """Whether a check failed because the computer could not reach Apple (network, timeouts),
    not because Apple answered with an error (see request_stores)."""
    return str(error).startswith("Could not reach Apple")


def refused(error):
    """Whether Apple refused the check (REFUSED_CODES), so that the computer pauses (see
    Backoff). `error` is the exception here, or its text from another computer."""
    codes = "|".join(str(code) for code in REFUSED_CODES)
    return getattr(error, "code", None) in REFUSED_CODES or bool(re.search(rf"\bHTTP ({codes})\b", str(error)))


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
            exc.close()  # only the status matters; free the connection now
            raise AppleHTTPError(exc.code) from exc
        except OSError as exc:  # URLError, timeouts, connection resets: usually gone a moment later
            reason = getattr(exc, "reason", exc)
            if attempt == 1:
                log(f"Apple did not answer ({reason}); trying again in {NETWORK_RETRY_SECONDS} s.")
                time.sleep(NETWORK_RETRY_SECONDS)
                continue
            raise RuntimeError(f"Could not reach Apple: {reason}; not treating this as out of stock.") from exc

    if status != 200:
        raise AppleHTTPError(status)

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
            if attempt == 1:  # a hung or reset connection: a fresh one usually works at once
                log(f"Telegram did not answer ({getattr(exc, 'reason', exc)}); trying again.")
                continue
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


def send_telegram(text, silent=False, chat_ids=None, buttons=None):
    """Send an HTML message to every chat in TELEGRAM_CHAT_ID (or to chat_ids); silent ones
    arrive without a sound. `buttons` (rows of inline buttons) go under its last part."""
    token, configured = telegram_settings()
    for chat_id in chat_ids or configured:
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
                "disable_notification": silent,
            }
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            telegram_api(token, "sendMessage", payload, timeout=TELEGRAM_SEND_TIMEOUT)


def tell(text):
    """send_telegram for notices about the watcher itself; True if it went out."""
    try:
        send_telegram(text)
        return True
    except Exception as exc:
        log(f"ERROR: could not send a notice to Telegram: {exc}", error=True)
        return False


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
    return f"🕐 Проверено: {(moment or utc_now()).astimezone(HKT):%d.%m.%Y %H:%M} (HKT)"


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


def status_message(result, parts, footer=None):
    """The configurations currently in stock (with links), without an unavailable list.
    `footer` goes under the check time."""
    in_stock = {item["partNumber"] for item in result.available}
    watched = [{"partNumber": p, "product": result.products[p]} for p in parts if p in result.products]
    available = [item for item in watched if item["partNumber"] in in_stock]
    blocks = ["📋 Наличие в Apple Store Hong Kong"]
    if available:
        blocks += config_blocks(available, checkout_link, "🟢 В наличии ·")
    else:
        blocks.append("Сейчас ничего из отслеживаемого нет в наличии.")
    blocks.append(checked_at(result.checked_at) + (f"\n{footer}" if footer else ""))
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
    now = utc_now()

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


def record_history(result=None, error=None, source=None, moment=None):
    """Append one check to HISTORY_FILE: the time (Hong Kong), the computer that checked (when
    several do) and which configurations were in stock in which stores, or the error. The first
    line of each run lists what is watched. Returns the lines."""
    global _history_started
    moment = (moment or (result.checked_at if result else None) or utc_now()).astimezone(HKT)
    stamp = {"time": moment.isoformat(timespec="seconds")}
    if source:
        stamp["source"] = source
    lines = []
    if result is None:
        lines.append({**stamp, "error": str(error)[:300]})
    else:
        if not _history_started:
            lines.append({"time": stamp["time"], "watched": list(result.products.values())})
            _history_started = True
        in_stock = {}
        for item in result.available:
            in_stock.setdefault(item["product"], []).append(item["storeName"])
        lines.append({
            **stamp,
            "stores": result.store_count,
            "in_stock": {product: sorted(stores, key=str.lower) for product, stores in in_stock.items()},
        })
    append_history(lines)
    return lines


def append_history(lines):
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


def missing_problem(result):
    """The problem for report_health if Apple returned nothing for some watched part numbers."""
    if not result.missing_parts:
        return None
    log(
        f'ERROR: Apple returned no availability record for {", ".join(result.missing_parts)}. '
        "Hong Kong part numbers look like MJXU4ZA/A; not treating this as out of stock.",
        error=True,
    )
    return f'Apple не вернул данные по артикулам {", ".join(result.missing_parts)}. Проверьте PART_NUMBERS.'


def apply_changes(result, parts, state, dry_run, confirm):
    """Tell Telegram what came into stock or sold out since the state, then update the state.
    `confirm(gone)` gets the configurations this check misses (maybe none) and returns the
    sell-outs to report now. Returns (whether a change was reported, whether the alert failed)."""
    known = known_stock(state, parts)
    current = {}
    for item in result.available:
        current.setdefault(item["partNumber"], item)
    appeared = [current[part] for part in parts if part in current and part not in known]
    sold_out = confirm([known[part] for part in parts if part in known and part not in current])

    if appeared or sold_out:
        log(f"CHANGES: {len(appeared)} came into stock, {len(sold_out)} sold out.")
        message = change_message(appeared, sold_out, parts, result.checked_at)
        if dry_run:
            print(f"--- dry run, Telegram message not sent ---\n{message}\n---", flush=True)
            return True, False
        try:
            send_telegram(message)
        except Exception as exc:
            # Keep the old state so the next check reports the changes again.
            log(f"ERROR: could not send the Telegram alert: {exc}", error=True)
            return False, True
        log("Telegram alert sent.")
    elif current:
        log(f"No changes: {len(current)} configuration(s) still in stock.")

    if not dry_run:
        gone = {item["partNumber"] for item in sold_out}
        still = [item for part, item in known.items() if part not in gone]
        state.update(part_numbers=parts, location=LOCATION, available=stock_snapshot(still + appeared))
        state.pop("recently_gone", None)  # used by older versions
        write_state(state)
    return bool(appeared or sold_out), False


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
    problem = missing_problem(result)

    def second_look(gone):
        return confirm_sold_out(parts, gone, record=not dry_run) if gone else []

    reported, alert_failed = apply_changes(result, parts, state, dry_run, second_look)
    return result, reported, alert_failed or problem is not None, problem


def check_counts_footer(minutes, counts, sources=()):
    """Telegram check totals, with a bold server name and count on each separate line."""
    combined = Counter()
    for source, count in counts.items():
        combined["secondary" if source == "backup" else source] += count
    wanted = {"secondary" if s == "backup" else s for s in sources if s}
    wanted.update(s for s in combined if s)
    shown = [s for s in COUNT_ORDER if s in wanted]
    shown += sorted(wanted - set(shown))
    lines = [f"🔁 Проверок за {minutes} мин: {sum(combined.values())}"]
    for source in shown:
        name = escape(source_name(source))
        lines.append(f"• <b>{name}</b> — {combined[source]}")
    return "\n".join(lines)


def recent_check_counts(minutes=10, now=None):
    """Successful checks in the existing history; read only, for a manual status message."""
    now = now or utc_now()
    since = now - timedelta(minutes=minutes)
    counts = Counter()
    try:
        with HISTORY_FILE.open(encoding="utf-8") as history:
            for line in history:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a writer may still be appending the last line
                if not isinstance(entry, dict):
                    continue
                moment = parse_utc(entry.get("time"))
                source = entry.get("source")
                if moment and since < moment <= now and "stores" in entry and isinstance(source, str):
                    counts[source] += 1
    except FileNotFoundError:
        pass
    return counts


class Status:
    """A quiet summary of everything watched every `minutes` minutes on the clock (10:00,
    10:10, …): sent with the first fresh check of a new period, even if stock changed.
    It goes out on a thread of its own (see STATUS_SEND_WAIT), so a slow Telegram never holds
    up the checks. A failed delivery is retried with the next fresh check, keeping the check
    counts."""

    def __init__(self, minutes):
        self.minutes = minutes
        self.period = None
        self.since = None  # time of the first check counted
        self.counts = Counter()
        self.sending = None  # while a status is on its way: (finished, outcome, period, moment, counts in it)

    def settle(self):
        """Take in the outcome of the status on its way, once it is known."""
        if self.sending is None or not self.sending[0].is_set():
            return
        _, outcome, period, moment, counted = self.sending
        self.sending = None
        if outcome == [True]:
            self.period, self.since = period, moment
            self.counts -= counted  # checks counted meanwhile belong to the next status

    @staticmethod
    def send(message, finished, outcome):
        try:
            send_telegram(message, silent=True)
            outcome.append(True)
            log("Status sent to Telegram.")
        except Exception as exc:
            outcome.append(False)
            log(f"ERROR: could not send the status: {exc}", error=True)
        finally:
            finished.set()

    def after_check(self, result, parts, reported, dry_run, source=None, sources=()):
        """Call with every fresh answer from Apple; `sources` are all computers that check."""
        if not self.minutes:
            return
        moment = result.checked_at or utc_now()
        period = int(moment.timestamp() // (self.minutes * 60))
        if self.period is None:
            self.period, self.since = period, moment
        self.settle()
        if period != self.period and self.sending is None:
            minutes = max(1, round((moment - self.since).total_seconds() / 60))
            footer = check_counts_footer(minutes, self.counts, sources)
            message = status_message(result, parts, footer)
            if dry_run:
                print(f"--- dry run, status not sent ---\n{message}\n---", flush=True)
                self.period, self.since = period, moment
                self.counts.clear()
            else:
                finished, outcome = threading.Event(), []
                self.sending = (finished, outcome, period, moment, Counter(self.counts))
                threading.Thread(target=self.send, args=(message, finished, outcome), daemon=True).start()
                finished.wait(STATUS_SEND_WAIT)
                self.settle()
        self.counts[source] += 1


def run_checks(parts, checks, pauses, dry_run, status_minutes=0):
    """Run `checks` checks (None: forever), pausing between them for the seconds in
    `pauses` (a number or a list used in turn), with a quiet status of everything watched
    every `status_minutes` (0: never). Return the process exit code."""
    pauses = list(pauses) if isinstance(pauses, (list, tuple)) else [pauses]
    schedule = itertools.cycle(pauses)
    state = read_state()
    status = Status(status_minutes)
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
        if result:
            status.after_check(result, parts, reported, dry_run)

    if not answered:
        log("ERROR: every check failed; Apple may be blocking requests or the endpoint changed.", error=True)
        return 1
    return 1 if failed else 0


def next_slot(now, every, offset=0):
    """The first moment after `now` (Unix time) that is `offset` seconds past a multiple of
    `every` seconds: next_slot(t, 60, 30) is the next :30 of a minute."""
    return (math.floor((now - offset) / every) + 1) * every + offset


def place(present, source):
    """(every, offset) of `source` in the cycle shared by the `present` computers (see
    CHECK_SPACING), or None if it does not take part."""
    order = [s for s in sorted(CYCLE_SLOTS, key=CYCLE_SLOTS.get) if s in (present or ())]
    if source not in order:
        return None
    every = max(CHECK_SPACING * len(order), MIN_EVERY)
    return every, order.index(source) * every / len(order)


def shares_cycle(source, every, offset):
    """Whether `source` checks at its own place of the full cycle (CYCLE_SLOTS), and so closes
    up with the others when one of them is out (see CHECK_SPACING)."""
    return CYCLE_SLOTS.get(source) == offset and every == CHECK_SPACING * len(CYCLE_SLOTS)


def out_after():
    """How long a computer may stay silent before it is left out of the cycle (see
    CHECK_SPACING): the longest gap a change of the cycle allows, plus a slow delivery."""
    return timedelta(seconds=CHECK_SPACING * len(CYCLE_SLOTS) + MIN_OWN_GAP + OUT_SLACK)


def plan_from_json(value):
    """The computers taking part, as the main server named them (see CHECK_SPACING), or None."""
    if isinstance(value, list) and all(isinstance(source, str) for source in value):
        return [source for source in value if source in CYCLE_SLOTS]
    return None


def describe_plan(present, source):
    """For the log: who takes part, who is out, and how often this computer checks."""
    out = [source_name(s) for s in CYCLE_SLOTS if s not in present]
    mine = place(present, source)
    text = f"Taking part: {', '.join(source_name(s) for s in present) or 'nobody'}"
    text += f" ({', '.join(out)} out)" if out else ""
    if mine:
        text += f"; a check every {mine[0] / len(present):g} s, from here every {mine[0]:g} s, {mine[1]:g} s into the cycle"
    return text + "."


class Backoff:
    """When Apple refuses, pause for 4, 8, then 15 minutes, including across restarts."""

    def __init__(self):
        self.refusals, self.until = 0, 0.0
        saved = read_shared(BACKOFF_FILE)
        refusals, until = saved.get("refusals"), saved.get("until")
        if (isinstance(refusals, int) and not isinstance(refusals, bool) and 1 <= refusals <= 100_000
                and isinstance(until, (int, float)) and not isinstance(until, bool)
                and math.isfinite(until) and 0 <= until <= time.time() + MAX_REFUSED_PAUSE_SECONDS + 1):
            self.refusals, self.until = min(refusals, 4), until

    def allows(self, moment):
        return moment >= self.until

    def look(self, parts):
        """fetch_stock, learning from how Apple answered: (result, None) or (None, error)."""
        try:
            result = fetch_stock(parts)
        except Exception as exc:
            if getattr(exc, "code", None) in REFUSED_CODES:
                self.refusals += 1
                pause = min(60 * 2 ** (self.refusals + 1), MAX_REFUSED_PAUSE_SECONDS)
                self.until = time.time() + pause
                try:
                    write_shared(BACKOFF_FILE, {"refusals": min(self.refusals, 4), "until": self.until}, mode=0o600)
                except OSError as save_error:
                    log(f"ERROR: could not save Apple's protective pause: {save_error}", error=True)
                log(f"Apple refused the request (HTTP {exc.code}); no checks from here for {pause // 60} min.", error=True)
            return None, exc
        self.refusals, self.until = 0, 0.0
        remove(BACKOFF_FILE)
        return result, None


class OwnCheck:
    """This computer's own Backoff.look on a thread of its own, waited for OWN_ANSWER_WAIT.
    When Apple takes longer, run_slots goes on handling the checks the other computers hand
    in, as they come, instead of holding them back until Apple answers and then putting them
    into the history only, as older than that answer."""

    def __init__(self, backoff, parts):
        self.answer, self.failure = (None, None), None
        self.finished = threading.Event()
        threading.Thread(target=self.ask, args=(backoff, parts), daemon=True).start()
        self.finished.wait(OWN_ANSWER_WAIT)

    def ask(self, backoff, parts):
        try:
            self.answer = backoff.look(parts)
        except BaseException as exc:  # look() answers Apple's errors itself; anything else is the loop's
            self.failure = exc
        finally:
            self.finished.set()

    def done(self):
        return self.finished.is_set()

    def result(self):
        """(result, None) or (None, error), as from Backoff.look."""
        if self.failure is not None:
            raise self.failure
        return self.answer


class SellOuts:
    """With checks every half minute a sell-out needs no extra request: it is reported when two
    checks in a row, at least CONFIRM_DELAY_SECONDS apart, miss the configuration."""

    def __init__(self):
        self.missing_since = {}  # part number -> Apple's time of the first check without it
        self.confirmed = set()

    def confirm(self, gone, moment):
        """Call with every check's would-be sell-outs (maybe none); returns those to report."""
        missing = {item["partNumber"] for item in gone}
        back = [part for part in self.missing_since if part not in missing and part not in self.confirmed]
        if back:
            log(f"{len(back)} configuration(s) were back at the next check (Apple flicker); not reported.")
        self.missing_since = {part: self.missing_since.get(part, moment) for part in missing}
        confirmed = [
            item for item in gone
            if (moment - self.missing_since[item["partNumber"]]).total_seconds() >= CONFIRM_DELAY_SECONDS
        ]
        self.confirmed = {item["partNumber"] for item in confirmed}
        if len(confirmed) < len(gone):
            log(f"{len(gone) - len(confirmed)} configuration(s) missing; reported as sold out if the next check agrees.")
        return confirmed


class Sources:
    """The computers that check (this server, GitHub, …) and when each last had an answer
    from Apple. Tells Telegram once when one has had none for PROBLEM_ALERT_AFTER while
    another still works, and once when it is back; if none works, that is report_health's.
    `occasional` computers (a standby filling in) count as working but may fall silent."""

    def __init__(self, state, now, occasional=()):
        self.state = state
        self.occasional = set(occasional)
        self.since = {s: now for s in state.get("sources") or [] if isinstance(s, str) and s not in self.occasional}
        self.ok = {}
        self.errors = {}

    def names(self):
        return list(self.since)

    def seen(self, source, moment, error=None):
        if source not in self.since and source not in self.occasional:
            self.since[source] = moment
            self.state["sources"] = sorted(self.since)
        if error is None:
            self.ok[source] = max(self.ok.get(source, moment), moment)
            self.errors.pop(source, None)
        else:
            self.errors[source] = str(error)

    def working(self, now, window=PROBLEM_ALERT_AFTER):
        return [source for source, moment in self.ok.items() if now - moment < window]

    def report(self, now):
        down = [s for s in self.state.get("down") or [] if s in self.since]
        working = self.working(now)
        for source in self.since:
            name = source_name(source)
            if source in working:
                if source in down and tell(f"✅ {name}: проверки снова работают."):
                    down.remove(source)
                continue
            silent = now - self.ok.get(source, self.since[source])
            if source in down or not working or silent < PROBLEM_ALERT_AFTER:
                continue
            minutes = int(silent.total_seconds() // 60)
            error = self.errors.get(source)
            text = f"⚠️ {name}: проверки {'не работают' if error else 'не приходят'} уже {minutes} мин. "
            names = list(dict.fromkeys(source_name(s) for s in working))
            text += f"Наличие продолжает проверять {', '.join(names)}."
            if error:
                text += f"\n\nПоследняя ошибка: {escape(error)}"
            if tell(text):
                down.append(source)
        self.state["down"] = down or None
        write_state(self.state)


class Watcher:
    """--watch --every: this computer's checks and those other computers hand in (--inbox),
    all against one state, so each change is reported once and every check is in the history.
    With `shared` (the inbox folder) it is the main server for a standby: it says there how
    it is (STATUS_SYNC) and takes back what the standby did while standing in."""

    def __init__(self, parts, dry_run, status_minutes, source, shared=None, offset=0, every=60):
        self.parts, self.dry_run, self.source, self.offset = parts, dry_run, source, offset
        self.every = every
        self.state = read_state()
        self.sell_outs = SellOuts()
        self.status = Status(status_minutes)
        self.shared = Path(shared) if shared else None
        self.sources = Sources(self.state, utc_now(), occasional=("backup",) if self.shared else ())
        self.latest = None  # Apple's time of the newest check acted upon
        self.acted = {}  # per computer, Apple's time of its newest check acted upon
        self.stuck = set()  # inbox files that could not be removed
        # For present(): per computer, when it was last heard from (by this clock), whether
        # Apple refused its last check, and its checks in a row that could not reach Apple.
        # Every computer of the cycle counts as heard at the start, so that none is left out
        # before it had the time to check.
        start = utc_now()
        self.heard = {source: start for source in CYCLE_SLOTS}
        self.refused = {}
        self.unreachable = Counter()
        self.backoff = None  # this computer's own (run_slots sets it): out while Apple makes it pause
        self.wait_until = None  # back after a break: report changes only after the standby's stock
        alive = parse_utc(read_shared(self.shared / STATUS_SYNC).get("alive")) if self.shared else None
        if alive and utc_now() - alive > MAIN_SILENT_AFTER:
            self.wait_until = utc_now() + RETURN_WAIT
            log(f"Back after {int((utc_now() - alive).total_seconds() // 60)} min away: changes are reported once a "
                f"standby hands back its stock, or from {self.wait_until.astimezone(HKT):%H:%M:%S} HKT.")

    def checking(self):
        """Whether this computer checks Apple itself now."""
        return True

    def beat(self):
        """Tell a standby (through STATUS_SYNC, which --sync reads) that this watcher works, and
        what is in stock."""
        if not self.shared or self.dry_run:
            return
        status = {
            "alive": utc_now().isoformat(timespec="seconds"),
            "checked": self.latest.isoformat(timespec="seconds") if self.latest else None,
            "available": self.state.get("available") or [],
            "present": self.present(utc_now()),
        }
        try:
            write_shared(self.shared / STATUS_SYNC, status, mode=0o644)
        except OSError as exc:
            log(f"ERROR: could not write {STATUS_SYNC}: {exc}", error=True)

    def take_handback(self):
        """What a standby left (via --sync) after standing in: its stock, which replaces the
        state here without alerts (it reported the changes), and its checks, for the history."""
        path = self.shared / HANDBACK_SYNC
        if path.exists():
            try:
                stock = stock_from_json(read_shared(path).get("available"))
            except ValueError as exc:
                log(f"ERROR: ignored the standby's stock: {exc}", error=True)
                stock = None
            remove(path)
            if stock is not None and not self.dry_run:
                self.state["available"] = stock
                write_state(self.state)
                self.sell_outs = SellOuts()
                # The standby stands aside at its next sync, within a minute; until then it
                # still reports, so stay quiet a little longer.
                self.wait_until = utc_now() + HANDBACK_QUIET
                log(f"The standby handed back: {len(stock)} configuration(s) in stock; reporting changes "
                    f"from {self.wait_until.astimezone(HKT):%H:%M:%S} HKT, once it stands aside.")
        for path in sorted(self.shared.glob(HISTORY_SYNC)):
            try:
                lines = history_from_json(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                log(f"ERROR: ignored the standby's checks in {path.name}: {exc}", error=True)
                lines = []
            remove(path)
            if lines and not self.dry_run:
                append_history(lines)
                log(f"Added the standby's {len(lines)} check(s) to the history.")

    def note(self, source, moment, result, error):
        """How each computer's checks go, for present()."""
        failed = result is None
        self.heard[source] = utc_now()
        self.refused[source] = failed and refused(error)
        self.unreachable[source] = self.unreachable[source] + 1 if failed and unreachable(error) else 0

    def present(self, now):
        """The computers of CYCLE_SLOTS that take part in the checking now (see CHECK_SPACING)."""
        taking = []
        for source in CYCLE_SLOTS:
            if source == self.source:
                out = self.backoff is not None and not self.backoff.allows(time.time())
            else:
                heard = self.heard.get(source)
                out = heard is None or now - heard > out_after() or self.refused.get(source)
            if not out and self.unreachable[source] < UNREACHABLE_AFTER:
                taking.append(source)
        return taking

    def following(self, now):
        """The computers taking part as this computer goes by, or None: then it keeps its own place."""
        return self.present(now)

    def record(self, result, error, source, moment):
        return record_history(result, error, source, moment)

    def take_inbox(self, inbox):
        """Handle the checks other computers handed in, oldest first, and remove them."""
        if self.shared:
            self.take_handback()
        for path in sorted(Path(inbox).glob("*.json")):
            if path.name in self.stuck:
                continue
            try:
                source, moment, result, error = check_from_json(json.loads(path.read_text(encoding="utf-8")))
                if moment > utc_now() + timedelta(minutes=2):
                    raise ValueError("its time is in the future")
            except Exception as exc:
                log(f"ERROR: ignored {path.name} from the inbox: {exc}", error=True)
                source = None
            try:
                path.unlink()
            except OSError as exc:
                log(f"ERROR: could not remove {path.name} from the inbox: {exc}", error=True)
                self.stuck.add(path.name)
            if source:
                self.handle(source, moment, result, error)

    def stale(self, source, moment):
        """Whether a check came too late to act upon (see SIMULTANEOUS)."""
        if self.latest is None:
            return False
        own = self.acted.get(source)
        return moment < self.latest - SIMULTANEOUS or (own is not None and moment <= own)

    def act_upon(self, source, moment):
        self.latest = max(self.latest, moment) if self.latest else moment
        self.acted[source] = moment

    def handle(self, source, moment, result=None, error=None):
        """One check by `source` at `moment`: Apple's answer or the error."""
        label = source_name(source)
        if not self.dry_run:
            self.record(result, error, source, moment)
        self.sources.seen(source, moment, error)
        self.note(source, moment, result, error)
        problem = None
        if result is None:
            log(f"{label}: ERROR: {error}", error=True)
            if not self.sources.working(utc_now(), WORKING_WINDOW):
                problem = f"Последняя ошибка: {error}"
        else:
            log(f"{label}: {summary(result, self.parts)}")
            problem = missing_problem(result)
            if self.wait_until and utc_now() >= self.wait_until:
                self.wait_until = None
                log("Reporting changes again.")
            if set(result.products) | set(result.missing_parts) != set(self.parts):
                log(f"ERROR: {label} checks other part numbers; its check went into the history only.", error=True)
            elif self.stale(source, moment):
                log(f"{label}: older than the last check; it went into the history only.")
            elif self.wait_until:
                self.act_upon(source, moment)
                log(f"{label}: waiting for the standby's stock before reporting changes.")
            else:
                self.act_upon(source, moment)
                reported, _ = apply_changes(
                    result, self.parts, self.state, self.dry_run, lambda gone: self.sell_outs.confirm(gone, moment)
                )
                everyone = [self.source] + sorted(s for s in self.sources.names() if s != self.source)
                self.status.after_check(result, self.parts, reported, self.dry_run, source, everyone)
        if not self.dry_run:
            report_health(self.state, problem)
            self.sources.report(utc_now())
        self.beat()


class Standby(Watcher):
    """--standby-of HOST: a second server that stands in for the main one. While the main
    server works it does not check Apple and sends nothing: once a minute (at SYNC_OFFSET) it
    asks the main server how it is (`ssh HOST` runs --sync there) and keeps a copy of its stock.
    When the main server has not been working for MAIN_SILENT_AFTER it checks and reports
    itself, also handling GitHub's checks (which then come here); when the main server works
    again it hands back its stock and its checks and stands aside.
    While the main server works, it passes on the checks that could not reach the main server,
    and with --participate it also checks at its own place of the cycle the main server names
    (see CHECK_SPACING), handing its checks to the main server without sending Telegram alerts
    until it needs to stand in. Standing in, it works out the cycle itself."""

    def __init__(self, parts, dry_run, status_minutes, source, main, participate=False, inbox=None,
                 every=60, offset=0):
        super().__init__(parts, dry_run, status_minutes, source, every=every, offset=offset)
        self.main = main
        self.participate = participate
        self.inbox = Path(inbox) if inbox else None
        self.active = False
        self.down_since = None
        self.history = []  # checks made while standing in, for the main server's history
        self.main_plan, self.main_plan_at = None, None  # who takes part, as the main server last said

    def checking(self):
        return self.active or self.participate

    def following(self, now):
        if self.active:
            return self.present(now)
        if self.main_plan is not None and now - self.main_plan_at < PLAN_TTL:
            return self.main_plan
        return None

    def take_plan(self, reply):
        plan = plan_from_json((reply or {}).get("present"))
        if plan is not None:
            self.main_plan, self.main_plan_at = plan, utc_now()

    def beat(self):
        """While standing in, say who takes part in the inbox (STATUS_SYNC, which --ingest here
        reads), so that the computers that hand in their checks here close up too."""
        if not self.active or not self.inbox or self.dry_run:
            return
        status = {"alive": utc_now().isoformat(timespec="seconds"), "present": self.present(utc_now())}
        try:
            write_shared(self.inbox / STATUS_SYNC, status, mode=0o644)
        except OSError as exc:
            log(f"ERROR: could not write {STATUS_SYNC}: {exc}", error=True)

    def handle(self, source, moment, result=None, error=None):
        if self.active:
            return super().handle(source, moment, result, error)
        log(f"{source_name(source)}: " + (summary(result, self.parts) if result else f"ERROR: {error}"))
        if self.dry_run:
            return
        data = check_to_json(source, result, error, moment)
        if self.down_since is None:
            reply, problem = exchange(self.main, {"check": data})
            if problem is None:
                self.take_plan(reply)
                return
            log(f"ERROR: could not hand the check to the main server: {problem}; keeping it in the inbox.", error=True)
            self.down_since = utc_now()
        try:
            save_check(self.inbox, data)
        except (OSError, ValueError) as exc:
            log(f"ERROR: could not keep the check: {exc}", error=True)

    def record(self, result, error, source, moment):
        lines = super().record(result, error, source, moment)
        self.history += [line for line in lines if "watched" not in line]
        return lines

    def take_inbox(self, inbox):
        if self.active:
            return super().take_inbox(inbox)
        if self.down_since is not None:
            return  # keep checks until the next successful sync, or until we stand in
        # GitHub could not reach the main server: pass its checks on, if the main server works.
        for path in sorted(Path(inbox).glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if data is not None:
                reply, problem = exchange(self.main, {"check": data})
                if problem:
                    log(f"ERROR: could not pass a check on to the main server: {problem}", error=True)
                    self.down_since = utc_now()
                    break  # do not discard checks or retry SSH on every loop iteration
                log("Passed a check on to the main server.")
            remove(path)

    def sync(self):
        """Ask the main server how it is: keep its stock, stand in, or hand back."""
        payload = {"available": self.state.get("available") or []} if self.active else {}
        reply, problem = exchange(self.main, payload)
        now = utc_now()
        age = reply.get("age") if reply else None
        if isinstance(age, int) and not isinstance(age, bool):  # seconds since it last worked, by its clock
            alive = now - timedelta(seconds=max(0, age))
        else:
            alive = parse_utc(reply.get("alive")) if reply else None
        if reply is not None and not self.dry_run:
            self.state["main_seen_utc"] = now.isoformat(timespec="seconds")
        if alive and now - alive < MAIN_SILENT_AFTER:
            self.down_since = None
            if self.active:  # the stock went with this call
                self.active = False
                log("The main server works again: handed back the stock; standing aside.")
                if not self.dry_run:
                    name = source_name(self.source)
                    behavior = f"{name} снова передаёт ему свои проверки." if self.participate else f"{name} вернулся в режим ожидания."
                    tell(f"✅ VPS 1 снова работает. {behavior}")
            elif not self.dry_run:
                try:
                    self.state["available"] = stock_from_json(reply.get("available"))
                    write_state(self.state)
                except ValueError as exc:
                    log(f"ERROR: ignored the main server's stock: {exc}", error=True)
            if self.history:
                _, problem = exchange(self.main, {"history": self.history})
                if problem:
                    log(f"ERROR: could not hand back the checks: {problem}; trying again in a minute.", error=True)
                else:
                    log(f"Handed back {len(self.history)} check(s) for the main server's history.")
                    self.history = []
            return
        if self.down_since is None:
            self.down_since = min(alive, now) if alive else now
            if reply is None and not parse_utc(self.state.get("main_seen_utc")):
                # Never reached it: rather a missing key (vps_install.sh sync-key) than an outage.
                log(f"ERROR: cannot reach the main server ({problem}); not standing in for a server "
                    "never seen working. Is this server's key on it (vps_install.sh sync-key)?", error=True)
        if not self.active and now - self.down_since >= MAIN_SILENT_AFTER and parse_utc(self.state.get("main_seen_utc")):
            why = "нет связи" if reply is None else "служба проверки на нём остановлена"
            self.stand_in(why, problem or "it is not checking", now)

    def stand_in(self, why, detail, now):
        minutes = int((now - self.down_since).total_seconds() // 60)
        log(f"The main server has not been working for {minutes} min ({detail}); standing in.")
        self.active = True
        self.sources = Sources(self.state, now)
        self.status = Status(self.status.minutes)
        self.sell_outs = SellOuts()
        self.latest, self.acted = None, {}
        # Who takes part from now on, as this server works it out: everyone the main server
        # last named counts as heard, except the main server itself.
        start = utc_now()
        self.heard = {source: start for source in (self.main_plan or CYCLE_SLOTS) if source != "vps"}
        self.refused, self.unreachable = {}, Counter()
        if not self.dry_run:
            tell(f"⚠️ VPS 1 не работает уже {minutes} мин ({why}). "
                 f"Проверку и отправку уведомлений продолжает {source_name(self.source)}.")


def run_slots(parts, every, offset, dry_run, status_minutes=0, inbox=None, source="vps", standby_of=None,
              participate=False):
    """Check at fixed times on the clock, `offset` seconds past every multiple of `every`
    seconds (60 and 0: at :00 of every minute), and in between handle the checks other
    computers hand in through `inbox`. With `standby_of` (an ssh host) this
    is a standby for that main server (see Standby). Checking at its own place of the full
    cycle (CYCLE_SLOTS), it closes up with the others when one of them is out (see
    CHECK_SPACING). This computer's own request to Apple does not hold up the checks handed
    in meanwhile (see OwnCheck). Runs until stopped."""
    if standby_of:
        watcher = Standby(parts, dry_run, status_minutes, source, standby_of, participate, inbox, every, offset)
        sync_due = next_slot(time.time(), 60, SYNC_OFFSET)
    else:
        watcher = Watcher(parts, dry_run, status_minutes, source, shared=inbox, offset=offset, every=every)
        sync_due = math.inf
    backoff = Backoff()
    watcher.backoff = backoff
    home = (every, offset)
    shared = shares_cycle(source, every, offset)
    plan, present_logged = home, None
    due = next_slot(time.time(), *plan)
    last_request = -math.inf  # this computer's last own request to Apple
    asking = None  # this computer's own check until Apple answers it
    while True:
        if inbox:
            watcher.take_inbox(inbox)
        if asking is not None and asking.done():  # after the checks handed in meanwhile, which are older
            result, error = asking.result()
            asking = None
            watcher.handle(source, (result.checked_at if result else None) or utc_now(), result, error)
        now = time.time()
        if shared:
            present = watcher.following(utc_now())
            if present is not None and present != present_logged:
                log(describe_plan(present, source))
                present_logged = present
            wanted = place(present, source) or home
            if wanted != plan:
                plan = wanted
                due = next_slot(now, *plan)
                if due - last_request < MIN_OWN_GAP:
                    due = next_slot(due, *plan)
        if now >= sync_due:
            watcher.sync()
            sync_due = next_slot(time.time(), 60, SYNC_OFFSET)
        elif now >= due:
            if asking is not None:
                log("Apple has not answered the last check from here yet; not asking again in this slot.")
                watcher.beat()
            elif watcher.checking() and backoff.allows(due):
                last_request = time.time()
                asking = OwnCheck(backoff, parts)
            else:
                watcher.beat()
            due = next_slot(time.time(), *plan)
        else:
            wait = min(due, sync_due) - now
            time.sleep(min(wait, INBOX_POLL_SECONDS) if inbox or asking is not None else wait)


def check_to_json(source, result=None, error=None, moment=None):
    """A check as JSON-ready data, to hand it to another computer (--probe, --ingest)."""
    moment = (result.checked_at if result else None) or moment or utc_now()
    data = {"source": source, "time": moment.isoformat(timespec="seconds")}
    if result is None:
        data["error"] = " ".join(str(error).split())[:300]
    else:
        data.update(stores=result.store_count, products=result.products,
                    available=result.available, missing=result.missing_parts)
    return data


def text(value, limit=200):
    """A string from another computer, checked: like clean(), any spaces (Apple's are
    non-breaking) become one plain space; no line breaks or invisible marks. Raises ValueError."""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"bad value {str(value)[:40]!r}")
    return " ".join("".join(ch for ch in value if ch.isprintable() or ch.isspace()).split())


def stock_from_json(items):
    """A state's stock list (see stock_snapshot) from another computer, checked."""
    if not isinstance(items, list) or len(items) > 200:
        raise ValueError("bad stock")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("bad stock record")
    return [{"partNumber": text(item.get("partNumber"), 20), "product": text(item.get("product"))} for item in items]


def history_from_json(lines):
    """History lines (see record_history) from another computer, checked and rebuilt."""
    if not isinstance(lines, list) or len(lines) > 100_000:
        raise ValueError("bad history")
    checked = []
    for line in lines:
        if not isinstance(line, dict) or parse_utc(line.get("time")) is None:
            raise ValueError("bad history line")
        entry = {"time": text(line["time"], 40)}
        if "source" in line:
            entry["source"] = text(line["source"], 20)
        if "error" in line:
            entry["error"] = text(line["error"], 300)
        elif isinstance(line.get("in_stock"), dict) and len(line["in_stock"]) <= 100:
            stores = line.get("stores")
            entry["stores"] = stores if isinstance(stores, int) and not isinstance(stores, bool) else 0
            entry["in_stock"] = {
                text(title): [text(store) for store in names[:20]]
                for title, names in line["in_stock"].items() if isinstance(names, list)
            }
        else:
            raise ValueError("bad history line")
        checked.append(entry)
    return checked


def write_shared(path, data, mode=0o640):
    """Write a file the watcher and the --sync command share, atomically and with `mode`."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False))
    os.chmod(temporary, mode)  # whatever the umask
    os.replace(temporary, path)


def read_shared(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remove(path):
    try:
        path.unlink()
    except OSError:
        pass


def check_from_json(data):
    """(source, time, CheckResult or None, error or None) from check_to_json's data, checked
    field by field since it comes from another computer. Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    source = text(data.get("source"), 20)
    if not re.fullmatch(r"[a-z0-9-]+", source):
        raise ValueError(f"bad source {source!r}")
    moment = parse_utc(text(data.get("time"), 40))
    if moment is None:
        raise ValueError("bad time")
    if "error" in data:
        return source, moment, None, text(data["error"], 300)
    stores, products, available, missing = (data.get(k) for k in ("stores", "products", "available", "missing"))
    if not isinstance(stores, int) or isinstance(stores, bool) or not 0 <= stores <= 100:
        raise ValueError("bad store count")
    if not isinstance(products, dict) or len(products) > 100:
        raise ValueError("bad products")
    products = {text(part, 20): text(title) for part, title in products.items()}
    if not isinstance(available, list) or len(available) > 5000 or not isinstance(missing, list) or len(missing) > 100:
        raise ValueError("bad availability")
    fields = ("storeNumber", "storeName", "city", "partNumber", "product", "pickup")
    checked = []
    for item in available:
        if not isinstance(item, dict) or item.get("partNumber") not in products:
            raise ValueError("bad availability record")
        checked.append({k: text(item.get(k, "")) for k in fields})
    return source, moment, CheckResult(stores, products, checked, [text(p, 20) for p in missing], moment), None


def oldest_age(paths):
    """Seconds since the oldest of `paths` was written (0 if none is left)."""
    for path in paths:
        try:
            return time.time() - path.stat().st_mtime
        except FileNotFoundError:
            continue  # just taken by the watcher
    return 0


def ingest(inbox, source, stream):
    """--ingest (the server's SSH command for GitHub): save the checks sent on `stream`, one
    JSON line each (see check_to_json), into `inbox` for --watch --inbox, as coming from
    `source` whatever they say. Returns 1 with the reason on stderr if a check is not taken,
    e.g. because the watcher has stopped reading the inbox, so that the sender notices.
    Answers on stdout with who takes part in the checking (see CHECK_SPACING), as the watcher
    here last said in STATUS_SYNC, or with {} if it has not said so lately."""
    inbox = Path(inbox)
    try:
        for line in iter(lambda: stream.readline(MAX_CHECK_BYTES + 1), b""):
            if len(line) > MAX_CHECK_BYTES:
                raise ValueError("the check is too large")
            if not line.strip():
                continue
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            data["source"] = source
            check_from_json(data)
            save_check(inbox, data)
    except (OSError, ValueError) as exc:
        print(f"ERROR: check not taken: {exc}", file=sys.stderr, flush=True)
        return 1
    status = read_shared(inbox / STATUS_SYNC)
    alive = parse_utc(status.get("alive"))
    present = plan_from_json(status.get("present"))
    fresh = alive is not None and present is not None and abs(utc_now() - alive) < MAIN_SILENT_AFTER
    print(json.dumps({"present": present} if fresh else {}), flush=True)
    return 0


def save_check(inbox, data):
    """Put a checked check (see check_from_json) into the inbox for the watcher. ValueError if
    the watcher has stopped reading it."""
    waiting = sorted(inbox.glob("*.json"))
    if len(waiting) >= INBOX_LIMIT:
        raise ValueError(f"{len(waiting)} checks are waiting on the server; is the watcher running?")
    if oldest_age(waiting) > INBOX_STALE_SECONDS:
        minutes = int(oldest_age(waiting) // 60)
        raise ValueError(f"the watcher on the server has not read checks for {minutes} min; is it running?")
    name = f"{time.time_ns()}-{os.getpid()}"
    temporary = inbox / f".{name}.tmp"
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640), "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False))
    os.replace(temporary, inbox / f"{name}.json")


def over_ssh(host, data):
    """Run `ssh host` — the server runs that key's one command (--ingest, --sync) — with `data`
    on stdin. Returns (its output, None) or (None, the problem)."""
    try:
        done = subprocess.run(["ssh", "-T", host], input=data, capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        return None, "the server did not answer within 45 s"
    except OSError as exc:
        return None, f"could not run ssh: {exc}"
    if done.returncode:
        lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, lines[-1] if lines else f"ssh exited with code {done.returncode}"
    return done.stdout, None


def deliver(feed, data):
    """Hand one check to the server, where `ssh feed` runs --ingest. Returns (its answer, a
    dict, or None; the problem or None)."""
    out, problem = over_ssh(feed, json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n")
    if problem:
        return None, problem
    try:
        answer = json.loads(out.decode("utf-8").strip().splitlines()[-1]) if out and out.strip() else {}
    except (ValueError, UnicodeDecodeError):
        answer = {}
    return (answer if isinstance(answer, dict) else {}), None


def exchange(main, payload):
    """A standby's call to its main server, where `ssh main` runs --sync: send `payload`, get the
    main server's status (STATUS_SYNC). Returns (status or None, the problem or None)."""
    out, problem = over_ssh(main, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if problem:
        return None, problem
    try:
        status = json.loads(out or b"{}")
    except ValueError:
        return None, "the main server's answer is not JSON"
    return (status, None) if isinstance(status, dict) else (None, "the main server's answer is not an object")


def sync(shared, stream, out):
    """--sync (the main server's SSH command for its standby): keep what the standby hands back
    after standing in — its stock, then its checks — in the `shared` folder for the watcher
    here, take a check it made in place of another computer or passed on from GitHub (into
    the inbox, which `shared` is), and answer with this server's status (STATUS_SYNC).
    Returns 1, with the reason on stderr, if what came is not right."""
    shared = Path(shared)
    try:
        raw = stream.read(MAX_SYNC_BYTES + 1)
        if len(raw) > MAX_SYNC_BYTES:
            raise ValueError("too large")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        if "available" in data:
            write_shared(shared / HANDBACK_SYNC, {"available": stock_from_json(data["available"])})
        if "history" in data:
            write_shared(shared / f"history-{time.time_ns()}.sync", history_from_json(data["history"]))
        if "check" in data:  # the standby's check in place of another computer, or GitHub's passed on
            check = data["check"]
            if not isinstance(check, dict) or check.get("source") not in ("backup", *(s for s in CYCLE_SLOTS if s != "vps")):
                raise ValueError("bad check")
            check_from_json(check)
            save_check(shared, check)
        status = read_shared(shared / STATUS_SYNC)
        alive = parse_utc(status.get("alive"))
        if alive:  # by this server's clock, so that the two servers' clocks need not agree
            status["age"] = max(0, int((utc_now() - alive).total_seconds()))
        out.write(json.dumps(status, ensure_ascii=False) + "\n")
        out.flush()
    except (OSError, ValueError) as exc:
        print(f"ERROR: sync failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


def run_probe(parts, feed, every, offset, minutes=0, source="github", backup=None):
    """--probe: check at fixed times on the clock like run_slots (GitHub: at :00, between the
    server's checks) and hand every answer to the server with deliver(); the server decides
    what to send. A check the server does not take goes to the standby server (`backup`).
    Stops after `minutes` (0: never). Tells Telegram once if neither has taken the checks for
    PROBLEM_ALERT_AFTER, and once when they are taken again. Checking at its own place of the
    full cycle (CYCLE_SLOTS), it closes up with the others as the server's answers say (see
    CHECK_SPACING)."""
    end = time.time() + minutes * 60 if minutes else None
    backoff = Backoff()
    failing_since, notified = None, False
    name = source_name(source)
    home = (every, offset)
    shared = shares_cycle(source, every, offset)
    plan, plan_at, logged = None, None, None  # who takes part, as the server last said, and when
    last_request = -math.inf
    while True:
        present = plan if plan is not None and time.time() - plan_at < PLAN_TTL.total_seconds() else None
        mine = (place(present, source) if shared else None) or home
        if shared and present is not None and present != logged:
            log(describe_plan(present, source))
            logged = present
        due = next_slot(time.time(), *mine)
        if due - last_request < MIN_OWN_GAP:  # the cycle changed just after this computer's check
            due = next_slot(due, *mine)
        if end is not None and due >= end:
            return 0
        time.sleep(max(0.0, due - time.time()))
        if not backoff.allows(due):
            continue
        last_request = time.time()
        result, error = backoff.look(parts)
        if result:
            log(summary(result, parts))
        else:
            log(f"ERROR: {error}", error=True)
        data = check_to_json(source, result, error)
        answer, problem = deliver(feed, data)
        if problem and backup:
            answer, spare = deliver(backup, data)
            if spare is None:
                log(f"The server did not take the check ({problem}); the standby did.")
                problem = None
            else:
                problem = f"{problem}; standby: {spare}"
        if answer and plan_from_json(answer.get("present")) is not None:
            plan, plan_at = plan_from_json(answer["present"]), time.time()
        if problem is None:
            failing_since = None
            if notified and tell(f"✅ {name} снова передаёт проверки на сервер."):
                notified = False
            continue
        log(f"ERROR: could not hand the check to the server: {problem}", error=True)
        failing_since = failing_since or utc_now()
        if not notified and utc_now() - failing_since >= PROBLEM_ALERT_AFTER:
            minutes_down = int((utc_now() - failing_since).total_seconds() // 60)
            text = (
                f"⚠️ {name} не может передать проверки на сервер уже {minutes_down} мин. Если сервер "
                f"не работает, уведомления о наличии сейчас не придут.\n\n{escape(problem)}"
            )
            if actions_run_url():
                text += f'\n\n<a href="{escape(actions_run_url())}">Лог запуска</a>'
            notified = tell(text)


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
            send_report(token, chat_id)  # the buttons to choose
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


def report_key(group):
    """The data of a /report button for (model, storage); Telegram allows 64 bytes."""
    return f"report:{group[0]} {group[1]}".encode("utf-8")[:64].decode("utf-8", "ignore")


def report_buttons(groups):
    """/report's buttons: one per model and storage (just the storage when one model is
    watched), a row per model, then "Все" for the whole report."""
    models = list(dict.fromkeys(model for model, _ in groups))
    rows = [
        [
            {"text": storage if len(models) == 1 else f"{model.removeprefix('iPhone ')} {storage}",
             "callback_data": report_key((model, storage))}
            for m, storage in groups if m == model
        ]
        for model in models
    ]
    return rows + [[{"text": "Все", "callback_data": REPORT_ALL}]]


def send_report(token, chat_id, choice=None):
    """/report from the history (stock_report.py): without a choice the buttons to pick a model
    and storage or "Все"; with one (a button's data) that report, the buttons under it again."""
    import stock_report  # imports this module, so only when needed

    try:
        watched, checks = stock_report.load([HISTORY_FILE])
    except OSError:
        watched, checks = [], []
    if not checks:
        send_telegram("Журнал наличия пока пуст: он заполняется с каждой проверкой.", chat_ids=[chat_id])
        return
    groups = stock_report.groups(watched)
    buttons = report_buttons(groups)
    if choice is None:
        what = "память" if len({model for model, _ in groups}) == 1 else "модель и память"
        send_telegram(
            f"📊 Статистика наличия\n\nВыберите {what} — покажу, в какие часы появляется и в каких цветах. "
            "«Все» — общий отчёт.",
            chat_ids=[chat_id],
            buttons=buttons,
        )
        return
    telegram_api(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
    group = next((g for g in groups if report_key(g) == choice), None)
    if group:
        text = stock_report.group_report(watched, checks, group, show_last=3)
    else:  # "Все", or a button from before the watch list changed
        text = stock_report.report(watched, checks, show_last=3, one_per_line=True)
    send_telegram(escape(text), chat_ids=[chat_id], buttons=buttons)
    log(f"Sent the report ({choice}) to chat {chat_id}.")


def answer_button(token, allowed, query):
    """A tap on a /report button: that report, in the chat with the button."""
    try:
        # Stops the button's spinner; fails harmlessly for taps older than Telegram keeps.
        telegram_api(token, "answerCallbackQuery", {"callback_query_id": query.get("id", "")})
    except Exception as exc:
        log(f"ERROR: could not answer a button: {exc}", error=True)
    data = query.get("data") or ""
    chat_id = str(((query.get("message") or {}).get("chat") or {}).get("id", ""))
    if not data.startswith("report:"):
        return
    if chat_id not in allowed:
        log(f"Ignored a report button from chat {chat_id}: it is not in TELEGRAM_CHAT_ID.")
        return
    try:
        send_report(token, chat_id, data)
    except Exception as exc:
        log(f"ERROR: could not send the report: {exc}", error=True)


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
        poll = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
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
            if "callback_query" in update:
                answer_button(token, allowed, update["callback_query"])
            else:
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
        help=f"check forever, pausing these minutes in turn (default: CHECK_SCHEDULE_MINUTES or {DEFAULT_SCHEDULE}), or at fixed times with --every",
    )
    parser.add_argument("--every", type=int, metavar="SECONDS", help="with --watch or --probe: check at fixed times, every SECONDS on the clock")
    parser.add_argument("--offset", type=int, default=0, metavar="SECONDS", help="with --every: that many seconds past those times (30: at :30 of every minute)")
    parser.add_argument(
        "--status-minutes",
        type=int,
        default=DEFAULT_STATUS_MINUTES,
        metavar="N",
        help=f"with --watch: a quiet status every N minutes, 0 = never (default {DEFAULT_STATUS_MINUTES})",
    )
    parser.add_argument("--inbox", metavar="DIR", help="with --watch --every: also handle the checks another computer hands in through DIR (see --ingest)")
    parser.add_argument("--source", metavar="NAME", help="this computer's name in logs, history and messages (default: vps; github for --probe and --ingest)")
    parser.add_argument("--probe", metavar="HOST", help="with --every: check and hand every answer to the server, where `ssh HOST` runs --ingest")
    parser.add_argument("--backup", metavar="HOST", help="with --probe: a check the server does not take goes to this standby server instead")
    parser.add_argument("--minutes", type=int, default=0, metavar="N", help="with --probe: stop after N minutes (default: never)")
    parser.add_argument("--ingest", metavar="DIR", help="save the checks sent on stdin into DIR for --watch --inbox (the server's SSH command)")
    parser.add_argument("--standby-of", metavar="HOST", help="with --watch --every: stand by for the main server (`ssh HOST` runs --sync there) and check only while it does not work")
    parser.add_argument("--participate", action="store_true", help="with --standby-of and --inbox: also check in this server's own slot while the main server works; hand it the results")
    parser.add_argument("--sync", metavar="DIR", help="answer a standby: keep what it hands back in DIR, print this server's status (the server's SSH command)")
    parser.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them; keep the state file")
    parser.add_argument("--test-telegram", action="store_true", help="send a test Telegram message and exit")
    parser.add_argument("--status", action="store_true", help="send what is in stock now to Telegram and exit")
    parser.add_argument("--bot", action="store_true", help="answer the /iphone command in Telegram (runs until stopped)")
    args = parser.parse_args()
    if args.participate and not (args.standby_of and args.inbox and args.watch is not None and args.every is not None):
        parser.error("--participate needs --watch --every --standby-of and --inbox")

    if args.ingest:
        return ingest(args.ingest, args.source or "github", sys.stdin.buffer)
    if args.sync:
        return sync(args.sync, sys.stdin.buffer, sys.stdout)
    parts = configured_parts()
    if args.test_telegram:
        send_test_message(parts)
        return 0
    if args.bot:
        return run_bot(parts)
    if args.status:
        result = fetch_stock(parts)
        footer = check_counts_footer(10, recent_check_counts(), COUNT_ORDER)
        send_telegram(status_message(result, parts, footer))
        log("Status sent to Telegram.")
        return 0
    if args.every is not None and (args.every < 10 or not 0 <= args.offset < args.every):
        parser.error("--every must be at least 10 seconds and --offset from 0 to less than --every")
    if args.probe:
        if args.every is None or args.minutes < 0:
            parser.error("--probe needs --every, and --minutes must be 0 or more")
        until = f" for {args.minutes} min" if args.minutes else ""
        spare = f" (or to the standby through ssh {args.backup})" if args.backup else ""
        log(f"Checking {len(parts)} configurations every {args.every} s, {args.offset} s past the clock{until}; "
            f"every check goes to the server through ssh {args.probe}{spare}.")
        return run_probe(parts, args.probe, args.every, args.offset, args.minutes, args.source or "github", args.backup)
    if args.watch is not None:
        if args.status_minutes < 0:
            parser.error("--status-minutes must be 0 or more")
        status = f"a quiet status every {args.status_minutes} min." if args.status_minutes else "no status messages."
        if args.every is not None:
            inbox = f" plus the checks handed in through {args.inbox}" if args.inbox else ""
            role = ""
            if args.standby_of:
                behavior = "checking in this slot and forwarding results; taking over if it stops working" if args.participate else "checking only while it does not work"
                role = f" Standing by for the main server (ssh {args.standby_of}): {behavior}."
            log(f"Watching {len(parts)} configurations: a check every {args.every} s, {args.offset} s past the clock"
                f"{inbox}; {status}{role}")
            source = args.source or ("secondary" if args.participate else "backup" if args.standby_of else "vps")
            return run_slots(parts, args.every, args.offset, args.dry_run, args.status_minutes, args.inbox, source, args.standby_of, args.participate)
        if args.standby_of:
            parser.error("--standby-of needs --watch --every")
        pauses = parse_schedule(args.watch or os.environ.get("CHECK_SCHEDULE_MINUTES") or DEFAULT_SCHEDULE)
        minutes = ", ".join(f"{p / 60:g}" for p in pauses)
        log(f"Watching {len(parts)} configurations; pauses of {minutes} min in turn; {status}")
        return run_checks(parts, None, pauses, args.dry_run, args.status_minutes)
    if args.inbox or args.every is not None or args.standby_of or args.backup:
        parser.error("--every, --inbox and --standby-of need --watch, --backup needs --probe")
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
