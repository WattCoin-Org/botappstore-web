# MCP Server & Tool-Permission Security Auditor

Canonical page: https://botappstore.com/bots/mcp-config-auditor.html

MCP Server & Tool-Permission Security Auditor — Find the leaked key, the unpinned package and the poisoned tool before your agent runs them.

Read-only MCP config security auditor for Grok Bot ($19 · card): finds hardcoded secrets, unpinned npx/uvx packages, typosquats, dangerous flags, auto-approve, filesystem and Docker exposure, cleartext remote endpoints and tool poisoning across Claude Desktop, Claude Code, Cursor, VS Code, Windsurf, Cline and Zed configs, with secret redaction, hardened copy-paste configs and rug-pull delta tracking.

## When to use

Use when an agent needs a security check of MCP server and tool-permission configuration.

## Price

- SKU: `mcp-config-auditor`
- Price: $19 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "mcp-config-auditor", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### Which MCP clients and files does it understand?

Claude Desktop (claude_desktop_config.json), Claude Code (.mcp.json), Cursor (.cursor/mcp.json), VS Code (.vscode/mcp.json with servers and inputs), Windsurf (mcp_config.json), Cline (cline_mcp_settings.json), Zed (settings.json context_servers) and any generic mcp.json with an mcpServers block. JSONC comments and trailing commas are tolerated and reported. --dir discovers these files recursively, skipping node_modules, .git and virtualenvs, and --tools-json adds a saved tools/list snapshot so tool descriptions and schemas are audited too.

### Does it run my MCP servers or touch the network?

No. It is a static, read-only audit: it parses the files you point it at and makes no network requests. It never launches an MCP server, never downloads or installs a package, and never modifies a config file. Tool checks use the tools/list snapshot you provide; the auditor cannot fetch one itself.

### How are secrets handled in the output?

Secrets are detected by known token prefixes (Anthropic, OpenAI, GitHub, GitLab, Slack, Stripe, AWS, Google, JWT, PEM private keys, webhooks and more), by secret-like key names and by entropy. Every detected value is replaced with [REDACTED:LABEL] in text, Markdown and JSON reports, in --remediate output and in the --state file, and the hardened snippet swaps it for a ${env:VAR} reference. Removing a key from a file does not revoke it, so the report tells you to rotate it. Detection is heuristic: an unusual secret format may not be recognised.

### What is an MCP rug pull and how does delta mode catch it?

A rug pull is when a server you already approved changes its tool descriptions or schemas later, for example adding hidden instructions to a tool description. With --state PATH --delta the auditor stores hashes of every tool description, input schema and annotation set, plus each server's launch config. Later runs report TOOL_DESC_CHANGED (HIGH), TOOL_SCHEMA_CHANGED (MEDIUM), SERVER_ADDED, SERVER_CONFIG_CHANGED, TOOL_ADDED and TOOL_ANNOTATIONS_CHANGED. The first run is a baseline, and a degraded run never overwrites the state.

### What does the hardened config change?

For each audited file it prints a copy-paste mcpServers (or servers / context_servers) snippet: literal secrets become ${env:VAR} references, unpinned or floating packages get @VERSION or #COMMIT_SHA placeholders, typosquatted names are replaced with the canonical package for you to confirm, root, home and credential paths are narrowed to /workspace/project, Docker mounts become :ro and --privileged is dropped, http:// remotes become https://, and servers that pipe remote code into a shell are removed. You review the snippet and apply it yourself.

### What does it refuse to do?

It audits only configs you own or are authorised to review. It will not harvest or validate secrets from other people's configs or public repos, test whether a found credential works, launch or exploit a server, or write poisoned tools, prompt-injection payloads or typosquat packages. A clean result is not a guarantee: secret, typosquat and injection detection are heuristic, and this is not a penetration test, malware analysis or compliance attestation.
