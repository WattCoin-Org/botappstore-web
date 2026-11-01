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

    def test_code_and_pre_contents_ignored(self):
        self.assertEqual(labels("<code>SOL WattCoin</code>"), [])
        self.assertEqual(labels("x\n<pre>\nSOL\nWattCoin\n</pre>\nSOL"), [(6, "SOL")])

    def test_markdown_code_is_ignored_only_for_markdown(self):
        md = "ok `SOL` and\n```text\nWattCoin\n```\nPay in SOL"
        self.assertEqual([(n, l) for n, l, _ in check_text(md, markdown=True)], [(5, "SOL")])
        self.assertEqual(len(check_text("`SOL`")), 1)  # backticks are not code in html/js/json

    def test_allowlist_tokens_pass(self):
        ok = (
            "/workspace/.wattcoin_env and .wattcoin_license_check.py "
            "https://www.wattcoin.org/api/v1/agents/license/claim WATTCOIN_AGENT_KEY=wck_x "
            "https://wattcoin-production-81a7.up.railway.app/x https://a.wattcoin.workers.dev/"
        )
        self.assertEqual(labels(ok), [])

    def test_allowlist_does_not_hide_brand_prose(self):
        self.assertEqual(labels("Use .wattcoin_env from the WattCoin team"), [(1, "WattCoin")])
        self.assertEqual(labels("https://www.wattcoin.org/ home"), [(1, "WattCoin")])
        self.assertEqual(labels("the WattCoin_agent_key brand"), [(1, "WattCoin")])

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

    def test_allowlist_still_passes_under_strict(self):
        ok = (
            ".wattcoin_license_check.py WATTCOIN_AGENT_KEY wattcoin.org/api/v1/scrape "
            "wattcoin-production-81a7.up.railway.app "
            "`User-Agent: WattCoinDeepDive/1.0 (https://botappstore.com; contact@wattcoin.org)`"
        )
        self.assertEqual(check_text(ok, markdown=True), [])

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
