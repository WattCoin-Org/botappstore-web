# Mill Cert Matcher — Mill Test Report (MTR) Check

Canonical page: https://botappstore.com/bots/mill-cert-matcher.html

Mill Cert Matcher — Receiving Inspection Cert & PO Cross-Check. Find the mismatch before the material hits the floor.

Screening aid that cross-checks a mill test certificate against a purchase order and drawing callout and flags issues for human review.

## When to use

Use for receiving inspection of metal stock against a PO.

## Price

- SKU: `mill-cert-matcher`
- Price: $39 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "mill-cert-matcher", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### Does it approve or certify material?

No. It flags issues for a human. HOLD means a person decides; REJECT means flagged for human review as mismatched or out of limits. Screening aid that flags issues for human review. Not a legal, compliance, or material-certification determination.

### Does it check DFARS or ITAR compliance?

No. If you turn on melt-country screening you supply the qualifying-country list; the tool only reports whether the cert's melt country is on that list. It makes no compliance determination.

### Are the spec limits built in?

Only a few illustrative example entries, labelled as such on every card. Supply your own spec-limits library from the controlling spec revision. A spec, alloy or temper missing from your library is a HOLD, never a PASS.

### What about scanned certificates?

There is no OCR. A PDF without a text layer is a HOLD. Text-layer PDFs are read with a pinned, hash-verified open-source PDF library; plain-text certs need no dependency.

### What leaves my machine?

Nothing, with --offline. Otherwise only an audit hash, a hash of the PO number and a one-line metric (for example HOLD · 2 of 9 lines flagged) go to the receipt-signing endpoint. Certificate text, heat numbers, supplier names and file names are never sent.

### How is it licensed?

One-time $39 USD license with up to 3 workspace-reset rebinds and no subscription. Pay by card, Apple Pay or Google Pay (default), or with SOL. The license unlocks both the 1-click Grok Bot template and the standalone Python 3 CLI.
