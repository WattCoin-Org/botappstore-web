# SEC EDGAR Supply Chain & Counterparty Mapper

Canonical page: https://botappstore.com/bots/sec-supply-chain-mapper.html

SEC EDGAR Supply Chain & Counterparty Mapper — Cited supplier, customer & foundry maps straight from SEC filings.

Map disclosed suppliers, >=10% major customers, Inline-XBRL concentration risk, YoY 10-K diffs, and 13D/13G stakes from primary SEC EDGAR filings.

## When to use

Use when an agent needs counterparty and supply-chain exposure for a public company.

## Price

- SKU: `sec-supply-chain-mapper`
- Price: $29 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "sec-supply-chain-mapper", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### How does the SEC EDGAR Supply Chain Mapper extract verified customers, suppliers, and foundry partners without hallucinating?

It never infers a relationship. The mapper reads primary SEC EDGAR filings only (10-K, 10-Q, 8-K, Exhibit 21, Schedule 13D/13G) and emits an edge only when a sentence in the filing text names the counterparty and the relationship. Every edge is typed (supplier, customer, foundry_compute, strategic_partner, subsidiary or equity_stake) and carries a verbatim quote of at most 300 characters plus the accession number and a direct sec.gov Archive URL. If a filing says 'Customer A' without a name, it is reported as an anonymous customer with its disclosed percentage, never matched to a guessed company. Output ends with an audit_sha256 so a run can be reproduced and compared.

### How does the Inline-XBRL Disclosed Concentration Scorer (CR-01 to CR-04) and Exhibit 21 subsidiary separation work?

The scorer reads Inline-XBRL facts from the filing itself (for example us-gaap:ConcentrationRiskPercentage with the srt:MajorCustomersAxis and srt:ProductOrServiceAxis dimensions) instead of scraping prose. CR-01 flags a single customer at or above the 10% disclosure threshold, CR-02 flags the top disclosed customers combined, CR-03 flags sole-source or single-source supplier disclosures, and CR-04 flags receivables concentration. Exhibit 21 is parsed separately, so legal subsidiaries are labelled subsidiary and are never counted as customers, suppliers or partners.

### What do the YoY 10-K Counterparty Diff and Schedule 13D/13G Equity Stake (EQ-01) checks detect?

The YoY diff compares the counterparty set and disclosed percentages between the two most recent 10-Ks and labels each change ADDED, REMOVED or PCT_CHANGED, so a new major customer, a dropped supplier or a shift in revenue share is visible with both accession numbers. EQ-01 reads Schedule 13D and 13G filings to report equity stakes of 5% or more between the target and another registrant, with the filer, the percentage, the filing date and the accession number. A stake is reported as a fact from the filing; it is not interpreted as control or a commercial relationship.

### What SSRF, rate-limit, and refusal guardrails does sec_supply_chain_mapper.py enforce?

The CLI is HTTPS-only and allow-lists sec.gov hosts (data.sec.gov, www.sec.gov, efts.sec.gov), resolves and pins DNS before connecting, and refuses redirects, private or link-local addresses and any non-allow-listed URL, which blocks SSRF. It identifies itself with a compliant SEC User-Agent and stays at or below 5 requests per second with a hard request and byte cap per run. It never outputs buy, sell or hold ratings, price targets or forecasts, never states that a relationship exists unless a filing quote supports it, and prepends a SCOPE_NOTE that private contracts and undisclosed counterparties are outside primary filings.
