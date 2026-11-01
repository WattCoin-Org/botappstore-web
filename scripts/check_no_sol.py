#!/usr/bin/env python3
"""Fail if served text carries the retired token / crypto-payment wording.

Scanned: *.html, *.md (root and bots/), *.txt, catalog.json, app.js, styles.css,
middleware.js, vercel.json, sitemap.xml, favicon.svg (scripts/ is not scanned: the
tests quote the forbidden words on purpose).

Brand rules. Before matching, <code>...</code> and <pre>...</pre> contents (and, in .md / .txt,
fenced blocks and `inline spans`) are blanked for the word SOL and the word Solana.
Forbidden: the case-sensitive word SOL, the word Solana, and (case-insensitive, EVERYWHERE,
including inside <code> / <pre> / backticks) the substring "wattcoin": `.wattcoin_*` helper
and env file names, `WATTCOIN_*` env var names, `wattcoin.org` URLs, the handle @WattCoin2026.

Functional exceptions for "wattcoin". Only these exact tokens, only on these exact lines, are
removed before the check (they are what checkout / activation / agent purchasing call, so
changing them needs a backend change). Every other occurrence, in any file, fails:
  - vercel.json: the `wattcoin-production-*` host (the /runtime rewrite destination).
  - catalog.json: a `https://www.wattcoin.org/api/v1/agents/license/...` URL on a `"url"` or
    `"license_lookup"` key line.
  - app.js: the `https://www.wattcoin.org/api` URL on the `REGISTRY_API_BASE =` line, and the
    `/workspace/.wattcoin_license_check.py` helper path on activation-command lines (those
    containing `--claim-stdin`).
  - llms.txt / llms-full.txt: a `https://www.wattcoin.org/api/v1/agents/license...` URL on an
    unindented numbered step line (`1. ...` at column 0, the "how an agent buys" steps).
  - bots/<slug>.md: a `https://www.wattcoin.org/api/v1/agents/license/...` URL on the two
    generated checkout bullets ("- Card, Apple Pay or Google Pay (default): ..." and
    "- License lookup after payment: ..."), which build_markdown.py derives from catalog.json.

Strict rules run on the text with only the allowlist removed (code / pre / backtick
spans are NOT blanked, so sample output is checked too): WATT amounts ("100 WATT",
"+25 WATT"), "WATT/call", the bare token WATT (case-sensitive), Solana in any case
(also CSS names such as --accent-solana), lamport, wallet, on-chain, phantom (but
"phantom dependency" is fine) and, in code files only (.css .js .json .xml .svg), a
standalone lowercase/uppercase `sol` identifier such as `.sol-unit`.

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
LICENSE_URL = re.compile(r"https://www\.wattcoin\.org/api/v1/agents/license[^\s\"'`<>)]*")
FUNCTIONAL = [
    # (path regex, line regex, token that may appear on that line)
    (re.compile(r"^vercel\.json$"), re.compile(r""), re.compile(r"wattcoin-production[A-Za-z0-9.-]*")),
    (re.compile(r"^catalog\.json$"), re.compile(r'^\s*"(?:url|license_lookup)"\s*:'), LICENSE_URL),
    (re.compile(r"^app\.js$"), re.compile(r"REGISTRY_API_BASE\s*="), re.compile(r"https://www\.wattcoin\.org/api\b")),
    (re.compile(r"^app\.js$"), re.compile(r"--claim-stdin"), re.compile(r"/workspace/\.wattcoin_license_check\.py")),
    (re.compile(r"^llms(?:-full)?\.txt$"), re.compile(r"^\d+\. "), LICENSE_URL),
    (re.compile(r"^bots/[^/]+\.md$"),
     re.compile(r"^- (?:Card, Apple Pay or Google Pay \(default\)|License lookup after payment): "), LICENSE_URL),
]
CODE_KINDS = {".css", ".js", ".json", ".xml", ".svg"}
STRICT = [
    ("WATT amount", re.compile(r"(?<![\w$])\+?\d[\d,]*\s*WATT\b")),
    ("WATT/call", re.compile(r"\bWATT\s*/")),
    ("WATT token", re.compile(r"(?<![\w$])WATT\b")),
    ("Solana", re.compile(r"solana", re.IGNORECASE)),
    ("lamport", re.compile(r"lamport", re.IGNORECASE)),
    ("phantom", re.compile(r"\bphantom\b(?!\s+dependenc)", re.IGNORECASE)),
    ("wallet", re.compile(r"wallet", re.IGNORECASE)),
    ("on-chain", re.compile(r"\bon[- ]chain\b", re.IGNORECASE)),
]
SOL_IDENT = ("sol identifier", re.compile(r"(?<![A-Za-z0-9])sol(?![A-Za-z0-9])", re.IGNORECASE))
FORBIDDEN = [
    ("SOL", re.compile(r"\bSOL\b")),
    ("Solana", re.compile(r"\bSolana\b")),
]
# Checked on the text with only the functional exceptions removed (code / pre / spans NOT blanked).
WATT_RULES = [
    ("@WattCoin2026", re.compile(r"@WattCoin2026", re.IGNORECASE)),
    ("WattCoin", re.compile(r"wattcoin", re.IGNORECASE)),
]


def _blank(m):
    """Replace a match with spaces, keeping newlines so line numbers survive."""
    return re.sub(r"[^\n]", " ", m.group(0))


def _strip_functional(text, path):
    """Blank the functional exceptions that apply to `path` (repo-relative, posix) line by line."""
    out = []
    for line in text.split("\n"):
        for prx, lrx, token in FUNCTIONAL:
            if prx.search(path) and lrx.search(line):
                line = token.sub(_blank, line)
        out.append(line)
    return "\n".join(out)


def check_text(text, markdown=False, code=False, path=""):
    """Return [(line, label, excerpt)] for each forbidden term outside the functional exceptions.

    markdown=True (for .md / .txt) also treats fenced blocks and `inline spans` as code
    for the SOL / Solana brand rules. code=True (.css .js .json .xml .svg) adds the `sol`
    identifier rule. path is the repo-relative posix path; it selects the FUNCTIONAL exceptions
    (none when empty). "wattcoin" is never blanked for code / pre / spans.
    """
    strict_text = _strip_functional(text, path)
    text = strict_text
    text = BLOCK.sub(_blank, text)
    if markdown:
        text = SPAN.sub(_blank, FENCE.sub(_blank, text))
    strict_rules = STRICT + [SOL_IDENT] if code else STRICT
    brand = text.splitlines()
    strict = strict_text.splitlines()
    found = []
    for n in range(1, len(strict) + 1):
        hit = None
        for label, rx in WATT_RULES:
            m = rx.search(strict[n - 1])
            if m:
                hit = (label, strict[n - 1], m)
                break
        if hit is None:
            for label, rx in FORBIDDEN:
                m = rx.search(brand[n - 1]) if n <= len(brand) else None
                if m:
                    hit = (label, brand[n - 1], m)
                    break
        if hit is None:
            for label, rx in strict_rules:
                m = rx.search(strict[n - 1])
                if m:
                    hit = (label, strict[n - 1], m)
                    break
        if hit:
            label, line, m = hit
            found.append((n, label, line[max(0, m.start() - 30):m.end() + 30].strip()))
    return found


def targets(root):
    files = set()
    for pat in ("*.html", "*.md", "*.txt", "catalog.json", "app.js", "styles.css",
                "middleware.js", "vercel.json", "sitemap.xml", "favicon.svg"):
        files.update(root.rglob(pat))
    return sorted(p for p in files if not (SKIP_DIRS | {"scripts"}) & set(p.relative_to(root).parts))


def main(argv):
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    files = targets(root)
    bad = 0
    for p in files:
        for n, label, excerpt in check_text(
            p.read_text(encoding="utf-8"), p.suffix in (".md", ".txt"), p.suffix in CODE_KINDS,
            p.relative_to(root).as_posix(),
        ):
            bad += 1
            print(f"FAIL: {p.relative_to(root)}:{n}: {label}: ...{excerpt}...")
    if bad:
        print(f"{bad} violation(s) in {len(files)} files")
        return 1
    print(f"OK: no SOL / Solana / WattCoin / WATT / wallet wording in {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
