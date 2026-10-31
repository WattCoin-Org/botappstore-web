#!/usr/bin/env python3
"""Assert every HTML page loads Vercel Analytics exactly once, in <head>."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = "/_vercel/insights/script.js"
STUB = "window.va = window.va ||"


def check_html(html):
    """Return a list of problem strings for one page's HTML."""
    problems = []
    if html.count(SRC) != 1:
        problems.append(f"{SRC} appears {html.count(SRC)} times (want 1)")
    if html.count(STUB) != 1:
        problems.append(f"window.va stub appears {html.count(STUB)} times (want 1)")
    head_end = html.find("</head>")
    pos = html.find(SRC)
    if pos != -1 and (head_end == -1 or pos > head_end):
        problems.append("insights script is not inside <head>")
    return problems


def main():
    pages = sorted(ROOT.glob("*.html")) + sorted((ROOT / "bots").glob("*.html"))
    errors = []
    for page in pages:
        for p in check_html(page.read_text(encoding="utf-8")):
            errors.append(f"{page.relative_to(ROOT)}: {p}")
    for e in errors:
        print("FAIL:", e)
    if errors:
        return 1
    print(f"OK: {len(pages)} pages load Vercel Analytics once in <head>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
