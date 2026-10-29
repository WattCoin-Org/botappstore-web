#!/usr/bin/env python3
"""Fail if any HTML page has a forbidden term in its title or social/SEO meta tags.

Checked: <title>, <meta name="description">, any og:* meta (property or name),
any twitter:* meta (content attribute).
Forbidden: the word SOL (\\bSOL\\b, case-insensitive; "Solana" is allowed)
and the substring "WattCoin" (case-insensitive; covers handles and URLs).

Also: "wattcoin2026" (case-insensitive) must not appear in llms.txt, llms-full.txt
or any application/ld+json block of a scanned HTML page.

Usage: check_meta_tags.py [root_dir]   (default: repo root)
Exit 1 on any violation, 0 otherwise.
"""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

FORBIDDEN = [
    ("SOL", re.compile(r"\bSOL\b", re.IGNORECASE)),
    ("WattCoin", re.compile(r"wattcoin", re.IGNORECASE)),
]
SKIP_DIRS = {".git", "node_modules"}
HANDLE = re.compile(r"wattcoin2026", re.IGNORECASE)
HANDLE_TEXT_FILES = ("llms.txt", "llms-full.txt")
LDJSON = re.compile(
    r"<script\b[^>]*\btype\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []  # (line, label, text)
        self._title = None  # (line, [chunks])

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._title = (self.getpos()[0], [])
        elif tag == "meta":
            self._meta(attrs)

    handle_startendtag = handle_starttag

    def _meta(self, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        keys = [a.get("name", ""), a.get("property", "")]
        for key in keys:
            k = key.strip().lower()
            if k == "description" or k.startswith(("og:", "twitter:")):
                self.items.append((self.getpos()[0], 'meta ' + key.strip(), a.get("content", "")))
                return

    def handle_data(self, data):
        if self._title is not None:
            self._title[1].append(data)

    def handle_endtag(self, tag):
        if tag == "title" and self._title is not None:
            line, chunks = self._title
            self.items.append((line, "title", "".join(chunks)))
            self._title = None


def check_html(html):
    """Return a list of (line, label, term, text) violations for an HTML string."""
    p = _Collector()
    p.feed(html)
    p.close()
    out = []
    for line, label, text in p.items:
        for term, rx in FORBIDDEN:
            if rx.search(text):
                out.append((line, label, term, " ".join(text.split())))
    return out


def check_handle_text(text):
    """Return a list of line numbers where the banned handle appears in plain text."""
    return [i for i, ln in enumerate(text.splitlines(), 1) if HANDLE.search(ln)]


def check_ldjson_handle(html):
    """Return line numbers of banned-handle hits inside application/ld+json blocks."""
    out = []
    for m in LDJSON.finditer(html):
        start = html.count("\n", 0, m.start(1)) + 1
        out += [start + n - 1 for n in check_handle_text(m.group(1))]
    return out


def main(argv):
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    count = 0
    for path in sorted(root.rglob("*.html")):
        if SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        for line, label, term, text in check_html(path.read_text(encoding="utf-8", errors="replace")):
            print(f"{path.relative_to(root)}:{line}: {label} contains {term}: {text!r}")
            count += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in check_ldjson_handle(text):
            print(f"{path.relative_to(root)}:{line}: ld+json contains wattcoin2026")
            count += 1
    for name in HANDLE_TEXT_FILES:
        f = root / name
        if f.is_file():
            for line in check_handle_text(f.read_text(encoding="utf-8", errors="replace")):
                print(f"{name}:{line}: contains wattcoin2026")
                count += 1
    if count:
        print(f"FAIL: {count} violation(s)")
        return 1
    print("OK: no forbidden terms in title/description/og:*/twitter:* tags")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
