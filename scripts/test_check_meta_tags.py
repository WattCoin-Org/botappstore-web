import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_meta_tags import check_html  # noqa: E402


def page(head):
    return f"<!doctype html><html><head>{head}</head><body></body></html>"


CLEAN = page(
    '<title>BotAppStore</title>'
    '<meta name="description" content="Bots for agents">'
    '<meta property="og:title" content="BotAppStore">'
    '<meta name="twitter:card" content="summary_large_image">'
)


class CheckMetaTags(unittest.TestCase):
    def test_clean_passes(self):
        self.assertEqual(check_html(CLEAN), [])

    def test_twitter_site_wattcoin_fails(self):
        v = check_html(page('<meta name="twitter:site" content="@WattCoin2026" />'))
        self.assertEqual([(x[0], x[2]) for x in v], [(1, "WattCoin")])

    def test_og_title_sol_fails(self):
        self.assertTrue(check_html(page("<meta property='og:title' content='Pay in SOL'>")))

    def test_attribute_order_and_multiline(self):
        html = page('<meta\n  content="sol tools"\n  property="og:description">')
        v = check_html(html)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0][0], 1)

    def test_title_wattcoin_fails(self):
        self.assertTrue(check_html(page("<title>The wattcoin store</title>")))

    def test_solana_and_console_allowed(self):
        self.assertEqual(check_html(page("<title>Solana console</title>")), [])

    def test_body_and_other_meta_ignored(self):
        html = page('<meta name="keywords" content="SOL WattCoin">') + "<p>SOL WattCoin</p>"
        self.assertEqual(check_html(html), [])


if __name__ == "__main__":
    unittest.main()
