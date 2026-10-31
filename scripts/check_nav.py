#!/usr/bin/env python3
"""Assert each bot page's nav checkout button points at its own SKU and catalog price."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER_RE = re.compile(r"<header.*?</header>", re.S)
# Pages use either data-open-checkout="<sku>" or onclick="openCheckoutModal('<sku>', this)".
BTN_RE = re.compile(r"""<button[^>]*(?:data-open-checkout="|openCheckoutModal\(')([^"']*)[^>]*>(.*?)</button>""", re.S)
FREE_URL = "https://x.ai/bot/"


def check_page(html, sku, price):
    """Return problem strings for one bot page's header; price 0 means a free page."""
    m = HEADER_RE.search(html)
    if not m:
        return ["no <header>"]
    header = m.group(0)
    buttons = BTN_RE.findall(header)
    if price == 0:
        problems = [f"free page has paid nav button for {s!r}" for s, _ in buttons]
        if FREE_URL not in header:
            problems.append("free page has no free x.ai CTA in nav")
        return problems
    if len(buttons) != 1:
        return [f"expected 1 nav checkout button, found {len(buttons)}"]
    nav_sku, text = buttons[0]
    text = " ".join(text.split())
    problems = []
    if nav_sku != sku:
        problems.append(f"nav button sku {nav_sku!r} != page sku {sku!r}")
    if f"(${price:g})" not in text:
        problems.append(f"nav text {text!r} lacks price ${price:g}")
    return problems


def main():
    cat = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    errors = []
    n = 0
    for p in cat["products"]:
        m = re.search(r"/bots/([^/]+)\.html$", p["url"])
        page = ROOT / "bots" / f"{m.group(1)}.html" if m else None
        if not page or not page.exists():
            continue
        n += 1
        for prob in check_page(page.read_text(encoding="utf-8"), p["sku"], float(p["price_usd"])):
            errors.append(f"bots/{page.name}: {prob}")
    for e in errors:
        print("FAIL:", e)
    if errors:
        return 1
    print(f"OK: {n} bot pages have nav checkout button with own SKU and catalog price")
    return 0


if __name__ == "__main__":
    sys.exit(main())
