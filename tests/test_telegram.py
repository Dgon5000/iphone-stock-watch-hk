"""Scenario tests for the Telegram version of check_stock.py and telegram_setup.py.

Apple and Telegram are replaced by in-process fakes; nothing leaves the machine.
"""
import csv
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

PROJECT = Path(os.environ.get("PROJECT_DIR") or Path(__file__).resolve().parent.parent)

STORES = [  # number, name, city — as Apple HK returns them
    ("R409", "Causeway Bay", "Causeway Bay"),
    ("R428", "ifc mall", "Central"),
    ("R485", "Festival Walk", "Kowloon Tong"),
    ("R499", "Canton Road", "Tsim Sha Tsui"),
    ("R610", "New Town Plaza", "Sha Tin"),
    ("R673", "apm Hong Kong", "Kwun Tong"),
]
PRO_MAX = {
    "MJXU4ZA/A": "512GB Silver", "MJXT4ZA/A": "512GB Black", "MJXW4ZA/A": "512GB Glacier", "MJXV4ZA/A": "512GB Burgundy",
    "MJXY4ZA/A": "1TB Silver", "MJXX4ZA/A": "1TB Black", "MJY14ZA/A": "1TB Glacier", "MJY04ZA/A": "1TB Burgundy",
    "MJY34ZA/A": "2TB Silver", "MJY24ZA/A": "2TB Black", "MJY54ZA/A": "2TB Glacier", "MJY44ZA/A": "2TB Burgundy",
}
CATALOG = {p: f"iPhone 18 Pro Max {v}" for p, v in PRO_MAX.items()}
PRO = ["MJRP4ZA/A", "MJRQ4ZA/A", "MJRR4ZA/A", "MJRT4ZA/A", "MJRU4ZA/A", "MJRV4ZA/A", "MJRW4ZA/A", "MJRX4ZA/A",
       "MJRY4ZA/A", "MJT04ZA/A", "MJT14ZA/A", "MJT24ZA/A", "MJT34ZA/A"]
CATALOG.update({p: f"iPhone 18 Pro variant {i}" for i, p in enumerate(PRO)})


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def parse_multipart(data, ctype):
    """{field: str} plus {"document": {"filename", "content"}} from a multipart/form-data body."""
    from email import policy as email_policy
    from email.parser import BytesParser
    message = BytesParser(policy=email_policy.HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + data)
    fields = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        content = part.get_payload(decode=True)
        if part.get_filename():
            fields[name] = {"filename": part.get_filename(), "content": content}
        else:
            fields[name] = content.decode("utf-8")
    return fields


class StopBot(BaseException):
    """Raised by the fake Telegram to end run_bot's endless loop in tests."""


class FakeWorld:
    """Fake apple.com pickup-message and api.telegram.org."""

    def __init__(self):
        self.stock = set()          # {(storeNumber, part)}
        self.stock_sequence = []    # per-request stock overrides, consumed in order
        self.store_names = {}       # overrides for storeName
        self.apple_errors = []      # exceptions/raw bodies to return instead of a normal Apple response
        self.apple_requests = []    # list of part lists
        self.telegram_calls = []    # (token, method, payload)
        self.telegram_errors = []   # queued (code, description, retry_after)
        self.updates = []           # getUpdates results (list of lists, consumed in order)
        self.apple_date = None      # HTTP Date header Apple sends, if set
        self.stop_bot = False       # end run_bot when the scripted getUpdates results run out
        self.stock_fn = None        # if set: stock = stock_fn() at every Apple request
        self.clock = None           # if set: request_times gets clock.now of every Apple request
        self.request_times = []

    def urlopen(self, req, timeout=None, context=None):
        url = req.full_url
        if url.startswith("https://www.apple.com/hk/shop/retail/pickup-message?"):
            return self.apple(url)
        if url.startswith("https://api.telegram.org/bot"):
            self.last_ctype = req.get_header("Content-type") or ""
            return self.telegram(url, req.data)
        raise AssertionError(f"unexpected URL {url}")

    def apple(self, url):
        query = parse_qs(urlparse(url).query)
        parts = [query[f"parts.{i}"][0] for i in range(len(query)) if f"parts.{i}" in query]
        assert query["location"] == ["Central"] and query["pl"] == ["true"]
        self.apple_requests.append(parts)
        if self.clock:
            self.request_times.append(self.clock.now)
        if self.apple_errors:
            item = self.apple_errors.pop(0)
            if isinstance(item, Exception):
                raise item
            if item is not None:  # None: answer normally this time
                return FakeResponse(item.encode())
        if self.stock_sequence:
            self.stock = self.stock_sequence.pop(0)
        if self.stock_fn:
            self.stock = self.stock_fn()
        known = [p for p in parts[:20] if p in CATALOG]  # real Apple ignores parts after the 20th
        if not known:
            return FakeResponse(json.dumps({"head": {"status": "200"}, "body": {"content": {}}}).encode())
        stores = []
        for number, name, city in STORES:
            pa = {}
            for p in known:
                on = (number, p) in self.stock
                pa[p] = {
                    "pickupDisplay": "available" if on else "unavailable",
                    "pickupSearchQuote": "Available Today" if on else "Currently unavailable",
                    "messageTypes": {"regular": {
                        "storeSelectionEnabled": on,
                        "storePickupProductTitle": CATALOG[p],
                        "storePickupQuote": f"Today at Apple {name}" if on else "Currently unavailable",
                    }},
                }
            stores.append({"storeNumber": number, "storeName": self.store_names.get(number, name),
                           "city": city, "partsAvailability": pa})
        response = FakeResponse(json.dumps({"head": {"status": "200"}, "body": {"stores": stores}}).encode())
        if self.apple_date:
            response.headers = {"Date": self.apple_date}
        return response

    def telegram(self, url, data):
        path = urlparse(url).path  # /bot<token>/<method>
        token, method = path[len("/bot"):].rsplit("/", 1)
        if getattr(self, "last_ctype", "").startswith("multipart/form-data"):
            payload = parse_multipart(data, self.last_ctype)
        else:
            payload = json.loads(data or b"{}")
        self.telegram_calls.append((token, method, payload))
        if self.telegram_errors:
            code, description, retry_after = self.telegram_errors.pop(0)
            body = {"ok": False, "error_code": code, "description": description}
            if retry_after:
                body["parameters"] = {"retry_after": retry_after}
            raise HTTPError(url, code, description, {}, io.BytesIO(json.dumps(body).encode()))
        if method == "getMe":
            return FakeResponse(json.dumps({"ok": True, "result": {"id": 1, "username": "hk_stock_bot"}}).encode())
        if method == "getUpdates":
            if not self.updates and self.stop_bot:
                raise StopBot()
            result = self.updates.pop(0) if self.updates else []
            return FakeResponse(json.dumps({"ok": True, "result": result}).encode())
        return FakeResponse(json.dumps({"ok": True, "result": {"message_id": len(self.telegram_calls)}}).encode())

    def sent(self):
        return [p for (_, m, p) in self.telegram_calls if m == "sendMessage"]


class TagChecker(HTMLParser):
    """Telegram HTML: only <b> and <a href> are used, and every tag must be closed."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.text = [], []

    def handle_starttag(self, tag, attrs):
        assert tag in ("b", "a"), tag
        if tag == "a":
            assert dict(attrs)["href"].startswith("https://"), attrs
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, tag

    def handle_data(self, data):
        self.text.append(data)


FOOTER_RE = re.compile(r"\n\n🕐 Проверено: \d{2}\.\d{2}\.\d{4} \d{2}:\d{2} \(HKT\)(\n🔁 Проверок за \d+ мин: \d+(\n• [^\n]+)*)?$")


def body(text):
    """Parsed message text without the "checked at" footer (which must be present)."""
    parsed = check_html(text)
    assert FOOTER_RE.search(parsed), parsed[-60:]
    return FOOTER_RE.sub("", parsed)


def check_html(text):
    checker = TagChecker()
    checker.feed(text)
    checker.close()
    assert not checker.stack, checker.stack
    return "".join(checker.text)


START = datetime(2026, 10, 3, 2, 0, 5, tzinfo=timezone.utc).timestamp()  # 10:00:05 HKT


class Clock:
    """Fake time.time and time.sleep: sleeping moves the clock, then runs the hooks."""

    def __init__(self, start=START):
        self.now = start
        self.sleeps = []
        self.hooks = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        # Rounded so that steps of a fraction of a second still meet whole seconds exactly.
        self.now = round(self.now + seconds, 6)
        for hook in list(self.hooks):
            hook(self.now)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for f in ("check_stock.py", "telegram_setup.py", "stock_report.py"):
            shutil.copy(PROJECT / f, self.tmp / f)
        sys.path.insert(0, str(self.tmp))
        for m in ("check_stock", "telegram_setup", "stock_report"):
            sys.modules.pop(m, None)
        import check_stock
        self.cs = check_stock
        self.world = FakeWorld()
        self.sleeps = []
        self.patches = [
            mock.patch.dict(os.environ, {
                "TELEGRAM_BOT_TOKEN": "123456:SECRET-token", "TELEGRAM_CHAT_ID": "987654321",
                "PART_NUMBERS": "", "GITHUB_SERVER_URL": "", "GITHUB_REPOSITORY": "", "GITHUB_RUN_ID": "",
            }),
            mock.patch.object(self.cs, "urlopen", self.world.urlopen),
            mock.patch.object(self.cs, "CONFIG_FILE", self.tmp / "home-config" / "config.env"),
            mock.patch("builtins.input", side_effect=AssertionError("unexpected input() call")),
            mock.patch.object(self.cs.time, "sleep", self.sleeps.append),
            mock.patch("sys.stderr", new_callable=io.StringIO),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        sys.path.remove(str(self.tmp))
        shutil.rmtree(self.tmp)

    def run_once(self, checks=1, parts=None):
        return self.cs.run_checks(parts or list(self.cs.DEFAULT_PART_NUMBERS), checks, 60, dry_run=False)

    def state(self):
        f = self.tmp / "stock_state.json"
        return json.loads(f.read_text()) if f.exists() else None

    def state_raw(self):
        f = self.tmp / "stock_state.json"
        return f.read_text() if f.exists() else None

    def stderr(self):
        return sys.stderr.getvalue()

    def age_gone(self, minutes):
        st = self.state()
        when = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        for item in st.get("recently_gone", []):
            item["gone_since_utc"] = when
        (self.tmp / "stock_state.json").write_text(json.dumps(st))


class AlertScenarios(Base):
    def test_config_file_fills_missing_settings_only(self):
        config = self.cs.CONFIG_FILE
        config.parent.mkdir(parents=True)
        config.write_text('# local settings\n\nTELEGRAM_BOT_TOKEN="9:from-file"\nTELEGRAM_CHAT_ID=1, 2\nPART_NUMBERS=MJXU4ZA/A\nBROKEN LINE\nX=a=b\n')
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "PART_NUMBERS": "MJY24ZA/A"}, clear=False):
            del os.environ["TELEGRAM_BOT_TOKEN"]
            os.environ.pop("TELEGRAM_CHAT_ID", None)
            os.environ.pop("X", None)
            self.cs.load_config()
            self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "9:from-file")
            self.assertEqual(os.environ["TELEGRAM_CHAT_ID"], "1, 2")
            self.assertEqual(os.environ["PART_NUMBERS"], "MJY24ZA/A")  # real environment wins
            self.assertEqual(os.environ["X"], "a=b")
            self.assertNotIn("BROKEN LINE", os.environ)
            os.environ.pop("X", None)

    def test_missing_config_file_is_fine(self):
        self.cs.load_config()  # no file: nothing happens

    def test_defaults_are_the_12_pro_max_parts(self):
        self.assertEqual(self.cs.DEFAULT_PART_NUMBERS, list(PRO_MAX))
        self.assertEqual(self.cs.configured_parts(), list(PRO_MAX))

    def test_lifecycle(self):
        w = self.world
        w.stock = {("R499", "MJXX4ZA/A")}
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(len(w.apple_requests), 1)
        self.assertEqual(len(w.apple_requests[0]), 12)
        [msg] = w.sent()
        self.assertEqual(w.telegram_calls[0][0], "123456:SECRET-token")
        self.assertEqual(msg["chat_id"], "987654321")
        self.assertEqual(msg["parse_mode"], "HTML")
        self.assertEqual(msg["link_preview_options"], {"is_disabled": True})
        self.assertEqual(body(msg["text"]), "🍏 Apple Store Hong Kong\n\n🟢 Появились · iPhone 18 Pro Max\n🖤 1TB · Black — 🛒 Оформить")
        self.assertIn('<a href="https://www.apple.com/hk/shop/product/MJXX4ZA/A">🛒 Оформить</a>', msg["text"])
        self.assertIn("<b>🟢 Появились · iPhone 18 Pro Max</b>", msg["text"])
        self.assertNotIn("Canton Road", msg["text"])
        st = self.state()
        self.assertEqual(st["part_numbers"], list(PRO_MAX))
        self.assertEqual(st["available"], [{"partNumber": "MJXX4ZA/A", "product": "iPhone 18 Pro Max 1TB Black"}])
        self.assertNotIn("problem", st)
        raw = self.state_raw()

        # unchanged stock: no message, byte-identical state, no extra Apple request
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(len(w.sent()), 1)
        self.assertEqual(self.state_raw(), raw)
        self.assertEqual(len(w.apple_requests), 2)

        # the same configuration in another store is not news; two new configurations are
        w.stock |= {("R428", "MJXX4ZA/A"), ("R610", "MJY44ZA/A"), ("R409", "MJXU4ZA/A")}
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(
            body(w.sent()[1]["text"]),
            "🍏 Apple Store Hong Kong\n\n🟢 Появились · iPhone 18 Pro Max\n🩶 512GB · Silver — 🛒 Оформить\n\n❤️ 2TB · Burgundy — 🛒 Оформить",
        )

        # some sell out: confirmed by a second look 20 s later, then reported without links
        w.stock = {("R499", "MJXX4ZA/A")}
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.sleeps, [20])
        self.assertEqual(
            body(w.sent()[2]["text"]),
            "🍏 Apple Store Hong Kong\n\n🔴 Закончились · iPhone 18 Pro Max\n"
            "🩶 512GB · Silver\n\n❤️ 2TB · Burgundy",
        )
        self.assertNotIn("<a href", w.sent()[2]["text"])
        self.assertEqual([i["partNumber"] for i in self.state()["available"]], ["MJXX4ZA/A"])

        # a swap in one check: one message with both sections
        w.stock = {("R409", "MJY54ZA/A")}
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(
            body(w.sent()[3]["text"]),
            "🍏 Apple Store Hong Kong\n\n🟢 Появились · iPhone 18 Pro Max\n🩵 2TB · Glacier — 🛒 Оформить\n\n"
            "🔴 Закончились · iPhone 18 Pro Max\n🖤 1TB · Black",
        )

        # everything gone, then back
        w.stock = set()
        self.run_once()
        self.assertEqual(self.state()["available"], [])
        w.stock = {("R409", "MJY54ZA/A")}
        self.run_once()
        self.assertEqual(len(w.sent()), 6)
        self.assertIn("🟢 Появились", w.sent()[5]["text"])

    def test_flicker_is_not_reported_as_a_sell_out(self):
        """Apple answers "nothing" for a moment while stock is released (seen 2026-10-02 07:43)."""
        w = self.world
        six = {("R409", p) for p in list(PRO_MAX)[:6]}
        w.stock = set(six)
        self.run_once()
        raw = self.state_raw()
        w.stock_sequence = [set(), set(six)]  # empty answer, then everything back on the second look
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(len(w.sent()), 1)  # only the first "appeared" message
        self.assertEqual(self.state_raw(), raw)
        self.assertEqual(self.sleeps, [20])

    def test_partial_flicker_reports_only_confirmed_sell_outs(self):
        w = self.world
        six = [("R409", p) for p in list(PRO_MAX)[:6]]
        w.stock = set(six)
        self.run_once()
        w.stock_sequence = [set(six[3:]), set(six[1:])]  # three missing; two of them back on the second look
        self.assertEqual(self.run_once(), 0)
        text = body(w.sent()[1]["text"])
        self.assertEqual(text, "🍏 Apple Store Hong Kong\n\n🔴 Закончились · iPhone 18 Pro Max\n🩶 512GB · Silver")
        self.assertEqual(len(self.state()["available"]), 5)

    def test_sell_out_not_reported_when_second_look_fails(self):
        w = self.world
        w.stock = {("R409", "MJXU4ZA/A")}
        self.run_once()
        w.stock = set()
        w.apple_errors = [None, HTTPError("u", 503, "x", {}, io.BytesIO(b""))]
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(len(w.sent()), 1)
        self.assertEqual([i["partNumber"] for i in self.state()["available"]], ["MJXU4ZA/A"])
        self.assertIn("Could not confirm the sell-out", self.stderr())
        self.assertEqual(self.run_once(), 0)  # next check confirms it
        self.assertIn("🔴 Закончились", w.sent()[1]["text"])

    def test_network_error_is_retried_once(self):
        w = self.world
        w.stock = {("R409", "MJXU4ZA/A")}
        w.apple_errors = [URLError("The read operation timed out")]
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.sleeps, [5])
        self.assertEqual(len(w.apple_requests), 2)
        self.assertEqual(len(w.sent()), 1)  # the check succeeded on the second try
        self.assertIn("trying again in 5 s", sys.stdout.getvalue())
        self.assertNotIn("Could not reach Apple", self.stderr())

    def test_two_network_errors_fail_the_check(self):
        self.world.apple_errors = [URLError("timed out"), URLError("timed out")]
        self.assertEqual(self.run_once(), 1)
        self.assertIn("Could not reach Apple: timed out", self.stderr())
        self.assertEqual(self.world.sent(), [])

    def test_http_errors_are_not_retried(self):
        self.world.apple_errors = [HTTPError("u", 503, "x", {}, io.BytesIO(b""))]
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(len(self.world.apple_requests), 1)
        self.assertEqual(self.sleeps, [])

    def test_watch_mode_pauses_5_3_4_minutes_in_turn(self):
        stop = RuntimeError("stop watching")

        def sleep(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) >= 7:
                raise stop

        with mock.patch.object(self.cs.time, "sleep", sleep):
            with self.assertRaises(RuntimeError):
                self.cs.run_checks(list(PRO_MAX), None, self.cs.parse_schedule("5,3,4"), dry_run=False)
        self.assertEqual(self.sleeps, [300, 180, 240, 300, 180, 240, 300])
        self.assertEqual(len(self.world.apple_requests), 7)
        self.assertIn("Next check in 3 min, at", sys.stdout.getvalue())

    def watch(self, sleeps_before_stop, status_minutes=10, pauses=(300, 180, 240)):
        stop = RuntimeError("stop watching")
        clock = Clock()

        def sleep(seconds):
            clock.sleep(seconds)
            self.sleeps.append(seconds)
            if len([x for x in self.sleeps if x != 20]) >= sleeps_before_stop:
                raise stop

        with mock.patch.object(self.cs.time, "sleep", sleep), mock.patch.object(self.cs.time, "time", clock.time), \
             mock.patch.object(self.cs, "utc_now", lambda: datetime.fromtimestamp(clock.now, timezone.utc)):
            with self.assertRaises(RuntimeError):
                self.cs.run_checks(list(PRO_MAX), None, list(pauses), dry_run=False, status_minutes=status_minutes)

    def test_quiet_status_every_10_minutes_on_the_clock(self):
        self.world.stock = {(st[0], p) for st in STORES[:2] for p in ("MJY34ZA/A", "MJY24ZA/A")}
        self.run_once()  # known already: no change alerts below
        self.watch(sleeps_before_stop=7)  # checks at 10:00, 10:05, 10:08, 10:12, 10:17, 10:20, 10:24
        statuses = [m for m in self.world.sent()[1:]]
        self.assertEqual(len(statuses), 2)  # with the first checks after 10:10 and 10:20
        self.assertTrue(statuses[0]["text"].endswith("🕐 Проверено: 03.10.2026 10:12 (HKT)\n🔁 Проверок за 12 мин: 3"))
        self.assertTrue(statuses[1]["text"].endswith("🕐 Проверено: 03.10.2026 10:20 (HKT)\n🔁 Проверок за 8 мин: 2"))
        for msg in statuses:
            self.assertTrue(msg["disable_notification"])
            lines = body(msg["text"]).splitlines()
            self.assertEqual(lines[:5], [
                "📋 Наличие в Apple Store Hong Kong", "",
                "🟢 В наличии · iPhone 18 Pro Max", "🩶 2TB · Silver — 🛒 Оформить", "🖤 2TB · Black — 🛒 Оформить",
            ])
            self.assertEqual(len(lines), 5)
            self.assertNotIn("Нет в наличии", msg["text"])
        self.assertFalse(self.world.sent()[0]["disable_notification"])  # change alerts keep their sound

    def test_status_with_pauses_of_1_2_3_minutes(self):
        self.world.stock = {("R409", "MJY34ZA/A")}
        self.run_once()
        # checks at 0, 1, 3, 6, 7, 9 | 12, 13, 15, 18, 19 | 21, 24, 25, 27 | 30, 31, 33, 36 min past 10:00
        self.watch(sleeps_before_stop=19, pauses=(60, 120, 180))
        self.assertEqual(self.sleeps, [60, 120, 180] * 6 + [60])
        statuses = self.world.sent()[1:]
        self.assertEqual(len(statuses), 3)  # at 10:12, 10:21 and 10:30
        self.assertEqual([check_html(m["text"]).splitlines()[-1] for m in statuses],
                         ["🔁 Проверок за 12 мин: 6", "🔁 Проверок за 9 мин: 5", "🔁 Проверок за 9 мин: 4"])
        for msg in statuses:
            self.assertTrue(msg["disable_notification"])
            self.assertIn("📋 Наличие в Apple Store Hong Kong", msg["text"])

    def test_status_is_sent_in_the_new_period_even_when_stock_changes(self):
        w = self.world
        # The check at 10:12 brings new stock and still produces the scheduled full status.
        w.stock_sequence = [set(), set(), set(), {("R409", "MJXU4ZA/A")}, {("R409", "MJXU4ZA/A")}]
        self.watch(sleeps_before_stop=5)  # checks at 10:00, 10:05, 10:08, 10:12, 10:17
        texts = [check_html(m["text"]) for m in w.sent()]
        self.assertEqual(len(texts), 2)
        self.assertIn("🟢 Появились", texts[0])
        self.assertIn("🕐 Проверено: 03.10.2026 10:12 (HKT)", texts[0])
        self.assertTrue(texts[1].startswith("📋 Наличие в Apple Store Hong Kong"), texts[1])
        self.assertIn("🕐 Проверено: 03.10.2026 10:12 (HKT)\n🔁 Проверок за 12 мин: 3", texts[1])

    def test_failed_status_is_retried_with_the_next_fresh_check(self):
        status = self.cs.Status(10)
        result = self.cs.CheckResult(6, dict(CATALOG), [], [], datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc))
        status.after_check(result, list(PRO_MAX), False, False, 'vps', ('vps', 'github'))
        self.world.telegram_errors = [(502, 'Bad Gateway', None)]
        result.checked_at += timedelta(minutes=10)
        status.after_check(result, list(PRO_MAX), False, False, 'github', ('vps', 'github'))
        self.assertEqual(status.counts, {'vps': 1, 'github': 1})
        result.checked_at += timedelta(seconds=15)
        status.after_check(result, list(PRO_MAX), False, False, 'vps', ('vps', 'github'))
        successful = self.world.sent()[-1]['text']
        self.assertIn('🔁 Проверок за 10 мин: 2', successful)
        self.assertIn('• <b>VPS 1</b> — 1\n• <b>GitHub</b> — 1', successful)
        self.assertEqual(status.counts, {'vps': 1})

    def test_status_can_be_switched_off(self):
        self.watch(sleeps_before_stop=7, status_minutes=0)
        self.assertEqual(self.world.sent(), [])
        with mock.patch.object(self.cs, "run_checks", return_value=0) as run, \
             mock.patch.object(sys, "argv", ["check_stock.py", "--watch", "--status-minutes", "0"]):
            self.cs.main()
        self.assertEqual(run.call_args.args[4], 0)
        self.assertIn("no status messages", sys.stdout.getvalue())
        with mock.patch.object(self.cs, "run_checks", return_value=0) as run, \
             mock.patch.object(sys, "argv", ["check_stock.py", "--watch"]):
            self.cs.main()
        self.assertEqual(run.call_args.args[2:5], ([60, 120, 180], False, 10))  # defaults: 1, 2, 3 min; every 10 min
        self.assertIn("pauses of 1, 2, 3 min in turn; a quiet status every 10 min.", sys.stdout.getvalue())
        with mock.patch.object(sys, "argv", ["check_stock.py", "--watch", "--status-minutes", "-1"]), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                self.cs.main()

    def test_status_command_sends_with_sound_and_keeps_state(self):
        self.world.stock = set()
        with mock.patch.object(sys, "argv", ["check_stock.py", "--status"]):
            self.assertEqual(self.cs.main(), 0)
        [msg] = self.world.sent()
        self.assertFalse(msg["disable_notification"])
        self.assertEqual(
            body(msg["text"]),
            "📋 Наличие в Apple Store Hong Kong\n\nСейчас ничего из отслеживаемого нет в наличии.",
        )
        self.assertIsNone(self.state())

    def test_manual_status_uses_recent_counts_without_changing_history_or_state(self):
        now = datetime(2026, 10, 3, 2, 10, tzinfo=timezone.utc)
        history = self.tmp / 'stock_history.jsonl'
        entries = [
            {'time': (now - timedelta(minutes=11)).isoformat(), 'source': 'vps', 'stores': 6},
            {'time': (now - timedelta(seconds=30)).isoformat(), 'source': 'vps', 'stores': 6},
            {'time': (now - timedelta(seconds=15)).isoformat(), 'source': 'secondary', 'stores': 6},
            {'time': now.isoformat(), 'source': 'github', 'stores': 6},
            {'time': now.isoformat(), 'source': 'github', 'error': 'Apple returned HTTP 541'},
        ]
        history.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
        state = self.tmp / 'stock_state.json'
        state.write_text('{"available": []}')
        before = (state.read_bytes(), history.read_bytes())
        with mock.patch.object(self.cs, 'utc_now', return_value=now), \
             mock.patch.object(sys, 'argv', ['check_stock.py', '--status']):
            self.assertEqual(self.cs.main(), 0)
        [message] = self.world.sent()
        self.assertTrue(message['text'].endswith(
            '🔁 Проверок за 10 мин: 3\n• <b>VPS 1</b> — 1\n• <b>VPS 2</b> — 1\n• <b>VPS 3</b> — 0\n• <b>VPS 4</b> — 0\n• <b>VPS 5</b> — 0\n'
            '• <b>VPS 6</b> — 0\n• <b>VPS 7</b> — 0\n• <b>VPS 8</b> — 0\n• <b>GitHub</b> — 1'))
        self.assertEqual((state.read_bytes(), history.read_bytes()), before)

    def test_schedule_parsing(self):
        self.assertEqual(self.cs.parse_schedule("5,3,4"), [300, 180, 240])
        self.assertEqual(self.cs.parse_schedule(" 2.5, 1 ,"), [150, 60])
        for bad in ("", "0", "5,x", "0.2"):
            with self.assertRaises(SystemExit):
                self.cs.parse_schedule(bad)

    def test_watch_flag_uses_the_schedule_variable(self):
        with mock.patch.object(self.cs, "run_checks", return_value=0) as run, \
             mock.patch.object(sys, "argv", ["check_stock.py", "--watch"]), \
             mock.patch.dict(os.environ, {"CHECK_SCHEDULE_MINUTES": "2,1"}):
            self.assertEqual(self.cs.main(), 0)
        self.assertEqual(run.call_args.args[1:3], (None, [120, 60]))
        with mock.patch.object(self.cs, "run_checks", return_value=0) as run, \
             mock.patch.object(sys, "argv", ["check_stock.py", "--watch", "5,3,4"]):
            self.cs.main()
        self.assertEqual(run.call_args.args[2], [300, 180, 240])

    def test_state_is_written_atomically(self):
        self.world.stock = {("R409", "MJXU4ZA/A")}
        self.run_once()
        self.assertEqual(sorted(x.name for x in self.tmp.iterdir() if x.name.startswith("stock_state")), ["stock_state.json"])

    def test_old_per_store_state_is_migrated_without_new_alerts(self):
        (self.tmp / "stock_state.json").write_text(json.dumps({
            "part_numbers": list(PRO_MAX), "location": "Central",
            "available": [
                {"storeNumber": "R409", "storeName": "Causeway Bay", "partNumber": "MJXU4ZA/A", "product": "iPhone 18 Pro Max 512GB Silver"},
                {"storeNumber": "R485", "storeName": "Festival Walk", "partNumber": "MJXU4ZA/A", "product": "iPhone 18 Pro Max 512GB Silver"},
                {"storeNumber": "R499", "storeName": "Canton Road", "partNumber": "MJY24ZA/A", "product": "iPhone 18 Pro Max 2TB Black"},
            ],
            "recently_gone": [{"partNumber": "MJY44ZA/A", "product": "x", "gone_since_utc": "2026-10-02T00:14:22+00:00"}],
            "updated_hkt": "2026-10-02 08:04:17 HKT",
        }))
        self.world.stock = {("R428", "MJXU4ZA/A"), ("R499", "MJY24ZA/A")}
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.world.sent(), [])
        self.assertEqual([i["partNumber"] for i in self.state()["available"]], ["MJXU4ZA/A", "MJY24ZA/A"])
        self.assertNotIn("storeNumber", self.state()["available"][0])
        self.assertNotIn("recently_gone", self.state())

    def test_flicker_within_one_run(self):
        w = self.world
        w.stock_sequence = [{("R409", "MJXU4ZA/A")}, set(), {("R409", "MJXU4ZA/A")}, set(), {("R409", "MJXU4ZA/A")}, {("R409", "MJXU4ZA/A")}]
        self.assertEqual(self.run_once(checks=3), 0)
        self.assertEqual(len(w.sent()), 1)  # each empty answer was checked again and found to be flicker

    def test_unwatched_parts_are_dropped_from_state(self):
        self.world.stock = {("R409", "MJXU4ZA/A"), ("R409", "MJXT4ZA/A")}
        self.run_once()
        self.assertEqual(len(self.state()["available"]), 2)
        self.run_once(parts=["MJXU4ZA/A"])
        self.assertEqual([i["partNumber"] for i in self.state()["available"]], ["MJXU4ZA/A"])
        self.assertEqual(len(self.world.sent()), 1)  # dropping an unwatched part is not a sell-out

    def test_hand_edited_state_is_tolerated(self):
        (self.tmp / "stock_state.json").write_text(json.dumps({
            "available": [{"storeNumber": "R409", "partNumber": "MJXU4ZA/A"}, "junk", {"storeNumber": "R428"}],
            "recently_gone": [{"storeNumber": "R499", "partNumber": "MJXT4ZA/A", "gone_since_utc": "2026-10-02T07:00:00"}],
        }))
        self.world.stock = {("R409", "MJXU4ZA/A"), ("R499", "MJXT4ZA/A")}
        self.assertEqual(self.run_once(), 0)
        [msg] = self.world.sent()  # MJXU4 was known; MJXT4 only "recently gone" in the old format, so it is new
        self.assertEqual(body(msg["text"]), "🍏 Apple Store Hong Kong\n\n🟢 Появились · iPhone 18 Pro Max\n🖤 512GB · Black — 🛒 Оформить")

    def test_telegram_error_keeps_state_and_retries(self):
        w = self.world
        w.stock = {("R485", "MJY24ZA/A")}
        w.telegram_errors = [(400, "Bad Request: chat not found", None)]
        self.assertEqual(self.run_once(), 1)
        self.assertIn("Telegram sendMessage failed: HTTP 400 Bad Request: chat not found", self.stderr())
        self.assertNotIn("SECRET", self.stderr())
        self.assertEqual(self.state() and self.state().get("available"), None)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(len(w.sent()), 2)  # failed attempt + successful retry
        self.assertEqual(len(self.state()["available"]), 1)

    def test_rate_limit_is_retried_once(self):
        w = self.world
        w.stock = {("R485", "MJY24ZA/A")}
        w.telegram_errors = [(429, "Too Many Requests: retry after 3", 3)]
        self.assertEqual(self.run_once(), 0)
        self.assertIn(3, self.sleeps)
        self.assertEqual(len(w.sent()), 2)

    def test_unauthorized_token(self):
        self.world.stock = {("R485", "MJY24ZA/A")}
        self.world.telegram_errors = [(401, "Unauthorized", None)]
        self.assertEqual(self.run_once(), 1)
        self.assertIn("HTTP 401 Unauthorized", self.stderr())

    def test_missing_telegram_settings(self):
        self.world.stock = {("R485", "MJY24ZA/A")}
        with mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": " "}):
            self.assertEqual(self.run_once(), 1)
        self.assertIn("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing", self.stderr())
        self.assertIsNone(self.state())

    def test_malformed_token_in_actions(self):
        self.world.stock = {("R485", "MJY24ZA/A")}
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "bot8000000001:SECRETabc"}):
            self.assertEqual(self.run_once(), 1)
        self.assertIn("TELEGRAM_BOT_TOKEN is malformed", self.stderr())
        self.assertNotIn("SECRETabc", self.stderr())
        self.assertEqual(self.world.telegram_calls, [])

    def test_invalid_url_error_does_not_leak_token(self):
        import http.client
        def boom(req, timeout=None, context=None):
            raise http.client.InvalidURL(f"URL can't contain control characters. {req.full_url!r}")
        with mock.patch.object(self.cs, "urlopen", boom):
            with self.assertRaises(RuntimeError) as ctx:
                self.cs.telegram_api("123:SECRET-token", "getMe")
        self.assertEqual(str(ctx.exception), "Telegram getMe request failed: InvalidURL")
        self.assertIsNone(ctx.exception.__cause__)

    def test_several_chat_ids(self):
        self.world.stock = {("R485", "MJY24ZA/A")}
        with mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "111, -1002222"}):
            self.assertEqual(self.run_once(), 0)
        self.assertEqual([m["chat_id"] for m in self.world.sent()], ["111", "-1002222"])

    def test_everything_in_stock_fits_one_short_message(self):
        w = self.world
        w.stock = {(st[0], p) for st in STORES for p in PRO_MAX}
        self.assertEqual(self.run_once(), 0)
        [msg] = w.sent()
        self.assertLess(len(msg["text"]), 2000)
        lines = body(msg["text"]).splitlines()
        self.assertEqual(lines[:3], ["🍏 Apple Store Hong Kong", "", "🟢 Появились · iPhone 18 Pro Max"])
        groups = [
            [f"{emoji} {cap} · {color} — 🛒 Оформить"
             for color, emoji in (("Silver", "🩶"), ("Black", "🖤"), ("Glacier", "🩵"), ("Burgundy", "❤️"))]
            for cap in ("512GB", "1TB", "2TB")
        ]
        self.assertEqual(lines[3:], groups[0] + [""] + groups[1] + [""] + groups[2])
        self.assertEqual(msg["text"].count("<a href="), 12)

    def test_html_is_escaped(self):
        CATALOG["MJXU4ZA/A"], saved = "iPhone <18> & Co 512GB Silver", CATALOG["MJXU4ZA/A"]
        try:
            self.world.stock = {("R409", "MJXU4ZA/A")}
            self.assertEqual(self.run_once(), 0)
        finally:
            CATALOG["MJXU4ZA/A"] = saved
        raw = self.world.sent()[0]["text"]
        self.assertIn("<b>🟢 Появились · iPhone &lt;18&gt; &amp; Co</b>", raw)
        self.assertIn("🟢 Появились · iPhone <18> & Co", check_html(raw))

    def test_more_than_20_parts_are_batched(self):
        parts = list(PRO_MAX) + PRO  # 25 parts
        self.world.stock = {("R673", PRO[-1])}  # the 25th part
        self.assertEqual(self.run_once(parts=parts), 0)
        self.assertEqual([len(r) for r in self.world.apple_requests], [20, 5])
        self.assertEqual(len(self.world.sent()), 1)
        self.assertIn("iPhone 18 Pro variant 12", self.world.sent()[0]["text"])
        self.assertNotIn("no availability record", self.stderr())

    def test_dry_run_sends_nothing(self):
        self.world.stock = {("R499", "MJXX4ZA/A")}
        self.assertEqual(self.cs.run_checks(list(PRO_MAX), 2, 60, dry_run=True), 0)
        self.assertEqual(self.world.telegram_calls, [])
        self.assertIsNone(self.state())
        self.assertIn("dry run, Telegram message not sent", sys.stdout.getvalue())

    def test_split_message_long_block_breaks_between_lines(self):
        lines = [f"• line {i} <b>x</b>" for i in range(400)]
        text = "head\n\n" + "\n".join(lines) + "\n\ntail"
        chunks = self.cs.split_message(text)
        self.assertTrue(all(len(c) <= self.cs.TELEGRAM_MAX_LENGTH for c in chunks))
        for c in chunks:
            check_html(c)
        self.assertEqual("\n".join(chunks).replace("\n\n", "\n").split("\n"), ["head", *lines, "tail"])


class HealthScenarios(Base):
    def setUp(self):
        super().setUp()
        os.environ.update(GITHUB_SERVER_URL="https://github.com", GITHUB_REPOSITORY="me/watch", GITHUB_RUN_ID="42")

    def age_problem(self, minutes):
        st = self.state()
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        st["problem"]["since_utc"] = since.isoformat(timespec="seconds")
        (self.tmp / "stock_state.json").write_text(json.dumps(st))

    def test_apple_outage_reported_once_after_30_min_then_recovery(self):
        w = self.world
        e503 = HTTPError("u", 503, "Service Unavailable", {}, io.BytesIO(b""))
        w.apple_errors = [e503] * 3
        self.assertEqual(self.run_once(checks=3), 1)
        self.assertEqual(w.sent(), [])  # too early to bother the user
        self.assertFalse(self.state()["problem"]["notified"])

        self.age_problem(35)
        w.apple_errors = [e503]
        self.assertEqual(self.run_once(), 1)
        [warning] = w.sent()
        text = check_html(warning["text"])
        self.assertTrue(text.startswith("⚠️ Проверка наличия в Apple Store Hong Kong не работает уже 35 мин"), text)
        self.assertIn("Последняя ошибка: Apple returned HTTP 503", text)
        self.assertIn('<a href="https://github.com/me/watch/actions/runs/42">Лог запуска</a>', warning["text"])
        self.assertTrue(self.state()["problem"]["notified"])

        w.apple_errors = [e503]
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(len(w.sent()), 1)  # no repeated warnings

        self.assertEqual(self.run_once(), 0)  # Apple is back
        self.assertEqual(check_html(w.sent()[1]["text"]), "✅ Проверка наличия в Apple Store Hong Kong снова работает.")
        self.assertNotIn("problem", self.state())

    def test_short_blip_is_never_reported(self):
        self.world.apple_errors = ["<html>busy</html>"]
        self.assertEqual(self.run_once(), 1)
        self.assertIn("problem", self.state())
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.world.sent(), [])
        self.assertNotIn("problem", self.state())

    def test_partial_failure_within_run_is_not_a_problem(self):
        e503 = HTTPError("u", 503, "x", {}, io.BytesIO(b""))
        self.world.apple_errors = [e503]
        self.assertEqual(self.run_once(checks=2), 0)
        self.assertIsNone(self.state().get("problem"))

    def test_bad_part_number_is_reported(self):
        parts = ["MJXU4ZA/A", "MJXU4QN/A"]
        self.assertEqual(self.run_once(parts=parts), 1)
        self.assertIn("no availability record for MJXU4QN/A", self.stderr())
        self.age_problem(31)
        self.assertEqual(self.run_once(parts=parts), 1)
        self.assertIn("Apple не вернул данные по артикулам MJXU4QN/A. Проверьте PART_NUMBERS.",
                      check_html(self.world.sent()[0]["text"]))

    def test_failed_warning_and_recovery_are_retried(self):
        w = self.world
        e503 = HTTPError("u", 503, "x", {}, io.BytesIO(b""))
        w.apple_errors = [e503]
        self.run_once()
        self.age_problem(40)
        w.apple_errors = [e503]
        w.telegram_errors = [(502, "Bad Gateway", None)]
        self.run_once()
        self.assertFalse(self.state()["problem"]["notified"])
        w.apple_errors = [e503]
        self.run_once()
        self.assertTrue(self.state()["problem"]["notified"])
        w.telegram_errors = [(502, "Bad Gateway", None)]
        self.assertEqual(self.run_once(), 0)
        self.assertIn("problem", self.state())  # recovery message still owed
        self.run_once()
        self.assertNotIn("problem", self.state())
        self.assertIn("снова работает", w.sent()[-1]["text"])

    def test_stock_alert_still_sent_while_a_part_is_bad(self):
        self.world.stock = {("R428", "MJXU4ZA/A")}
        self.assertEqual(self.run_once(parts=["MJXU4ZA/A", "BOGUS1ZA/A"]), 1)
        self.assertIn("512GB · Silver", check_html(self.world.sent()[0]["text"]))


class TestMessage(Base):
    def test_test_message_lists_every_watched_configuration(self):
        self.world.stock = {("R499", "MJY34ZA/A"), ("R428", "MJY34ZA/A")}
        self.cs.send_test_message(list(PRO_MAX))
        [msg] = self.world.sent()
        lines = body(msg["text"]).splitlines()
        self.assertEqual(lines[:5], [
            "🧪 Тест: уведомления о наличии в Apple Store Hong Kong будут приходить сюда.", "",
            "Сейчас:", "", "📱 iPhone 18 Pro Max",
        ])
        self.assertIn("🩶 2TB · Silver — ✅ в наличии", lines)
        self.assertEqual(sum(line.endswith("— ➖ нет") for line in lines), 11)
        self.assertNotIn("Canton Road", msg["text"])

    def test_footer_shows_hong_kong_date_and_time(self):
        class Fixed(datetime):
            @classmethod
            def now(cls, tz=None):
                moment = datetime(2026, 10, 2, 16, 5, 59, tzinfo=timezone.utc)  # 00:05 next day in HK
                return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

        self.world.stock = {("R409", "MJXU4ZA/A")}
        with mock.patch.object(self.cs, "datetime", Fixed):
            self.assertEqual(self.run_once(), 0)
            self.cs.send_test_message(list(PRO_MAX))
        for msg in self.world.sent():
            self.assertTrue(check_html(msg["text"]).endswith("\n\n🕐 Проверено: 03.10.2026 00:05 (HKT)"), msg["text"][-60:])

    def test_footer_uses_apples_clock_not_the_servers(self):
        class Skewed(datetime):  # this computer's clock is an hour off
            @classmethod
            def now(cls, tz=None):
                moment = datetime(2026, 10, 2, 9, 0, 0, tzinfo=timezone.utc)
                return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

        self.world.apple_date = "Fri, 02 Oct 2026 07:59:41 GMT"
        self.world.stock = {("R409", "MJXU4ZA/A")}
        with mock.patch.object(self.cs, "datetime", Skewed):
            self.run_once()
            self.cs.send_test_message(list(PRO_MAX))
        for msg in self.world.sent():
            self.assertTrue(check_html(msg["text"]).endswith("🕐 Проверено: 02.10.2026 15:59 (HKT)"), msg["text"][-50:])

    def test_test_message_when_apple_fails(self):
        self.world.apple_errors = [HTTPError("u", 541, "x", {}, io.BytesIO(b""))]
        self.cs.send_test_message(list(PRO_MAX))
        self.assertIn("⚠️ Не удалось проверить наличие: Apple returned HTTP 541", self.world.sent()[0]["text"])


class TelegramDelivery(Base):
    """Now and then a connection to Telegram hangs for a minute (seen from VPS 1 1–4 times a day)."""

    def test_a_hung_connection_is_given_up_and_the_message_sent_once_more(self):
        timeouts, telegram = [], self.world.telegram
        failures = [URLError(TimeoutError("The handshake operation timed out"))]
        self.world.telegram = lambda url, data: (_ for _ in ()).throw(failures.pop()) if failures else telegram(url, data)

        def urlopen(req, timeout=None, context=None):
            timeouts.append(timeout)
            return self.world.urlopen(req, timeout, context)

        with mock.patch.object(self.cs, "urlopen", urlopen):
            self.cs.send_telegram("🟢 Появились")
        self.assertEqual(timeouts, [10, 10])
        self.assertEqual(len(self.world.sent()), 1)
        self.assertIn("Telegram did not answer (The handshake operation timed out); trying again.", sys.stdout.getvalue())

    def test_a_second_failed_connection_is_an_error_without_the_token(self):
        self.world.telegram = lambda url, data: (_ for _ in ()).throw(URLError(ConnectionResetError(104, "Connection reset by peer")))
        with self.assertRaisesRegex(RuntimeError, "Could not reach Telegram") as caught:
            self.cs.send_telegram("🟢 Появились")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_a_slow_status_holds_nothing_up_and_its_checks_are_counted_once(self):
        hkt = timezone(timedelta(hours=8))
        release, calls = threading.Event(), []
        self.addCleanup(release.set)

        def send(message, silent=False):
            calls.append(message)
            if len(calls) == 1:
                release.wait(10)  # Telegram hangs on the first status

        def check(status, hhmmss, source):
            moment = datetime(2026, 10, 3, *map(int, hhmmss.split(":")), tzinfo=hkt)
            status.after_check(self.cs.CheckResult(6, {}, [], [], moment), [], False, False, source, ("vps", "github"))

        status = self.cs.Status(10)
        with mock.patch.object(self.cs, "send_telegram", send), mock.patch.object(self.cs, "STATUS_SEND_WAIT", 0.05):
            check(status, "10:00:00", "vps")
            check(status, "10:00:15", "github")
            started = time.monotonic()
            check(status, "10:10:00", "vps")      # the status of 10:00–10:10 hangs
            self.assertLess(time.monotonic() - started, 2)
            check(status, "10:10:15", "github")   # counted for the next status; no second send meanwhile
            self.assertEqual(len(calls), 1)
            release.set()
            self.assertTrue(status.sending[0].wait(5))
            check(status, "10:10:30", "vps")
            check(status, "10:20:00", "github")   # the status of 10:10–10:20
        self.assertEqual(len(calls), 2)
        self.assertIn("🔁 Проверок за 10 мин: 2", check_html(calls[0]))
        self.assertIn("🔁 Проверок за 10 мин: 3", check_html(calls[1]))
        self.assertEqual(sys.stdout.getvalue().count("Status sent to Telegram."), 2)


class TelegramSetup(Base):
    def test_finds_chat_after_user_presses_start(self):
        import telegram_setup
        self.world.updates = [
            [],
            [{"update_id": 1, "message": {"chat": {"id": 555, "type": "private", "first_name": "Ivan"}, "text": "/start"}}],
        ]
        prompts = []
        with mock.patch.object(telegram_setup.getpass, "getpass", return_value=" 123:abc "), \
             mock.patch("builtins.input", side_effect=lambda p: prompts.append(p) or ""):
            telegram_setup.main()
        out = sys.stdout.getvalue()
        self.assertIn("Бот: @hk_stock_bot", out)
        self.assertIn("555  (private) Ivan", out)
        self.assertIn("TELEGRAM_CHAT_ID   = 555", out)
        self.assertEqual(len(prompts), 2)
        self.assertIn("https://t.me/hk_stock_bot", prompts[0])
        self.assertIn("Сохранить токен и chat ID", prompts[1])
        config = self.cs.CONFIG_FILE
        self.assertEqual(config.read_text(), "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=555\n")
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual({c[0] for c in self.world.telegram_calls}, {"123:abc"})
        self.assertEqual(self.world.sent()[-1]["chat_id"], 555)

    def test_config_save_keeps_other_settings_and_can_be_declined(self):
        import telegram_setup
        config = self.cs.CONFIG_FILE
        config.parent.mkdir(parents=True)
        config.write_text("PART_NUMBERS=MJXU4ZA/A\nTELEGRAM_BOT_TOKEN=old\n")
        self.world.updates = [[{"update_id": 1, "message": {"chat": {"id": 9, "type": "private"}}}]] * 2
        good = "8000000001:" + "A" * 35
        with mock.patch.object(telegram_setup.getpass, "getpass", return_value=good), \
             mock.patch("builtins.input", return_value="н"):
            telegram_setup.main()
        self.assertIn("TELEGRAM_BOT_TOKEN=old", config.read_text())  # declined: untouched
        with mock.patch.object(telegram_setup.getpass, "getpass", return_value=good), \
             mock.patch("builtins.input", return_value="да"):
            telegram_setup.main()
        self.assertEqual(config.read_text(), f"PART_NUMBERS=MJXU4ZA/A\nTELEGRAM_BOT_TOKEN={good}\nTELEGRAM_CHAT_ID=9\n")

    def run_setup(self, typed):
        import telegram_setup
        with mock.patch.object(telegram_setup.getpass, "getpass", return_value=typed):
            with self.assertRaises(SystemExit) as ctx:
                telegram_setup.main()
        return str(ctx.exception.code)

    def test_wrong_token_401(self):
        self.world.telegram_errors = [(401, "Unauthorized", None)]
        msg = self.run_setup("8000000001:" + "A" * 35)
        self.assertIn("Telegram не принял токен (401 Unauthorized): он неверный или отозван.", msg)
        self.assertNotIn("скопирован не полностью", msg)
        self.world.telegram_errors = [(401, "Unauthorized", None)]
        self.assertIn("скопирован не полностью", self.run_setup("8000000001:AAFo1EgL"))

    def test_malformed_tokens_are_explained_without_calling_telegram(self):
        secret = "S3cretPart_abcdefghijklmnopqrstuvwx"
        cases = {
            "8000000001" + secret: "в нём нет двоеточия (введено символов: 45)",
            "x8000000001:" + secret: "до двоеточия должен быть только номер бота из цифр, а введено «x8000000001»",
            "ppython3 telegram_setup.py8000000001:" + secret: "введено «ppython3telegram_setup.py8000000001»",
            "8000000001:" + secret + "/": "после двоеточия есть лишние символы",
            "8000000001:" + secret + ":x": "после двоеточия есть лишние символы",
        }
        for typed, expected in cases.items():
            msg = self.run_setup(typed)
            self.assertIn("Это не похоже на токен бота", msg)
            self.assertIn(expected, msg)
            self.assertNotIn(secret, msg)  # the secret part is never printed
        self.assertEqual(self.world.telegram_calls, [])

    def test_paste_accidents_are_cleaned_up(self):
        import telegram_setup
        good = "8000000001:" + "A" * 35
        for typed in (f"  {good}\n", f'"{good}"', f"`{good}`", f"bot{good}", f"\u200b{good}\ufeff", f"<{good}>"):
            self.assertEqual(telegram_setup.normalize_token(typed), good, repr(typed))
        self.assertEqual(telegram_setup.normalize_token(""), "")

    def test_format_ok_line_hides_secret(self):
        import telegram_setup
        self.world.updates = [[{"update_id": 1, "message": {"chat": {"id": 7, "type": "private", "first_name": "A"}}}]]
        good = "8000000001:" + "Q" * 35
        with mock.patch.object(telegram_setup.getpass, "getpass", return_value=good), \
             mock.patch("builtins.input", return_value="н"):
            telegram_setup.main()
        out = sys.stdout.getvalue()
        self.assertIn("Токен по формату в порядке: бот 8000000001, секретная часть — 35 символов.", out)
        self.assertNotIn("Q" * 35, out)

    def test_several_chats(self):
        import telegram_setup
        self.world.updates = [[
            {"update_id": 1, "message": {"chat": {"id": 1, "type": "private", "username": "me"}}},
            {"update_id": 2, "my_chat_member": {"chat": {"id": -100, "type": "group", "title": "Family"}}},
        ]]
        with mock.patch.object(telegram_setup.getpass, "getpass", return_value="8000000001:" + "A" * 35):
            telegram_setup.main()
        out = sys.stdout.getvalue()
        self.assertIn("1  (private) @me", out)
        self.assertIn("-100  (group) Family", out)
        self.assertIn("ID нужного чата из списка выше", out)
        self.assertEqual(self.world.sent(), [])  # no guessing which chat to message


class TelegramBot(Base):
    def setUp(self):
        super().setUp()
        os.environ["TELEGRAM_CHAT_ID"] = "987654321, 111"
        self.world.stop_bot = True

    def message(self, update_id, text, chat=987654321, age=5):
        return {"update_id": update_id, "message": {"chat": {"id": chat}, "date": int(self.cs.time.time()) - age, "text": text}}

    def run_bot(self, *batches):
        self.world.updates = [list(b) for b in batches]
        with self.assertRaises(StopBot):
            self.cs.run_bot(list(PRO_MAX))

    def calls(self, method):
        return [payload for (_, m, payload) in self.world.telegram_calls if m == method]

    def test_iphone_is_answered_in_the_asking_chat_only(self):
        (self.tmp / "stock_state.json").write_text('{"available": [], "part_numbers": []}')
        before = (self.tmp / "stock_state.json").read_text()
        self.world.stock = {("R409", "MJY34ZA/A")}
        self.run_bot([self.message(41, "/iphone")])
        self.assertEqual(self.world.telegram_calls[0][1], "setMyCommands")
        self.assertEqual(self.calls("setMyCommands")[1]["commands"][0]["command"], "iphone")
        self.assertEqual(self.calls("sendChatAction"), [{"chat_id": "987654321", "action": "typing"}])
        [reply] = self.world.sent()
        self.assertEqual(reply["chat_id"], "987654321")  # not the other configured chat
        self.assertFalse(reply["disable_notification"])
        lines = body(reply["text"]).splitlines()
        self.assertEqual(lines[:4], ["📋 Наличие в Apple Store Hong Kong", "", "🟢 В наличии · iPhone 18 Pro Max", "🩶 2TB · Silver — 🛒 Оформить"])
        self.assertEqual(self.calls("getUpdates")[1]["offset"], 42)  # update acknowledged
        self.assertEqual((self.tmp / "stock_state.json").read_text(), before)  # watcher state untouched

    def test_strangers_old_commands_and_chatter_are_ignored(self):
        self.run_bot([
            self.message(1, "/iphone", chat=555),
            self.message(2, "/iphone", age=600),
            self.message(3, "привет"),
            {"update_id": 4, "edited_message": {"text": "/iphone"}},
        ])
        self.assertEqual(self.world.sent(), [])
        self.assertEqual(self.world.apple_requests, [])
        self.assertIn("Ignored /iphone from chat 555", sys.stdout.getvalue())

    def test_command_with_bot_name_and_help(self):
        self.run_bot([self.message(1, "/iphone@appl_17_max_bot"), self.message(2, "/start")])
        texts = [m["text"] for m in self.world.sent()]
        self.assertIn("📋 Наличие в Apple Store Hong Kong", texts[0])
        self.assertTrue(texts[1].startswith("/iphone — что сейчас есть в наличии"))

    def test_repeated_iphone_reuses_the_last_answer_for_20_seconds(self):
        clock = [1000.0]
        with mock.patch.object(self.cs.time, "monotonic", lambda: clock[0]):
            self.world.updates = [[self.message(1, "/iphone"), self.message(2, "/iphone")]]
            original = self.world.telegram

            def telegram(url, data):
                if url.endswith("/getUpdates") and not self.world.updates:
                    clock[0] += 25
                    if not hasattr(self, "_later"):
                        self._later = True
                        self.world.updates = [[self.message(3, "/iphone")]]
                return original(url, data)

            self.world.telegram = telegram
            with self.assertRaises(StopBot):
                self.cs.run_bot(list(PRO_MAX))
        self.assertEqual(len(self.world.sent()), 3)
        self.assertEqual(len(self.world.apple_requests), 2)  # 1st and 3rd; the 2nd reused the 1st

    def test_apple_failure_is_reported_to_the_asker(self):
        self.world.apple_errors = [HTTPError("u", 503, "x", {}, io.BytesIO(b""))]
        self.run_bot([self.message(1, "/iphone")])
        [reply] = self.world.sent()
        self.assertIn("⚠️ Не удалось проверить наличие: Apple returned HTTP 503", reply["text"])

    def test_telegram_errors_do_not_stop_the_bot(self):
        self.world.telegram_errors = [(None, None, None)]  # placeholder replaced below
        self.world.telegram_errors = []
        calls = {"n": 0}
        original = self.world.telegram

        def telegram(url, data):
            if url.endswith("/getUpdates"):
                calls["n"] += 1
                if calls["n"] == 1:
                    self.world.telegram_errors = [(409, "Conflict: terminated by other getUpdates request", None)]
            return original(url, data)

        self.world.telegram = telegram
        self.run_bot([self.message(1, "/iphone")])
        self.assertIn(10, self.sleeps)
        self.assertIn("Conflict", self.stderr())
        self.assertEqual(len(self.world.sent()), 1)  # answered after the error

    def write_history(self):
        (self.tmp / "stock_history.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in [
            {"time": "2026-10-03T07:00:00+08:00", "watched": ["iPhone 18 Pro Max 512GB Silver", "iPhone 18 Pro Max 1TB Burgundy"]},
            {"time": "2026-10-03T07:00:00+08:00", "stores": 6, "in_stock": {}},
            {"time": "2026-10-03T07:12:00+08:00", "stores": 6, "in_stock": {"iPhone 18 Pro Max 512GB Silver": ["Canton Road"]}},
            {"time": "2026-10-03T07:16:00+08:00", "stores": 6, "in_stock": {}},
        ]))

    BUTTONS = [
        [{"text": "512GB", "callback_data": "report:iPhone 18 Pro Max 512GB"},
         {"text": "1TB", "callback_data": "report:iPhone 18 Pro Max 1TB"}],
        [{"text": "Все", "callback_data": "report:all"}],
    ]

    def tap(self, update_id, data, chat=987654321):
        return {"update_id": update_id, "callback_query": {
            "id": f"q{update_id}", "from": {"id": chat}, "data": data,
            "message": {"message_id": 7, "chat": {"id": chat}, "date": int(self.cs.time.time()) - 3600}}}

    def test_report_offers_buttons_by_storage_and_all(self):
        self.write_history()
        self.run_bot([self.message(1, "/report")])
        [menu] = self.world.sent()
        self.assertEqual(menu["chat_id"], "987654321")
        self.assertEqual(menu["text"], "📊 Статистика наличия\n\nВыберите память — покажу, в какие часы появляется и в каких "
                                       "цветах. «Все» — общий отчёт.")
        self.assertEqual(menu["reply_markup"], {"inline_keyboard": self.BUTTONS})
        self.assertEqual(self.calls("sendDocument"), [])
        self.assertEqual(self.calls("getUpdates")[0]["allowed_updates"], ["message", "callback_query"])

    def test_all_button_sends_the_whole_report_without_a_table(self):
        self.write_history()
        self.run_bot([self.tap(1, "report:all")])
        self.assertEqual(self.calls("answerCallbackQuery"), [{"callback_query_id": "q1"}])
        self.assertEqual(self.calls("sendChatAction"), [{"chat_id": "987654321", "action": "typing"}])
        [text] = self.world.sent()
        self.assertEqual(text["chat_id"], "987654321")
        body_text = check_html(text["text"])
        self.assertTrue(body_text.startswith("📊 История наличия в Apple Store Hong Kong\nПериод: 03.10 07:00 – 03.10 07:16 (HKT), проверок: 3"))
        self.assertIn("🩶 512GB · Silver — 1 раз:\n   03.10 07:12 → 07:16 (4 мин)", body_text)
        self.assertIn("➖ Ни разу не появлялись: 1TB · Burgundy", body_text)
        self.assertEqual(text["reply_markup"], {"inline_keyboard": self.BUTTONS})  # to switch without scrolling up
        self.assertEqual(self.calls("sendDocument"), [])
        self.assertIn("Sent the report (report:all) to chat 987654321", sys.stdout.getvalue())

    def test_storage_button_shows_its_hours_and_colours(self):
        self.write_history()
        self.run_bot([self.tap(1, "report:iPhone 18 Pro Max 512GB"), self.tap(2, "report:iPhone 18 Pro Max 1TB")])
        first, second = (check_html(m["text"]) for m in self.world.sent())
        self.assertEqual(first, "\n".join([
            "📊 iPhone 18 Pro Max 512GB",
            "Период: 03.10 07:00 – 03.10 07:16 (HKT), проверок: 3",
            "",
            "🕐 В какие часы появляется (HKT, число появлений):",
            "07:00–08:00  ██████████ 1  🩶1",
            "",
            "Появления (появилось → закончилось, сколько держалось):",
            "🩶 512GB · Silver — 1 раз:",
            "   03.10 07:12 → 07:16 (4 мин)",
            "",
            "🏬 Где появлялось (число появлений): Canton Road 1",
        ]))
        self.assertEqual(second, "📊 iPhone 18 Pro Max 1TB\nПериод: 03.10 07:00 – 03.10 07:16 (HKT), проверок: 3\n\n"
                                 "➖ Ни разу не появлялись: 1TB · Burgundy")
        self.assertEqual(len(self.calls("answerCallbackQuery")), 2)

    def test_strange_buttons(self):
        self.write_history()
        self.run_bot([self.tap(1, "report:all", chat=555), self.tap(2, "something else"),
                      self.tap(3, "report:iPhone 99 1TB")])
        self.assertEqual(len(self.calls("answerCallbackQuery")), 3)  # every spinner stops
        [reply] = self.world.sent()  # a button from an old watch list: the whole report
        self.assertEqual(reply["chat_id"], "987654321")
        self.assertTrue(check_html(reply["text"]).startswith("📊 История наличия в Apple Store Hong Kong"))
        self.assertIn("Ignored a report button from chat 555", sys.stdout.getvalue())

    def test_an_old_button_still_works(self):
        self.write_history()
        original = self.world.telegram

        def telegram(url, data):  # Telegram refuses to stop the spinner of an old tap
            if url.endswith("/answerCallbackQuery"):
                self.world.telegram_errors = [(400, "Bad Request: query is too old and response timeout expired", None)]
            return original(url, data)

        self.world.telegram = telegram
        self.run_bot([self.tap(1, "report:all")])
        self.assertEqual(len(self.world.sent()), 1)
        self.assertIn("could not answer a button", self.stderr())

    def test_buttons_for_several_models_fit_telegram(self):
        groups = [("iPhone 18 Pro", "256GB"), ("iPhone 18 Pro", "1TB"), ("iPhone 18 Pro Max", "2TB")]
        rows = self.cs.report_buttons(groups)
        self.assertEqual([[b["text"] for b in row] for row in rows], [["18 Pro 256GB", "18 Pro 1TB"], ["18 Pro Max 2TB"], ["Все"]])
        long_key = self.cs.report_key(("iPhone " + "Ultra Wide " * 10, "2TB"))
        self.assertLessEqual(len(long_key.encode("utf-8")), 64)

    def test_report_with_an_empty_history(self):
        self.run_bot([self.message(1, "/report")])
        [reply] = self.world.sent()
        self.assertEqual(reply["text"], "Журнал наличия пока пуст: он заполняется с каждой проверкой.")
        self.assertEqual(self.calls("sendDocument"), [])

    def test_menu_and_help_list_both_commands(self):
        self.run_bot([self.message(1, "/help")])
        commands = self.calls("setMyCommands")
        self.assertEqual([c["scope"]["type"] for c in commands], ["default", "all_private_chats"])
        for call in commands:
            self.assertEqual([(c["command"], c["description"]) for c in call["commands"]],
                             [("iphone", "Что сейчас в наличии"), ("report", "Статистика наличия")])
        self.assertEqual(self.calls("setChatMenuButton"), [{"menu_button": {"type": "commands"}}])
        help_text = self.world.sent()[0]["text"]
        self.assertIn("/iphone", help_text)
        self.assertIn("/report", help_text)

    def test_strangers_cannot_get_the_report(self):
        self.write_history()
        self.run_bot([self.message(1, "/report", chat=555)])
        self.assertEqual(self.world.sent(), [])

    def test_bot_flag(self):
        with mock.patch.object(self.cs, "run_bot", return_value=0) as run, \
             mock.patch.object(sys, "argv", ["check_stock.py", "--bot"]):
            self.assertEqual(self.cs.main(), 0)
        run.assert_called_once()


class History(Base):
    def history(self):
        f = self.tmp / "stock_history.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def test_every_check_is_recorded(self):
        self.world.apple_date = "Sat, 03 Oct 2026 00:07:00 GMT"
        self.world.stock = {("R409", "MJY34ZA/A"), ("R499", "MJY34ZA/A"), ("R428", "MJXU4ZA/A")}
        self.run_once()
        self.run_once()
        header, first, second = self.history()
        self.assertEqual(header["watched"][:2], ["iPhone 18 Pro Max 512GB Silver", "iPhone 18 Pro Max 512GB Black"])
        self.assertEqual(len(header["watched"]), 12)
        self.assertEqual(first["time"], "2026-10-03T08:07:00+08:00")  # Apple's clock, Hong Kong time
        self.assertEqual(first["stores"], 6)
        self.assertEqual(first["in_stock"], {
            "iPhone 18 Pro Max 512GB Silver": ["ifc mall"],
            "iPhone 18 Pro Max 2TB Silver": ["Canton Road", "Causeway Bay"],
        })
        self.assertEqual(second["in_stock"], first["in_stock"])  # unchanged checks are recorded too

    def test_errors_and_second_looks_are_recorded(self):
        w = self.world
        w.stock = {("R409", "MJXU4ZA/A")}
        self.run_once()
        w.stock_sequence = [set(), {("R409", "MJXU4ZA/A")}]  # flicker, then back on the second look
        self.run_once()
        w.apple_errors = [HTTPError("u", 503, "x", {}, io.BytesIO(b""))]
        self.run_once()
        lines = self.history()[1:]
        self.assertEqual([bool(l.get("in_stock")) for l in lines[:3]], [True, False, True])
        self.assertIn("HTTP 503", lines[3]["error"])

    def test_dry_run_records_nothing(self):
        self.world.stock = {("R409", "MJXU4ZA/A")}
        self.cs.run_checks(list(PRO_MAX), 1, 60, dry_run=True)
        self.assertEqual(self.history(), [])


class Report(Base):
    def setUp(self):
        super().setUp()
        import stock_report
        self.sr = stock_report

    def write(self, *entries):
        path = self.tmp / "h.jsonl"
        path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
        return path

    @staticmethod
    def at(hhmm, day=3):
        return f"2026-10-{day:02d}T{hhmm}:00+08:00"

    def sample(self):
        S, B, G = "iPhone 18 Pro Max 512GB Silver", "iPhone 18 Pro Max 2TB Black", "iPhone 18 Pro Max 1TB Glacier"
        watched = [S, "iPhone 18 Pro Max 512GB Black", G, B]
        return self.write(
            {"time": self.at("06:00"), "watched": watched},
            {"time": self.at("06:00"), "stores": 6, "in_stock": {B: ["Canton Road"]}},
            {"time": self.at("07:05"), "stores": 6, "in_stock": {B: ["Canton Road"], S: ["ifc mall"]}},
            {"time": self.at("07:07"), "stores": 6, "in_stock": {B: ["Canton Road"]}},          # S flickers away 2 min
            {"time": self.at("07:09"), "stores": 6, "in_stock": {B: ["Canton Road"], S: ["Causeway Bay"]}},
            {"time": self.at("07:20"), "stores": 6, "in_stock": {B: ["Canton Road"]}},          # S sold out at 07:20
            {"time": self.at("07:40"), "error": "Apple returned HTTP 503"},
            {"time": self.at("09:30"), "stores": 6, "in_stock": {B: ["Canton Road"], S: ["ifc mall"]}},
            {"time": self.at("10:00"), "stores": 6, "in_stock": {B: ["Canton Road"], S: ["ifc mall"]}},
        )

    def test_intervals_merge_flicker_and_keep_real_sell_outs(self):
        watched, checks = self.sr.load([self.sample()])
        runs = self.sr.intervals(checks, "iPhone 18 Pro Max 512GB Silver")
        self.assertEqual(len(runs), 2)
        (a1, s1, st1), (a2, s2, _) = runs
        self.assertEqual((a1.strftime("%H:%M"), s1.strftime("%H:%M")), ("07:05", "07:20"))
        self.assertEqual(st1, {"ifc mall", "Causeway Bay"})
        self.assertEqual((a2.strftime("%H:%M"), s2), ("09:30", None))
        self.assertEqual(len(self.sr.intervals(checks, "iPhone 18 Pro Max 2TB Black")), 1)

    def test_checks_with_the_same_stock_share_memory_and_all_still_count(self):
        # Six computers add about 5,800 checks a day, nearly all with the stock of the one
        # before: those share one dict, so /report does not need memory for every check.
        silver, black = "iPhone 18 Pro Max 512GB Silver", "iPhone 18 Pro Max 2TB Black"
        path = self.write(
            {"time": self.at("06:00"), "watched": [silver, black]},
            *({"time": f"2026-10-03T06:{m:02d}:{s:02d}+08:00", "source": "vps", "stores": 6,
               "in_stock": {black: ["Canton Road"]}} for m in range(1, 5) for s in (0, 15, 30, 45)),
            {"time": self.at("06:05"), "stores": 6, "in_stock": {black: ["Canton Road"], silver: ["ifc mall"]}},
            {"time": self.at("06:06"), "stores": 6, "in_stock": {black: ["Canton Road"]}},
        )
        watched, checks = self.sr.load([path])
        self.assertEqual(len(checks), 18)
        self.assertEqual(len({id(in_stock) for _, in_stock in checks}), 2)
        self.assertEqual(checks[-1][1], {black: ["Canton Road"]})
        self.assertEqual(watched, [silver, black])
        text = self.sr.report(watched, checks)
        self.assertIn("проверок: 18", text)
        self.assertIn("🩶 512GB · Silver — 1 раз: 03.10 06:05 → 06:06 (1 мин)", text)

    def test_legacy_empty_store_lists_do_not_create_or_extend_stock(self):
        silver = "iPhone 18 Pro Max 512GB Silver"
        black = "iPhone 18 Pro Max 512GB Black"
        path = self.write(
            {"time": self.at("06:00"), "watched": [silver, black]},
            {"time": self.at("06:00"), "stores": 6, "in_stock": {silver: [], black: []}},
            {"time": self.at("07:05"), "stores": 6, "in_stock": {silver: ["ifc mall"], black: []}},
            {"time": self.at("07:20"), "stores": 6, "in_stock": {silver: [], black: []}},
            {"time": self.at("07:40"), "stores": 6, "in_stock": {silver: [], black: []}},
        )
        watched, checks = self.sr.load([path])
        self.assertEqual(len(checks), 4)  # Keep the successful checks, including empty ones.
        text = self.sr.report(watched, checks)
        self.assertIn("🩶 512GB · Silver — 1 раз: 03.10 07:05 → 07:20 (15 мин)", text)
        self.assertIn("➖ Ни разу не появлялись: 512GB · Black", text)
        self.assertNotIn("сейчас в наличии", text)
        self.assertNotIn("06:00–07:00", text)
        rows = list(csv.DictReader(io.StringIO(self.sr.csv_text(watched, checks)), delimiter=";"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Появилось (HKT)"], "2026-10-03 07:05")
        self.assertEqual(rows[0]["Закончилось (HKT)"], "2026-10-03 07:20")
        self.assertEqual(rows[0]["Минут"], "15")

    def test_report_text(self):
        watched, checks = self.sr.load([self.sample()])
        text = self.sr.report(watched, checks)
        self.assertIn("Период: 03.10 06:00 – 03.10 10:00 (HKT), проверок: 7", text)
        self.assertIn("🩶 512GB · Silver — 2 раза: 03.10 07:05 → 07:20 (15 мин); 03.10 09:30 → сейчас в наличии", text)
        self.assertIn("🖤 2TB · Black — 1 раз: с начала наблюдений → сейчас в наличии", text)
        self.assertIn("➖ Ни разу не появлялись: 512GB · Black, 1TB · Glacier", text)
        self.assertIn("07:00–08:00  ██████████ 1", text)
        self.assertIn("09:00–10:00  ██████████ 1", text)
        self.assertNotIn("06:00–07:00", text)  # already in stock when watching began: not an appearance
        self.assertIn("🏬 Где появлялось (число появлений): ifc mall 2, ", text)
        self.assertIn("Causeway Bay 1", text)
        self.assertNotIn("\n\n\n", text)  # no empty storage groups
        self.assertEqual(self.sr.times(1) + self.sr.times(3) + self.sr.times(5) + self.sr.times(11) + self.sr.times(22), "1 раз3 раза5 раз11 раз22 раза")

    def test_compact_report_for_a_phone(self):
        watched, checks = self.sr.load([self.sample()])
        text = self.sr.report(watched, checks, show_last=1, one_per_line=True)
        self.assertIn("🩶 512GB · Silver — 2 раза, последние:\n   03.10 09:30 → сейчас в наличии", text)
        self.assertIn("🖤 2TB · Black — 1 раз:\n   с начала наблюдений → сейчас в наличии", text)

    def test_csv_export(self):
        watched, checks = self.sr.load([self.sample()])
        out = self.tmp / "out.csv"
        self.sr.write_csv(out, watched, checks)
        raw = out.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # BOM for Excel
        rows = [line.split(";") for line in raw.decode("utf-8-sig").splitlines()]
        self.assertEqual(rows[0][:4], ["Модель", "Память", "Цвет", "Появилось (HKT)"])
        self.assertIn(["iPhone 18 Pro Max", "512GB", "Silver", "2026-10-03 07:05", "2026-10-03 07:20", "15", "Causeway Bay, ifc mall", ""], rows)
        self.assertIn(["iPhone 18 Pro Max", "2TB", "Black", "2026-10-03 06:00", "", "", "Canton Road", "в наличии с начала наблюдений"], rows)

    def test_journal_import(self):
        log = [
            "Started iphone-stock-watch-hk.service",
            "[2026-10-02 15:29:58 HKT] 6 stores × 12 models checked — iPhone 18 Pro Max 2TB Silver: apm Hong Kong, Canton Road; iPhone 18 Pro Max 2TB Burgundy: ifc mall",
            "[2026-10-02 15:29:58 HKT] Still in stock: 4 previously alerted configuration(s); no duplicate alert.",
            "[2026-10-02 15:35:02 HKT] Check 2/5: 6 stores × 12 models checked — no pickup stock",
            "[2026-10-03 18:00:00 HKT] VPS: 6 stores × 12 models checked — no pickup stock",
            "[2026-10-03 18:00:31 HKT] GitHub: 6 stores × 12 models checked — iPhone 18 Pro Max 2TB Silver: ifc mall",
        ]
        entries = list(self.sr.import_journal(log))
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[3]["in_stock"], {"iPhone 18 Pro Max 2TB Silver": ["ifc mall"]})
        self.assertEqual(entries[0]["time"], "2026-10-02T15:29:58+08:00")
        self.assertEqual(entries[0]["in_stock"], {
            "iPhone 18 Pro Max 2TB Silver": ["apm Hong Kong", "Canton Road"],
            "iPhone 18 Pro Max 2TB Burgundy": ["ifc mall"],
        })
        self.assertEqual(entries[1]["in_stock"], {})

    def test_journal_import_recognizes_current_and_legacy_server_names(self):
        names = ("VPS 1", "VPS 2", "VPS 3", "VPS 4", "VPS 5", "GitHub", "VPS", "Запасной VPS", "Дополнительный VPS")
        lines = [
            f"[2026-10-03 18:00:{i:02d} HKT] {name}: 6 stores × 12 models checked — iPhone 18 Pro Max 2TB Silver: ifc mall"
            for i, name in enumerate(names)
        ]
        entries = list(self.sr.import_journal(lines))
        self.assertEqual(len(entries), len(names))
        self.assertTrue(all(e["in_stock"] == {"iPhone 18 Pro Max 2TB Silver": ["ifc mall"]} for e in entries))

    def test_groups_in_watch_order(self):
        watched, _ = self.sr.load([self.sample()])
        self.assertEqual(self.sr.groups(watched), [("iPhone 18 Pro Max", "512GB"), ("iPhone 18 Pro Max", "1TB"),
                                                   ("iPhone 18 Pro Max", "2TB")])

    def test_one_storage_shows_the_hours_first_with_the_colours(self):
        watched, checks = self.sr.load([self.sample()])
        self.assertEqual(self.sr.group_report(watched, checks, ("iPhone 18 Pro Max", "512GB")), "\n".join([
            "📊 iPhone 18 Pro Max 512GB",
            "Период: 03.10 06:00 – 03.10 10:00 (HKT), проверок: 7",
            "",
            "🕐 В какие часы появляется (HKT, число появлений):",
            "07:00–08:00  ██████████ 1  🩶1",
            "09:00–10:00  ██████████ 1  🩶1",
            "",
            "Появления (появилось → закончилось, сколько держалось):",
            "🩶 512GB · Silver — 2 раза:",
            "   03.10 07:05 → 07:20 (15 мин)",
            "   03.10 09:30 → сейчас в наличии",
            "",
            "➖ Ни разу не появлялись: 512GB · Black",
            "",
            "🏬 Где появлялось (число появлений): ifc mall 2, Causeway Bay 1",
        ]))
        text = self.sr.group_report(watched, checks, ("iPhone 18 Pro Max", "2TB"))  # in stock since the start
        self.assertNotIn("🕐", text)
        self.assertIn("🖤 2TB · Black — 1 раз:\n   с начала наблюдений → сейчас в наличии", text)

    def test_colours_of_each_hour(self):
        S, B, G = "iPhone 18 Pro Max 1TB Silver", "iPhone 18 Pro Max 1TB Black", "iPhone 18 Pro Max 1TB Glacier"
        path = self.write(
            {"time": self.at("06:00"), "watched": [S, B, G]},
            {"time": self.at("06:00"), "stores": 6, "in_stock": {}},
            {"time": self.at("07:01"), "stores": 6, "in_stock": {S: ["ifc mall"], B: ["ifc mall"]}},
            {"time": self.at("07:30"), "stores": 6, "in_stock": {}},
            {"time": self.at("07:40"), "stores": 6, "in_stock": {B: ["Canton Road"]}},
            {"time": self.at("08:10"), "stores": 6, "in_stock": {G: ["apm Hong Kong"]}},
            {"time": self.at("08:20"), "stores": 6, "in_stock": {}},
        )
        watched, checks = self.sr.load([path])
        text = self.sr.group_report(watched, checks, ("iPhone 18 Pro Max", "1TB"))
        self.assertIn("07:00–08:00  ██████████ 3  🩶1 🖤2\n08:00–09:00  ███ 1  🩵1\n", text)
        self.assertNotIn("➖", text)
        self.assertNotIn("🩶 1TB · Silver — 1 раз", self.sr.group_report(watched, checks, ("iPhone 18 Pro Max", "2TB")))

    def test_history_written_by_checks_feeds_the_report(self):
        self.world.stock = {("R409", "MJY34ZA/A")}
        self.run_once()
        self.world.stock = set()
        self.run_once()
        watched, checks = self.sr.load([self.tmp / "stock_history.jsonl"])
        self.assertEqual(len(watched), 12)
        text = self.sr.report(watched, checks)
        self.assertIn("2TB · Silver — 1 раз: с начала наблюдений →", text)
        self.assertIn("Ни разу не появлялись: 512GB · Silver", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
