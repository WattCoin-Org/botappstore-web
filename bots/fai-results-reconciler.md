# FAI Results Reconciler — Balloon List Screen

Canonical page: https://botappstore.com/bots/fai-results-reconciler.html

FAI Results Reconciler — Balloon List vs Inspection Results Screen. Catch the mismatches before the package goes out.

Screening aid that matches an FAI balloon list to inspection results and flags issues for human review. Not a quality sign-off or AS9102 approval.

## When to use

Use when an agent needs to reconcile a first-article balloon list with inspection results.

## Price

- SKU: `fai-results-reconciler`
- Price: $39 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "fai-results-reconciler", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### Does it approve my first article?

No. It flags lines for a person. PASS means no flags were found by the checks above, not that the article is accepted. Screening aid that flags issues for human review. Not a quality sign-off or AS9102 approval.

### What happens when units differ?

If the files declare different units and at least three characteristics agree on a 25.4 ratio, results are scaled, the factor is shown and the verdict is REVIEW. Otherwise the affected characteristics are not evaluated and the line is a FAIL that means "cannot be evaluated", not "out of tolerance".

### How does it match characteristics to results?

By balloon number after normalising case, spaces and leading zeros; 12 never matches 12a. A match by feature name plus nominal is used only when it is unique and is always a REVIEW. Ambiguous matches are reported as duplicates or unmatched, never guessed.

### Which report formats work?

Generic CSV or XLSX, Calypso-style CSV and PC-DMIS text reports, plus a column map for unusual exports. Anything it cannot read is listed on the card.

### What leaves my machine?

Nothing, with --offline. Otherwise only an audit hash, a hash of the part number and a one-line metric (for example FAIL · 2 out of tolerance, 1 missing of 48 characteristics) go to the receipt-signing endpoint. Measurements, descriptions, part numbers and file names are never sent.

### How is it licensed?

One-time $39 USD license with up to 3 workspace-reset rebinds and no subscription. Pay by card, Apple Pay or Google Pay (default), or with SOL. The license unlocks both the 1-click Grok Bot template and the standalone Python 3 CLI.
