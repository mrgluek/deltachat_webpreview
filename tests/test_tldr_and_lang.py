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
        database.init_db()
        bot.dc_accid = 1

    def tearDown(self):
        try:
            os.remove(_TEST_DB)
        except Exception:
            pass

    def test_database_chat_lang(self):
        # Default should be EN
        self.assertEqual(database.get_chat_lang(123), "EN")

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


if __name__ == "__main__":
    unittest.main()
