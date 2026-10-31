import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_analytics import check_html  # noqa: E402

STUB = '<script>window.va = window.va || function () {};</script>'
SRC = '<script defer src="/_vercel/insights/script.js"></script>'


def page(head):
    return f"<!doctype html><html><head>{head}</head><body></body></html>"


class CheckAnalytics(unittest.TestCase):
    def test_clean_passes(self):
        self.assertEqual(check_html(page(STUB + SRC)), [])

    def test_missing_fails(self):
        self.assertEqual(len(check_html(page("<title>x</title>"))), 2)

    def test_duplicate_fails(self):
        self.assertTrue(check_html(page(STUB + SRC + SRC)))

    def test_outside_head_fails(self):
        html = page(STUB) .replace("<body>", "<body>" + SRC)
        self.assertEqual(check_html(html), ["insights script is not inside <head>"])


if __name__ == "__main__":
    unittest.main()
