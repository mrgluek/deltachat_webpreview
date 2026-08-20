"""
Tests for /tldr command, /lang command, Gemini API summarization, and caption formatting.
"""
import os
import sys
import json
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
        self.msg.file = None
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
        bot._do_tldr(mock_bot, 1, 1, 100, 10, "https://example.com/test")
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

    @patch("threading.Thread")
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._is_bot_blocked", return_value=False)
    def test_dynamic_tldr_trigger(self, mock_blocked, mock_rate_limit, mock_thread_cls):
        urlhash = database.get_or_create_url_hash("https://news.org/dynamic")
        mock_bot = MagicMock()

        event = MockEvent(chat_id=1, text=f"/tldr_{urlhash}")
        bot.on_new_message(mock_bot, 1, event)

        mock_thread_cls.assert_called_once()
        kwargs = mock_thread_cls.call_args[1]
        self.assertEqual(kwargs["target"], bot._do_tldr)
        self.assertEqual(kwargs["args"][5], "https://news.org/dynamic")

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
        self.assertIn("/tldr", help_user)
        self.assertIn("/preview <url>", help_user)
        self.assertIn("/webxdc <url>", help_user)
        self.assertNotIn("/keep <url>", help_user)

        # Admin user help text
        mock_is_admin.return_value = True
        help_admin = bot.get_help_text(mock_bot, 1, 10)
        self.assertIn("/keep <url>", help_admin)

    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_tldr_text")
    def test_tldr_quoted_text_without_link(self, mock_do_tldr_text, mock_rate_limit):
        mock_bot = MagicMock()
        quoted_msg = MagicMock()
        quoted_msg.quote = {"text": "This is a very long text message without any links inside it, providing background and detailed instructions for everyone in the group."}
        
        event = MockEvent(chat_id=1, text="/tldr", quote=quoted_msg.quote)
        bot._handle_tldr_command(mock_bot, 1, event)

        mock_do_tldr_text.assert_called_once()
        args, kwargs = mock_do_tldr_text.call_args
        self.assertEqual(args[5], quoted_msg.quote["text"])

    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_tldr_text")
    def test_tldr_direct_plain_text(self, mock_do_tldr_text, mock_rate_limit):
        mock_bot = MagicMock()
        long_text = "This is a direct long text provided as payload without any HTTP links, describing the situation in great detail."
        
        event = MockEvent(chat_id=1, payload=long_text, text=f"/tldr {long_text}")
        bot._handle_tldr_command(mock_bot, 1, event)

        mock_do_tldr_text.assert_called_once()
        args, kwargs = mock_do_tldr_text.call_args
        self.assertEqual(args[5], long_text)

    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._send")
    def test_tldr_plain_text_too_short(self, mock_send, mock_rate_limit):
        mock_bot = MagicMock()
        event = MockEvent(chat_id=1, payload="short", text="/tldr short")
        bot._handle_tldr_command(mock_bot, 1, event)

    @patch.object(bot, "GEMINI_API_KEY", "fake_key")
    @patch("bot._urlopen")
    def test_ask_gemini_ai_direct_query(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "Quantum computing uses qubits."}]}}]}'
        )
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = bot._ask_gemini_ai("Explain quantum computing", target_lang="EN")
        self.assertEqual(res, "Quantum computing uses qubits.")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch.object(bot, "GEMINI_API_KEY", "fake_key")
    @patch("bot._urlopen")
    def test_ask_gemini_ai_with_context(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "The conclusion is positive."}]}}]}'
        )
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = bot._ask_gemini_ai(
            "What is the conclusion?",
            context="The study investigated solar power and found 95% efficiency.",
            target_lang="RU"
        )
        self.assertEqual(res, "The conclusion is positive.")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch.object(bot, "GEMINI_API_KEY", "fake_key")
    @patch("bot._urlopen")
    def test_ai_caching(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "Cached AI answer."}]}}]}'
        )
        mock_urlopen.return_value.__enter__.return_value = mock_response

        query_key = "test_ai_hash_abc"
        # First call: hits _urlopen
        res1 = bot._ask_gemini_ai("What is Delta Chat?", target_lang="AUTO", query_key=query_key)
        self.assertEqual(res1, "Cached AI answer.")
        self.assertEqual(mock_urlopen.call_count, 1)

        # Second call: served from database cache without calling Gemini API again
        res2 = bot._ask_gemini_ai("What is Delta Chat?", target_lang="AUTO", query_key=query_key)
        self.assertEqual(res2, "Cached AI answer.")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch.object(bot, "GEMINI_API_KEY", "")
    @patch("bot._send")
    def test_handle_ai_command_no_key(self, mock_send):
        mock_bot = MagicMock()
        event = MockEvent(chat_id=1, payload="Explain physics", text="/ai Explain physics")
        bot._handle_ai_command(mock_bot, 1, event)
        # Note: _do_ai_query runs in background or is called
        # Wait slightly or test _do_ai_query directly
        bot._do_ai_query(mock_bot, 1, 1, 100, 10, "Explain physics")
        mock_send.assert_called()
        self.assertIn("GEMINI_API_KEY", mock_send.call_args[0][3])

    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._send")
    def test_handle_ai_command_empty(self, mock_send, mock_rate_limit):
        mock_bot = MagicMock()
        event = MockEvent(chat_id=1, payload="", text="/ai")
        bot._handle_ai_command(mock_bot, 1, event)

        mock_send.assert_called_once()
        self.assertIn("Usage:", mock_send.call_args[0][3])
        self.assertIn("/ai", mock_send.call_args[0][3])

    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_ai_query")
    def test_handle_ai_command_direct_payload(self, mock_do_ai, mock_rate_limit):
        mock_bot = MagicMock()
        event = MockEvent(chat_id=1, payload="Explain relativity", text="/ai Explain relativity")
        bot._handle_ai_command(mock_bot, 1, event)

        mock_do_ai.assert_called_once()
        args, kwargs = mock_do_ai.call_args
        self.assertEqual(args[5], "Explain relativity")
        self.assertIsNone(args[6])

    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_ai_query")
    def test_handle_ai_command_quote_only(self, mock_do_ai, mock_rate_limit):
        mock_bot = MagicMock()
        quoted_msg = MagicMock()
        quoted_msg.quote = {"text": "How do rockets work in vacuum?"}

        event = MockEvent(chat_id=1, text="/ai", quote=quoted_msg.quote)
        bot._handle_ai_command(mock_bot, 1, event)

        mock_do_ai.assert_called_once()
        args, kwargs = mock_do_ai.call_args
        self.assertEqual(args[5], "How do rockets work in vacuum?")
        self.assertIsNone(args[6])

    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_ai_query")
    def test_handle_ai_command_quote_with_payload(self, mock_do_ai, mock_rate_limit):
        mock_bot = MagicMock()
        quoted_msg = MagicMock()
        quoted_msg.quote = {"text": "The experiment showed unexpected anomalies in sector 4."}

        event = MockEvent(chat_id=1, payload="Explain why this happened", text="/ai Explain why this happened", quote=quoted_msg.quote)
        bot._handle_ai_command(mock_bot, 1, event)

        mock_do_ai.assert_called_once()
        args, kwargs = mock_do_ai.call_args
        self.assertEqual(args[5], "Explain why this happened")
        self.assertEqual(args[6], quoted_msg.quote["text"])

    @patch("bot._is_dc_admin", return_value=False)
    def test_help_text_includes_ai(self, mock_is_admin):
        mock_bot = MagicMock()
        mock_contact = MagicMock()
        mock_contact.address = "user@example.com"
        mock_bot.rpc.get_contact.return_value = mock_contact
        database.set_config("admin_dc_email", "admin@example.com")
        help_user = bot.get_help_text(mock_bot, 1, 10)
        self.assertIn("/ai [text]", help_user)

    @patch.object(bot, "GEMINI_API_KEY", "fake_key")
    @patch("bot._urlopen")
    def test_ask_gemini_ai_short_single_word_answer(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "42"}]}}]}'
        )
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = bot._ask_gemini_ai("What is 6 * 7?", target_lang="EN")
        self.assertEqual(res, "42")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("threading.Thread", side_effect=lambda target, args=(), kwargs={}, **kw: MagicMock(start=lambda: target(*args, **kwargs)))
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_ai_query")
    def test_ai_command_case_insensitive(self, mock_do_ai, mock_rate_limit, mock_thread):
        mock_bot = MagicMock()

        # /AI uppercase
        event_upper = MockEvent(chat_id=1, msg_id=201, payload="What is 2+2?", text="/AI What is 2+2?")
        bot.ai_command(mock_bot, 1, event_upper)
        mock_do_ai.assert_called_once()
        self.assertEqual(mock_do_ai.call_args[0][5], "What is 2+2?")
        mock_do_ai.reset_mock()

        # /Ai mixed case
        event_mixed = MockEvent(chat_id=1, msg_id=202, payload="Explain speed", text="/Ai Explain speed")
        bot.ai_command(mock_bot, 1, event_mixed)
        mock_do_ai.assert_called_once()
        self.assertEqual(mock_do_ai.call_args[0][5], "Explain speed")

    @patch("threading.Thread", side_effect=lambda target, args=(), kwargs={}, **kw: MagicMock(start=lambda: target(*args, **kwargs)))
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_tldr")
    def test_tldr_command_case_insensitive(self, mock_do_tldr, mock_rate_limit, mock_thread):
        mock_bot = MagicMock()

        # /TLDR uppercase
        event_upper = MockEvent(chat_id=1, msg_id=203, payload="https://example.com/page", text="/TLDR https://example.com/page")
        bot.tldr_command(mock_bot, 1, event_upper)
        mock_do_tldr.assert_called_once()
        self.assertEqual(mock_do_tldr.call_args[0][5], "https://example.com/page")
        mock_do_tldr.reset_mock()

        # /Tldr mixed case
        event_mixed = MockEvent(chat_id=1, msg_id=204, payload="https://example.com/page", text="/Tldr https://example.com/page")
        bot.tldr_command(mock_bot, 1, event_mixed)
        mock_do_tldr.assert_called_once()
        self.assertEqual(mock_do_tldr.call_args[0][5], "https://example.com/page")

    def test_detect_image_mime(self):
        self.assertEqual(bot._detect_image_mime(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(bot._detect_image_mime(b"\x89PNG\r\n\x1a\n\x00"), "image/png")
        self.assertEqual(bot._detect_image_mime(b"RIFF\x00\x00\x00\x00WEBP"), "image/webp")
        self.assertEqual(bot._detect_image_mime(b"GIF89a\x01\x00"), "image/gif")
        self.assertEqual(bot._detect_image_mime(b"BM\x00\x00"), "image/bmp")
        self.assertEqual(bot._detect_image_mime(b"unknown_bytes", "photo.jpg"), "image/jpeg")
        self.assertEqual(bot._detect_image_mime(b"unknown_bytes", "diagram.png"), "image/png")
        self.assertIsNone(bot._detect_image_mime(b"unknown_bytes", "document.pdf"))

    def test_extract_image_from_msg_or_quote(self):
        # 1. Direct message file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\nfake_png_data")
            tmp_png = f.name
        
        try:
            mock_bot = MagicMock()
            msg_direct = MagicMock()
            msg_direct.file = tmp_png
            msg_direct.quote = None

            data, mime = bot._extract_image_from_msg_or_quote(mock_bot, 1, msg_direct)
            self.assertIsNotNone(data)
            self.assertEqual(mime, "image/png")

            # 2. Quote with image path
            msg_quote_img = MagicMock()
            msg_quote_img.file = None
            msg_quote_img.quote = {"image": tmp_png}

            data, mime = bot._extract_image_from_msg_or_quote(mock_bot, 1, msg_quote_img)
            self.assertIsNotNone(data)
            self.assertEqual(mime, "image/png")

            # 3. Quote with message_id (fetch via RPC)
            msg_quote_rpc = MagicMock()
            msg_quote_rpc.file = None
            msg_quote_rpc.quote = {"message_id": 555}

            quoted_rpc_msg = MagicMock()
            quoted_rpc_msg.file = tmp_png
            mock_bot.rpc.get_message.return_value = quoted_rpc_msg

            data, mime = bot._extract_image_from_msg_or_quote(mock_bot, 1, msg_quote_rpc)
            self.assertIsNotNone(data)
            self.assertEqual(mime, "image/png")
            mock_bot.rpc.get_message.assert_called_with(1, 555)

            # 4. Quote with camelCase messageId (standard Delta Chat JSON-RPC)
            msg_quote_camel = MagicMock()
            msg_quote_camel.file = None
            msg_quote_camel.quote = {"messageId": 666}

            mock_bot.rpc.get_message.reset_mock()
            mock_bot.rpc.get_message.return_value = quoted_rpc_msg
            data, mime = bot._extract_image_from_msg_or_quote(mock_bot, 1, msg_quote_camel)
            self.assertIsNotNone(data)
            self.assertEqual(mime, "image/png")
            mock_bot.rpc.get_message.assert_called_with(1, 666)

            # 5. Reply via parent_id / parentId without quote object
            msg_reply_parent = MagicMock()
            msg_reply_parent.file = None
            msg_reply_parent.quote = None
            msg_reply_parent.parent_id = None
            msg_reply_parent.parentId = 777

            mock_bot.rpc.get_message.reset_mock()
            mock_bot.rpc.get_message.return_value = quoted_rpc_msg
            data, mime = bot._extract_image_from_msg_or_quote(mock_bot, 1, msg_reply_parent)
            self.assertIsNotNone(data)
            self.assertEqual(mime, "image/png")
            mock_bot.rpc.get_message.assert_called_with(1, 777)
        finally:
            if os.path.exists(tmp_png):
                os.remove(tmp_png)

    @patch.object(bot, "GEMINI_API_KEY", "fake_key")
    @patch("bot._urlopen")
    def test_call_gemini_api_with_image(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "A photo of a cat."}]}}]}'
        )
        mock_urlopen.return_value.__enter__.return_value = mock_response

        fake_img_bytes = b"\xff\xd8\xff\xe0jpeg_data"
        res = bot._call_gemini_api("What is on this photo?", image_bytes=fake_img_bytes, image_mime="image/jpeg")
        self.assertEqual(res, "A photo of a cat.")

        req = mock_urlopen.call_args[0][0]
        req_payload = json.loads(req.data.decode("utf-8"))
        parts = req_payload["contents"][0]["parts"]
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]["text"], "What is on this photo?")
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/jpeg")

    @patch.object(bot, "GEMINI_API_KEY", "fake_key")
    @patch("bot._urlopen")
    def test_ask_gemini_ai_with_image(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "Diagram showing neural net layers."}]}}]}'
        )
        mock_urlopen.return_value.__enter__.return_value = mock_response

        fake_img_bytes = b"\x89PNG\r\n\x1a\npng_data"
        res = bot._ask_gemini_ai(
            query="",
            target_lang="RU",
            image_bytes=fake_img_bytes,
            image_mime="image/png",
            query_key="img_test_123"
        )
        self.assertEqual(res, "Diagram showing neural net layers.")
        self.assertEqual(mock_urlopen.call_count, 1)

        # Caching check: second call should not hit _urlopen
        res2 = bot._ask_gemini_ai(
            query="",
            target_lang="RU",
            image_bytes=fake_img_bytes,
            image_mime="image/png",
            query_key="img_test_123"
        )
        self.assertEqual(res2, "Diagram showing neural net layers.")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("threading.Thread", side_effect=lambda target, args=(), kwargs={}, **kw: MagicMock(start=lambda: target(*args, **kwargs)))
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_ai_query")
    def test_handle_ai_command_with_image(self, mock_do_ai, mock_rate_limit, mock_thread):
        mock_bot = MagicMock()

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"\xff\xd8\xff\xe0jpeg_data")
            tmp_jpg = f.name

        try:
            # Direct image with question
            event = MockEvent(chat_id=1, msg_id=301, payload="What kind of animal is this?", text="/ai What kind of animal is this?")
            event.msg.file = tmp_jpg

            bot._handle_ai_command(mock_bot, 1, event)
            mock_do_ai.assert_called_once()
            args, kwargs = mock_do_ai.call_args
            self.assertEqual(args[5], "What kind of animal is this?")
            self.assertIsNotNone(kwargs.get("image_bytes"))
            self.assertEqual(kwargs.get("image_mime"), "image/jpeg")
            mock_do_ai.reset_mock()

            # Direct image without question (just /ai)
            event_empty = MockEvent(chat_id=1, msg_id=302, payload="", text="/ai")
            event_empty.msg.file = tmp_jpg

            bot._handle_ai_command(mock_bot, 1, event_empty)
            mock_do_ai.assert_called_once()
            args, kwargs = mock_do_ai.call_args
            self.assertEqual(args[5], "")
            self.assertIsNotNone(kwargs.get("image_bytes"))
        finally:
            if os.path.exists(tmp_jpg):
                os.remove(tmp_jpg)


if __name__ == "__main__":
    unittest.main()
