# Domain, TLS & Email Auth Hygiene Auditor

Canonical page: https://botappstore.com/bots/domain-infra-hygiene.html

Domain, TLS & Email Auth Hygiene Auditor — Find the expiring cert, the spoofable domain and the dangling CNAME first.

Read-only domain posture auditor for Grok Bot ($19 · card): DNS and DNSSEC AD-bit validation, TLS chain, expiry and CAA cross-check, SPF with RFC 7208 10-lookup counting, DMARC, DKIM, MTA-STS, TLS-RPT, HTTPS security headers, dangling CNAMEs and Certificate Transparency, with copy-paste DNS, Nginx, Vercel and Netlify fixes.

## When to use

Use when an agent needs a read-only hygiene check of a domain's DNS, TLS and email authentication.

## Price

- SKU: `domain-infra-hygiene`
- Price: $19 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "domain-infra-hygiene", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### How does the auditor count SPF DNS lookups against the RFC 7208 10-lookup limit?

It parses the apex v=spf1 record and recursively expands every include: and redirect= target, counting each DNS-querying term (include, a, mx, ptr, exists and redirect) exactly as RFC 7208 section 4.6.4 specifies; ip4, ip6 and all cost nothing. More than 10 lookups is SPF_LOOKUP_LIMIT_EXCEEDED (HIGH, receivers return permerror and treat SPF as failed), 8 to 10 is SPF_LOOKUP_NEAR_LIMIT (LOW). Include loops or nesting deeper than 15 hops are SPF_RECURSIVE_LOOP (HIGH), include targets without an SPF record are SPF_INCLUDE_NO_RECORD (MEDIUM), and the lookup count plus the full include tree are printed in the report.

### How is DNSSEC validated, and what does the AD bit tell you?

DNSSEC is reported VALIDATED only when the apex publishes a DNSKEY and the validating DNS-over-HTTPS resolver (Cloudflare, with Google as fallback) returns the Authenticated Data (AD) flag, meaning it verified the full chain from the root through your registrar's DS record. No DNSKEY is DNSSEC_NOT_ENABLED (MEDIUM). A DNSKEY with AD=false means a missing or mismatched DS at the registrar and is DNSSEC_BROKEN_CHAIN (HIGH). If the validating resolver SERVFAILs but answers with checking disabled (cd=1), the chain is BOGUS (expired RRSIG or DS/DNSKEY mismatch) and is also flagged HIGH because validating clients cannot resolve the domain.

### How do I protect a domain that never sends email from being spoofed?

Run the audit with --no-mail. The auditor then expects the three records that together block spoofing of a parked or web-only domain: an SPF record of exactly v=spf1 -all, a DMARC record with p=reject (the printed template also sets sp=reject, adkim=s and aspf=s), and an RFC 7505 null MX of 0 . so mail servers stop trying to deliver. Anything weaker is flagged: SPF_NO_MAIL_NOT_STRICT, DMARC_NO_MAIL_NOT_REJECT and NO_MAIL_MX_PRESENT. Even without the flag, a domain with no MX is treated as non-sending and the report prints the exact BIND-syntax TXT and MX records to publish.

### What does the TLS handshake check, and how is it cross-checked against CAA?

It makes one TLS handshake to port 443 with SNI and full certificate verification, then records protocol, cipher, issuer, serial, SHA-256 fingerprint, SANs and validity. Verification failures and hostname mismatches are CRITICAL; TLS below 1.2 and weak ciphers are HIGH; expiry is CRITICAL under 7 days, HIGH under 14 and MEDIUM under 30. The live issuer is then compared with the domain's CAA issue tags: a certificate from a CA the CAA record does not authorise is TLS_ISSUER_CAA_MISMATCH (HIGH). With no CAA record (CAA_MISSING) it prints CAA issue records for the CAs seen on the live certificate and in CT logs, an iodef contact, and issuewild ";" when no wildcard certificate is in use.

### How do Certificate Transparency and dangling CNAME detection work?

Certificate Transparency issuances are read from Cert Spotter (crt.sh as fallback) to list issuers and names; unexpired certificates from a CA not authorised by CAA raise CT_ISSUER_NOT_IN_CAA (MEDIUM), and an empty crt.sh answer is INCONCLUSIVE, never clean. For takeover risk it follows the CNAME chains of the audited domain's own apex and www records only: a chain ending in NXDOMAIN, or pointing at a takeover-prone provider such as GitHub Pages, Heroku, AWS S3, Azure, Netlify or Vercel without any A/AAAA answer, is DANGLING_CNAME_TAKEOVER_RISK (HIGH). It never registers or claims the target and never enumerates third-party subdomains.

### How do stateful --state --delta monitoring routines work?

Pass --state /workspace/domain-hygiene/state.json --delta. The first run per domain stores a baseline snapshot of nameservers, SPF, DMARC, DNSSEC status, certificate fingerprint, issuer and expiry, CT names and findings. Later runs report new, changed and resolved findings plus events: CERT_ROTATED, CERT_EXPIRY_WINDOW, NS_CHANGED, SPF_CHANGED, DMARC_CHANGED (MEDIUM when the policy weakens), DNSSEC_LOST (HIGH), NEW_CT_NAMES, NEW_CT_ISSUANCES and HEADER_REGRESSED. Degraded runs (a resolver, TLS or CT source failed) skip the delta and never overwrite the baseline, and exit 11 so an outage is not mistaken for a quiet day. A daily or weekly Grok Bot routine alerts only on events or exit code 11.
