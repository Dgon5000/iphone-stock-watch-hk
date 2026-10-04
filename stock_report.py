#!/usr/bin/env python3
"""When did which iPhone configuration come into stock at Apple Store Hong Kong?

Reads stock_history.jsonl (check_stock.py appends a line on every check) and prints:
each configuration's appearances with how long they lasted and in which stores, the
configurations that never appeared, and at which hours (Hong Kong time) stock shows up.

    python3 stock_report.py [history.jsonl ...] [--csv intervals.csv]
    journalctl -u iphone-stock-watch-hk -o cat | python3 stock_report.py --import-journal >> stock_history.jsonl
"""
import argparse
import csv
import io
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from check_stock import HISTORY_FILE, HKT, color_emoji, describe

# Absences shorter than this are Apple's flicker while it releases stock, not a sell-out.
MERGE_GAP = timedelta(minutes=5)
SHOW_LAST = 8  # appearances listed per configuration
JOURNAL_LINE = re.compile(
    r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) HKT\] (?:(?:Check \d+/\d+|VPS|GitHub|Mac): )?(\d+) stores × \d+ models checked — (.*)$"
)


def import_journal(lines):
    """History entries from check_stock.py's log lines ("… 6 stores × 12 models checked — …")."""
    for line in lines:
        match = JOURNAL_LINE.match(line.strip())
        if not match:
            continue
        moment = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=HKT)
        in_stock = {}
        if match[3] != "no pickup stock":
            for piece in match[3].split("; "):
                title, _, stores = piece.partition(": ")
                in_stock[title] = [store for store in stores.split(", ") if store]
        yield {"time": moment.isoformat(timespec="seconds"), "stores": int(match[2]), "in_stock": in_stock, "source": "journal"}


def load(paths):
    """Watched titles (in watch order) and checks [(time, {title: [stores]})] sorted by time."""
    watched, checks = [], []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                moment = datetime.fromisoformat(entry["time"])
            except (ValueError, KeyError, TypeError):
                continue
            watched += [title for title in entry.get("watched", []) if title not in watched]
            if isinstance(entry.get("in_stock"), dict):
                checks.append((moment, entry["in_stock"]))
    checks.sort(key=lambda check: check[0])
    for _, in_stock in checks:
        watched += [title for title in in_stock if title not in watched]
    return watched, checks


def intervals(checks, title):
    """[(appeared, sold_out or None, stores)] for one configuration. A sell-out is the first
    check without it; absences shorter than MERGE_GAP are merged away."""
    runs = []
    start, stores = None, set()
    for moment, in_stock in checks:
        if title in in_stock:
            if start is None:
                start, stores = moment, set()
            stores.update(in_stock[title])
        elif start is not None:
            runs.append([start, moment, stores])
            start = None
    if start is not None:
        runs.append([start, None, stores])

    merged = []
    for run in runs:
        if merged and merged[-1][1] is not None and run[0] - merged[-1][1] < MERGE_GAP:
            merged[-1][1] = run[1]
            merged[-1][2] |= run[2]
        else:
            merged.append(run)
    return [tuple(run) for run in merged]


def when(moment):
    return moment.astimezone(HKT).strftime("%d.%m %H:%M")


def lasted(delta):
    total = int(delta.total_seconds() // 60)
    return f"{total // 60} ч {total % 60} мин" if total >= 60 else f"{total} мин"


def times(count):
    """1 раз, 2 раза, 5 раз."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} раз"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return f"{count} раза"
    return f"{count} раз"


def label(title):
    _, storage, color = describe(title)
    return f"{storage} · {color}" if storage else title


def groups(watched):
    """(model, storage) of the watched configurations, in watch order: the bot's report buttons."""
    return list(dict.fromkeys(describe(title)[:2] for title in watched if describe(title)[1]))


def hours(found, first, colors=False):
    """The "at which hours" histogram (HKT) as lines; with colors, also which colours came into
    stock in each hour ("🩶2 🖤1"). `found` is {title: intervals} in watch order."""
    starts = [(appeared, title) for title, runs in found.items() for appeared, _, _ in runs if appeared != first]
    if not starts:
        return []
    by_hour = Counter(moment.astimezone(HKT).hour for moment, _ in starts)
    top = max(by_hour.values())
    lines = ["🕐 В какие часы появляется (HKT, число появлений):"]
    for hour in sorted(by_hour):
        bar = "█" * max(1, round(10 * by_hour[hour] / top))
        line = f"{hour:02d}:00–{hour + 1:02d}:00  {bar} {by_hour[hour]}"
        if colors:
            per = Counter(title for moment, title in starts if moment.astimezone(HKT).hour == hour)
            line += "  " + " ".join(f"{color_emoji(describe(title)[2])}{per[title]}" for title in found if per[title])
        lines.append(line)
    return lines + [""]


def report(watched, checks, show_last=SHOW_LAST, one_per_line=False,
           title="📊 История наличия в Apple Store Hong Kong", hours_first=False):
    """The report text for the configurations in `watched`. one_per_line puts each appearance
    on its own line (for a phone). hours_first (one model and storage, see group_report) puts
    the hours, with the colours of each hour, before the appearances."""
    first, last = checks[0][0], checks[-1][0]
    found = {title: intervals(checks, title) for title in watched}
    lines = [title, f"Период: {when(first)} – {when(last)} (HKT), проверок: {len(checks)}", ""]
    if hours_first:
        lines += hours(found, first, colors=True)

    by_model = {}
    for name in watched:
        model, storage, _ = describe(name)
        by_model.setdefault(model, {}).setdefault(storage, []).append(name)
    appearances = []
    for model, by_storage in by_model.items():
        blocks = []
        for titles in by_storage.values():
            block = []
            for name in titles:
                runs = found[name]
                if not runs:
                    continue
                parts = []
                for appeared, sold_out, _ in runs[-show_last:]:
                    start = "с начала наблюдений" if appeared == first else when(appeared)
                    end = "сейчас в наличии"
                    if sold_out:
                        same_day = sold_out.astimezone(HKT).date() == appeared.astimezone(HKT).date()
                        end = f"{when(sold_out)[6:] if same_day else when(sold_out)} ({lasted(sold_out - appeared)})"
                    parts.append(f"{start} → {end}")
                head = f"{color_emoji(describe(name)[2])} {label(name)} — {times(len(runs))}"
                if one_per_line:
                    shown = "последние:" if len(runs) > show_last else ""
                    block.append(f"{head}{', ' + shown if shown else ':'}\n" + "\n".join(f"   {part}" for part in parts))
                else:
                    more = f" и ещё {len(runs) - show_last} раньше" if len(runs) > show_last else ""
                    block.append(f"{head}: " + "; ".join(parts) + more)
            if block:
                blocks.append("\n".join(block))
        if blocks:
            appearances += ([] if hours_first else [f"📱 {model}"]) + ["\n\n".join(blocks), ""]
    if appearances or not hours_first:
        lines += ["Появления (появилось → закончилось, сколько держалось):", *appearances]

    never = [label(name) for name in watched if not found[name]]
    if never:
        lines += [f"➖ Ни разу не появлялись: {', '.join(never)}", ""]

    if not hours_first:
        lines += hours(found, first)

    stores = Counter(store for runs in found.values() for _, _, names in runs for store in names)
    if stores:
        lines.append("🏬 Где появлялось (число появлений): " + ", ".join(f"{name} {count}" for name, count in stores.most_common()))
    return "\n".join(lines).rstrip()


def group_report(watched, checks, group, show_last=SHOW_LAST):
    """One model and storage, e.g. ("iPhone 18 Pro Max", "512GB"), for a phone: at which hours
    it comes into stock and in which colours, then each colour's appearances."""
    model, storage = group
    titles = [title for title in watched if describe(title)[:2] == (model, storage)]
    return report(titles, checks, show_last, one_per_line=True, title=f"📊 {model} {storage}", hours_first=True)


def csv_text(watched, checks):
    """Every appearance as a ;-separated table (Excel and Numbers read it as columns)."""
    first = checks[0][0]
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\n")
    writer.writerow(["Модель", "Память", "Цвет", "Появилось (HKT)", "Закончилось (HKT)", "Минут", "Магазины", "Примечание"])
    for title in watched:
        model, storage, color = describe(title)
        for appeared, sold_out, stores in intervals(checks, title):
            writer.writerow([
                model,
                storage,
                color,
                appeared.astimezone(HKT).strftime("%Y-%m-%d %H:%M"),
                sold_out.astimezone(HKT).strftime("%Y-%m-%d %H:%M") if sold_out else "",
                int((sold_out - appeared).total_seconds() // 60) if sold_out else "",
                ", ".join(sorted(stores, key=str.lower)),
                "в наличии с начала наблюдений" if appeared == first else ("ещё в наличии" if not sold_out else ""),
            ])
    return out.getvalue()


def write_csv(path, watched, checks):
    # The BOM makes Excel read the file as UTF-8.
    Path(path).write_text(csv_text(watched, checks), encoding="utf-8-sig")


def main():
    parser = argparse.ArgumentParser(description="Report when iPhone configurations came into stock.")
    parser.add_argument("files", nargs="*", help=f"history files (default: {HISTORY_FILE.name} next to this script)")
    parser.add_argument("--csv", metavar="PATH", help="also write every appearance to a CSV file for Excel/Numbers")
    parser.add_argument("--import-journal", action="store_true", help="turn check_stock.py log lines on stdin into history lines")
    args = parser.parse_args()

    if args.import_journal:
        for entry in import_journal(sys.stdin):
            print(json.dumps(entry, ensure_ascii=False))
        return 0
    try:
        watched, checks = load(args.files or [HISTORY_FILE])
    except FileNotFoundError:
        watched, checks = [], []
    if not checks:
        sys.exit("В журнале пока нет проверок.")
    print(report(watched, checks))
    if args.csv:
        write_csv(args.csv, watched, checks)
        print(f"\nТаблица: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
