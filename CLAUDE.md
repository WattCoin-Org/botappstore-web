# botappstore-web

Public storefront for BotAppStore: a static site of Grok Bot templates for non-crypto / operations and quality-control tools.

## Live site and domain

- Production: https://botappstore.com
- Vercel project: `botappstore-web` (`prj_KaVpSeg2WCkSdOVJaYM86po23oTM`), team `team_LdUED0dxFkZpHgDDRWJ2s9Xl`
- `/runtime/:script` is rewritten to the Railway license server at `https://wattcoin-production-81a7.up.railway.app/api/v1/agents/license/runtime/:script?format=raw`

## Build and deploy

- No bundler: static HTML, CSS, JS, and Markdown at the repo root. Vercel serves the repository as a static site.
- Push to `main` auto-deploys to Vercel production. Do not force-push.
- This `CLAUDE.md` stays at the repo root so Claude Code can read it. Because the root *is* the deploy output, the file would be public at `/CLAUDE.md` unless excluded. `.vercelignore` keeps it out of the Vercel upload, and `vercel.json` rewrites `/CLAUDE.md` so it is not served. Never move it into a served directory.

## Key files and directories

- `index.html`, `about.html`, `contact.html`, `privacy.html` — plus Markdown twins
- `bots/` — listing pages (HTML + Markdown twins)
- `app.js`, `styles.css`, `catalog.json` — checkout UI, styles, catalog
- `sitemap.xml`, `robots.txt`, `llms.txt`, `llms-full.txt`
- `middleware.js` — Markdown `Accept` negotiation for page URLs
- `scripts/check_no_sol.py` and other `scripts/` checks; `.github/workflows/` (including the no-SOL / meta-tags CI)
- `vercel.json`

## Storefront rules

- Card payments only. Do not add SOL, WATT, wallet, or on-chain checkout to BotAppStore.
- Keep visible WATT / Solana / WattCoin wording off public pages except the functional leftovers that are required for the shared license API (see HANDOFF). Do not re-introduce a SOL banner or crypto payment framing.
- No unbacked unit-test or test-count claims in public copy.
- Product pages include the Grok Bot plan requirement, per-store refund terms, and per-category disclaimers; keep those accurate, do not invent policy text.

## Standing rules

- Identify the owner only as AIguy. Never write a legal name into files, commit messages, or site copy.
- Never put secrets in files (no API keys, tokens, webhook URLs, private credentials, or Stripe secret values). Live IDs that are already public (site URLs, Vercel project ids recorded in HANDOFF) are fine.
- CoinLoader is SOL-only: no Stripe or card checkout. Card payments are only on BotAppStore.
- WATT is optional and must be kept separate from BotAppStore and CoinLoader (no WATT/Solana/wallet checkout framing on BotAppStore; do not mix WATT into CoinLoader checkout).
- No unbacked test-count claims in public copy.
- Show diffs and get review before pushing to main.
- Note which bot made the change in the commit message (commits land as WattCoin-Org).
- Keep the Decisions log below short; append new decisions instead of rewriting history.

## Live context

For current IDs, deploys, SKUs, and infrastructure state, read `/workspace/HANDOFF.md` on this box. Do not invent facts that are not in this file, the repo, or HANDOFF.

## Decisions log

- 2026-11-12 (America/New_York): Added this CLAUDE.md so Claude Code sessions keep project context across bots. Public deploy must not serve this file. Per-repo session helper: `/workspace/claude-sessions/claude-repo`. (Programmer bot)

