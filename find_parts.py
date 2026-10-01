#!/usr/bin/env python3
"""List Apple Hong Kong part numbers for an iPhone model.

Usage:
    python3 find_parts.py                 # iPhone 18 Pro / 18 Pro Max
    python3 find_parts.py iphone-air      # any slug from apple.com/hk/shop/buy-iphone/<slug>
"""
import html
import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from check_stock import USER_AGENT, ssl_context

BUY_URL = "https://www.apple.com/hk/shop/buy-iphone/{model}"


def label(value):
    """Turn Apple's selector HTML ("256<small>GB</small><as-footnote>...") into plain text."""
    text = re.sub(r"<as-footnote.*?</as-footnote>", "", str(value), flags=re.S)
    text = re.sub(r"<[^>]+>", "", text.split("\n")[0])
    return " ".join(html.unescape(text).split())


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "iphone-18-pro"
    url = BUY_URL.format(model=model)
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=30, context=ssl_context()) as response:
            page = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError) as exc:
        sys.exit(f"Could not open {url}: {exc}")

    marker = "productSelectionData:"
    start = page.find(marker)
    if start < 0:
        sys.exit(f"No product data on {url}; Apple may have changed the page.")
    data, _ = json.JSONDecoder().raw_decode(page[start + len(marker):].lstrip())

    dimensions = [section["dimension"] for section in data["sections"]]
    values = data["displayValues"]
    prices = values.get("prices") or {}

    def position(dim, value):
        order = (values.get(dim) or {}).get("variantOrder") or []
        return order.index(value) if value in order else len(order)

    rows = []
    for product in data["products"]:
        names = []
        for dim in dimensions:
            shown = (values.get(dim) or {}).get(product.get(dim))
            names.append(label(shown.get("value") if isinstance(shown, dict) else product.get(dim)))
        price = ((prices.get(product.get("fullPrice")) or {}).get("currentPrice") or {}).get("amount", "")
        order = [position(dim, product.get(dim)) for dim in dimensions]
        rows.append((order, product["partNumber"], names, price))

    title = re.search(r"<title>(.*?)</title>", page, re.S)
    print(label(title.group(1)) if title else url)
    for _, part, names, price in sorted(rows):
        print(f"  {part:<11} {'  '.join(f'{n:<18}' for n in names)}{price}")
    print("\nНужные артикулы перечислите через запятую в переменной PART_NUMBERS, например: MJXU4ZA/A,MJXT4ZA/A")


if __name__ == "__main__":
    main()
