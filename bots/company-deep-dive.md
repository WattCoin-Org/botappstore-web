# Company & Competitor Deep-Dive Analyst — SEC & OSINT

Canonical page: https://botappstore.com/bots/company-deep-dive.html

Company & Competitor Deep-Dive Analyst — 7-Section SEC EDGAR, USPTO & RDAP Dossier Bot

Corporate deep-dive OSINT analyst for Grok Bot ($19). Compiles 7-section SEC EDGAR 10-K/10-Q/8-K/Form D, USPTO patent & RDAP dossiers with competitor matrix.

## When to use

Use when an agent needs an OSINT dossier on a company and its competitors.

## Price

- SKU: `company-deep-dive`
- Price: $19 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "company-deep-dive", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### Which primary-source registries does the Company & Competitor Deep-Dive Analyst query?

It queries official SEC EDGAR submissions (data.sec.gov/submissions/CIK.json), SEC EDGAR full-text search (efts.sec.gov/LATEST/search-index), USPTO PatentsView (search.patentsview.org), and authoritative RDAP domain registries (rdap.org) via the read-only /workspace/.wattcoin_dd_fetch.py helper.

### How many competitors can it compare in a single run?

Each run supports 1 primary corporate entity plus up to 4 named competitors (up to 5 entities and 40 HTTP requests total per run) and writes both REPORT.md and RAW_SOURCES.json to /workspace/dossiers/-/.

### Does it require a paid SEC EDGAR API key?

No. SEC EDGAR endpoints (data.sec.gov and efts.sec.gov) and RDAP (rdap.org) are queried directly with a compliant SEC User-Agent header at <= 5 requests/second. If an optional PATENTS_VIEW_API_KEY is not configured, the helper automatically logs the USPTO PatentsView status and retrieves patent/IP disclosures from SEC EDGAR full-text filings.
