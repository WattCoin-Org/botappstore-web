#!/usr/bin/env python3
"""Generate Markdown twins (index.md, bots/<slug>.md) from the HTML pages.

Content comes from each page's <title>, meta description, h1 and JSON-LD
(SoftwareApplication, Offer, FAQPage); "When to use" and checkout details come
from catalog.json. Output is deterministic, so re-running is idempotent.

  python3 scripts/build_markdown.py          # write the files
  python3 scripts/build_markdown.py --check  # exit 1 if any file is stale
"""
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SITE = "https://botappstore.com"
LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def meta(src, name):
    m = re.search(r'<meta\s+(?:name|property)="%s"\s+content="([^"]*)"' % re.escape(name), src)
    return html.unescape(m.group(1)).strip() if m else ""


def title(src):
    m = re.search(r"<title>(.*?)</title>", src, re.S)
    return clean(m.group(1)) if m else ""


def h1(src):
    m = re.search(r"<h1[^>]*>(.*?)</h1>", src, re.S)
    return clean(m.group(1)) if m else ""


def nodes(src):
    out = []
    for block in LD.findall(src):
        d = json.loads(block)
        out.extend(d.get("@graph", [d]))
    return out


def price_line(price):
    return "Free" if float(price) == 0 else f"${float(price):.0f} one-time (USD)"


def checkout_md(cat, sku):
    card = cat["checkout"]["card"]
    sol = cat["checkout"]["sol"]
    return "\n".join([
        "## How an agent buys",
        "",
        f"- Card, Apple Pay or Google Pay (default): `{card['method']} {card['url']}` with body "
        f"`{json.dumps({'sku': sku, 'storefront': 'botappstore'})}`. It returns `checkout_url`, a hosted page where a human completes payment.",
        f"- License lookup after payment: `{card['license_lookup']}`.",
        f"- Price check without creating an order: add `\"dry_run\": true` to the quote body (`{sol['quote']}`).",
        f"- Full machine-readable catalog: {SITE}/catalog.json",
        f"- Support, refunds and privacy: {SITE}/contact.html , {SITE}/privacy.html",
        "",
        "Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.",
        "",
    ])


def bot_md(path, cat, products):
    src = path.read_text(encoding="utf-8")
    slug = path.stem
    prod = products.get(slug)
    ns = nodes(src)
    app = next((n for n in ns if n.get("@type") == "SoftwareApplication"), {})
    faq = next((n for n in ns if n.get("@type") == "FAQPage"), {})
    offer = app.get("offers", {})
    price = offer.get("price", prod["price_usd"] if prod else 0)
    sku = prod["sku"] if prod else app.get("sku", slug)
    lines = [f"# {title(src)}", "", f"Canonical page: {SITE}/bots/{slug}.html", ""]
    desc = app.get("description") or meta(src, "description")
    lines += [h1(src), "", desc, ""]
    if prod and prod.get("when_to_use"):
        lines += ["## When to use", "", prod["when_to_use"], ""]
    lines += ["## Price", "", f"- SKU: `{sku}`", f"- Price: {price_line(price)}", ""]
    if float(price) > 0:
        lines.append(checkout_md(cat, sku))
    else:
        lines += ["## How an agent gets it", "", f"Free. Install details are on {SITE}/bots/{slug}.html ; "
                  f"catalog: {SITE}/catalog.json", ""]
    entities = faq.get("mainEntity", [])
    if entities:
        lines += ["## FAQ", ""]
        for q in entities:
            lines += [f"### {clean(q['name'])}", "", clean(q["acceptedAnswer"]["text"]), ""]
    return "\n".join(lines).rstrip() + "\n"


def index_md(cat):
    src = (ROOT / "index.html").read_text(encoding="utf-8")
    lines = [f"# {title(src)}", "", meta(src, "description"), "",
             f"Canonical page: {SITE}/", "",
             "## Bots", ""]
    for p in cat["products"]:
        slug = Path(urlparse(p["url"]).path).stem
        lines.append(f"- [{p['name']}]({SITE}/bots/{slug}.html) — {price_line(p['price_usd'])}, `{p['sku']}`. {p['summary']}")
    lines += ["", "## Bundles", ""]
    for b in cat.get("bundles", []):
        lines.append(f"- [{b['name']}]({b['url']}) — {price_line(b['price_usd'])}, `{b['sku']}`; includes {', '.join(b['includes'])}.")
    lines += ["", "## For agents", "",
              f"- Catalog (JSON): {SITE}/catalog.json",
              f"- LLM index: {SITE}/llms.txt",
              f"- Full catalog with sample reports: {SITE}/llms-full.txt",
              f"- Sitemap: {SITE}/sitemap.xml",
              f"- About, contact, privacy: {SITE}/about.html , {SITE}/contact.html , {SITE}/privacy.html",
              "- Buying: pick a `sku` from the catalog and follow `checkout` in catalog.json (card checkout is a hosted page a human completes).",
              ""]
    return "\n".join(lines)


def build():
    cat = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    products = {Path(urlparse(p["url"]).path).stem: p for p in cat["products"]}
    out = {ROOT / "index.md": index_md(cat)}
    for path in sorted((ROOT / "bots").glob("*.html")):
        out[ROOT / "bots" / f"{path.stem}.md"] = bot_md(path, cat, products)
    return out


def main():
    files = build()
    if "--check" in sys.argv:
        stale = [str(p.relative_to(ROOT)) for p, t in files.items()
                 if not p.exists() or p.read_text(encoding="utf-8") != t]
        for s in stale:
            print("STALE:", s)
        print("OK: %d markdown files up to date" % len(files) if not stale else f"{len(stale)} stale; run scripts/build_markdown.py")
        return 1 if stale else 0
    for p, t in files.items():
        p.write_text(t, encoding="utf-8")
    print(f"wrote {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
