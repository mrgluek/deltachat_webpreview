"""
Tests for _fetch_telegram_og_data (Telegram oEmbed parser).
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot


def _make_mock_response(data: dict) -> MagicMock:
    """Return a mock context-manager response that yields JSON bytes."""
    raw = json.dumps(data).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=mock_resp)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class TestFetchTelegramOgData(unittest.TestCase):
    """Unit tests for _fetch_telegram_og_data."""

    @patch("bot._urlopen")
    def test_standard_post_url(self, mock_urlopen):
        """https://t.me/channel/123 → title built from author + text excerpt."""
        mock_urlopen.return_value = _make_mock_response({
            "author_name": "nerdpapers",
            "html": "<blockquote>Interesting article about Python.</blockquote>",
            "thumbnail_url": "https://cdn.t.me/thumb.jpg",
        })
        title, thumb = bot._fetch_telegram_og_data("https://t.me/nerdpapers/3349")
        self.assertIn("nerdpapers", title)
        self.assertIn("Interesting article about Python", title)
        self.assertEqual(thumb, "https://cdn.t.me/thumb.jpg")

    @patch("bot._urlopen")
    def test_s_prefix_url_normalised(self, mock_urlopen):
        """https://t.me/s/channel/123 should be resolved to the same oEmbed URL."""
        mock_urlopen.return_value = _make_mock_response({
            "author_name": "nerdpapers",
            "html": "<blockquote>Post text</blockquote>",
        })
        title, thumb = bot._fetch_telegram_og_data("https://t.me/s/nerdpapers/3349")
        self.assertIn("nerdpapers", title)
        # Verify the oEmbed call used the canonical URL (no 's/')
        req = mock_urlopen.call_args[0][0]
        self.assertIn("t.me%2Fnerdpapers%2F3349", req.full_url)

    @patch("bot._urlopen")
    def test_long_text_truncated_to_200_chars(self, mock_urlopen):
        long_text = "A" * 300
        mock_urlopen.return_value = _make_mock_response({
            "author_name": "chan",
            "html": f"<blockquote>{long_text}</blockquote>",
        })
        title, _ = bot._fetch_telegram_og_data("https://t.me/chan/1")
        # excerpt is max 200 chars + ellipsis, plus "chan: " prefix
        self.assertIn("…", title)
        self.assertLessEqual(len(title), len("chan: ") + 200 + 1)  # 1 for ellipsis char

    @patch("bot._urlopen")
    def test_no_html_field_uses_author_only(self, mock_urlopen):
        mock_urlopen.return_value = _make_mock_response({
            "author_name": "mychannel",
        })
        title, thumb = bot._fetch_telegram_og_data("https://t.me/mychannel/42")
        self.assertEqual(title, "mychannel")
        self.assertIsNone(thumb)

    @patch("bot._urlopen")
    def test_html_tags_stripped_from_text(self, mock_urlopen):
        mock_urlopen.return_value = _make_mock_response({
            "author_name": "chan",
            "html": '<blockquote><b>Bold</b> and <a href="https://x.com">link</a></blockquote>',
        })
        title, _ = bot._fetch_telegram_og_data("https://t.me/chan/7")
        self.assertNotIn("<b>", title)
        self.assertNotIn("<a ", title)
        self.assertIn("Bold", title)
        self.assertIn("link", title)

    @patch("bot._urlopen")
    def test_returns_none_on_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Connection refused")
        title, thumb = bot._fetch_telegram_og_data("https://t.me/chan/1")
        self.assertIsNone(title)
        self.assertIsNone(thumb)

    def test_returns_none_for_channel_root_url(self):
        """URL without post_id (just https://t.me/channel) → None, None."""
        title, thumb = bot._fetch_telegram_og_data("https://t.me/nerdpapers")
        self.assertIsNone(title)
        self.assertIsNone(thumb)


class TestGetOgPreviewDataTelegramEarlyReturn(unittest.TestCase):
    """Verify _get_og_preview_data delegates to Telegram oEmbed for t.me URLs."""

    def test_early_return_for_tme_url(self):
        """When _fetch_telegram_og_data succeeds, _get_og_preview_data returns
        its data directly without calling standard _urlopen."""
        with (
            patch.object(bot, "_fetch_telegram_og_data",
                         return_value=("nerdpapers: Some post text", "https://cdn.t.me/img.jpg")) as mock_tg,
            patch.object(bot, "_urlopen") as mock_urlopen,
        ):
            title, image_url, is_invidious, warning, jina_md = bot._get_og_preview_data(
                "https://t.me/nerdpapers/3349"
            )

        mock_tg.assert_called_once_with("https://t.me/nerdpapers/3349")
        self.assertEqual(title, "nerdpapers: Some post text")
        self.assertEqual(image_url, "https://cdn.t.me/img.jpg")
        self.assertFalse(is_invidious)
        self.assertIsNone(warning)
        self.assertIsNone(jina_md)
        # Standard urlopen should NOT have been called (early exit)
        mock_urlopen.assert_not_called()

    def test_falls_through_when_oembed_returns_none(self):
        """If oEmbed returns (None, None), standard _urlopen fetch should proceed."""
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "text/html"
        mock_resp.read.return_value = b"<html><title>Fallback</title></html>"
        mock_urlopen_cm = MagicMock()
        mock_urlopen_cm.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen_cm.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(bot, "_fetch_telegram_og_data", return_value=(None, None)),
            patch.object(bot, "_urlopen", return_value=mock_urlopen_cm) as mock_urlopen,
        ):
            bot._get_og_preview_data("https://t.me/nerdpapers/3349")
            mock_urlopen.assert_called()





if __name__ == "__main__":
    unittest.main()
