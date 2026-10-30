# Agent Council Grok Bot — Multi-Model AI Debate

Canonical page: https://botappstore.com/bots/agent-council.html

Agent Council — Multi-Model Swarm Debate & Pair-Architect Collaborator — Put several models on the problem and keep the argument, not just the answer.

Multi-model AI council for Grok Bot ($39 · card): 2-6 seats from local claude personas, codex, gemini and aider or BYOK OpenAI, Anthropic, xAI/Grok, Gemini, OpenRouter, DeepSeek and Ollama models debate, pair-program, compete or review code, with blind round 1, anonymized cross-examination, timers, a budget guard, mid-flight steering and a self-contained HTML council viewer.

## When to use

Use when an agent needs several models to debate a decision or review code before committing.

## Price

- SKU: `agent-council`
- Price: $39 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "agent-council", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### Which seats work out of the box, and do I need API keys?

No API key is needed to start. If the claude CLI is installed and logged in (it is on Grok Bot's computer), the default council is three claude personas: architect, skeptic and pragmatist, or driver and navigator in pair mode. Each claude seat runs tool-less from an empty temp directory with a lean system prompt, so a call costs a few hundred prompt tokens of overhead instead of the full agent context. --discover also lists the codex, gemini, aider and ollama CLIs it finds, and which BYOK provider keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, XAI_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, DEEPSEEK_API_KEY) are set, without ever printing their values.

### How does a deliberation avoid groupthink and anchoring?

Round 1 is blind: every seat answers the same pinned brief in parallel without seeing any peer. From round 2 each seat sees only its peers' structured positions, critiques and changes under anonymous labels (Seat A, Seat B), never raw chain-of-thought and never which vendor produced them, and must say what it changed and what it still disputes. The Chair, prompted as a judge that weighs arguments on merit, then separates real consensus from polite agreement, keeps credible minority views in a dissent list with severities, and records each seat's confidence shift.

### How do pair, compete and review modes work?

Pair: the Driver writes a concrete patch or plan, Navigators critique it adversarially with cited findings, the Driver refines, and the result is a final patch plus a verdict of APPROVE, REQUEST_CHANGES (MEDIUM) or BLOCK (HIGH). Compete: seats solve independently, then grade each other's solutions (never their own) on Correctness 0.40, Security 0.30, Simplicity 0.15 and Edge Cases 0.15; the winner is computed deterministically from the scores and the Chair lists cherry-picks from the runners-up. Review: every seat reviews the same --files slices or --git-diff, findings are merged and deduplicated into F1..Fn ranked CRITICAL to INFO with file, line and fix, and later rounds let seats confirm or dispute each one.

### How are tokens kept down without throttling the models?

Every call starts with the same pinned brief (problem statement, line-numbered context bundle, protocol), so provider prompt caches hit; Anthropic seats mark it with cache_control. You pass only the line ranges that matter (--files path:85-177) or a git diff. Round 2 and later forward only structured peer positions and deltas, clipped per field, and the council stops early once every seat has converged with no HIGH disagreement. Output is not capped: HTTP seats omit max_tokens where the API allows it, Anthropic seats default to 16,384 output tokens with extended thinking, and reasoning effort defaults to high.

### What happens when a seat hangs or the budget runs out?

Each call is bounded by --seat-timeout (default 120 s). A seat that times out is marked DEGRADED_TIMEOUT (errors are DEGRADED_ERROR) and sits out while the rest continue, as long as --quorum active seats remain. --total-deadline (default 600 s) stops new rounds early enough for the Chair to synthesise. --max-cost-usd refuses the run up front (exit 10) if even the minimum estimate is over budget, and starts no new round once spend reaches 85% of the cap. Any degradation makes the run exit 11, so a half-finished council is never reported as a clean consensus.

### Can I steer a council while it is running?

Yes. Start it with --session ID, and from a second command use --status to see rounds, seat status and spend, --inject "note" to queue a moderator instruction that every seat receives as high-priority steering in the next round, or --pause to stop before the next round. --resume continues a paused session with the same seats, context and options, or adds more rounds to a finished one. Sessions are stored under /workspace/agent-council/sessions/ with 0700 permissions, and injected notes are redacted like everything else.

### What is the HTML council viewer?

--html-out PATH writes one self-contained dark-slate HTML file with no external scripts, fonts or images: the problem statement, context bundle hashes, seat table, every round per seat with latency, tokens and cost, the Chair synthesis, consensus matrix, minority report, confidence shifts, action plan or patch, findings and the cost ledger. You can open it offline or attach it to a pull request.

### What does it refuse to do?

It is advisory and read-only: it never edits, commits, runs or deploys anything. It refuses to ingest node_modules, .git, lockfiles, .env* and .wattcoin_env* files and key or credential files, and it redacts private keys, wck_/wcl_ codes, sk-, ghp_, AKIA, xai-, AIza, Slack, Stripe, JWT and Bearer tokens, password assignments and other high-entropy strings before any seat sees the prompt, context or moderator notes. Seats are instructed to decline exploit, credential-harvesting and other offensive tasks, and cost figures for BYOK seats are estimates, not invoices.
