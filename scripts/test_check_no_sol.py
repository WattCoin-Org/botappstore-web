import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_no_sol import check_text, main  # noqa: E402


def labels(text):
    return [(n, label) for n, label, _ in check_text(text)]


class CheckNoSol(unittest.TestCase):
    def test_clean_passes(self):
        self.assertEqual(labels("<p>Card, Apple Pay or Google Pay. Console and solid.</p>"), [])

    def test_sol_word_fails_with_line(self):
        self.assertEqual(labels("a\nSOL ALSO ACCEPTED"), [(2, "SOL")])

    def test_sol_is_case_sensitive_and_word_bounded(self):
        self.assertEqual(labels("sol console SOLID Solid"), [])

    def test_solana_fails(self):
        self.assertEqual(labels("CoinLoader (Solana Bots)"), [(1, "Solana")])

    def test_wattcoin_brand_fails_any_case(self):
        self.assertEqual(labels("The WattCoin Registry"), [(1, "WattCoin")])
        self.assertEqual(labels("wattcoin treasury"), [(1, "WattCoin")])

    def test_handle_fails(self):
        self.assertEqual(labels("follow @WattCoin2026"), [(1, "@WattCoin2026")])

    def test_code_and_pre_contents_ignored_for_sol_only(self):
        self.assertEqual(labels("<code>SOL</code>"), [])
        self.assertEqual(labels("x\n<pre>\nSOL\n</pre>\nSOL"), [(5, "SOL")])
        self.assertEqual(labels("<code>WattCoin</code>"), [(1, "WattCoin")])

    def test_markdown_code_is_ignored_only_for_markdown(self):
        md = "ok `SOL` and\n```text\nSOL\n```\nPay in SOL"
        self.assertEqual([(n, l) for n, l, _ in check_text(md, markdown=True)], [(5, "SOL")])
        md = "ok `.wattcoin_env`\n```text\nWATTCOIN_AGENT_KEY\n```"
        self.assertEqual([(n, l) for n, l, _ in check_text(md, markdown=True)], [(1, "WattCoin"), (3, "WattCoin")])
        self.assertEqual(len(check_text("`SOL`")), 1)  # backticks are not code in html/js/json

    def test_wattcoin_identifiers_fail_without_functional_path(self):
        for text in (
            "/workspace/.wattcoin_env",
            ".wattcoin_license_check.py",
            "https://www.wattcoin.org/api/v1/agents/license/claim",
            "WATTCOIN_AGENT_KEY=wck_x",
            "https://wattcoin-production-81a7.up.railway.app/x",
            "https://a.wattcoin.workers.dev/",
            "Use .wattcoin_env from the WattCoin team",
        ):
            self.assertEqual([l for _, l, _ in check_text(text)], ["WattCoin"], text)

    def test_visible_env_file_name_in_html_fails(self):
        html = "<p>Credentials live in <code>/workspace/.wattcoin_env</code> (0600).</p>"
        self.assertEqual(labels(html), [(1, "WattCoin")])
        self.assertEqual(check_text(html, path="bots/web-text-extractor.html")[0][1], "WattCoin")

    def test_env_var_in_pre_block_fails(self):
        html = "<pre>\n$ curl -H \"X-Agent-Key: $WATTCOIN_AGENT_KEY\"\n</pre>"
        self.assertEqual(labels(html), [(2, "WattCoin")])
        self.assertEqual(check_text(html, path="llms-full.txt"), check_text(html))
        self.assertEqual(labels("`WATTCOIN_AGENT_KEY`".join(["```\n", "\n```"])), [(2, "WattCoin")])

    def test_functional_exceptions_pass(self):
        url = "https://www.wattcoin.org/api/v1/agents/license/stripe-checkout"
        cases = [
            ("vercel.json", '"destination": "https://wattcoin-production-81a7.up.railway.app/api/x"'),
            ("catalog.json", f'      "url": "{url}",'),
            ("catalog.json", '      "license_lookup": "GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}",'),
            ("app.js", "  const REGISTRY_API_BASE = 'https://www.wattcoin.org/api';"),
            ("app.js", "return `/usr/bin/python3 -I /workspace/.wattcoin_license_check.py --sku ${s} --claim-stdin <<< \"${code}\"`;"),
            ("llms.txt", f"1. **Card:** `POST {url}` with a body"),
            ("llms-full.txt", "4. **`GET https://www.wattcoin.org/api/v1/agents/license?sku=<sku>`**"),
            ("bots/agent-council.md", f"- Card, Apple Pay or Google Pay (default): `POST {url}` with body `{{}}`."),
            ("bots/agent-council.md", "- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`."),
        ]
        for path, text in cases:
            self.assertEqual(check_text(text, markdown=path.endswith((".md", ".txt")), code=path.endswith((".js", ".json")), path=path), [], path)

    def test_functional_exceptions_are_line_and_file_scoped(self):
        url = "https://www.wattcoin.org/api/v1/agents/license/claim"
        bad = [
            ("vercel.json", f'"x": "{url}"'),
            ("catalog.json", f'"description": "{url}"'),
            ("catalog.json", '"url": "https://www.wattcoin.org/scrape-ads"'),
            ("app.js", f"const x = '{url}';"),
            ("app.js", "const t = `<code>/workspace/.wattcoin_env</code>`;"),
            ("app.js", "const REGISTRY_API_BASE = 'https://x'; // .wattcoin_env"),
            ("llms.txt", f"  1. **Turn-1 Onboarding:** binds via `POST {url}`"),
            ("llms.txt", f"- **Endpoints:** {url}"),
            ("llms.txt", "1. **Card:** see .wattcoin_env"),
            ("llms-full.txt", "1. **Setup:** installs `.wattcoin_license_check.py`"),
            ("index.html", f"1. **Card:** `POST {url}`"),
            ("bots/agent-council.md", f"- Sample: `POST {url}`"),
            ("bots/agent-council.html", f"- Card, Apple Pay or Google Pay (default): `POST {url}`"),
        ]
        for path, text in bad:
            self.assertEqual([l for _, l, _ in check_text(text, markdown=path.endswith((".md", ".txt")), path=path)], ["WattCoin"], f"{path}: {text}")

    def test_main_exit_codes_and_scope(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "bots").mkdir()
            (root / "index.html").write_text("<p>ok</p>")
            self.assertEqual(main(["x", d]), 0)
            (root / "bots" / "a.md").write_text("fine\nPay in SOL\n")
            self.assertEqual(main(["x", d]), 1)
            (root / "bots" / "a.md").write_text("fine")
            (root / "styles.css").write_text("--accent-solana: red; /* SOL */")
            (root / "llms.txt").write_text("WattCoin")
            self.assertEqual(main(["x", d]), 1)

    def test_main_wattcoin_by_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "bots").mkdir()
            (root / "app.js").write_text("const REGISTRY_API_BASE = 'https://www.wattcoin.org/api';\n")
            (root / "llms.txt").write_text("1. **Card:** `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout`\n")
            self.assertEqual(main(["x", d]), 0)
            (root / "bots" / "a.html").write_text("<p>File: <code>.wattcoin_env</code></p>\n")
            self.assertEqual(main(["x", d]), 1)
            (root / "bots" / "a.html").write_text("<p>ok</p>")
            (root / "llms.txt").write_text("- Endpoints: https://www.wattcoin.org/api/v1/scrape\n")
            self.assertEqual(main(["x", d]), 1)

    def test_strict_violations(self):
        cases = [
            ("<p>completes the 100 WATT starter</p>", "WATT amount"),
            ("<p>+25 WATT</p>", "WATT amount"),
            ("<p>costs 1,000 WATT</p>", "WATT amount"),
            ("<p>100 WATT/call</p>", "WATT amount"),
            ("<p>paid per WATT/call</p>", "WATT/call"),
            ("<p>never spends WATT</p>", "WATT token"),
            ("<p>Phantom wallet</p>", "phantom"),
            ("<p>seed wallet</p>", "wallet"),
            ("<p>checkout-wallet-actions</p>", "wallet"),
            ("<p>On-Chain Modes</p>", "on-chain"),
            ("<p>on chain</p>", "on-chain"),
            ("<p>.lamports-sub</p>", "lamport"),
            ("color: var(--accent-solana);", "Solana"),
        ]
        for text, label in cases:
            self.assertEqual([l for _, l, _ in check_text(text)], [label], text)

    def test_sol_identifier_only_in_code_kinds(self):
        css = ".sol-unit { color: red; }"
        self.assertEqual([l for _, l, _ in check_text(css, code=True)], ["sol identifier"])
        self.assertEqual(check_text(css), [])
        self.assertEqual(check_text(".solid-box, .console, .isolated {}", code=True), [])

    def test_strict_rules_see_inside_code_and_pre(self):
        self.assertEqual([l for _, l, _ in check_text("<pre>+25 WATT</pre>")], ["WATT amount"])
        self.assertEqual([l for _, l, _ in check_text("`+25 WATT`", markdown=True)], ["WATT amount"])
        self.assertEqual([l for _, l, _ in check_text("<code>wallet</code>")], ["wallet"])

    def test_strict_no_false_positives(self):
        ok = (
            "solid solution console absolute isolated resolve consolidated wattage "
            "kilowatt Watt watchdog sec-patent-watchdog phantom dependency onchain-free $WATT "
            "Console and solid SOLID Solid"
        )
        self.assertEqual(check_text(ok), [])
        self.assertEqual(check_text(ok, code=True), [])

    def test_wattcoin_fails_under_strict_markdown_too(self):
        ua = "`User-Agent: WattCoinDeepDive/1.0 (https://botappstore.com; contact@wattcoin.org)`"
        self.assertEqual([l for _, l, _ in check_text(ua, markdown=True)], ["WattCoin"])

    def test_main_covers_css_vercel_and_middleware(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "vercel.json").write_text(
                '{"rewrites":[{"destination":"https://wattcoin-production-81a7.up.railway.app/x"}]}'
            )
            (root / "styles.css").write_text("a { color: var(--accent-emerald); }")
            self.assertEqual(main(["x", d]), 0)
            (root / "styles.css").write_text("a { color: var(--accent-solana); }")
            self.assertEqual(main(["x", d]), 1)
            (root / "styles.css").write_text("a {}")
            (root / "middleware.js").write_text("// wallet\n")
            self.assertEqual(main(["x", d]), 1)
            (root / "middleware.js").write_text("// ok\n")
            (root / "robots.txt").write_text("Allow: / 100 WATT")
            self.assertEqual(main(["x", d]), 1)


if __name__ == "__main__":
    unittest.main()
