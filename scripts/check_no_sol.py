#!/usr/bin/env python3
"""Fail if visible or crawler-visible text mentions SOL, Solana or WattCoin.

Scanned: *.html, *.md (root and bots/), llms*.txt, catalog.json, app.js.
Before matching, <code>...</code> and <pre>...</pre> contents (and, in .md / .txt,
fenced blocks and `inline spans`) are blanked and a
short allowlist of functional identifiers is removed: `.wattcoin_*` runtime file
names, `WATTCOIN_*` environment variable names, `wattcoin.org/api` URLs, the
`wattcoin-production-*` Railway host and `wattcoin.workers.dev`.
Forbidden: the case-sensitive word SOL, the word Solana, the substring WattCoin
(case-insensitive brand) and the handle @WattCoin2026.

Usage: check_no_sol.py [root_dir]   (default: repo root)
Exit 1 on any violation (file:line listed), 0 otherwise.
"""
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "__pycache__"}
BLOCK = re.compile(r"<(code|pre)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
# .md / .txt equivalents of <pre> and <code>: fenced blocks and single-line `spans`.
FENCE = re.compile(r"^```.*?^```[^\n]*$", re.DOTALL | re.MULTILINE)
SPAN = re.compile(r"`[^`\n]*`")
ALLOW = re.compile(
    r"\.wattcoin_[A-Za-z0-9_.*-]*"
    r"|wattcoin\.org/api[^\s\"'`<>)]*"
    r"|wattcoin-production[A-Za-z0-9.-]*"
    r"|wattcoin\.workers\.dev"
    r"|(?-i:\bWATTCOIN_[A-Z0-9_]+\b)",
    re.IGNORECASE,
)
FORBIDDEN = [
    ("SOL", re.compile(r"\bSOL\b")),
    ("Solana", re.compile(r"\bSolana\b")),
    ("@WattCoin2026", re.compile(r"@WattCoin2026", re.IGNORECASE)),
    ("WattCoin", re.compile(r"wattcoin", re.IGNORECASE)),
]


def _blank(m):
    """Replace a match with spaces, keeping newlines so line numbers survive."""
    return re.sub(r"[^\n]", " ", m.group(0))


def check_text(text, markdown=False):
    """Return [(line, label, excerpt)] for each forbidden term outside the allowlist.

    markdown=True (for .md / .txt) also treats fenced blocks and `inline spans` as code.
    """
    text = BLOCK.sub(_blank, text)
    if markdown:
        text = SPAN.sub(_blank, FENCE.sub(_blank, text))
    text = ALLOW.sub(_blank, text)
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for label, rx in FORBIDDEN:
            m = rx.search(line)
            if m:
                found.append((n, label, line[max(0, m.start() - 30):m.end() + 30].strip()))
                break
    return found


def targets(root):
    files = set()
    for pat in ("*.html", "*.md", "llms*.txt", "catalog.json", "app.js"):
        files.update(root.rglob(pat))
    return sorted(p for p in files if not SKIP_DIRS & set(p.relative_to(root).parts))


def main(argv):
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    files = targets(root)
    bad = 0
    for p in files:
        for n, label, excerpt in check_text(p.read_text(encoding="utf-8"), p.suffix in (".md", ".txt")):
            bad += 1
            print(f"FAIL: {p.relative_to(root)}:{n}: {label}: ...{excerpt}...")
    if bad:
        print(f"{bad} violation(s) in {len(files)} files")
        return 1
    print(f"OK: no SOL / Solana / WattCoin wording in {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
