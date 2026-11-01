# Web & API Text Extractor for Grok Bot (Free) | BotAppStore

Canonical page: https://botappstore.com/bots/web-text-extractor.html

Web & API Text Extractor — Free Grok Bot Web Scraper & Starter Kit

Free self-onboarding web & JSON endpoint text scraper for Grok Bot. Provisions a 0600 wck_ key on Turn 1 and runs 10 free scrapes/UTC day.

## When to use

Use when an agent needs readable text from a public web page or API response.

## Price

- SKU: `web-extractor`
- Price: Free

## How an agent gets it

Free. Install details are on https://botappstore.com/bots/web-text-extractor.html ; catalog: https://botappstore.com/catalog.json

## FAQ

### Is the Web & API Text Extractor really free forever?

Yes. The template is 100% free ($0) and includes 10 free cloud text extractions per UTC day on the hosted text-extraction API (POST /api/v1/scrape) plus a one-time starter activation (onboarding_v1 + starter_v1) and unlimited local curl + Python fallback in /workspace.

### Where is my wck_... agent key stored?

Your agent key is written to a private env file in /workspace with chmod 600 (-rw-------) permissions inside your isolated Grok Bot Linux sandbox and is never printed in chat.

### Does it render client-side JavaScript SPAs?

No. Both the cloud /api/v1/scrape endpoint and the local /workspace fallback use deterministic static HTTP fetches + BeautifulSoup HTML-to-text parsing (15-second timeout, 2 MB cap) and never make false headless-browser claims.
