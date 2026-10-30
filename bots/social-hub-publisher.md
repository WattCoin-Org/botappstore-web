# Social Hub Publisher Grok Bot — X & Discord Queue

Canonical page: https://botappstore.com/bots/social-hub-publisher.html

Social Hub Publisher — Write once, approve once, publish everywhere.

Config-driven publishing queue for X, Discord webhooks, Bluesky and Telegram for Grok Bot ($29). Gate 1 lint, HMAC approval, Gate 2 verification, free X Web Intents, dry-run by default.

## When to use

Use when an agent needs to queue, lint and approval-gate social posts before publishing.

## Price

- SKU: `social-hub-publisher`
- Price: $29 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "social-hub-publisher", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### How can I automate multi-account X and Discord announcements without paying monthly SaaS fees like Buffer or Postiz?

Social Hub Publisher is a one-time $29 license with no subscription. You queue one announcement and publish it to the X, Discord, Bluesky and Telegram targets defined in a single config.json, with credentials kept in environment variables. X defaults to free 1-click Web Intent links; optional X API mode uses 2026 pay-per-use pricing of $0.015 per plain post and $0.20 per post containing a link. Discord uses webhooks with mentions suppressed. It is Python standard library only and runs as a dry run unless you pass --live.

### How does the 9-rule pre-flight policy linter prevent cross-brand leaks, banned terms, and duplicate posts?

Gate 1 lints every queued item before it can be approved. It checks per-channel length (X counts URLs as 23 characters), a link-domain allowlist, banned terms, hashtag and mention caps, punctuation and duplicate detection. Because the allowlist and banned-terms list live in your config, you can block another brand's domains or names from a queue. Items with Gate 1 errors are refused at approval. The sample session shows a banned-term post and an off-allowlist link both refused.

### How do HMAC-SHA256 approval tokens and 1-click X web intents work together in the Social Hub?

The approve command signs the exact post text with HMAC-SHA256 and an expiry. Gate 2 in the dispatcher refuses any item that was edited after approval, has expired or is unsigned; in the sample, editing approved text produces REFUSED (text changed after approval). For X, the intent command prints an x.com/intent/post URL for an approved item. It is free and you review the text and press Post yourself, so nothing is sent without your click.

### What safety and refusal boundaries does the Social Hub Publisher enforce (no fake engagement or unapproved auto-posting)?

It posts only to accounts you configure and only text you approved. It does not do fake engagement, duplicate-post spam, or bypass the domain allowlist or banned terms. It never prints or stores your credentials in the queue. It sends nothing unless you pass --live, and an ambiguous send is marked unknown_outcome and never auto-retried.
