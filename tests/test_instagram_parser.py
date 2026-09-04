"""
Tests for _fetch_instagram_og_data and Instagram URL handling via OGInstagram.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot


def _make_mock_response(html_str: str) -> MagicMock:
    """Return a mock context-manager response that yields HTML bytes."""
    raw = html_str.encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw
    mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=mock_resp)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class TestIsInstagramUrl(unittest.TestCase):
    """Tests for _is_instagram_url detection."""

    def test_standard_instagram_domains(self):
        self.assertTrue(bot._is_instagram_url("https://www.instagram.com/p/Dcscta2oz5v/"))
        self.assertTrue(bot._is_instagram_url("https://instagram.com/reel/C_12345/"))
        self.assertTrue(bot._is_instagram_url("https://instagr.am/p/ABC123/"))
        self.assertTrue(bot._is_instagram_url("https://www.instagr.am/username"))

    def test_oginstagram_domains(self):
        self.assertTrue(bot._is_instagram_url("https://oginstagram.com/p/Dcscta2oz5v/"))
        self.assertTrue(bot._is_instagram_url("https://d.oginstagram.com/p/Dcscta2oz5v/"))
        self.assertTrue(bot._is_instagram_url("https://g.oginstagram.com/username"))
        self.assertTrue(bot._is_instagram_url("https://www.oginstagram.com/p/123"))

    def test_custom_oginstagram_host(self):
        with patch.object(bot, "OGINSTAGRAM_HOST", "ig.example.org"):
            self.assertTrue(bot._is_instagram_url("https://ig.example.org/p/Dcscta2oz5v/"))
            self.assertTrue(bot._is_instagram_url("https://d.ig.example.org/p/Dcscta2oz5v/"))

        with patch.object(bot, "OGINSTAGRAM_HOST", "http://oginstagram:3000"):
            self.assertTrue(bot._is_instagram_url("http://oginstagram:3000/p/Dcscta2oz5v/"))
            self.assertTrue(bot._is_instagram_url("http://oginstagram/p/Dcscta2oz5v/"))

    def test_non_instagram_domains(self):
        self.assertFalse(bot._is_instagram_url("https://example.com/p/Dcscta2oz5v/"))
        self.assertFalse(bot._is_instagram_url("https://t.me/nerdpapers/3349"))
        self.assertFalse(bot._is_instagram_url("https://twitter.com/user/status/123"))


class TestFetchInstagramOgData(unittest.TestCase):
    """Unit tests for _fetch_instagram_og_data."""

    @patch("bot._urlopen")
    def test_post_with_author_caption_and_alt(self, mock_urlopen):
        """Extracts og:title, og:description, og:image:alt, and og:image."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
          <meta property="og:title" content="✨Кот Кефирчик✨ (@kot_kefirchick)">
          <meta property="og:description" content="Утренний котик передает привет!">
          <meta property="og:image:alt" content="Photo by ✨Кот Кефирчик✨ on August 30, 2026. May be a meme of text that says 'Я нападаю по команде хозяина'">
          <meta property="og:image" content="https://d.oginstagram.com/media/Dcscta2oz5v.jpg">
        </head>
        <body></body>
        </html>
        """
        mock_urlopen.return_value = _make_mock_response(html)
        title, image_url, md = bot._fetch_instagram_og_data("https://www.instagram.com/p/Dcscta2oz5v/")

        self.assertIn("✨Кот Кефирчик✨ (@kot_kefirchick)", title)
        self.assertIn("Утренний котик передает привет!", title)
        self.assertEqual(image_url, "https://d.oginstagram.com/media/Dcscta2oz5v.jpg")
        self.assertIn("# ✨Кот Кефирчик✨ (@kot_kefirchick)", md)
        self.assertIn("Утренний котик передает привет!", md)
        self.assertIn("Photo by ✨Кот Кефирчик✨", md)
        self.assertIn("![Media](https://d.oginstagram.com/media/Dcscta2oz5v.jpg)", md)

    @patch("bot._urlopen")
    def test_post_with_alt_text_only(self, mock_urlopen):
        """When caption is empty, uses og:image:alt text as the excerpt."""
        alt_caption = "Photo by ✨Кот Кефирчик✨ on August 30, 2026. May be a meme of text that says 'Я нападаю по команде хозяина я нападаю, если хозяину угрожают Я нападу на хозяина'."
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
          <meta property="og:title" content="✨Кот Кефирчик✨ (@kot_kefirchick)">
          <meta property="og:image:alt" content="{alt_caption}">
          <meta property="og:image" content="https://d.oginstagram.com/p/Dcscta2oz5v/">
        </head>
        </html>
        """
        mock_urlopen.return_value = _make_mock_response(html)
        title, image_url, md = bot._fetch_instagram_og_data("https://www.instagram.com/p/Dcscta2oz5v/")

        self.assertIn("✨Кот Кефирчик✨ (@kot_kefirchick)", title)
        self.assertIn("Я нападаю по команде хозяина", title)
        self.assertEqual(image_url, "https://d.oginstagram.com/p/Dcscta2oz5v/")
        self.assertIn(alt_caption, md)

    @patch("bot._urlopen")
    def test_fallback_to_direct_media_when_og_image_missing(self, mock_urlopen):
        """When og:image is missing in HTML, falls back to https://d.{OGINSTAGRAM_HOST}{path}."""
        html = """
        <html>
        <head>
          <meta property="og:title" content="Photographer">
          <meta property="og:description" content="Nice photo">
        </head>
        </html>
        """
        mock_urlopen.return_value = _make_mock_response(html)
        title, image_url, md = bot._fetch_instagram_og_data("https://instagram.com/p/ABC123xyz/")

        self.assertEqual(title, "Photographer: Nice photo")
        self.assertEqual(image_url, "https://d.oginstagram.com/p/ABC123xyz/")
        self.assertIn("![Media](https://d.oginstagram.com/p/ABC123xyz/)", md)

    @patch("bot._urlopen")
    def test_long_caption_truncation(self, mock_urlopen):
        """Long caption is truncated to 200 chars with ellipsis in title."""
        long_desc = "Word " * 60
        html = f"""
        <html>
        <head>
          <meta property="og:title" content="Author">
          <meta property="og:description" content="{long_desc}">
          <meta property="og:image" content="https://example.com/img.jpg">
        </head>
        </html>
        """
        mock_urlopen.return_value = _make_mock_response(html)
        title, _, _ = bot._fetch_instagram_og_data("https://instagram.com/p/12345/")

        self.assertIn("…", title)
        self.assertLessEqual(len(title), len("Author: ") + 200 + 1)

    @patch("bot._urlopen")
    def test_html_entity_unescaping(self, mock_urlopen):
        """HTML entities such as &quot; and &#39; are properly unescaped."""
        html = """
        <html>
        <head>
          <meta property="og:title" content="Tom &amp; Jerry">
          <meta property="og:description" content="&quot;Meme&quot; &#39;test&#39;">
          <meta property="og:image" content="https://example.com/img.jpg">
        </head>
        </html>
        """
        mock_urlopen.return_value = _make_mock_response(html)
        title, _, md = bot._fetch_instagram_og_data("https://instagram.com/p/123/")

        self.assertIn("Tom & Jerry", title)
        self.assertIn('"Meme" \'test\'', title)
        self.assertIn('"Meme" \'test\'', md)

    @patch("bot._urlopen")
    def test_direct_image_content_type(self, mock_urlopen):
        """When proxy returns direct image/jpeg, it is used directly as image_url."""
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "image/jpeg"}
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=mock_resp)
        cm.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = cm

        title, image_url, md = bot._fetch_instagram_og_data("https://www.instagram.com/p/Dcscta2oz5v/")
        self.assertEqual(title, "Instagram")
        self.assertIn("https://oginstagram.com/p/Dcscta2oz5v/", image_url)
        self.assertIn("![Media]", md)

    @patch("bot._urlopen")
    def test_multi_host_fallback(self, mock_urlopen):
        """If first host fails or returns challenge, falls back to next host."""
        # First call fails (e.g. 403 or challenge), second call succeeds
        challenge_resp = _make_mock_response("<html><head><title>Just a moment...</title></head></html>")
        valid_html = """
        <html><head>
          <meta property="og:title" content="Cat Video">
          <meta property="og:description" content="Meow">
          <meta property="og:image" content="https://d.kkinstagram.com/p/123.jpg">
        </head></html>
        """
        valid_resp = _make_mock_response(valid_html)
        mock_urlopen.side_effect = [challenge_resp, valid_resp]

        title, image_url, md = bot._fetch_instagram_og_data("https://www.instagram.com/p/123/")
        self.assertEqual(title, "Cat Video: Meow")
        self.assertEqual(image_url, "https://d.kkinstagram.com/p/123.jpg")

    @patch("bot._urlopen")
    def test_internal_docker_http_host(self, mock_urlopen):
        """When OGINSTAGRAM_HOST is an internal docker host with http/port, uses http."""
        html = """
        <html><head>
          <meta property="og:title" content="Docker Test">
          <meta property="og:description" content="Local preview">
          <meta property="og:image" content="/media/pic.jpg">
        </head></html>
        """
        mock_urlopen.return_value = _make_mock_response(html)
        with patch.object(bot, "OGINSTAGRAM_HOST", "http://oginstagram:3000"):
            title, image_url, md = bot._fetch_instagram_og_data("https://www.instagram.com/p/dockertest/")
            self.assertEqual(title, "Docker Test: Local preview")
            self.assertEqual(image_url, "http://oginstagram:3000/media/pic.jpg")
            req = mock_urlopen.call_args[0][0]
            self.assertTrue(req.full_url.startswith("http://oginstagram:3000/p/dockertest/"))

    @patch("bot._urlopen")
    def test_network_error_returns_none(self, mock_urlopen):
        """Returns (None, None, None) on network exception across all hosts."""
        mock_urlopen.side_effect = Exception("Connection timed out")
        title, img, md = bot._fetch_instagram_og_data("https://instagram.com/p/error/")
        self.assertIsNone(title)
        self.assertIsNone(img)
        self.assertIsNone(md)

    def test_root_path_returns_none(self):
        """Bare domain without path returns None."""
        title, img, md = bot._fetch_instagram_og_data("https://instagram.com")
        self.assertIsNone(title)
        self.assertIsNone(img)
        self.assertIsNone(md)


class TestGetOgPreviewDataInstagramEarlyReturn(unittest.TestCase):
    """Verify _get_og_preview_data delegates to Instagram parser for Instagram URLs."""

    def test_early_return_for_instagram_url(self):
        """When _fetch_instagram_og_data succeeds, _get_og_preview_data returns
        its data directly without calling generic _urlopen or Jina."""
        with (
            patch.object(bot, "_fetch_instagram_og_data",
                         return_value=("Kefir: Cute cat", "https://d.oginstagram.com/p/123/", "# Kefir\n\nCute cat")) as mock_ig,
            patch.object(bot, "_urlopen") as mock_urlopen,
            patch.object(bot, "_fetch_from_jina") as mock_jina,
        ):
            title, image_url, is_invidious, warning, jina_md = bot._get_og_preview_data(
                "https://www.instagram.com/p/Dcscta2oz5v/"
            )

        mock_ig.assert_called_once_with("https://www.instagram.com/p/Dcscta2oz5v/")
        self.assertEqual(title, "Kefir: Cute cat")
        self.assertEqual(image_url, "https://d.oginstagram.com/p/123/")
        self.assertFalse(is_invidious)
        self.assertIsNone(warning)
        self.assertEqual(jina_md, "# Kefir\n\nCute cat")
        mock_urlopen.assert_not_called()
        mock_jina.assert_not_called()

    def test_falls_through_when_ig_parser_returns_none(self):
        """If Instagram parser returns (None, None, None), standard flow proceeds."""
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_resp.read.return_value = b"<html><head><title>Instagram Login</title></head></html>"
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=mock_resp)
        cm.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(bot, "_fetch_instagram_og_data", return_value=(None, None, None)),
            patch.object(bot, "_urlopen", return_value=cm),
            patch.object(bot, "_fetch_from_jina", return_value=(None, None, None, None)),
        ):
            title, image_url, is_invidious, warning, jina_md = bot._get_og_preview_data(
                "https://www.instagram.com/p/invalid/"
            )

        self.assertEqual(title, "Instagram Login")


if __name__ == "__main__":
    unittest.main()
