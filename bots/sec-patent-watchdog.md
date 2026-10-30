# SEC & Patent Watchdog Digest — 8-K & USPTO Monitor

Canonical page: https://botappstore.com/bots/sec-patent-watchdog.html

SEC & Patent Watchdog Digest — Zero-Noise SEC EDGAR & USPTO Delta Monitor

SEC EDGAR 8-K, 10-K, 10-Q, Form 4, 13F-HR & USPTO patent watchdog for Grok Bot ($29). Tracks up to 10 tickers/CIKs with stateful diffing & zero noise.

## When to use

Use when an agent needs a digest of new SEC filings and patent activity for a watchlist.

## Price

- SKU: `sec-patent-watchdog`
- Price: $29 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "sec-patent-watchdog", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### What happens on a scheduled run when none of my watched companies file anything new?

Nothing is sent. The watchdog compares current SEC EDGAR accession numbers and patent records against /workspace/watchlists/state.json; when delta == 0, it updates the last-checked timestamp and suppresses all chat/notification output.

### How many tickers or CIKs can I monitor in a watchlist?

Up to 10 corporate tickers or 10-digit SEC CIKs per watchlist, covering 10-K, 10-Q, 8-K, Form 4, 13F-HR, S-1, DEF 14A, Form D, and patent disclosures.

### How is the SEC & Patent Watchdog different from the Company & Competitor Deep-Dive Analyst?

Company & Competitor Deep-Dive ($19) builds a comprehensive 7-section point-in-time dossier and 5-company comparison matrix on demand. SEC & Patent Watchdog Digest ($29) maintains a persistent state.json baseline for up to 10 tickers/CIKs and runs on a recurring schedule to alert you only to new filings and patent publications.
