"""Tests for the shared watch: the server checks at :00, GitHub at :30 and hands its checks in.

Apple, Telegram, ssh and the clock are fakes; nothing leaves the machine.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from test_telegram import CATALOG, PRO_MAX, PROJECT, STORES, Base, Clock, check_html

HKT = timezone(timedelta(hours=8))
U, B = "MJXU4ZA/A", "MJY24ZA/A"  # 512GB Silver, 2TB Black


class Stop(BaseException):
    """Ends an endless loop in a test."""


def hkt(text):
    """Unix time of "10:02:30" on 3 October 2026, Hong Kong time."""
    h, m, s = (int(x) for x in text.split(":"))
    return datetime(2026, 10, 3, h, m, s, tzinfo=HKT).timestamp()


def utc(text):
    return datetime.fromtimestamp(hkt(text), timezone.utc)


class FakeClockBase(Base):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.world.clock = self.clock
        extra = [
            mock.patch.object(self.cs.time, "time", self.clock.time),
            mock.patch.object(self.cs.time, "sleep", self.clock.sleep),
            mock.patch.object(self.cs, "utc_now", lambda: datetime.fromtimestamp(self.clock.now, timezone.utc)),
        ]
        for p in extra:
            p.start()
        self.patches += extra

    def ingest(self, inbox, source, stream):
        code = self.cs.ingest(inbox, source, stream)
        for f in Path(inbox).glob("*.json"):
            os.utime(f, (self.clock.now, self.clock.now))
        return code

    def result(self, stock, when, parts=PRO_MAX):
        products = {p: self.cs.clean(CATALOG[p]) for p in parts}
        available = [
            {"storeNumber": n, "storeName": name, "city": city, "partNumber": p, "product": products[p], "pickup": "Available Today"}
            for n, name, city in STORES for p in parts if (n, p) in stock
        ]
        return self.cs.CheckResult(6, products, available, [], when)

    def texts(self):
        return [check_html(m["text"]) for m in self.world.sent()]

    def history(self):
        f = self.tmp / "stock_history.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


class SharedBase(FakeClockBase):
    """run_slots on the fake clock: the server checks the fake Apple at :00 of every minute;
    GitHub's check of every :30 arrives in the inbox through ingest() a second later."""

    def setUp(self):
        super().setUp()
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir()
        self.vps = lambda moment: set()     # the server's stock at a moment
        self.github = lambda moment: set()  # GitHub's at a :30 (None: sends nothing; an exception: an error)
        self.world.stock_fn = lambda: self.vps(self.clock.now)
        self.next_github = self.cs.next_slot(self.clock.now, 60, 30)
        self.clock.hooks.append(self.hand_in)

    def hand_in(self, now):
        while now >= self.next_github + 1:
            moment, self.next_github = self.next_github, self.next_github + 60
            stock = self.github(moment)
            if stock is None:
                continue
            when = datetime.fromtimestamp(moment, timezone.utc)
            if isinstance(stock, Exception):
                data = self.cs.check_to_json("github", error=stock, moment=when)
            else:
                data = self.cs.check_to_json("github", self.result(stock, when))
            self.put(data)

    def put(self, data):
        self.assertEqual(self.ingest(self.inbox, "github", io.BytesIO(json.dumps(data).encode() + b"\n")), 0)

    def run_until(self, until, status_minutes=0):
        def stop(now):
            if now >= until:
                raise Stop()

        self.clock.hooks.append(stop)
        with self.assertRaises(Stop):
            self.cs.run_slots(list(PRO_MAX), 60, 0, False, status_minutes, self.inbox, "vps")


class Shared(SharedBase):
    def test_checks_alternate_and_each_change_is_reported_once(self):
        def stock(moment):  # 512GB Silver at Canton Road from 10:02:30 until 10:05
            return {("R499", U)} if hkt("10:02:30") <= moment < hkt("10:05:00") else set()

        self.vps = self.github = stock
        self.run_until(hkt("10:07:10"))
        # the server asks Apple at :00 only, once a minute
        self.assertEqual(self.world.request_times, [hkt(f"10:0{m}:00") for m in range(1, 8)])
        texts = self.texts()
        self.assertEqual(len(texts), 2)
        self.assertIn("🟢 Появились · iPhone 18 Pro Max\n🩶 512GB · Silver — 🛒 Оформить", texts[0])
        self.assertIn("🕐 Проверено: 03.10.2026 10:02 (HKT)", texts[0])  # GitHub's 10:02:30 check saw it first
        self.assertIn("🔴 Закончились · iPhone 18 Pro Max\n🩶 512GB · Silver", texts[1])
        self.assertIn("🕐 Проверено: 03.10.2026 10:05 (HKT)", texts[1])  # missing at 10:05:00 and 10:05:30
        checks = [line for line in self.history() if "in_stock" in line]
        self.assertEqual([c["source"] for c in checks], ["github", "vps"] * 7)
        self.assertEqual(checks[0]["time"], "2026-10-03T10:00:30+08:00")
        self.assertEqual(checks[1]["time"], "2026-10-03T10:01:00+08:00")
        self.assertEqual(checks[4]["in_stock"], {"iPhone 18 Pro Max 512GB Silver": ["Canton Road"]})
        self.assertEqual(list(self.inbox.glob("*.json")), [])
        self.assertIn("GitHub: 6 stores × 12 models checked — iPhone 18 Pro Max 512GB Silver: Canton Road", sys.stdout.getvalue())
        self.assertEqual(self.state()["sources"], ["github", "vps"])

    def test_a_miss_seen_by_one_computer_only_is_flicker(self):
        self.vps = lambda moment: {("R499", U)}
        self.github = lambda moment: set() if hkt("10:02:00") < moment < hkt("10:03:00") else {("R499", U)}
        self.run_until(hkt("10:05:10"))
        texts = self.texts()
        self.assertEqual(len(texts), 1)  # the appearance only
        self.assertIn("🟢 Появились", texts[0])
        self.assertIn("1 configuration(s) were back at the next check (Apple flicker)", sys.stdout.getvalue())

    def test_failed_sell_out_alert_is_sent_with_the_next_check(self):
        self.vps = self.github = lambda moment: {("R499", U)} if moment < hkt("10:02:00") else set()
        self.run_until(hkt("10:01:10"))  # in stock: reported
        self.world.telegram_errors = [(502, "Bad Gateway", None)]
        self.clock.hooks.pop()
        self.run_until(hkt("10:04:10"))
        texts = self.texts()  # the fake Telegram also lists the attempt that failed
        self.assertEqual(len(texts), 3)
        self.assertIn("🔴 Закончились", texts[1])
        self.assertIn("🕐 Проверено: 03.10.2026 10:02 (HKT)", texts[1])  # GitHub's 10:02:30: not delivered
        self.assertIn("🔴 Закончились", texts[2])
        self.assertIn("🕐 Проверено: 03.10.2026 10:03 (HKT)", texts[2])  # so the server's 10:03:00 sends it

    def test_status_every_10_minutes_counts_the_checks_of_both(self):
        self.vps = self.github = lambda moment: {("R409", B)}
        self.run_until(hkt("10:20:10"), status_minutes=10)
        sent = self.world.sent()
        self.assertEqual(len(sent), 3)  # the appearance, then statuses at 10:10 and 10:20
        statuses = sent[1:]
        for msg in statuses:
            self.assertTrue(msg["disable_notification"])
            self.assertIn("🟢 В наличии · iPhone 18 Pro Max\n🖤 2TB · Black — 🛒 Оформить", check_html(msg["text"]))
        self.assertTrue(statuses[0]["text"].endswith("🕐 Проверено: 03.10.2026 10:10 (HKT)\n🔁 Проверок за 10 мин: 19 — VPS 9, GitHub 10"))
        self.assertTrue(statuses[1]["text"].endswith("🕐 Проверено: 03.10.2026 10:20 (HKT)\n🔁 Проверок за 10 мин: 20 — VPS 10, GitHub 10"))

    def test_github_silence_is_reported_once_and_so_is_its_return(self):
        self.github = lambda moment: None if hkt("10:05:00") < moment < hkt("10:50:00") else set()
        self.run_until(hkt("10:52:10"))
        self.assertEqual(self.texts(), [
            "⚠️ GitHub: проверки не приходят уже 30 мин. Наличие продолжает проверять VPS.",
            "✅ GitHub: проверки снова работают.",
        ])
        self.assertNotIn("down", self.state())

    def test_silence_alert_survives_a_restart_without_repeating(self):
        self.github = lambda moment: None if moment > hkt("10:01:00") else set()
        self.run_until(hkt("10:32:10"))
        self.assertEqual(len(self.texts()), 1)
        self.assertEqual(self.state()["down"], ["github"])
        self.clock.hooks.pop()
        self.run_until(hkt("11:20:10"))  # a new watcher process: no second warning
        self.assertEqual(len(self.texts()), 1)
        self.github = lambda moment: set()
        self.clock.hooks.pop()
        self.run_until(hkt("11:22:10"))
        self.assertEqual(self.texts()[-1], "✅ GitHub: проверки снова работают.")

    def test_refusals_pause_the_server_while_github_goes_on(self):
        self.world.apple_errors = [HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b"")) for _ in range(10)]
        problems = []
        self.clock.hooks.append(lambda now: problems.append("problem" in self.state()) if now == hkt("10:30:05") else None)
        self.run_until(hkt("10:46:10"))
        self.assertEqual(problems, [False])  # right after the server's failed 10:30 check: GitHub works, so no outage
        self.assertEqual(self.world.request_times,
                         [hkt(t) for t in ("10:01:00", "10:03:00", "10:07:00", "10:15:00", "10:30:00", "10:45:00")])
        texts = self.texts()
        self.assertEqual(len(texts), 1)  # GitHub kept watching, so no "nothing works" alert
        self.assertTrue(texts[0].startswith("⚠️ VPS: проверки не работают уже 30 мин. Наличие продолжает проверять GitHub."), texts[0])
        self.assertIn("Последняя ошибка: Apple returned HTTP 429", texts[0])
        self.assertIn("no checks from here for 2 min", self.stderr())
        self.assertIn("no checks from here for 15 min", self.stderr())

    def test_when_both_fail_there_is_one_overall_alert(self):
        self.world.apple_errors = [HTTPError("u", 503, "x", {}, io.BytesIO(b""))] * 60
        self.github = lambda moment: RuntimeError("Could not reach Apple: timed out")
        self.run_until(hkt("10:40:10"))
        texts = self.texts()
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("⚠️ Проверка наличия в Apple Store Hong Kong не работает уже 30 мин"), texts[0])
        errors = [line for line in self.history() if "error" in line]
        self.assertEqual({line["source"] for line in errors}, {"vps", "github"})

    def test_late_foreign_and_broken_checks_only_go_to_the_history(self):
        self.github = lambda moment: None

        def drop(now):
            if now == hkt("10:01:05"):
                self.put(self.cs.check_to_json("github", self.result({("R499", U)}, utc("10:00:20"))))  # late
                self.put(self.cs.check_to_json("github", self.result({("R499", U)}, utc("10:01:02"), parts=[U, B])))
                self.put(self.cs.check_to_json("github", self.result({("R499", U)}, utc("10:30:00"))))  # future
                (self.inbox / "9-broken.json").write_text("{not json")

        self.clock.hooks.append(drop)
        self.run_until(hkt("10:01:10"))
        self.assertEqual(self.world.sent(), [])
        checks = [line for line in self.history() if "in_stock" in line]
        self.assertEqual([(c["time"][11:19], c["source"]) for c in checks],
                         [("10:01:00", "vps"), ("10:00:20", "github"), ("10:01:02", "github")])
        self.assertEqual(list(self.inbox.glob("*.json")), [])
        self.assertIn("GitHub: older than the last check; it went into the history only.", sys.stdout.getvalue())
        self.assertIn("ERROR: GitHub checks other part numbers; its check went into the history only.", self.stderr())
        self.assertIn("its time is in the future", self.stderr())
        self.assertIn("ignored 9-broken.json from the inbox", self.stderr())


class Ingest(FakeClockBase):
    def setUp(self):
        super().setUp()
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir()

    def line(self, data):
        return json.dumps(data).encode() + b"\n"

    def check(self, source="vps"):
        return self.cs.check_to_json(source, self.result({("R409", U)}, utc("10:00:30")))

    def test_a_check_is_saved_for_the_watcher_as_from_github(self):
        self.assertEqual(self.ingest(self.inbox, "github", io.BytesIO(self.line(self.check("vps")))), 0)
        [saved] = list(self.inbox.iterdir())
        self.assertTrue(saved.name.endswith(".json") and not saved.name.startswith("."))
        self.assertEqual(saved.stat().st_mode & 0o777, 0o640)
        data = json.loads(saved.read_text())
        self.assertEqual(data["source"], "github")  # whatever the sender claims
        source, moment, result, error = self.cs.check_from_json(data)
        self.assertEqual((source, moment, error), ("github", utc("10:00:30"), None))
        self.assertEqual([i["storeName"] for i in result.available], ["Causeway Bay"])

    def test_several_checks_in_one_session(self):
        lines = self.line(self.check()) + b"\n" + self.line(self.check())
        self.assertEqual(self.ingest(self.inbox, "github", io.BytesIO(lines)), 0)
        self.assertEqual(len(list(self.inbox.glob("*.json"))), 2)

    def test_bad_checks_are_refused(self):
        good = self.check()
        bad = [
            b"not json\n",
            b"[1, 2]\n",
            self.line({**good, "time": "2026-10-03 10:00:30"}),  # no time zone
            self.line({**good, "stores": "6"}),
            self.line({**good, "products": ["x"]}),
            self.line({**good, "available": [{"partNumber": "NOPE1ZA/A"}]}),
            self.line({**good, "products": {**good["products"], U: "x" * 500}}),
            b'{"source": "github", "time": "2026-10-03T10:00:30+08:00", "error": "' + b"x" * 70000 + b'"}\n',
        ]
        for line in bad:
            with self.subTest(line=line[:40]):
                self.assertEqual(self.ingest(self.inbox, "github", io.BytesIO(line)), 1)
        self.assertEqual(list(self.inbox.iterdir()), [])
        self.assertEqual(self.stderr().count("ERROR: check not taken:"), len(bad))

    def test_full_or_unread_inbox_refuses_so_github_notices(self):
        with mock.patch.object(self.cs, "INBOX_LIMIT", 2):
            for expected in (0, 0, 1):
                self.assertEqual(self.ingest(self.inbox, "github", io.BytesIO(self.line(self.check()))), expected)
        self.assertIn("2 checks are waiting on the server; is the watcher running?", self.stderr())
        for f in self.inbox.iterdir():
            f.unlink()
        self.assertEqual(self.ingest(self.inbox, "github", io.BytesIO(self.line(self.check()))), 0)
        [old] = list(self.inbox.iterdir())
        os.utime(old, (self.clock.now - 600, self.clock.now - 600))
        self.assertEqual(self.ingest(self.inbox, "github", io.BytesIO(self.line(self.check()))), 1)
        self.assertIn("the watcher on the server has not read checks for 10 min; is it running?", self.stderr())

    def test_text_is_cleaned_and_errors_fit_one_line(self):
        result = self.result({("R409", U)}, utc("10:00:30"))
        result.products[U] = "iPhone 18 Pro Max‎ 512GB Silver"
        result.available[0]["storeName"] = "Causeway\nBay"
        _, _, back, _ = self.cs.check_from_json(json.loads(json.dumps(self.cs.check_to_json("github", result))))
        self.assertEqual(back.products[U], "iPhone 18 Pro Max 512GB Silver")
        self.assertEqual(back.available[0]["storeName"], "Causeway Bay")
        data = self.cs.check_to_json("github", error=RuntimeError("first\nsecond"), moment=utc("10:00:30"))
        self.assertEqual(self.cs.check_from_json(data)[1:], (utc("10:00:30"), None, "first second"))
        for broken in ({**data, "source": "GitHub!"}, {**data, "time": "yesterday"}, "text", {**self.check(), "stores": True}):
            with self.subTest(broken=str(broken)[:40]), self.assertRaises(ValueError):
                self.cs.check_from_json(broken)


class ProbeBase(FakeClockBase):
    def setUp(self):
        super().setUp()
        self.delivered = []
        self.outcomes = []  # (returncode, stderr) per delivery; default success

    def fake_run(self, args, input=None, capture_output=None, timeout=None):
        self.delivered.append((args, json.loads(input), self.clock.now))
        code, err = self.outcomes.pop(0) if self.outcomes else (0, b"")
        return subprocess.CompletedProcess(args, code, b"", err)

    def probe(self, minutes):
        with mock.patch.object(self.cs.subprocess, "run", self.fake_run):
            return self.cs.run_probe(list(PRO_MAX), "feed", 60, 30, minutes)


class Probe(ProbeBase):
    def test_checks_at_30_seconds_past_and_hands_every_check_in(self):
        self.world.stock = {("R428", B)}
        self.assertEqual(self.probe(minutes=3), 0)
        self.assertEqual(self.world.request_times, [hkt("10:00:30"), hkt("10:01:30"), hkt("10:02:30")])
        self.assertEqual([d[2] for d in self.delivered], self.world.request_times)
        args, data, _ = self.delivered[0]
        self.assertEqual(args, ["ssh", "-T", "feed"])
        source, moment, result, error = self.cs.check_from_json(data)
        self.assertEqual((source, moment, error), ("github", utc("10:00:30"), None))
        self.assertEqual([(i["storeName"], i["partNumber"]) for i in result.available], [("ifc mall", B)])
        self.assertEqual(self.world.sent(), [])  # the server decides what to send

    def test_server_out_of_reach_is_reported_once_then_its_return(self):
        os.environ.update(GITHUB_SERVER_URL="https://github.com", GITHUB_REPOSITORY="me/watch", GITHUB_RUN_ID="7")
        self.outcomes = [(255, b"ssh: connect to host 203.0.113.9 port 22: Connection timed out\n")] * 35
        self.probe(minutes=40)
        self.assertEqual(len(self.delivered), 40)
        sent = self.world.sent()
        self.assertEqual(len(sent), 2)
        warning = check_html(sent[0]["text"])
        self.assertTrue(warning.startswith(
            "⚠️ GitHub не может передать проверки на сервер уже 30 мин. Если сервер не работает, уведомления о наличии сейчас не придут."), warning)
        self.assertIn("Connection timed out", warning)
        self.assertIn('<a href="https://github.com/me/watch/actions/runs/7">Лог запуска</a>', sent[0]["text"])
        self.assertEqual(check_html(sent[1]["text"]), "✅ GitHub снова передаёт проверки на сервер.")

    def test_apple_refusal_pauses_the_probe_and_the_error_is_handed_in(self):
        self.world.apple_errors = [HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b""))]
        self.probe(minutes=4)
        self.assertEqual(self.world.request_times, [hkt("10:00:30"), hkt("10:02:30"), hkt("10:03:30")])
        self.assertTrue(self.delivered[0][1]["error"].startswith("Apple returned HTTP 403"))
        self.assertIn("stores", self.delivered[1][1])

    def test_apples_541_is_a_refusal_too(self):
        # 541: what Apple answered the server when it asked once a minute for an hour
        self.world.apple_errors = [HTTPError("u", 541, "", {}, io.BytesIO(b""))] * 2
        self.probe(minutes=8)
        self.assertEqual(self.world.request_times, [hkt(t) for t in ("10:00:30", "10:02:30", "10:06:30", "10:07:30")])
        self.assertIn("Apple refused the request (HTTP 541); no checks from here for 4 min.", self.stderr())

    def test_delivery_problems_are_explained(self):
        def run(outcome):
            with mock.patch.object(self.cs.subprocess, "run", side_effect=outcome if isinstance(outcome, BaseException) else None,
                                   return_value=outcome):
                return self.cs.deliver("feed", {"x": 1})

        self.assertEqual(run(subprocess.TimeoutExpired("ssh", 45)), "the server did not answer within 45 s")
        self.assertIn("could not run ssh", run(FileNotFoundError("ssh")))
        self.assertEqual(run(subprocess.CompletedProcess([], 1, b"", b"ERROR: check not taken: bad time\n")),
                         "ERROR: check not taken: bad time")
        self.assertEqual(run(subprocess.CompletedProcess([], 255, b"", b"")), "ssh exited with code 255")
        self.assertIsNone(run(subprocess.CompletedProcess([], 0, b"", b"")))


class Cli(Base):
    def main(self, *args):
        with mock.patch.object(sys, "argv", ["check_stock.py", *args]):
            return self.cs.main()

    def test_modes(self):
        parts = list(self.cs.DEFAULT_PART_NUMBERS)
        with mock.patch.object(self.cs, "run_slots", return_value=0) as run:
            self.main("--watch", "--every", "60", "--inbox", "/var/spool/x")
        run.assert_called_once_with(parts, 60, 0, False, 10, "/var/spool/x", "vps", None)
        self.assertIn("a check every 60 s, 0 s past the clock plus the checks handed in through /var/spool/x; a quiet status every 10 min.",
                      sys.stdout.getvalue())
        with mock.patch.object(self.cs, "run_probe", return_value=0) as run:
            self.main("--probe", "feed", "--every", "60", "--offset", "30", "--minutes", "330")
        run.assert_called_once_with(parts, "feed", 60, 30, 330, "github", None)
        with mock.patch.object(self.cs, "ingest", return_value=0) as run:
            self.main("--ingest", "/var/spool/x", "--source", "github")
        self.assertEqual(run.call_args.args[:2], ("/var/spool/x", "github"))

    def test_bad_combinations(self):
        for args in (["--watch", "--every", "5"], ["--watch", "--every", "60", "--offset", "60"],
                     ["--inbox", "/x"], ["--every", "60"], ["--probe", "feed"],
                     ["--probe", "feed", "--every", "60", "--minutes", "-1"]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.main(*args)


SILVER = [{"partNumber": U, "product": "iPhone 18 Pro Max 512GB Silver"}]


class MainServer(SharedBase):
    """The main server's side of a standby: it says how it is in status.sync, takes back what
    the standby did, and after a break waits for it before reporting changes."""

    def back_after_a_break(self, minutes=10):
        alive = datetime.fromtimestamp(self.clock.now - minutes * 60, timezone.utc).isoformat()
        (self.inbox / "status.sync").write_text(json.dumps({"alive": alive}))

    def test_status_file_says_it_works_and_what_is_in_stock(self):
        self.vps = self.github = lambda moment: {("R499", U)}
        self.run_until(hkt("10:02:10"))
        status = json.loads((self.inbox / "status.sync").read_text())
        self.assertEqual(status, {"alive": "2026-10-03T02:02:00+00:00", "checked": "2026-10-03T02:02:00+00:00",
                                  "available": SILVER})
        self.assertEqual((self.inbox / "status.sync").stat().st_mode & 0o777, 0o644)  # the --sync user reads it

    def test_back_after_a_break_it_waits_two_minutes_then_reports(self):
        self.back_after_a_break()
        self.vps = self.github = lambda moment: {("R499", U)}
        self.run_until(hkt("10:03:10"))
        [alert] = self.texts()
        self.assertIn("🟢 Появились", alert)
        self.assertIn("🕐 Проверено: 03.10.2026 10:02 (HKT)", alert)  # GitHub's 10:02:30, after the wait
        self.assertIn("Back after 10 min away", sys.stdout.getvalue())
        self.assertIn("Reporting changes again.", sys.stdout.getvalue())

    def test_the_standbys_stock_and_checks_are_taken_back_without_alerts(self):
        self.back_after_a_break()
        self.vps = self.github = lambda moment: {("R499", U)}
        checks = [{"time": "2026-10-03T09:55:00+08:00", "source": "backup", "stores": 6,
                   "in_stock": {"iPhone 18 Pro Max 512GB Silver": ["Canton Road"]}}]

        def standby(now):  # its syncs at :45: the stock while standing in, then its checks
            if now == hkt("10:00:45"):
                out = io.StringIO()
                self.assertEqual(self.cs.sync(self.inbox, io.BytesIO(json.dumps({"available": SILVER}).encode()), out), 0)
                self.assertIn("alive", json.loads(out.getvalue()))
            if now == hkt("10:01:45"):
                self.assertEqual(self.cs.sync(self.inbox, io.BytesIO(json.dumps({"history": checks}).encode()), io.StringIO()), 0)

        self.clock.hooks.append(standby)
        self.run_until(hkt("10:05:10"))
        self.assertEqual(self.world.sent(), [])  # the standby had reported 512GB Silver already
        self.assertEqual(self.state()["available"], SILVER)
        self.assertIn(checks[0], self.history())
        self.assertEqual(list(self.inbox.glob("*.sync")), [self.inbox / "status.sync"])
        out = sys.stdout.getvalue()
        self.assertIn("The standby handed back: 1 configuration(s) in stock", out)
        self.assertIn("Added the standby's 1 check(s) to the history.", out)

    def test_a_change_soon_after_the_hand_back_is_left_to_the_standby_then_reported_once(self):
        # 2TB Black comes at 10:01:30, while the standby may still be standing in (it stands
        # aside at its next sync): this server reports it only after 75 s of quiet.
        self.back_after_a_break()
        self.vps = self.github = lambda moment: {("R499", U)} | ({("R409", B)} if moment >= hkt("10:01:30") else set())

        def standby(now):
            if now == hkt("10:00:45"):
                self.cs.sync(self.inbox, io.BytesIO(json.dumps({"available": SILVER}).encode()), io.StringIO())

        self.clock.hooks.append(standby)
        self.run_until(hkt("10:04:10"))
        [alert] = self.texts()
        self.assertIn("🖤 2TB · Black", alert)
        self.assertNotIn("512GB", alert)
        self.assertIn("🕐 Проверено: 03.10.2026 10:02 (HKT)", alert)  # GitHub's 10:02:30, after the quiet


class SyncCommand(FakeClockBase):
    def setUp(self):
        super().setUp()
        self.shared = self.tmp / "inbox"
        self.shared.mkdir()
        (self.shared / "status.sync").write_text('{"alive": "2026-10-03T02:00:00+00:00", "available": []}')

    def sync(self, raw):
        out = io.StringIO()
        return self.cs.sync(self.shared, io.BytesIO(raw), out), out.getvalue()

    def test_answers_with_the_status_and_keeps_what_the_standby_hands_back(self):
        self.assertEqual(self.sync(b""), (0, '{"alive": "2026-10-03T02:00:00+00:00", "available": []}\n'))
        self.assertEqual([p.name for p in self.shared.iterdir()], ["status.sync"])
        stock = [{"partNumber": U, "product": "iPhone 18 Pro Max 512GB Silver"}]
        self.assertEqual(self.sync(json.dumps({"available": stock}).encode())[0], 0)
        handback = self.shared / "handback.sync"
        self.assertEqual(json.loads(handback.read_text()), {"available": SILVER})
        self.assertEqual(handback.stat().st_mode & 0o777, 0o640)  # for the watcher's group
        lines = [{"time": "2026-10-03T10:00:00+08:00", "source": "backup", "error": "Apple returned HTTP 541"}]
        self.assertEqual(self.sync(json.dumps({"history": lines}).encode())[0], 0)
        [saved] = self.shared.glob("history-*.sync")
        self.assertEqual(json.loads(saved.read_text()), lines)

    def test_refuses_what_is_not_right(self):
        with mock.patch.object(self.cs, "MAX_SYNC_BYTES", 200):
            for raw in (b"[1]", b"{not json", json.dumps({"available": "x"}).encode(),
                        json.dumps({"history": [{"time": "no"}]}).encode(), b"{" + b" " * 300 + b"}"):
                with self.subTest(raw=raw[:30]):
                    self.assertEqual(self.sync(raw)[0], 1)
        self.assertEqual(self.stderr().count("ERROR: sync failed:"), 5)
        self.assertEqual([p.name for p in self.shared.iterdir()], ["status.sync"])


class StandbyBase(FakeClockBase):
    """run_slots as a standby (--standby-of main) on the fake clock. The main server's --sync
    is a fake that answers with main_status(), or fails while main_down."""

    def setUp(self):
        super().setUp()
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir()
        self.main_down = False
        self.main_alive = None  # None: now
        self.main_stock = SILVER
        self.calls = []  # (time, what the standby sent)
        self.apple = lambda moment: {("R499", U)}
        self.world.stock_fn = lambda: self.apple(self.clock.now)
        patch = mock.patch.object(self.cs.subprocess, "run", self.fake_ssh)
        patch.start()
        self.patches.append(patch)

    def fake_ssh(self, args, input=None, capture_output=None, timeout=None):
        self.assertEqual(args, ["ssh", "-T", "main"])
        self.calls.append((self.clock.now, json.loads(input or b"{}")))
        if self.main_down:
            return subprocess.CompletedProcess(args, 255, b"", b"ssh: connect to host 198.51.100.7 port 22: Connection timed out\n")
        alive = self.main_alive or datetime.fromtimestamp(self.clock.now, timezone.utc).isoformat()
        return subprocess.CompletedProcess(args, 0, json.dumps({"alive": alive, "available": self.main_stock}).encode(), b"")

    def github_check(self, stock, when):
        data = self.cs.check_to_json("github", self.result(stock, utc(when)))
        self.assertEqual(self.ingest(self.inbox, "github", io.BytesIO(json.dumps(data).encode() + b"\n")), 0)

    def run_until(self, until, *hooks):
        def stop(now):
            if now >= until:
                raise Stop()

        self.clock.hooks[:] = [*hooks, stop]
        with self.assertRaises(Stop):
            self.cs.run_slots(list(PRO_MAX), 60, 0, False, 10, self.inbox, "backup", "main")


class StandbyServer(StandbyBase):
    def test_while_the_main_server_works_it_only_asks_how_it_is(self):
        self.main_stock = SILVER + [{"partNumber": B, "product": "iPhone 18 Pro Max 2TB Black"}]
        self.run_until(hkt("10:05:10"), lambda now: self.github_check({("R499", U)}, "10:01:30") if now == hkt("10:01:31") else None)
        self.assertEqual(self.world.apple_requests, [])
        self.assertEqual(self.world.sent(), [])
        self.assertEqual([t for t, _ in self.calls], [hkt(f"10:0{m}:45") for m in range(5)])
        self.assertEqual({json.dumps(p) for _, p in self.calls}, {"{}"})
        self.assertEqual(self.state()["available"], self.main_stock)  # ready to stand in
        self.assertEqual(list(self.inbox.glob("*.json")), [])  # GitHub's check was the main server's business

    def test_stands_in_and_hands_back(self):
        def world(now):
            self.main_down = hkt("10:02:00") <= now < hkt("10:09:00")
            if now == hkt("10:06:31"):
                self.github_check({("R499", U)}, "10:06:30")

        self.apple = lambda moment: {("R499", U)} | ({("R409", B)} if moment >= hkt("10:07:00") else set())
        self.run_until(hkt("10:12:10"), world)
        texts = self.texts()
        self.assertEqual(texts[0], "⚠️ Основной сервер не работает уже 3 мин (нет связи). Проверку продолжает запасной сервер.")
        self.assertIn("🟢 Появились · iPhone 18 Pro Max\n🖤 2TB · Black", texts[1])  # 512GB Silver was known: no alert
        self.assertEqual(texts[2], "✅ Основной сервер снова проверяет. Запасной вернулся в режим ожидания.")
        self.assertEqual(len(texts), 3)
        self.assertEqual(self.world.request_times, [hkt(f"10:{m:02d}:00") for m in range(6, 10)])  # 10:05:45 → 10:09:45
        back = [(t, p) for t, p in self.calls if p]
        self.assertEqual(back[-2][0], hkt("10:09:45"))
        self.assertEqual({i["partNumber"] for i in back[-2][1]["available"]}, {U, B})  # the stock for the main server
        history = back[-1][1]["history"]
        self.assertEqual([(line["time"][11:19], line["source"]) for line in history],
                         [("10:06:00", "backup"), ("10:06:30", "github"), ("10:07:00", "backup"),
                          ("10:08:00", "backup"), ("10:09:00", "backup")])
        self.assertEqual([p for t, p in self.calls if t > hkt("10:09:45")], [{}, {}])  # standing by again

    def test_stands_in_at_once_when_the_main_server_stopped_checking_long_ago(self):
        self.main_alive = datetime.fromtimestamp(self.clock.now - 600, timezone.utc).isoformat()
        self.run_until(hkt("10:02:10"))
        self.assertEqual(self.texts()[0], "⚠️ Основной сервер не работает уже 10 мин (служба проверки на нём остановлена). "
                                          "Проверку продолжает запасной сервер.")
        self.assertEqual(self.world.request_times, [hkt("10:01:00"), hkt("10:02:00")])

    def test_short_trouble_with_the_main_server_is_not_a_reason(self):
        def trouble(now):  # two short outages, five minutes apart
            self.main_down = hkt("10:02:00") <= now < hkt("10:04:00") or hkt("10:07:00") <= now < hkt("10:08:00")

        self.run_until(hkt("10:09:10"), trouble)
        self.assertEqual(self.world.apple_requests, [])
        self.assertEqual(self.world.sent(), [])


class StandbyFirstContact(StandbyBase):
    def test_never_stands_in_for_a_server_it_never_reached(self):
        self.main_down = True  # e.g. its key is not on the main server yet
        self.run_until(hkt("10:10:10"))
        self.assertEqual(self.world.apple_requests, [])
        self.assertEqual(self.world.sent(), [])
        self.assertIn("not standing in for a server never seen working", self.stderr())

    def test_after_a_restart_it_remembers_the_main_server_worked(self):
        (self.tmp / "stock_state.json").write_text(json.dumps({"main_seen_utc": "2026-10-03T01:00:00+00:00", "available": SILVER}))
        self.main_down = True
        self.run_until(hkt("10:05:10"))
        self.assertTrue(self.texts()[0].startswith("⚠️ Основной сервер не работает уже 3 мин (нет связи)."))
        self.assertEqual(self.world.request_times, [hkt("10:04:00"), hkt("10:05:00")])


class ProbeWithStandby(ProbeBase):
    def test_a_check_the_server_does_not_take_goes_to_the_standby(self):
        def run(args, input=None, capture_output=None, timeout=None):
            self.delivered.append((args[-1], self.clock.now))
            refused = args[-1] == "feed" or self.both_down
            return subprocess.CompletedProcess(args, 255 if refused else 0, b"", b"ssh: connect to host 203.0.113.9 port 22: Connection refused\n" if refused else b"")

        self.both_down = False
        with mock.patch.object(self.cs.subprocess, "run", run):
            self.cs.run_probe(list(PRO_MAX), "feed", 60, 30, 35, "github", "feed2")
        self.assertEqual([host for host, _ in self.delivered[:4]], ["feed", "feed2", "feed", "feed2"])
        self.assertEqual(self.world.sent(), [])  # the standby took every check
        self.assertIn("The server did not take the check (ssh: connect to host 203.0.113.9 port 22: Connection refused); the standby did.",
                      sys.stdout.getvalue())
        self.both_down = True
        with mock.patch.object(self.cs.subprocess, "run", run):
            self.cs.run_probe(list(PRO_MAX), "feed", 60, 30, 35, "github", "feed2")
        [warning] = self.world.sent()
        self.assertIn("не может передать проверки на сервер уже 30 мин", warning["text"])
        self.assertIn("; standby: ssh: connect to host", warning["text"])


class CliStandby(Base):
    def main(self, *args):
        with mock.patch.object(sys, "argv", ["check_stock.py", *args]):
            return self.cs.main()

    def test_standby_sync_and_backup(self):
        parts = list(self.cs.DEFAULT_PART_NUMBERS)
        with mock.patch.object(self.cs, "run_slots", return_value=0) as run:
            self.main("--watch", "--every", "60", "--inbox", "/var/spool/x", "--standby-of", "main")
        run.assert_called_once_with(parts, 60, 0, False, 10, "/var/spool/x", "backup", "main")
        self.assertIn("Standing by for the main server (ssh main)", sys.stdout.getvalue())
        with mock.patch.object(self.cs, "sync", return_value=0) as run:
            self.main("--sync", "/var/spool/x")
        self.assertEqual(run.call_args.args[0], "/var/spool/x")
        with mock.patch.object(self.cs, "run_probe", return_value=0) as run:
            self.main("--probe", "feed", "--backup", "feed2", "--every", "60", "--offset", "30")
        run.assert_called_once_with(parts, "feed", 60, 30, 0, "github", "feed2")
        for args in (["--watch", "--standby-of", "main"], ["--backup", "feed2"], ["--standby-of", "main"]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.main(*args)


class Installer(unittest.TestCase):
    """vps_install.sh in a sandbox: fake systemctl, useradd, chown; folders in a temp dir."""

    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGZha2VrZXlmb3J0ZXN0aW5nb25seQ github-feed"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        fakes = {
            "id": '[ "$1" = "-u" ] && { echo 0; exit 0; }\nexit 0',
            "install": 'args=(); while [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; done\n'
                       'exec /usr/bin/install "${args[@]}"',
            "chown": 'echo "chown $*" >> "$SIM/calls.log"',
            "useradd": 'echo "useradd $*" >> "$SIM/calls.log"',
            "systemctl": 'echo "systemctl $*" >> "$SIM/calls.log"',
            "journalctl": 'echo "journalctl $*" >> "$SIM/calls.log"',
            "sleep": 'echo "sleep $*" >> "$SIM/calls.log"',
            "runuser": 'echo "runuser $*" >> "$SIM/calls.log"\nwhile [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"',
            "ssh": 'echo "ssh $*" >> "$SIM/calls.log"\nexit 255',
        }
        (self.tmp / "bin").mkdir()
        for name, text in fakes.items():
            f = self.tmp / "bin" / name
            f.write_text(f"#!/bin/bash\n{text}\n")
            f.chmod(0o755)
        self.src = self.tmp / "src"
        self.src.mkdir()
        for f in ("vps_install.sh", "check_stock.py", "stock_report.py"):
            shutil.copy(PROJECT / f, self.src / f)
        self.app, self.units, self.feed, self.lib, self.inbox = (self.tmp / d for d in ("app", "units", "feed", "lib", "inbox"))
        self.app.mkdir()
        self.units.mkdir()
        (self.app / "config.env").write_text("TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_CHAT_ID=1\n")
        path = f"{self.tmp / 'bin'}:{os.environ['PATH']}"
        self.python = shutil.which("python3", path=path)
        self.env = dict(os.environ, PATH=path, SIM=str(self.tmp), APP=str(self.app), UNIT_DIR=str(self.units),
                        FEED_HOME=str(self.feed), FEED_LIB=str(self.lib), INBOX=str(self.inbox), HOME=str(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def install(self, *args, **env):
        return subprocess.run(["bash", str(self.src / "vps_install.sh"), *args], env={**self.env, **env},
                              capture_output=True, text=True, timeout=60)

    def keys(self):
        f = self.feed / ".ssh" / "authorized_keys"
        return f.read_text() if f.exists() else None

    def test_install_sets_up_the_shared_watch_and_githubs_key(self):
        done = self.install("feed-key", self.KEY)
        self.assertEqual(done.returncode, 0, done.stderr)
        unit = (self.units / "iphone-stock-watch-hk.service").read_text()
        self.assertIn(f"check_stock.py --watch --every 60 --offset 0 --status-minutes 10 --inbox {self.inbox}\n", unit)
        self.assertIn(f"ReadWritePaths={self.app} {self.inbox}\n", unit)
        self.assertEqual(self.keys(), f'restrict,command="{self.python} -I {self.lib}/check_stock.py --ingest {self.inbox} '
                                      f'--source github" {self.KEY}\n')
        self.assertTrue((self.lib / "check_stock.py").is_file())
        self.assertTrue(self.inbox.is_dir())
        self.assertIn("GitHub может передавать свои проверки (ключ установлен).", done.stdout)

    def test_the_ssh_command_takes_a_check(self):
        self.assertEqual(self.install("feed-key", self.KEY).returncode, 0)
        command = self.keys().split('command="')[1].split('"')[0].split()
        line = json.dumps({"source": "x", "time": "2026-10-03T10:00:30+08:00", "error": "Apple returned HTTP 503"}) + "\n"
        done = subprocess.run(command, input=line, capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        [saved] = list(self.inbox.glob("*.json"))
        self.assertEqual(json.loads(saved.read_text())["source"], "github")
        done = subprocess.run(command, input="rm -rf /\n", capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(done.returncode, 1)
        self.assertIn("check not taken", done.stderr)

    def test_bad_keys_are_refused(self):
        for key in (self.KEY + "\nssh-ed25519 AAAAevil other", "ssh-rsa AAAAB3NzaC1yc2E user", 'no-pty ' + self.KEY,
                    self.KEY.replace("github-feed", 'x" command="sh'), ""):
            with self.subTest(key=key[:30]):
                done = self.install("feed-key", key)
                self.assertEqual(done.returncode, 1)
                self.assertIn("Нужен открытый ключ ssh-ed25519", done.stderr)
        self.assertIsNone(self.keys())

    def test_update_keeps_the_key_and_uninstall_closes_the_door(self):
        self.install("feed-key", self.KEY)
        first = self.keys()
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.keys(), first)
        done = self.install("uninstall")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNone(self.keys())
        self.assertEqual(list(self.units.iterdir()), [])
        self.assertIn("доступ по SSH для GitHub и запасного сервера закрыт", done.stdout)

    def test_without_a_key_github_is_explained(self):
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNone(self.keys())
        self.assertIn("Чтобы GitHub передавал свои проверки", done.stdout)

    MAIN_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC5tYWluc2VydmVyaG9zdGtleWZvcnRlc3RzMDAw"

    def test_standby_role(self):
        self.install("feed-key", self.KEY)
        done = self.install("standby", "198.51.100.7", MAIN_HOST_KEY=self.MAIN_KEY)
        self.assertEqual(done.returncode, 0, done.stderr)
        unit = (self.units / "iphone-stock-watch-hk.service").read_text()
        self.assertIn(f"--inbox {self.inbox} --source backup --standby-of main\n", unit)
        self.assertFalse((self.units / "iphone-stock-watch-hk-bot.service").exists())  # the bot stays on the main server
        self.assertEqual((self.lib / "role").read_text(), "standby 198.51.100.7\n")
        ssh = self.app / ".ssh"
        config = (ssh / "config").read_text()
        for line in ("Host main", "HostName 198.51.100.7", "User stockfeed", f"IdentityFile {ssh}/sync_key",
                     "StrictHostKeyChecking yes", "BatchMode yes"):
            self.assertIn(f"{line}\n", config)
        self.assertEqual((ssh / "known_hosts").read_text(), f"198.51.100.7 {self.MAIN_KEY}\n")
        self.assertEqual((ssh / "sync_key").stat().st_mode & 0o777, 0o600)
        pub = (ssh / "sync_key.pub").read_text().strip()
        self.assertIn(f"sudo bash vps_install.sh sync-key '{pub}'", done.stdout)
        self.assertIn("Основной сервер пока не отвечает этому", done.stdout)  # its key is not on the main server yet
        self.assertIn(f"runuser -u stockwatch -- ssh -F {ssh}/config -T main", (self.tmp / "calls.log").read_text())
        self.assertIn("--source github", self.keys())  # GitHub may hand its checks to the standby too
        again = self.install()  # an update keeps the role and the key
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("--standby-of main", (self.units / "iphone-stock-watch-hk.service").read_text())
        self.assertEqual((ssh / "sync_key.pub").read_text().strip(), pub)
        refused = self.install("sync-key", self.KEY.replace("github-feed", "standby"))
        self.assertEqual(refused.returncode, 1)
        self.assertIn("sync-key ставится на основной", refused.stderr)

    def test_the_main_server_lets_the_standby_ask(self):
        self.install("feed-key", self.KEY)
        standby_key = self.KEY.replace("github-feed", "standby-vps2")
        done = self.install("sync-key", standby_key)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.keys().splitlines(), [
            f'restrict,command="{self.python} -I {self.lib}/check_stock.py --ingest {self.inbox} --source github" {self.KEY}',
            f'restrict,command="{self.python} -I {self.lib}/check_stock.py --sync {self.inbox}" {standby_key}',
        ])
        self.assertIn("Запасной сервер может узнавать, работает ли этот (ключ установлен).", done.stdout)
        self.assertTrue((self.units / "iphone-stock-watch-hk-bot.service").exists())

    def test_the_sync_command_answers(self):
        self.install("feed-key", self.KEY)
        self.install("sync-key", self.KEY.replace("github-feed", "standby"))
        command = self.keys().splitlines()[1].split('command="')[1].split('"')[0].split()
        (self.inbox / "status.sync").write_text('{"alive": "2026-10-03T02:00:00+00:00"}')
        done = subprocess.run(command, input='{"available": []}', capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"alive": "2026-10-03T02:00:00+00:00"})
        self.assertEqual(json.loads((self.inbox / "handback.sync").read_text()), {"available": []})

    def test_bad_standby_settings_are_refused(self):
        for args, env in ((["standby"], {}), (["standby", "1.2.3.4; rm -rf /"], {}),
                          (["standby", "198.51.100.7"], {"MAIN_HOST_KEY": "ssh-rsa AAAA"}),
                          (["sync-key", "ssh-rsa AAAAB3 x"], {})):
            with self.subTest(args=args):
                done = self.install(*args, **env)
                self.assertEqual(done.returncode, 1)
        self.assertFalse((self.lib / "role").exists())

    def test_report_runs_as_the_service_user(self):
        self.assertEqual(self.install().returncode, 0)
        done = self.install("report")
        self.assertIn("В журнале пока нет проверок.", done.stderr)
        self.assertIn("runuser -u stockwatch -- python3", (self.tmp / "calls.log").read_text())

    def test_bad_settings_and_commands_are_refused(self):
        for env in ({"EVERY": "5"}, {"OFFSET": "60"}, {"STATUS_MINUTES": "x"}, {"EVERY": "1m"}):
            with self.subTest(env=env):
                self.assertEqual(self.install(**env).returncode, 1)
        done = self.install("frobnicate")
        self.assertEqual(done.returncode, 1)
        self.assertIn("Неизвестная команда", done.stderr)
        custom = self.install(EVERY="30", OFFSET="15", STATUS_MINUTES="0")
        self.assertEqual(custom.returncode, 0, custom.stderr)
        self.assertIn("--every 30 --offset 15 --status-minutes 0 ", (self.units / "iphone-stock-watch-hk.service").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
