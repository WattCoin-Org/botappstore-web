# Changelog

## 2026-11-01 — After You Buy strip on all 15 listing pages

Approved by owner AIguy. Status: committed to main locally, not yet pushed or deployed. Page commit `39fa86d`.

### Changes (`bots/*.html` only, 15 pages)
- New "After You Buy" section directly above "Licensing & Setup" on 12 pages, and above "Checkout" on `fai-results-reconciler`, `mill-cert-matcher` and `rfq-flowdown-triage`, which have no Licensing section. Steps: check out, add the bot to the Grok Bot sidebar, run setup with the activation prompt, first report. Payment step reads "Pay by card, Apple Pay or Google Pay".
- Steps 3 and 4 are written per bot from its setup skill (runtime download vs read-only helpers). The agent-council first step lists the seats it can field and asks before convening a live council.
- `web-text-extractor` is free, so it gets "After You Install": no checkout, no license code, 10 free text scrapes per UTC day.
- Scoped `<style>` block using existing page variables. No JS, checkout logic, prices, SKUs, catalog, llms files or `.md` twins were touched (`build_markdown.py` does not read this section).

### Checks
- Playwright at 1280 and 390 px on all 15 pages (local server, external requests blocked): strip present once, directly before `#licensing-flow` or `#checkout`, strip does not overflow, text at least body size. The 1280 px header button overflow that already exists on the pages is unchanged.
- Greps on the strip blocks: none of safe, guarantee, profit, human review, Stripe, MoonPay, or time units, and none of the terms `check_no_sol.py` rejects.
- Checkout modal and mocked receipt on `company-deep-dive` and `print-farm-ops`: names, price and steps follow the clicked bot after `app.js` runs; no Auditor text.
- Guards: check_analytics OK (19), check_catalog OK (15), check_nav OK (15), check_meta_tags OK, check_no_sol OK (50 files), build_markdown --check OK (20), pytest 34 passed, test_middleware passed.

### Commit
- 39fa86d (15 listing pages)

## 2026-10-31 — Brand sweep: leftover crypto wording removed

Approved by owner AIguy. Status: merged to main and live. Commits: `2bf7409` (content + CSS cleanup), `5619f04` (check_no_sol strengthening + tests), merge `7db79e2`. Production deploy `dpl_JD2yCGyvMJE7X8c7ULgevY2SexmJ` READY. Rollback deploy (previous production, `75d64ff`): `dpl_DhsTX1zbVjZevgB3g3pcbAcumTJ6`.

### Removed / renamed
- `styles.css`: the crypto-named CSS variable renamed to the existing `--accent-emerald` (same colour), the old checkout comment fixed, dead crypto-checkout selectors deleted.
- Token amounts and crypto wording removed from marketing copy, JSON-LD and meta across `app.js`, `index.html`, 14 bot pages, the `catalog.json` description, `llms.txt` and `llms-full.txt` (md twin regenerated).
- Pointers to the other site's `llms.txt` dropped; descriptive User-Agent mentions neutralised.
- `index.html` blurbs and FAQ now say "the license API" / "a 0600 env file" instead of file names.

### Kept on purpose (functional)
- License API endpoint URLs on `www.wattcoin.org/api` (checkout base in `app.js`, catalog `checkout` / `license_lookup`, agent buy steps). No BotAppStore-branded API host exists.
- `.wattcoin_*` helper/env file names and `WATTCOIN_*` env var names that the published templates create and that buyers copy in install commands.
- The `/runtime` rewrite to the Railway host in `vercel.json`.
- Verbatim sample-report quotes of the deep-dive helper's User-Agent.
- Changing these needs a template/backend change and an owner decision. A brand-green `rgba(20,241,149)` colour left as is.

### Guard changes
- `check_no_sol.py` now also scans `styles.css`, `middleware.js`, `vercel.json`, `sitemap.xml`, `robots.txt`, `favicon.svg` and all `*.txt`, and flags token amounts and crypto-related terms (see `scripts/check_no_sol.py`; functional allowlist unchanged).

### Checks
- check_nav OK (15), check_analytics OK (19), check_catalog OK (15), check_no_sol OK (50 files, was 44), check_meta_tags OK, build_markdown --check OK (20).
- pytest `scripts/`: 34 passed (before 28). `test_middleware.mjs` all passed.
- Live: all 45 served pages/files return 200. Live `app.js`, `styles.css`, `llms.txt`, `catalog.json` sha256 match main. Live grep finds 0 hits for every term the checker forbids.
- 390px mobile check on home, FAI and web-text-extractor pages: scrollWidth 390, buy buttons visible (16 and 3), checkout modal opens, no page errors.

## 2026-10-31 — Mobile optimization (CSS only)

Approved by owner AIguy. Status: merged to main and live in production. Merge commit `c8bed60` (`feat/mobile-optimization` @ `5827aa5` into main). Production deploy `dpl_B1s2frmYjHF5ABuh9j1NxjijXq2B` READY. Rollback deploy (previous production, `ad57a76`): `dpl_3n5GsyA3QUy7qDY7KJEBqfGcuL2k`.

### Changes (`styles.css` only)
- Removed the root causes of horizontal overflow at 360/390/414 px: status bar and ticker no longer force one line, hero card, architecture cards, FAQ list and nav button group can shrink and wrap, grids collapse to one column, long inline code wraps, code blocks and tables scroll inside their own box.
- `body{overflow-x:hidden}` is kept; a base `img,svg,video{max-width:100%}` rule was added.
- Tap targets on phones (max-width 680px): buttons, filter pills, copy buttons, modal close, nav links, menu items, footer links and card titles are at least 44px tall. Inline links inside paragraphs are unchanged.
- Checkout modal: fits inside the viewport at 360 px (8px margin, scrolls inside, tighter padding, full-width action buttons, long values wrap, inputs 44px tall and 16px text).
- Type floor of 12px for small labels and badges on phones.
- Buy and install buttons share one row, full width below 480px.
- No HTML, JS, checkout logic, prices, SKUs, analytics, nav labels or links, sitemap, meta tags, catalog, llms files or `.md` twins were touched.
- Follow-up: header and bot-page buttons can no longer clip their text at 360/390/414 px. Buy and install buttons now stack one per row (full width, at least 44px tall) at 480px and below, and button text wraps and centers instead of staying on one line at 680px and below. Verified on every page, including the checkout modal (0 clipped or out-of-viewport buttons, no horizontal overflow).

### Checks (local static server of the branch, 360/390/414 px)
- scrollWidth equals the viewport width on every audited page (before: 619 px).
- Checkout modal: before 611 px wide, clipped; after fully inside the viewport at all widths.
- Remaining small tap targets: inline paragraph links only (80-83 per home page; now 0-5 per page).
- Guards and tests: all pass.

### Production
- Guards pass on main: check_nav 15, check_analytics 19, check_catalog 15, check_no_sol (44 files), check_meta_tags, pytest scripts/ 28 passed, test_middleware.mjs.
- Live check (playwright, 360/390/414 px, home + `/bots/fai-results-reconciler.html`): document and body scrollWidth equal the viewport width on all 6 runs, buy/checkout buttons render (17 on home, 3 on bot page), 0 clipped, minimum height 44px.
- Live `styles.css` sha256 matches repo main.

### Commit
- 1aedfdc (styles.css)
