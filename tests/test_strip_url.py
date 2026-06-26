"""
Tests for _strip_url_trailing_junk helper.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot


class TestStripUrlTrailingJunk(unittest.TestCase):
    """Tests for _strip_url_trailing_junk."""

    def _check(self, raw: str, expected: str):
        result = bot._strip_url_trailing_junk(raw)
        self.assertEqual(result, expected, msg=f"Input: {raw!r}")

    # ASCII punctuation
    def test_strips_trailing_period(self):
        self._check("https://example.com/page.", "https://example.com/page")

    def test_strips_trailing_comma(self):
        self._check("https://example.com/page,", "https://example.com/page")

    def test_strips_trailing_semicolon(self):
        self._check("https://example.com/page;", "https://example.com/page")

    def test_strips_trailing_colon(self):
        self._check("https://example.com/page:", "https://example.com/page")

    def test_strips_trailing_exclamation(self):
        self._check("https://example.com/page!", "https://example.com/page")

    def test_strips_trailing_question_mark(self):
        self._check("https://example.com/path?", "https://example.com/path")

    # Unicode closing quotation marks (the original bug)
    def test_strips_russian_closing_guillemet(self):
        self._check("https://www.kommersant.ru/doc/8765189)»", "https://www.kommersant.ru/doc/8765189")

    def test_strips_guillemets(self):
        self._check("https://example.com/«page»", "https://example.com/«page")  # only trailing stripped

    def test_strips_smart_double_quotes(self):
        self._check('https://example.com/page"', "https://example.com/page")

    def test_strips_smart_single_quotes(self):
        self._check("https://example.com/page\u2019", "https://example.com/page")

    def test_strips_angle_quotation_marks(self):
        self._check("https://example.com/page›", "https://example.com/page")

    # Parentheses handling
    def test_strips_unbalanced_closing_paren(self):
        self._check("https://example.com/doc/8765189)", "https://example.com/doc/8765189")

    def test_preserves_balanced_parens(self):
        # Wikipedia-style URL with balanced parens
        self._check("https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)")

    def test_strips_unbalanced_closing_bracket(self):
        self._check("https://example.com/page]", "https://example.com/page")

    def test_preserves_balanced_brackets(self):
        self._check("https://example.com/api[v1]", "https://example.com/api[v1]")

    # Multi-character junk sequence
    def test_strips_mixed_trailing_junk(self):
        self._check("https://www.kommersant.ru/doc/8765189)».", "https://www.kommersant.ru/doc/8765189")

    # URL with query string — should not be mangled
    def test_preserves_query_string(self):
        self._check("https://example.com/search?q=foo&bar=baz", "https://example.com/search?q=foo&bar=baz")

    # Clean URL passes through unchanged
    def test_clean_url_unchanged(self):
        self._check("https://yandex.ru/news", "https://yandex.ru/news")


if __name__ == "__main__":
    unittest.main()
