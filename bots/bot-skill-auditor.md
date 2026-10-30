# Bot & Skill Security Auditor — Prompt Injection Scan

Canonical page: https://botappstore.com/bots/bot-skill-auditor.html

Bot & Skill Security Auditor — Line-Cited 5-Bucket Static Inspector for AI Skills & Repos

AI skill security auditor for Grok Bot ($29). Line-cited 5-bucket static code & SKILL.md inspector for prompt injection, secret reads, webhooks & escalation.

## When to use

Use when an agent needs a static security review of a third-party skill or bot before running it.

## Price

- SKU: `bot-skill-auditor`
- Price: $29 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "bot-skill-auditor", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### Does the Bot & Skill Security Auditor execute the code it inspects?

Never. It is a strictly static, read-only inspector. Target files are normalized and wrapped in random > tags by /workspace/.wattcoin_audit_prep.py (chmod 500) and never executed.

### Why doesn't the report give a 'Safe' or '100/100 Pass' badge?

Static analysis can only verify what is present in the files at audit time. It cannot see dynamically fetched remote payloads whose server responses change later, delayed API prompt injections, or off-file maintainer compromise. Every report prepends SCOPE_NOTE.txt and cites exact line numbers instead of issuing false safety guarantees.

### Can I use my $29 license across both BotAppStore and CoinLoader?

Yes. The SKU identifier (bot-skill-auditor) and WattCoin Registry license endpoints are shared across both storefronts, and every wcl_... license includes up to 3 workspace-reset rebinds.
