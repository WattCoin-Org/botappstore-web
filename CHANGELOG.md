# Changelog

## 2026-10-31 — Mobile optimization (CSS only)

Approved by owner AIguy. Status: on branch `feat/mobile-optimization`, preview only, not merged, not deployed.

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

### Commit
- 1aedfdc (styles.css)
