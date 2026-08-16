"""
Tests for /tldr command, /lang command, Gemini API summarization, and caption formatting.
"""
import os
import sys
import unittest
import tempfile
from unittest.mock import MagicMock, patch

_TEST_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TEST_DB

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import database

database.init_db()


class MockEvent:
    def __init__(self, from_id=10, chat_id=1, msg_id=100, payload="", text="", quote=None):
        self.msg = MagicMock()
        self.msg.from_id = from_id
        self.msg.chat_id = chat_id
        self.msg.id = msg_id
        self.msg.text = text
        self.msg.is_bot = False
        self.msg.is_info = False
        self.msg.quote = quote
        self.payload = payload


class TestTldrAndLang(unittest.TestCase):

    def setUp(self):
        self.old_db_path = database.DB_PATH
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        database.DB_PATH = self.tmp_db.name
        database.init_db()
        bot.dc_accid = 1
        bot._processed_msg_ids.clear()

    def tearDown(self):
        try:
            os.remove(self.tmp_db.name)
        except Exception:
            pass
        database.DB_PATH = self.old_db_path

    def test_database_chat_lang(self):
        # Default should be AUTO
        self.assertEqual(database.get_chat_lang(123), "AUTO")

        # Set to RU
        database.set_chat_lang(123, "RU")
        self.assertEqual(database.get_chat_lang(123), "RU")

        # Set to DE
        database.set_chat_lang(123, "de")
        self.assertEqual(database.get_chat_lang(123), "DE")

    def test_extract_url_from_payload_or_quote(self):
        # 1. Payload URL
        url = bot._extract_url_from_msg_or_payload("https://example.com/article", MockEvent().msg)
        self.assertEqual(url, "https://example.com/article")

        # 2. Quote URL
        msg_with_quote = MagicMock()
        msg_with_quote.quote = {"text": "Check this out https://news.org/post"}
        url2 = bot._extract_url_from_msg_or_payload("", msg_with_quote)
        self.assertEqual(url2, "https://news.org/post")

        # 3. No URL
        self.assertIsNone(bot._extract_url_from_msg_or_payload("", MockEvent().msg))

    @patch.object(bot, "GEMINI_API_KEY", "fake_key")
    @patch("bot._urlopen")
    def test_summarize_text_with_gemini(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json_resp = (
            b'{"candidates": [{"content": {"parts": [{"text": "Sample AI summary of the article."}]}}]}'
        )
        mock_urlopen.return_value.__enter__.return_value = mock_response

        text = "This is a long article text about technology and science. " * 10
        summary = bot._summarize_text_with_gemini(text, title="Test", target_lang="RU", short_paragraph=True)
        
        self.assertEqual(summary, "Sample AI summary of the article.")

    @patch.object(bot, "GEMINI_API_KEY", "fake_key")
    @patch("bot._urlopen")
    def test_tldr_caching(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "Cached AI summary."}]}}]}'
        )
        mock_urlopen.return_value.__enter__.return_value = mock_response

        text = "This is a long article text about technology and science. " * 10
        url_key = "test_url_hash_123"

        # First call: hits _urlopen
        res1 = bot._summarize_text_with_gemini(text, title="Test", target_lang="RU", short_paragraph=True, url_key=url_key)
        self.assertEqual(res1, "Cached AI summary.")
        self.assertEqual(mock_urlopen.call_count, 1)

        # Second call: served from database cache without calling Gemini API again
        res2 = bot._summarize_text_with_gemini(text, title="Test", target_lang="RU", short_paragraph=True, url_key=url_key)
        self.assertEqual(res2, "Cached AI summary.")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch.object(bot, "GEMINI_API_KEY", "fake_key")
    @patch.object(bot, "GEMINI_MODELS", ["model1", "model2"])
    @patch("bot._urlopen")
    def test_multi_model_fallback(self, mock_urlopen):
        import urllib.error
        mock_resp_ok = MagicMock()
        mock_resp_ok.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "Fallback model summary."}]}}]}'
        )
        cm_ok = MagicMock()
        cm_ok.__enter__.return_value = mock_resp_ok
        
        err = urllib.error.HTTPError("http://example.com", 429, "Too Many Requests", {}, MagicMock(read=lambda: b"{}"))
        mock_urlopen.side_effect = [
            err,
            cm_ok
        ]

        text = "This is a long article text about technology and science. " * 10
        res = bot._summarize_text_with_gemini(text, title="Test", target_lang="EN")
        self.assertEqual(res, "Fallback model summary.")
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch.object(bot, "GEMINI_API_KEY", "fake_key")
    @patch("bot._summarize_text_with_gemini")
    def test_format_preview_caption_with_tldr(self, mock_summarize):
        mock_summarize.return_value = "Brief 1-paragraph summary."
        database.set_chat_lang(999, "RU")

        caption = bot._format_preview_caption(
            title="Activist charged",
            url="https://arstechnica.com/article",
            mode="webxdc",
            chat_id=999,
            jina_markdown="Some article markdown content..."
        )

        expected = (
            "⚡ TL;DR: Brief 1-paragraph summary.\n\n"
            "🔗 [Activist charged](https://arstechnica.com/article)"
        )
        self.assertEqual(caption, expected)

    @patch.object(bot, "GEMINI_API_KEY", "")
    def test_format_preview_caption_without_gemini_key(self):
        caption = bot._format_preview_caption(
            title="My Page",
            url="https://example.com",
            mode="readability",
            chat_id=1,
            jina_markdown="Text"
        )
        self.assertEqual(caption, "🔗 [My Page](https://example.com)")

    @patch.object(bot, "GEMINI_API_KEY", "")
    @patch("bot._send")
    def test_handle_tldr_command_no_key(self, mock_send):
        mock_bot = MagicMock()
        event = MockEvent(chat_id=1, payload="https://example.com/test", text="/tldr https://example.com/test")
        bot._handle_tldr_command(mock_bot, 1, event)
        mock_send.assert_called_once()
        self.assertIn("GEMINI_API_KEY", mock_send.call_args[0][3])

    @patch("bot._send")
    def test_lang_command(self, mock_send):
        mock_bot = MagicMock()
        event = MockEvent(chat_id=50, payload="RU", text="/lang RU")
        bot._handle_lang_command(mock_bot, 1, event)

        self.assertEqual(database.get_chat_lang(50), "RU")
        mock_send.assert_called_once()
        self.assertIn("Preferred summary language for this chat set to **RU**", mock_send.call_args[0][3])

    @patch("bot._do_tldr")
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._is_bot_blocked", return_value=False)
    def test_dynamic_tldr_trigger(self, mock_blocked, mock_rate_limit, mock_do_tldr):
        urlhash = database.get_or_create_url_hash("https://news.org/dynamic")
        mock_bot = MagicMock()

        event = MockEvent(chat_id=1, text=f"/tldr_{urlhash}")
        bot.on_new_message(mock_bot, 1, event)

        mock_do_tldr.assert_called_once()
        args, kwargs = mock_do_tldr.call_args
        self.assertEqual(args[5], "https://news.org/dynamic")

    @patch("bot._is_dc_admin")
    def test_format_preview_buttons(self, mock_is_admin):
        mock_bot = MagicMock()
        urlhash = "1234abcd"

        # Regular user: /tldr, /preview, /webxdc
        mock_is_admin.return_value = False
        buttons_user = bot._format_preview_buttons(mock_bot, 1, 10, urlhash)
        self.assertEqual(buttons_user, "⚡\u00a0/tldr_1234abcd   🖥️\u00a0/preview_1234abcd   📦\u00a0/webxdc_1234abcd")
        self.assertNotIn("/keep", buttons_user)

        # Admin user: /tldr, /preview, /webxdc, /keep
        mock_is_admin.return_value = True
        buttons_admin = bot._format_preview_buttons(mock_bot, 1, 10, urlhash)
        self.assertEqual(buttons_admin, "⚡\u00a0/tldr_1234abcd   🖥️\u00a0/preview_1234abcd   📦\u00a0/webxdc_1234abcd   🏛️\u00a0/keep_1234abcd")
        self.assertIn("/keep_1234abcd", buttons_admin)

    @patch("bot._is_dc_admin")
    def test_help_text_keep_visibility(self, mock_is_admin):
        mock_bot = MagicMock()
        mock_contact = MagicMock()
        mock_contact.address = "user@example.com"
        mock_bot.rpc.get_contact.return_value = mock_contact
        database.set_config("admin_dc_email", "admin@example.com")

        # Regular user help text
        mock_is_admin.return_value = False
        help_user = bot.get_help_text(mock_bot, 1, 10)
        self.assertIn("/tldr <url>", help_user)
        self.assertIn("/preview <url>", help_user)
        self.assertIn("/webxdc <url>", help_user)
        self.assertNotIn("/keep <url>", help_user)

        # Admin user help text
        mock_is_admin.return_value = True
        help_admin = bot.get_help_text(mock_bot, 1, 10)
        self.assertIn("/keep <url>", help_admin)


if __name__ == "__main__":
    unittest.main()
