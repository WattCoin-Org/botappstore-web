#!/usr/bin/env python3
"""Validate catalog.json against the bot pages (Offer JSON-LD).

Asserts: valid JSON; no x.ai URLs, email addresses, internal-shop names or
"$WATT"; every catalog product maps to a bots/<slug>.html page whose Offer
price equals price_usd; every bot page with a paid Offer is in the catalog.
Run from anywhere: python3 scripts/check_catalog.py
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = [
    (re.compile(r"x\.ai", re.I), "x.ai URL"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "email address"),
    (re.compile(r"mesh-printability|stl-mesh-preflight|shop-catalog-compiler", re.I), "internal-shop name"),
    (re.compile(r"\$WATT"), "$WATT"),
]
LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def offer_prices(html):
    """Return all Offer prices (float) found in the page's JSON-LD."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("@type") == "Offer" and "price" in node:
                found.append(float(node["price"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for block in LD.findall(html):
        walk(json.loads(block))
    return found


def main():
    errors = []
    raw = (ROOT / "catalog.json").read_text(encoding="utf-8")
    try:
        cat = json.loads(raw)
    except ValueError as e:
        print(f"FAIL: catalog.json is not valid JSON: {e}")
        return 1
    for rx, label in FORBIDDEN:
        if rx.search(raw):
            errors.append(f"catalog.json contains {label}")

    pages = {p.stem: p for p in (ROOT / "bots").glob("*.html")}
    covered = set()
    for prod in cat.get("products", []):
        sku = prod["sku"]
        slug = Path(urlparse(prod.get("url", "")).path).stem
        if slug not in pages:
            errors.append(f"{sku}: no bots/{slug}.html page")
            continue
        covered.add(slug)
        prices = offer_prices(pages[slug].read_text(encoding="utf-8"))
        if not prices:
            errors.append(f"{sku}: no Offer price on bots/{slug}.html")
        elif any(p != float(prod["price_usd"]) for p in prices):
            errors.append(f"{sku}: Offer price {prices} != price_usd {prod['price_usd']}")

    for slug, path in sorted(pages.items()):
        prices = offer_prices(path.read_text(encoding="utf-8"))
        if any(p > 0 for p in prices) and slug not in covered:
            errors.append(f"bots/{slug}.html has a paid Offer but is not in catalog.json")

    for e in errors:
        print("FAIL:", e)
    if not errors:
        print(f"OK: {len(cat['products'])} products match {len(pages)} bot pages; no forbidden strings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
