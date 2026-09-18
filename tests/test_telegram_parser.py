"""
Tests for _fetch_telegram_og_data (Telegram public preview page parser).
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
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=mock_resp)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class TestFetchTelegramOgData(unittest.TestCase):
    """Unit tests for _fetch_telegram_og_data."""

    @patch("bot._urlopen")
    def test_standard_post_url(self, mock_urlopen):
        """https://t.me/channel/123 → title built from author + text excerpt, and returns markdown."""
        html = """
        <div class="tgme_widget_message" data-post="nerdpapers/3349">
          <div class="tgme_widget_message_owner_name">nerdpapers</div>
          <div class="tgme_widget_message_text">Interesting article about Python.</div>
          <div class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn.t.me/thumb.jpg')"></div>
        </div>
        """
        mock_urlopen.return_value = _make_mock_response(html)
        title, thumb, md = bot._fetch_telegram_og_data("https://t.me/nerdpapers/3349")
        self.assertIn("nerdpapers", title)
        self.assertIn("Interesting article about Python", title)
        self.assertEqual(thumb, "https://cdn.t.me/thumb.jpg")
        self.assertIn("# nerdpapers", md)
        self.assertIn("Interesting article about Python.", md)
        self.assertIn("![Media](https://cdn.t.me/thumb.jpg)", md)

    @patch("bot._urlopen")
    def test_s_prefix_url_normalised(self, mock_urlopen):
        """https://t.me/s/channel/123 should be resolved and parsed correctly."""
        html = """
        <div class="tgme_widget_message" data-post="nerdpapers/3349">
          <div class="tgme_widget_message_owner_name">nerdpapers</div>
          <div class="tgme_widget_message_text">Post text</div>
        </div>
        """
        mock_urlopen.return_value = _make_mock_response(html)
        title, thumb, md = bot._fetch_telegram_og_data("https://t.me/s/nerdpapers/3349")
        self.assertIn("nerdpapers", title)
        self.assertIn("Post text", md)
        # Verify the call used the canonical URL (with 's/')
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://t.me/s/nerdpapers/3349")

    @patch("bot._urlopen")
    def test_long_text_truncated_to_200_chars(self, mock_urlopen):
        long_text = "A" * 300
        html = f"""
        <div class="tgme_widget_message" data-post="chan/1">
          <div class="tgme_widget_message_owner_name">chan</div>
          <div class="tgme_widget_message_text">{long_text}</div>
        </div>
        """
        mock_urlopen.return_value = _make_mock_response(html)
        title, _, _ = bot._fetch_telegram_og_data("https://t.me/chan/1")
        # excerpt is max 200 chars + ellipsis, plus "chan: " prefix
        self.assertIn("…", title)
        self.assertLessEqual(len(title), len("chan: ") + 200 + 1)  # 1 for ellipsis char

    @patch("bot._urlopen")
    def test_no_text_field_uses_author_only(self, mock_urlopen):
        html = """
        <div class="tgme_widget_message" data-post="mychannel/42">
          <div class="tgme_widget_message_owner_name">mychannel</div>
        </div>
        """
        mock_urlopen.return_value = _make_mock_response(html)
        title, thumb, md = bot._fetch_telegram_og_data("https://t.me/mychannel/42")
        self.assertEqual(title, "mychannel")
        self.assertIsNone(thumb)
        self.assertEqual(md, "# mychannel")

    @patch("bot._urlopen")
    def test_html_tags_stripped_and_newlines_preserved(self, mock_urlopen):
        html = """
        <div class="tgme_widget_message" data-post="chan/7">
          <div class="tgme_widget_message_owner_name">chan</div>
          <div class="tgme_widget_message_text"><b>Bold</b><br/>and <a href="https://x.com">link</a></div>
        </div>
        """
        mock_urlopen.return_value = _make_mock_response(html)
        title, _, md = bot._fetch_telegram_og_data("https://t.me/chan/7")
        self.assertNotIn("<b>", title)
        self.assertNotIn("<a ", title)
        self.assertIn("Bold", title)
        self.assertIn("link", title)
        # Check preserved newlines in markdown
        self.assertIn("Bold\nand link", md)

    @patch("bot._urlopen")
    def test_returns_none_on_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Connection refused")
        title, thumb, md = bot._fetch_telegram_og_data("https://t.me/chan/1")
        self.assertIsNone(title)
        self.assertIsNone(thumb)
        self.assertIsNone(md)

    def test_returns_none_for_channel_root_url(self):
        """URL without post_id (just https://t.me/channel) → None, None, None."""
        title, thumb, md = bot._fetch_telegram_og_data("https://t.me/nerdpapers")
        self.assertIsNone(title)
        self.assertIsNone(thumb)
        self.assertIsNone(md)


class TestGetOgPreviewDataTelegramEarlyReturn(unittest.TestCase):
    """Verify _get_og_preview_data delegates to Telegram parser for t.me URLs."""

    def test_early_return_for_tme_url(self):
        """When _fetch_telegram_og_data succeeds, _get_og_preview_data returns
        its data directly including parsed markdown without calling standard _urlopen."""
        with (
            patch.object(bot, "_fetch_telegram_og_data",
                         return_value=("nerdpapers: Some post text", "https://cdn.t.me/img.jpg", "# nerdpapers\n\nSome post text")) as mock_tg,
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
        self.assertEqual(jina_md, "# nerdpapers\n\nSome post text")
        # Standard urlopen should NOT have been called (early exit)
        mock_urlopen.assert_not_called()

    def test_falls_through_when_tg_parser_returns_none(self):
        """If Telegram parser returns (None, None, None), standard _urlopen fetch should proceed."""
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "text/html"
        mock_resp.read.return_value = b"<html><title>Fallback</title></html>"
        mock_urlopen_cm = MagicMock()
        mock_urlopen_cm.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen_cm.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(bot, "_fetch_telegram_og_data", return_value=(None, None, None)),
            patch.object(bot, "_urlopen", return_value=mock_urlopen_cm) as mock_urlopen,
        ):
            bot._get_og_preview_data("https://t.me/nerdpapers/3349")
            mock_urlopen.assert_called()


class TestTgBridgeDelegation(unittest.TestCase):
    """Verify delegation of Telegram post links to TG Bridge when present in chat."""

    def test_is_telegram_post_url(self):
        self.assertTrue(bot._is_telegram_post_url("https://t.me/channel/123"))
        self.assertTrue(bot._is_telegram_post_url("https://t.me/s/channel/456"))
        self.assertTrue(bot._is_telegram_post_url("https://telegram.me/channel/789"))
        self.assertTrue(bot._is_telegram_post_url("https://t.me/durov/100?single"))
        # Invalid / channel root / private channel numeric
        self.assertFalse(bot._is_telegram_post_url("https://t.me/channel"))
        self.assertFalse(bot._is_telegram_post_url("https://t.me/c/123456/10"))
        self.assertFalse(bot._is_telegram_post_url("https://example.com/channel/123"))

    def test_is_tg_bridge_in_chat_true(self):
        mock_bot = MagicMock()
        mock_bot.rpc.get_chat_contacts.return_value = [10, 20]
        c1 = MagicMock()
        c1.display_name = "Alice"
        c2 = MagicMock()
        c2.display_name = "TG Bridge"
        mock_bot.rpc.get_contact.side_effect = lambda accid, cid: c1 if cid == 10 else c2

        self.assertTrue(bot._is_tg_bridge_in_chat(mock_bot, 1, 100))

    def test_is_tg_bridge_in_chat_false(self):
        mock_bot = MagicMock()
        mock_bot.rpc.get_chat_contacts.return_value = [10]
        c1 = MagicMock()
        c1.display_name = "Alice"
        mock_bot.rpc.get_contact.return_value = c1

        self.assertFalse(bot._is_tg_bridge_in_chat(mock_bot, 1, 100))

    @patch("bot._do_group_link_preview")
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._is_duplicate_msg", return_value=False)
    @patch("bot._is_bot_blocked", return_value=False)
    @patch("bot._is_tg_bridge_in_chat", return_value=True)
    def test_on_new_message_skips_telegram_post_if_tg_bridge_in_chat(
        self, mock_tg_bridge, mock_blocked, mock_dup, mock_rate, mock_preview
    ):
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.id = 1
        mock_event.msg.is_info = False
        mock_event.msg.from_id = 42
        mock_event.msg.chat_id = 99
        mock_event.msg.text = "Check this out https://t.me/channel/123"

        with patch.object(bot, "dc_accid", 1):
            bot.on_new_message(mock_bot, 1, mock_event)

        mock_preview.assert_not_called()

    @patch("threading.Thread")
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._is_duplicate_msg", return_value=False)
    @patch("bot._is_bot_blocked", return_value=False)
    @patch("bot._is_tg_bridge_in_chat", return_value=False)
    def test_on_new_message_processes_telegram_post_if_no_tg_bridge(
        self, mock_tg_bridge, mock_blocked, mock_dup, mock_rate, mock_thread
    ):
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.id = 1
        mock_event.msg.is_info = False
        mock_event.msg.from_id = 42
        mock_event.msg.chat_id = 99
        mock_event.msg.text = "Check this out https://t.me/channel/123"

        with patch.object(bot, "dc_accid", 1):
            bot.on_new_message(mock_bot, 1, mock_event)

        mock_thread.assert_called_once()

    @patch("bot._do_group_link_preview")
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._is_duplicate_msg", return_value=False)
    @patch("bot._is_bot_blocked", return_value=False)
    @patch("bot._is_tg_bridge_in_chat", return_value=False)
    def test_on_new_message_skips_bot_card_prefixes(
        self, mock_tg_bridge, mock_blocked, mock_dup, mock_rate, mock_preview
    ):
        """Messages starting with bot prefixes like 📰, 🌐, 🤖, 📷, 💬 must never be auto-parsed."""
        mock_bot = MagicMock()
        with patch.object(bot, "dc_accid", 1):
            for prefix in ("📰", "🌐", "🤖", "📷", "💬"):
                mock_event = MagicMock()
                mock_event.msg.id = 10
                mock_event.msg.is_info = False
                mock_event.msg.from_id = 42
                mock_event.msg.chat_id = 99
                mock_event.msg.text = f"{prefix} **Pavel Durov**\n\nCheck post https://t.me/durov/42"
                bot.on_new_message(mock_bot, 1, mock_event)
                mock_preview.assert_not_called()

    def test_is_bot_blocked_detects_contact_is_bot(self):
        """_is_bot_blocked should check contact.is_bot if msg.is_bot is not True."""
        mock_bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.is_bot = False
        mock_msg.from_id = 77

        # When contact is a bot
        bot_contact = MagicMock()
        bot_contact.is_bot = True
        bot_contact.address = "tgbot@example.org"
        mock_bot.rpc.get_contact.return_value = bot_contact
        self.assertTrue(bot._is_bot_blocked(mock_bot, 1, mock_msg))

        # When contact is a human user
        human_contact = MagicMock()
        human_contact.is_bot = False
        human_contact.address = "human@example.org"
        mock_bot.rpc.get_contact.return_value = human_contact
        self.assertFalse(bot._is_bot_blocked(mock_bot, 1, mock_msg))


if __name__ == "__main__":
    unittest.main()

