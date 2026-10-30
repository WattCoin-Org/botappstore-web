# RFQ Flowdown Triage — Quality Clause Screen

Canonical page: https://botappstore.com/bots/rfq-flowdown-triage.html

RFQ Flowdown Triage — Quality Clause & Export-Control Screen. Know what the RFQ is asking before you quote.

Screening aid that triages RFQ quality and export-control flowdown clauses against a supplier capability profile and flags issues for human review.

## When to use

Use when an agent needs to triage the flowdown clauses of an incoming RFQ.

## Price

- SKU: `rfq-flowdown-triage`
- Price: $39 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "rfq-flowdown-triage", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### Does it tell me whether to bid?

No. It flags clauses for a human. CLARIFY means a person (or the buyer) decides; NO-BID-RISK means flagged for human review because your profile shows a gap. Screening aid that flags issues for human review. Not a legal, export-control, or compliance determination.

### Does it check ITAR, DFARS or CMMC compliance?

No. It detects that clause text is present and compares it with the profile you wrote. It does not say a clause applies and makes no compliance or export-control determination.

### What is the supplier profile?

A JSON file describing your shop: certifications, processes, registrations and the certs you provide. Every key is optional; a blank key is UNKNOWN, never a pass. An expired certification date is also UNKNOWN.

### What about scanned RFQs?

There is no OCR. A PDF without a text layer is a CLARIFY. Text-layer PDFs are read with a pinned, hash-verified open-source PDF library; plain-text input needs no dependency.

### What leaves my machine?

Nothing, with --offline. Otherwise only an audit hash, a hash of the RFQ number and a one-line metric (for example CLARIFY · 3 of 11 clauses need buyer input) go to the receipt-signing endpoint. RFQ text, clause text, customer names and file names are never sent.

### How is it licensed?

One-time $39 USD license with up to 3 workspace-reset rebinds and no subscription. Pay by card, Apple Pay or Google Pay (default), or with SOL. The license unlocks both the 1-click Grok Bot template and the standalone Python 3 CLI.
