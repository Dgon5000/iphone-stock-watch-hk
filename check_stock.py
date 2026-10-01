#!/usr/bin/env python3
"""Apple Store Hong Kong pickup stock watcher.

Checks in-store pickup availability of the watched iPhone part numbers at every
Apple Store in Hong Kong and sends one Telegram alert when a store newly has stock.
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
# seconds apart). A store/model counts as sold out only after it has been missing
# this long, so the flicker does not repeat alerts but a real restock still does.
FORGET_AFTER = timedelta(minutes=10)
STATE_FILE = Path(__file__).resolve().with_name("stock_state.json")
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


def stock_key(item):
    return f'{item["storeNumber"]}|{item["partNumber"]}'


def store_label(item):
    label = f'Apple {item["storeName"]}'
    city = item.get("city") or ""
    if city and city.lower() not in item["storeName"].lower():
        label += f", {city}"
    return label


def read_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def write_state(state):
    """Write the state file only when something besides the timestamp changed,
    so the workflow commits real changes instead of a timestamp every run."""
    state = {k: v for k, v in state.items() if k != "updated_hkt" and v is not None}
    previous = read_state()
    previous.pop("updated_hkt", None)
    if previous == state:
        return
    state["updated_hkt"] = now_hkt()
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_utc(text):
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def remembered_stock(state, parts):
    """Stock that was already alerted: in stock at the last check, or missing for
    less than FORGET_AFTER. Keyed by stock_key."""
    items = {}
    for field in ("available", "recently_gone"):
        for item in state.get(field) or []:
            if isinstance(item, dict) and item.get("storeNumber") and item.get("partNumber") in parts:
                items[stock_key(item)] = item
    return items


def stock_snapshot(items, extra=()):
    return sorted(
        ({k: item.get(k, "") for k in ("storeNumber", "storeName", "partNumber", "product", *extra)} for item in items),
        key=lambda i: (i["partNumber"], i["storeNumber"]),
    )


def recently_gone(remembered, current, now):
    """Remembered stock missing from this check that has not been gone for FORGET_AFTER yet."""
    gone = []
    for key, item in remembered.items():
        if key in current:
            continue
        since = parse_utc(item.get("gone_since_utc")) or now
        if now - since < FORGET_AFTER:
            gone.append({**item, "gone_since_utc": since.isoformat(timespec="seconds")})
    return stock_snapshot(gone, extra=("gone_since_utc",))


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

    try:
        with urlopen(req, timeout=20, context=ssl_context()) as response:
            status = getattr(response, "status", 200)
            raw = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"Apple returned HTTP {exc.code}; not treating this as out of stock.") from exc
    except OSError as exc:  # URLError, timeouts, connection resets
        reason = getattr(exc, "reason", exc)
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
    return stores


def fetch_stock(parts):
    store_numbers = set()
    products = {}
    available = []
    for start in range(0, len(parts), MAX_PARTS_PER_REQUEST):
        batch = parts[start:start + MAX_PARTS_PER_REQUEST]
        for store in request_stores(batch):
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
    return CheckResult(len(store_numbers), products, available, missing)


def telegram_api(token, method, payload=None):
    """Call a Telegram Bot API method and return its result."""
    request = Request(
        TELEGRAM_API.format(token=token, method=method),
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    for attempt in (1, 2):
        try:
            with urlopen(request, timeout=30, context=ssl_context()) as response:
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


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = os.environ.get("TELEGRAM_CHAT_ID", "").replace(",", " ").split()
    if not token or not chat_ids:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
    if not TELEGRAM_TOKEN_RE.fullmatch(token):
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is malformed: expected the bot number, a colon and the secret "
            "(123456789:AAH…) with no extra characters."
        )

    for chat_id in chat_ids:
        for chunk in split_message(text):
            telegram_api(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
            )


def alert_message(new_items, parts):
    order = {part: i for i, part in enumerate(parts)}
    by_part = {}
    for item in sorted(new_items, key=lambda i: (order.get(i["partNumber"], len(order)), i["storeName"].lower())):
        by_part.setdefault(item["partNumber"], []).append(item)
    products = [items[0]["product"] for items in by_part.values()]

    headline = f"🟢 В наличии: {products[0]}"
    if len(products) > 1:
        headline += f" и ещё {len(products) - 1}"
    blocks = [escape(headline)]
    for part, items in by_part.items():
        lines = [f'<b>{escape(items[0]["product"])}</b>']
        lines += [f'• {escape(store_label(item))} — {escape(item["pickup"])}' for item in items]
        lines.append(f'<a href="{escape(PRODUCT_URL.format(part=part))}">Оформить самовывоз</a>')
        blocks.append("\n".join(lines))
    blocks.append(f"Apple Store Hong Kong · проверено {now_hkt()}")
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


def run_checks(parts, checks, interval, dry_run):
    """Return the process exit code."""
    state = read_state()
    remembered = remembered_stock(state, parts)
    successful_checks = 0
    failed = False
    last_error = ""
    missing = []

    for n in range(1, checks + 1):
        if n > 1:
            time.sleep(interval)
        prefix = f"Check {n}/{checks}: " if checks > 1 else ""

        try:
            result = fetch_stock(parts)
        except Exception as exc:
            last_error = str(exc)
            log(f"{prefix}ERROR: {exc}", error=True)
            continue
        successful_checks += 1
        log(prefix + summary(result, parts))

        missing = result.missing_parts
        if missing:
            failed = True
            log(
                f'ERROR: Apple returned no availability record for {", ".join(missing)}. '
                "Hong Kong part numbers look like MJXU4ZA/A; not treating this as out of stock.",
                error=True,
            )

        current = {stock_key(item): item for item in result.available}
        new_items = [current[k] for k in sorted(set(current) - set(remembered))]

        if new_items:
            message = alert_message(new_items, parts)
            log(f"NEW STOCK FOUND: {len(new_items)} store/model combination(s).")
            if dry_run:
                print(f"--- dry run, Telegram message not sent ---\n{message}\n---", flush=True)
                continue
            try:
                send_telegram(message)
            except Exception as exc:
                # Keep the old state so the next check retries the alert.
                failed = True
                log(f"ERROR: could not send the Telegram alert: {exc}", error=True)
                continue
            log("Telegram alert sent.")
        elif current:
            log(f"Still available at {len(current)} previously alerted store/model combination(s); no duplicate alert.")

        if dry_run:
            continue
        gone = recently_gone(remembered, current, datetime.now(timezone.utc))
        state.update(
            part_numbers=parts,
            location=LOCATION,
            available=stock_snapshot(result.available),
            recently_gone=gone or None,
        )
        remembered = remembered_stock(state, parts)
        write_state(state)

    if successful_checks == 0:
        failed = True
        log("ERROR: every check failed; Apple may be blocking requests or the endpoint changed.", error=True)
        problem = f"Проверок за запуск: {checks}, успешных: 0. Последняя ошибка: {last_error}"
    elif missing:
        problem = f'Apple не вернул данные по артикулам {", ".join(missing)}. Проверьте PART_NUMBERS.'
    else:
        problem = None
    if not dry_run:
        report_health(state, problem)
    return 1 if failed else 0


def send_test_message(parts):
    try:
        result = fetch_stock(parts)
    except Exception as exc:
        status = [f"Не удалось проверить наличие: {escape(str(exc))}"]
    else:
        status = [f"Магазинов: {result.store_count}, конфигураций: {len(parts)}. Сейчас:"]
        for part in parts:
            stores = stores_with_stock(result, part)
            if part in result.missing_parts:
                state = "Apple не вернул данные — проверьте артикул"
            else:
                state = ("есть в " + ", ".join(stores)) if stores else "нет в наличии"
            status.append(f"• {escape(result.products.get(part, part))} — {escape(state)}")
    send_telegram(
        "\n".join(
            [
                "✅ Тест: уведомления о наличии iPhone в Apple Store Hong Kong будут приходить сюда.",
                "",
                *status,
                "",
                f"Проверено: {now_hkt()}",
            ]
        )
    )
    log("Test Telegram message sent.")


def main():
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
    parser.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them; keep the state file")
    parser.add_argument("--test-telegram", action="store_true", help="send a test Telegram message and exit")
    args = parser.parse_args()

    parts = configured_parts()
    if args.test_telegram:
        send_test_message(parts)
        return 0
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
