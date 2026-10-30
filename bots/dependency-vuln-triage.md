# Dependency Vulnerability Triage Grok Bot — KEV & EPSS

Canonical page: https://botappstore.com/bots/dependency-vuln-triage.html

Dependency Vulnerability Triage — Fix what is being exploited first.

Lockfile vulnerability triage for Grok Bot ($29): OSV.dev advisories enriched with FIRST EPSS and CISA KEV, sorted into four action buckets with fix versions.

## When to use

Use when an agent needs to prioritise known vulnerabilities in a project's dependencies.

## Price

- SKU: `dependency-vuln-triage`
- Price: $29 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "dependency-vuln-triage", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### How do I prioritize which CVEs to patch first using CISA KEV and FIRST EPSS exploit probability scores?

Triage by exploitation evidence instead of severity order. The bot batch-queries OSV.dev, merges GHSA, PYSEC and CVE aliases into one advisory, then enriches each with FIRST EPSS v1 scores and CISA KEV listings. Advisories land in four buckets: KEV_ACTIVE_EXPLOIT (listed in CISA KEV, patch first), HIGH_EPSS_URGENT (EPSS at or above the default 0.10 threshold), PATCH_AVAILABLE (a fixed version exists) and LOW_PRIORITY_MONITOR. Each package also gets the lowest upgrade version that clears its advisories. EPSS is a probability estimate, not a verdict.

### Which dependency lockfiles (package-lock.json, yarn.lock, Pipfile.lock, poetry.lock, requirements.txt, Cargo.lock, go.sum) are supported?

It natively parses 7 ecosystems out of the box: npm package-lock.json (v1-v3), yarn.lock, Python Pipfile.lock, poetry.lock, pinned requirements.txt, Rust Cargo.lock and Go go.sum. Output is Markdown or JSON, with exit code 1 on findings at --fail-on and 11 when a data source is down so an outage never looks clean.

### Does the Dependency Vulnerability Triage bot upload proprietary source code or require paid scanner API keys?

No source code is uploaded. Only package names and versions leave your machine, sent to the public OSV.dev, FIRST EPSS and CISA KEV data sources, and the bot runs locally in your Grok Bot /workspace. It does not depend on a paid scanner subscription. The license is a one-time $29 purchase with up to 3 workspace-reset rebinds and no recurring fee.

### What safety and refusal boundaries does the Dependency Vulnerability Triage bot enforce?

It does not scan your source code or judge whether a vulnerable function is reachable, and it treats EPSS as a probability rather than a verdict. It never installs, upgrades or edits your project, so every fix stays your decision. It never claims a project is secure, and when a data source is unreachable it exits with code 11 instead of reporting a clean result.
