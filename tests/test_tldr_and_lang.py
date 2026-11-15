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

    def test_gemini_models_default_chain(self):
        self.assertTrue(len(bot.GEMINI_MODELS) > 0)
        self.assertEqual(bot.GEMINI_MODELS[0], "gemini-4-hydrogen")
        self.assertIn("gemini-3.8-flash", bot.GEMINI_MODELS)
        self.assertIn("gemini-3.7-flash", bot.GEMINI_MODELS)

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

    @patch.object(bot, "OPENROUTER_API_KEY", "")
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

    @patch.object(bot, "OPENROUTER_API_KEY", "")
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

    @patch("threading.Thread", side_effect=lambda target, args=(), kwargs={}, **kw: MagicMock(start=lambda: target(*args, **kwargs)))
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._send")
    def test_handle_ai_command_empty(self, mock_send, mock_rate_limit, mock_thread):
        mock_bot = MagicMock()
        event = MockEvent(chat_id=1, payload="", text="/ai")
        bot._handle_ai_command(mock_bot, 1, event)

        mock_send.assert_called_once()
        self.assertIn("Usage:", mock_send.call_args[0][3])
        self.assertIn("/ai", mock_send.call_args[0][3])

    @patch("threading.Thread", side_effect=lambda target, args=(), kwargs={}, **kw: MagicMock(start=lambda: target(*args, **kwargs)))
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_ai_query")
    def test_handle_ai_command_direct_payload(self, mock_do_ai, mock_rate_limit, mock_thread):
        mock_bot = MagicMock()
        event = MockEvent(chat_id=1, payload="Explain relativity", text="/ai Explain relativity")
        bot._handle_ai_command(mock_bot, 1, event)

        mock_do_ai.assert_called_once()
        args, kwargs = mock_do_ai.call_args
        self.assertEqual(args[5], "Explain relativity")
        self.assertIsNone(args[6])

    @patch("threading.Thread", side_effect=lambda target, args=(), kwargs={}, **kw: MagicMock(start=lambda: target(*args, **kwargs)))
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_ai_query")
    def test_handle_ai_command_quote_only(self, mock_do_ai, mock_rate_limit, mock_thread):
        mock_bot = MagicMock()
        quoted_msg = MagicMock()
        quoted_msg.quote = {"text": "How do rockets work in vacuum?"}

        event = MockEvent(chat_id=1, text="/ai", quote=quoted_msg.quote)
        bot._handle_ai_command(mock_bot, 1, event)

        mock_do_ai.assert_called_once()
        args, kwargs = mock_do_ai.call_args
        self.assertEqual(args[5], "How do rockets work in vacuum?")
        self.assertIsNone(args[6])

    @patch("threading.Thread", side_effect=lambda target, args=(), kwargs={}, **kw: MagicMock(start=lambda: target(*args, **kwargs)))
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_ai_query")
    def test_handle_ai_command_quote_with_payload(self, mock_do_ai, mock_rate_limit, mock_thread):
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


class TestOpenRouterFallback(unittest.TestCase):
    def setUp(self):
        bot._GEMINI_MODEL_COOLDOWNS.clear()

    @staticmethod
    def _resp(body):
        m = MagicMock()
        m.__enter__.return_value.read.return_value = json.dumps(body).encode()
        return m

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_MODELS", ["openrouter/free"])
    @patch.object(bot, "OPENROUTER_API_KEY", "or_key")
    @patch.object(bot, "GEMINI_MODELS", ["gemini-x"])
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    @patch("bot._urlopen")
    def test_falls_back_to_openrouter_when_gemini_fails(self, mock_urlopen, _log):
        import urllib.error
        gemini_err = urllib.error.HTTPError("u", 503, "Unavailable", {}, None)
        mock_urlopen.side_effect = [gemini_err, self._resp({"model": "x:free", "choices": [{"message": {"content": "Fallback answer."}}]})]
        self.assertEqual(bot._call_gemini_api("hi"), "Fallback answer.")
        req = mock_urlopen.call_args_list[1][0][0]
        self.assertIn("openrouter.ai", req.full_url)
        self.assertEqual(json.loads(req.data)["model"], "openrouter/free")
        self.assertEqual(req.get_header("Authorization"), "Bearer or_key")

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_MODELS", ["gemini-gone"])
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    @patch("bot._urlopen")
    def test_gemini_404_gets_long_cooldown(self, mock_urlopen, _log):
        import urllib.error, time
        mock_urlopen.side_effect = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        self.assertIsNone(bot._call_gemini_api("hi"))
        self.assertGreater(bot._GEMINI_MODEL_COOLDOWNS["gemini-gone"], time.time() + 3600)

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_MODELS", ["openrouter/free"])
    @patch.object(bot, "OPENROUTER_API_KEY", "or_key")
    @patch.object(bot, "GEMINI_API_KEY", "")
    @patch("bot._urlopen")
    def test_openrouter_only_with_image(self, mock_urlopen, _log):
        mock_urlopen.return_value = self._resp({"choices": [{"message": {"content": "A cat."}}]})
        self.assertEqual(bot._call_gemini_api("describe", image_bytes=b"img", image_mime="image/png"), "A cat.")
        content = json.loads(mock_urlopen.call_args[0][0].data)["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "describe"})
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    @patch.object(bot, "OPENROUTER_API_KEY", "or_key")
    @patch.object(bot, "GEMINI_API_KEY", "")
    @patch("bot._urlopen")
    def test_openrouter_skips_audio(self, mock_urlopen):
        self.assertIsNone(bot._call_gemini_api("sum", media_bytes=b"ogg", media_mime="audio/ogg"))
        mock_urlopen.assert_not_called()

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_MODELS", ["openrouter/free"])
    @patch.object(bot, "OPENROUTER_API_KEY", "or_key")
    @patch.object(bot, "GEMINI_API_KEY", "")
    @patch("bot._urlopen")
    def test_openrouter_retries_empty_answer_once(self, mock_urlopen, _log):
        mock_urlopen.side_effect = [
            self._resp({"choices": [{"finish_reason": "length", "message": {"content": None}}]}),
            self._resp({"choices": [{"message": {"content": "Second try."}}]}),
        ]
        self.assertEqual(bot._call_gemini_api("hi"), "Second try.")
        self.assertEqual(json.loads(mock_urlopen.call_args[0][0].data)["reasoning"]["effort"], "low")

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_MODELS", ["openrouter/free"])
    @patch.object(bot, "OPENROUTER_API_KEY", "or_key")
    @patch.object(bot, "GEMINI_API_KEY", "")
    @patch("bot._urlopen")
    def test_openrouter_does_not_retry_timeout(self, mock_urlopen, _log):
        mock_urlopen.side_effect = TimeoutError("read timed out")
        self.assertIsNone(bot._call_gemini_api("hi"))
        self.assertEqual(mock_urlopen.call_count, 1)


class TestAIModelTag(unittest.TestCase):
    def setUp(self):
        bot._GEMINI_MODEL_COOLDOWNS.clear()
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self._old_db = database.DB_PATH
        database.DB_PATH = self.db
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self._old_db
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db + suffix):
                os.remove(self.db + suffix)

    def test_tag_formatting(self):
        self.assertEqual(bot._ai_model_tag(bot.AIText("x", "gemini-3.8-flash")), " *(gemini-3.8-flash)*")
        self.assertEqual(bot._ai_model_tag("plain"), "")
        self.assertEqual(bot.AIText("hello", "m"), "hello")

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_MODELS", ["gemini-a", "gemini-b"])
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    @patch("bot._urlopen")
    def test_gemini_answer_carries_fallback_model(self, mock_urlopen, _log):
        import urllib.error
        ok = MagicMock()
        ok.__enter__.return_value.read.return_value = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "Answer."}]}}]}).encode()
        mock_urlopen.side_effect = [urllib.error.HTTPError("u", 503, "x", {}, None), ok]
        self.assertEqual(bot._call_gemini_api("hi").model, "gemini-b")

    @patch("bot._call_gemini_api")
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    def test_model_survives_cache(self, mock_call):
        mock_call.return_value = bot.AIText("Cached summary text.", "gemini-3.8-flash")
        text = "A long enough article text to be summarized by the bot. " * 3
        first = bot._summarize_text_with_gemini(text, url_key="abc")
        second = bot._summarize_text_with_gemini(text, url_key="abc")
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(first.model, "gemini-3.8-flash")
        self.assertEqual(second, "Cached summary text.")
        self.assertEqual(second.model, "gemini-3.8-flash")

    @patch("bot._summarize_text_with_gemini")
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    def test_preview_caption_shows_model(self, mock_sum):
        mock_sum.return_value = bot.AIText("Short summary.", "openrouter-model:free")
        caption = bot._format_preview_caption("T", "https://example.com", "readability", 1, jina_markdown="Text")
        self.assertTrue(caption.startswith("⚡ TL;DR *(openrouter-model:free)*: Short summary."))


class TestGeminiChainLimits(unittest.TestCase):
    def setUp(self):
        bot._GEMINI_MODEL_COOLDOWNS.clear()

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_MODELS", ["gemma-4-26b-a4b-it"])
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    @patch("bot._urlopen")
    def test_thought_parts_are_dropped(self, mock_urlopen, _log):
        ok = MagicMock()
        ok.__enter__.return_value.read.return_value = json.dumps({"candidates": [{"content": {"parts": [
            {"text": "*   Question: ... Draft 1 ...", "thought": True},
            {"text": "Я — большая языковая модель."},
        ]}}]}).encode()
        mock_urlopen.return_value = ok
        self.assertEqual(bot._call_gemini_api("hi"), "Я — большая языковая модель.")

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_MODELS", ["a", "b", "c", "d"])
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    @patch("bot._urlopen")
    def test_stops_after_two_timeouts(self, mock_urlopen, _log):
        mock_urlopen.side_effect = TimeoutError("The read operation timed out")
        self.assertIsNone(bot._call_gemini_api("hi"))
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_TIME_BUDGET", 0)
    @patch.object(bot, "GEMINI_MODELS", ["a"])
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    @patch("bot._urlopen")
    def test_time_budget_exhausted(self, mock_urlopen, _log):
        self.assertIsNone(bot._call_gemini_api("hi"))
        mock_urlopen.assert_not_called()


class TestGeminiTools(unittest.TestCase):
    """GEMINI_TOOLS adds built-in tools to /ai requests only, and never costs a model its place in the chain."""

    def setUp(self):
        bot._GEMINI_MODEL_COOLDOWNS.clear()
        bot._GEMINI_TOOLS_BACKOFF.clear()

    @staticmethod
    def _ok(text="answer"):
        ok = MagicMock()
        ok.__enter__.return_value.read.return_value = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode()
        return ok

    @staticmethod
    def _tools_sent(call):
        return json.loads(call.args[0].data.decode()).get("tools")

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_TOOLS", ["code_execution", "url_context"])
    @patch.object(bot, "GEMINI_MODELS", ["gemini-4-hydrogen"])
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    @patch("bot._urlopen")
    def test_tools_only_when_requested(self, mock_urlopen, _log):
        mock_urlopen.return_value = self._ok()
        bot._call_gemini_api("hi")
        self.assertIsNone(self._tools_sent(mock_urlopen.call_args))
        bot._call_gemini_api("hi", for_ai=True)
        self.assertEqual(self._tools_sent(mock_urlopen.call_args), [{"code_execution": {}}, {"url_context": {}}])

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_TOOLS", [])
    @patch.object(bot, "GEMINI_MODELS", ["gemini-4-hydrogen"])
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    @patch("bot._urlopen")
    def test_off_by_default(self, mock_urlopen, _log):
        mock_urlopen.return_value = self._ok()
        bot._call_gemini_api("hi", for_ai=True)
        self.assertIsNone(self._tools_sent(mock_urlopen.call_args))

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_TOOLS", ["code_execution"])
    @patch.object(bot, "GEMINI_MODELS", ["gemma-4-31b-it"])
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    @patch("bot._urlopen")
    def test_gemma_gets_no_tools(self, mock_urlopen, _log):
        mock_urlopen.return_value = self._ok()
        bot._call_gemini_api("hi", for_ai=True)
        self.assertIsNone(self._tools_sent(mock_urlopen.call_args))

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_TOOLS", ["google_search"])
    @patch.object(bot, "GEMINI_MODELS", ["gemini-4-hydrogen"])
    @patch.object(bot, "GEMINI_API_KEY", "g_key")
    @patch("bot._urlopen")
    def test_rejected_tools_retry_same_model_without_tools(self, mock_urlopen, _log):
        import urllib.error
        quota = urllib.error.HTTPError("u", 429, "quota", {}, None)
        mock_urlopen.side_effect = [quota, self._ok("plain")]
        res = bot._call_gemini_api("hi", for_ai=True)
        self.assertEqual(res, "plain")
        self.assertEqual(res.model, "gemini-4-hydrogen")
        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertIsNotNone(self._tools_sent(mock_urlopen.call_args_list[0]))
        self.assertIsNone(self._tools_sent(mock_urlopen.call_args_list[1]))
        # The model is not put on the 1-hour rate-limit cooldown, only its tools are skipped
        self.assertNotIn("gemini-4-hydrogen", bot._GEMINI_MODEL_COOLDOWNS)
        mock_urlopen.side_effect = None
        mock_urlopen.return_value = self._ok("plain again")
        bot._call_gemini_api("hi", for_ai=True)
        self.assertIsNone(self._tools_sent(mock_urlopen.call_args))


class TestGoogleSearch(unittest.TestCase):
    """/google uses GEMINI_SEARCH_API_KEY with the google_search tool only, and never falls back to a key that cannot search."""

    def setUp(self):
        bot._GEMINI_MODEL_COOLDOWNS.clear()
        bot._GEMINI_TOOLS_BACKOFF.clear()
        bot._GEMINI_KEY_BACKOFF.clear()

    @staticmethod
    def _grounded(text="Python 3.15.2 is current."):
        ok = MagicMock()
        ok.__enter__.return_value.read.return_value = json.dumps({"candidates": [{
            "content": {"parts": [{"text": text}]},
            "groundingMetadata": {"groundingChunks": [
                {"web": {"uri": "https://python.org/a", "title": "Python"}},
                {"web": {"uri": "https://python.org/a", "title": "Dup"}},
                {"web": {"uri": "https://example.org/b", "title": "Ex [1]"}},
            ]}}]}).encode()
        return ok

    @staticmethod
    def _sent(call):
        req = call.args[0]
        return req.get_header("X-goog-api-key"), json.loads(req.data.decode()).get("tools")

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_TOOLS", ["code_execution"])
    @patch.object(bot, "GEMINI_SEARCH_API_KEY", "paid")
    @patch.object(bot, "GEMINI_MODELS", ["gemini-4-hydrogen"])
    @patch.object(bot, "GEMINI_API_KEY", "free")
    @patch("bot._urlopen")
    def test_search_uses_paid_key_and_only_search_tool(self, mock_urlopen, _log):
        mock_urlopen.return_value = self._grounded()
        with patch("bot._resolve_grounding_url", side_effect=lambda u, *a, **k: u):
            res = bot._google_answer("latest python?", "EN")
        self.assertEqual(self._sent(mock_urlopen.call_args), ("paid", [{"google_search": {}}]))
        self.assertIn("Python 3.15.2 is current.", res)
        # Sources: de-duplicated, brackets in titles neutralised
        self.assertIn("1. [Python](https://python.org/a)", res)
        self.assertIn("2. [Ex (1)](https://example.org/b)", res)
        self.assertNotIn("Dup", res)

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_SEARCH_API_KEY", "paid")
    @patch.object(bot, "GEMINI_MODELS", ["gemini-4-hydrogen"])
    @patch.object(bot, "GEMINI_API_KEY", "free")
    @patch("bot._urlopen")
    def test_other_requests_never_use_the_search_key(self, mock_urlopen, _log):
        ok = MagicMock()
        ok.__enter__.return_value.read.return_value = json.dumps({"candidates": [{"content": {"parts": [{"text": "x"}]}}]}).encode()
        mock_urlopen.return_value = ok
        bot._call_gemini_api("hi")
        bot._call_gemini_api("hi", for_ai=True)
        for call in mock_urlopen.call_args_list:
            self.assertEqual(call.args[0].get_header("X-goog-api-key"), "free")

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "or_key")
    @patch.object(bot, "GEMINI_SEARCH_API_KEY", "paid")
    @patch.object(bot, "GEMINI_MODELS", ["gemini-4-hydrogen", "gemini-3.8-flash"])
    @patch.object(bot, "GEMINI_API_KEY", "free")
    @patch("bot._urlopen")
    def test_depleted_key_pauses_search_without_fallback(self, mock_urlopen, _log):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError("u", 402, "credits", {}, None)
        self.assertIsNone(bot._google_answer("q?"))
        self.assertEqual(mock_urlopen.call_count, 1)  # one call, not one per model; no free key, no OpenRouter
        self.assertIsNone(bot._google_answer("q?"))
        self.assertEqual(mock_urlopen.call_count, 1)  # paused for the hour

    @patch.object(bot, "GEMINI_SEARCH_API_KEY", "")
    @patch("bot._urlopen")
    def test_disabled_without_key(self, mock_urlopen):
        self.assertIsNone(bot._google_answer("q?"))
        mock_urlopen.assert_not_called()


class TestAiLimits(unittest.TestCase):
    @patch.dict(os.environ, {"X_LIM": "48000"})
    def test_env_int_reads_value(self):
        self.assertEqual(bot._env_int("X_LIM", 16000, 1000, 200000), 48000)

    @patch.dict(os.environ, {"X_LIM": "99999999"})
    def test_env_int_clamps(self):
        self.assertEqual(bot._env_int("X_LIM", 16000, 1000, 200000), 200000)

    @patch.dict(os.environ, {"X_LIM": "lots"})
    def test_env_int_bad_value_uses_default(self):
        self.assertEqual(bot._env_int("X_LIM", 16000, 1000, 200000), 16000)

    def test_env_int_unset_uses_default(self):
        self.assertEqual(bot._env_int("X_LIM_UNSET_FOR_TEST", 4096, 512, 16384), 4096)

    @patch("bot._call_gemini_api")
    @patch.object(bot, "AI_MAX_OUTPUT_TOKENS", 8000)
    @patch.object(bot, "AI_CONTEXT_CHARS", 30000)
    @patch.object(bot, "_ai_available", return_value=True)
    def test_ask_ai_uses_limits(self, _avail, mock_call):
        mock_call.return_value = bot.AIText("ok", "m")
        bot._ask_gemini_ai("question here", context="x" * 40000)
        self.assertEqual(mock_call.call_args.kwargs["max_tokens"], 8000)
        prompt = mock_call.call_args.args[0]
        self.assertIn("x" * 30000, prompt)
        self.assertNotIn("x" * 30001, prompt)


class TestAnswerStyle(unittest.TestCase):
    @patch("bot._call_gemini_api")
    @patch.object(bot, "AI_ANSWER_STYLE", "STYLE-MARKER answer like a pirate.")
    @patch.object(bot, "_ai_available", return_value=True)
    def test_ai_prompt_uses_style(self, _avail, mock_call):
        mock_call.return_value = bot.AIText("ok", "m")
        bot._ask_gemini_ai("why is the sky blue")
        self.assertIn("STYLE-MARKER", mock_call.call_args.args[0])
        bot._ask_gemini_ai("why is the sky blue", context="some article")
        self.assertIn("STYLE-MARKER", mock_call.call_args.args[0])

    @patch("bot._call_gemini_api")
    @patch.object(bot, "AI_ANSWER_STYLE", "STYLE-MARKER answer like a pirate.")
    @patch.object(bot, "GEMINI_SEARCH_API_KEY", "paid")
    def test_google_prompt_uses_style(self, mock_call):
        mock_call.return_value = bot.AIText("ok", "m")
        bot._google_answer("latest python?")
        self.assertIn("STYLE-MARKER", mock_call.call_args.args[0])

    def test_default_has_no_paragraph_cap_and_covers_both_cases(self):
        d = bot._DEFAULT_AI_ANSWER_STYLE
        self.assertNotIn("paragraph", d)
        self.assertIn("simple", d)
        self.assertIn("complex", d)


class TestGroundingLinks(unittest.TestCase):
    def test_other_hosts_are_not_contacted(self):
        with patch("urllib.request.OpenerDirector.open") as op:
            self.assertEqual(bot._resolve_grounding_url("https://evil.example/x"), "https://evil.example/x")
            self.assertEqual(bot._resolve_grounding_url("http://vertexaisearch.cloud.google.com/a"), "http://vertexaisearch.cloud.google.com/a")
            op.assert_not_called()

    def test_redirect_location_is_returned(self):
        import urllib.error
        from email.message import Message
        h = Message(); h["Location"] = "https://en.wikipedia.org/wiki/X"
        err = urllib.error.HTTPError("u", 302, "Found", h, None)
        uri = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
        with patch("urllib.request.OpenerDirector.open", side_effect=err):
            self.assertEqual(bot._resolve_grounding_url(uri), "https://en.wikipedia.org/wiki/X")

    def test_failure_keeps_original_link(self):
        uri = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
        with patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError("t")):
            self.assertEqual(bot._resolve_grounding_url(uri), uri)

    def test_non_http_location_is_ignored(self):
        import urllib.error
        from email.message import Message
        h = Message(); h["Location"] = "javascript:alert(1)"
        err = urllib.error.HTTPError("u", 302, "Found", h, None)
        uri = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
        with patch("urllib.request.OpenerDirector.open", side_effect=err):
            self.assertEqual(bot._resolve_grounding_url(uri), uri)

    def test_sources_use_real_urls_and_dedupe(self):
        cand = {"groundingMetadata": {"groundingChunks": [
            {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/1", "title": "wikipedia.org"}},
            {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/2", "title": "wiki again"}},
            {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/3", "title": "rb.ru"}},
        ]}}
        real = {"1": "https://en.wikipedia.org/wiki/A", "2": "https://en.wikipedia.org/wiki/A", "3": "https://rb.ru/b"}
        with patch("bot._resolve_grounding_url", side_effect=lambda u, *a, **k: real[u.rsplit("/", 1)[1]]):
            out = bot._format_grounding_sources(cand)
        self.assertIn("1. [wikipedia.org](https://en.wikipedia.org/wiki/A)", out)
        self.assertIn("2. [rb.ru](https://rb.ru/b)", out)
        self.assertNotIn("vertexaisearch", out)
        self.assertNotIn("3.", out)


class TestFitDcText(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(bot._fit_dc_text("H:\n\n", "short", "\n\nT"), "H:\n\nshort\n\nT")

    def test_long_text_fits_chars_and_keeps_tail(self):
        body = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(30))
        tail = "\n\nSources:\n1. [a](https://example.org/" + "x" * 150 + ")"
        out = bot._fit_dc_text("🔎 **Google** (RU):\n\n", body, tail)
        self.assertLessEqual(len(out), bot.DC_SAFE_CHARS)
        self.assertTrue(out.endswith(tail))
        self.assertIn("…\n\nSources:", out)
        self.assertTrue(out.startswith("🔎 **Google** (RU):\n\nParagraph 0."))

    def test_many_short_lines_fit_line_limit(self):
        body = "\n".join(f"line {i}" for i in range(100))
        out = bot._fit_dc_text("H\n", body)
        self.assertLessEqual(out.count("\n") + 1, bot.DC_SAFE_LINES)
        self.assertTrue(out.endswith("…"))

    def test_cut_on_sentence_end(self):
        body = "First sentence here. " * 400
        out = bot._fit_dc_text("H\n", body)
        self.assertLessEqual(len(out), bot.DC_SAFE_CHARS)
        self.assertTrue(out.endswith("here.…"))

    def test_default_style_mentions_length_budget(self):
        self.assertIn("2800", bot._DEFAULT_AI_ANSWER_STYLE)


class TestPdfAndVideoMedia(unittest.TestCase):
    def test_pdf_by_header(self):
        self.assertEqual(bot._detect_pdf_or_video(b"%PDF-1.7 ...", "x.bin", ""), ("application/pdf", "document"))

    def test_pdf_by_declared_mime(self):
        self.assertEqual(bot._detect_pdf_or_video(b"zzz", "x", "application/pdf"), ("application/pdf", "document"))

    def test_mp4_video_by_declared_or_extension(self):
        mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20
        self.assertEqual(bot._detect_pdf_or_video(mp4, "clip", "video/mp4"), ("video/mp4", "video"))
        self.assertEqual(bot._detect_pdf_or_video(mp4, "clip.MOV", ""), ("video/mov", "video"))
        self.assertEqual(bot._detect_pdf_or_video(mp4, "clip.mp4", ""), ("video/mp4", "video"))

    def test_mp4_voice_recording_stays_audio(self):
        mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20
        self.assertIsNone(bot._detect_pdf_or_video(mp4, "voice.m4a", "audio/mp4"))
        self.assertIsNone(bot._detect_pdf_or_video(mp4, "voice.m4a", ""))
        self.assertIsNone(bot._detect_pdf_or_video(mp4, "noname", ""))

    def test_unsupported_video_type_is_ignored(self):
        self.assertIsNone(bot._detect_pdf_or_video(b"x", "a.mkv", "video/x-matroska"))

    def test_size_limit_applies_to_pdf_and_video_only(self):
        big = b"0" * (bot.MAX_DOC_VIDEO_BYTES + 1)
        with self.assertRaises(bot.MediaTooLargeError):
            bot._check_doc_video_size(big, "video")
        with self.assertRaises(bot.MediaTooLargeError):
            bot._check_doc_video_size(big, "document")
        bot._check_doc_video_size(big, "image")
        bot._check_doc_video_size(big, "audio")

    @patch("bot._call_gemini_api")
    @patch.object(bot, "_ai_available", return_value=True)
    def test_prompts_for_pdf_and_video(self, _avail, mock_call):
        mock_call.return_value = bot.AIText("ok", "m")
        bot._ask_gemini_ai("", media_bytes=b"%PDF-1.4", media_mime="application/pdf", media_type="document")
        self.assertIn("PDF document", mock_call.call_args.args[0])
        self.assertEqual(mock_call.call_args.kwargs["media_mime"], "application/pdf")
        bot._ask_gemini_ai("who speaks?", media_bytes=b"vid", media_mime="video/mp4", media_type="video")
        p = mock_call.call_args.args[0]
        self.assertIn("video", p)
        self.assertIn("who speaks?", p)

    @patch("bot._extract_media_from_msg_or_quote", return_value=(b"vid", "video/mp4", "video"))
    def test_tldr_audio_wrapper_still_takes_video(self, _m):
        self.assertEqual(bot._extract_audio_from_msg_or_quote(None, 1, MagicMock()), (b"vid", "video/mp4"))

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_MODELS", ["gemini-4-hydrogen", "gemma-4-31b-it"])
    @patch.object(bot, "GEMINI_API_KEY", "k")
    @patch("bot._urlopen")
    def test_video_request_sent_inline_to_multimodal_model_only(self, mock_urlopen, _log):
        ok = MagicMock()
        ok.__enter__.return_value.read.return_value = json.dumps({"candidates": [{"content": {"parts": [{"text": "x"}]}}]}).encode()
        mock_urlopen.return_value = ok
        bot._call_gemini_api("describe", media_bytes=b"vid", media_mime="video/mp4")
        body = json.loads(mock_urlopen.call_args.args[0].data.decode())
        self.assertEqual(body["contents"][0]["parts"][1]["inline_data"]["mime_type"], "video/mp4")
        self.assertIn("hydrogen", mock_urlopen.call_args.args[0].full_url)


class TestToolsSkippedForPdfAndVideo(unittest.TestCase):
    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_TOOLS", ["code_execution"])
    @patch.object(bot, "GEMINI_MODELS", ["gemini-4-hydrogen"])
    @patch.object(bot, "GEMINI_API_KEY", "k")
    @patch("bot._urlopen")
    def test_no_tools_with_pdf_or_video(self, mock_urlopen, _log):
        bot._GEMINI_TOOLS_BACKOFF.clear()
        ok = MagicMock()
        ok.__enter__.return_value.read.return_value = json.dumps({"candidates": [{"content": {"parts": [{"text": "x"}]}}]}).encode()
        mock_urlopen.return_value = ok
        for mime in ("application/pdf", "video/mp4"):
            bot._call_gemini_api("q", media_bytes=b"data", media_mime=mime, for_ai=True)
            self.assertNotIn("tools", json.loads(mock_urlopen.call_args.args[0].data.decode()))
        bot._call_gemini_api("q", media_bytes=b"data", media_mime="image/png", for_ai=True)
        self.assertIn("tools", json.loads(mock_urlopen.call_args.args[0].data.decode()))


class TestGoogleCostControls(unittest.TestCase):
    def setUp(self):
        bot._google_cache.clear()
        bot._google_user_calls.clear()
        bot._GEMINI_MODEL_COOLDOWNS.clear()
        bot._GEMINI_KEY_BACKOFF.clear()
        bot._GEMINI_TOOLS_BACKOFF.clear()

    @staticmethod
    def _grounded():
        ok = MagicMock()
        ok.__enter__.return_value.read.return_value = json.dumps({"candidates": [{"content": {"parts": [{"text": "A"}]}}]}).encode()
        return ok

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_SEARCH_THINKING", "medium")
    @patch.object(bot, "GEMINI_SEARCH_API_KEY", "paid")
    @patch.object(bot, "GEMINI_SEARCH_MODELS", [])
    @patch.object(bot, "GEMINI_MODELS", ["gemma-4-31b-it", "gemini-4-hydrogen"])
    @patch.object(bot, "GEMINI_API_KEY", "free")
    @patch("bot._urlopen")
    def test_thinking_level_and_no_gemma(self, mock_urlopen, _log):
        mock_urlopen.return_value = self._grounded()
        bot._google_answer("q?")
        self.assertEqual(mock_urlopen.call_count, 1)
        req = mock_urlopen.call_args.args[0]
        self.assertIn("gemini-4-hydrogen", req.full_url)
        body = json.loads(req.data.decode())
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingLevel": "medium"})

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_API_KEY", "")
    @patch.object(bot, "GEMINI_SEARCH_THINKING", "")
    @patch.object(bot, "GEMINI_SEARCH_API_KEY", "paid")
    @patch.object(bot, "GEMINI_SEARCH_MODELS", ["gemini-3.8-flash"])
    @patch.object(bot, "GEMINI_MODELS", ["gemini-4-hydrogen"])
    @patch.object(bot, "GEMINI_API_KEY", "free")
    @patch("bot._urlopen")
    def test_search_models_override_and_empty_thinking(self, mock_urlopen, _log):
        mock_urlopen.return_value = self._grounded()
        bot._google_answer("q?")
        req = mock_urlopen.call_args.args[0]
        self.assertIn("gemini-3.8-flash", req.full_url)
        self.assertNotIn("thinkingConfig", json.loads(req.data.decode())["generationConfig"])

    @patch.object(bot, "GOOGLE_CACHE_MINUTES", 60)
    def test_cache_normalises_and_expires(self):
        k1 = bot._google_cache_key("  What IS  new? ", "ru")
        self.assertEqual(k1, bot._google_cache_key("what is new?", "RU"))
        self.assertNotEqual(k1, bot._google_cache_key("what is new?", "EN"))
        bot._google_cache_put(k1, bot.AIText("ans", "m"))
        self.assertEqual(bot._google_cache_get(k1), "ans")
        with patch("bot.time.time", return_value=bot.time.time() + 3601):
            self.assertIsNone(bot._google_cache_get(k1))

    @patch.object(bot, "GOOGLE_CACHE_MINUTES", 0)
    def test_cache_off(self):
        bot._google_cache_put("k", bot.AIText("ans", "m"))
        self.assertIsNone(bot._google_cache_get("k"))

    @patch.object(bot, "GOOGLE_DAILY_LIMIT", 0)
    @patch.object(bot, "GOOGLE_USER_DAILY_LIMIT", 2)
    def test_per_user_limit_and_admin_exempt(self):
        self.assertIsNone(bot._google_limit_reached(7, False))
        self.assertIsNone(bot._google_limit_reached(7, False))
        self.assertIn("2", bot._google_limit_reached(7, False))
        self.assertIsNone(bot._google_limit_reached(8, False))  # other user unaffected
        for _ in range(5):
            self.assertIsNone(bot._google_limit_reached(9, True))  # admin exempt

    @patch.object(bot, "GOOGLE_USER_DAILY_LIMIT", 0)
    @patch.object(bot, "GOOGLE_DAILY_LIMIT", 3)
    def test_global_limit_uses_db_count(self):
        with patch("bot.database.count_api_calls_since", return_value=2):
            self.assertIsNone(bot._google_limit_reached(1, False))
        with patch("bot.database.count_api_calls_since", return_value=3):
            self.assertIn("daily limit", bot._google_limit_reached(1, False))

    def test_db_counts_calls_in_window(self):
        db_path = os.path.join(tempfile.gettempdir(), "test_google_count.db")
        old = database.DB_PATH
        database.DB_PATH = db_path
        try:
            database.init_db()
            database.log_api_call("google")
            database.log_api_call("google")
            database.log_api_call("gemini")
            self.assertEqual(database.count_api_calls_since("google", 86400), 2)
            conn = database._connect()
            conn.execute("INSERT INTO api_log (service, created_at) VALUES ('google', 1000)")  # long ago
            conn.commit()
            conn.close()
            self.assertEqual(database.count_api_calls_since("google", 86400), 2)
        finally:
            database.DB_PATH = old
            for suf in ("", "-wal", "-shm"):
                if os.path.exists(db_path + suf):
                    os.remove(db_path + suf)


class TestGoogleCostReport(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(tempfile.gettempdir(), "test_google_cost.db")
        self.old = database.DB_PATH
        database.DB_PATH = self.db_path
        database.init_db()
        bot._google_cache.clear()

    def tearDown(self):
        database.DB_PATH = self.old
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + suf):
                os.remove(self.db_path + suf)

    def test_extract_usage(self):
        body = {"usageMetadata": {"promptTokenCount": 250, "toolUsePromptTokenCount": 50,
                                  "candidatesTokenCount": 900, "thoughtsTokenCount": 800}}
        cand = {"groundingMetadata": {"webSearchQueries": ["a", "b", "c"]}}
        self.assertEqual(bot._extract_google_usage(body, cand), {"prompt": 300, "output": 900, "thoughts": 800, "queries": 3})
        self.assertEqual(bot._extract_google_usage({}, {}), {"prompt": 0, "output": 0, "thoughts": 0, "queries": 0})

    def test_prices_follow_dates_and_unknown_models(self):
        before = bot._ts(2026, 12, 31)
        after = bot._ts(2027, 1, 2)
        self.assertEqual(bot._model_price("gemini-3.8-flash", before), (0.75, 3.75))
        self.assertEqual(bot._model_price("gemini-3.8-flash", after), (1.50, 7.50))
        self.assertEqual(bot._model_price("gemini-4-hydrogen", after), (1.50, 7.50))
        self.assertEqual(bot._model_price("some-new-model", before), (1.50, 7.50))

    def test_token_cost(self):
        now = bot._ts(2026, 11, 15)
        rows = [(now, "gemini-4-hydrogen", 1_000_000, 100_000, 100_000, 4)]
        usd, p, o, q = bot._google_token_cost(rows)
        self.assertAlmostEqual(usd, 1.50 + 0.2 * 7.50)
        self.assertEqual((p, o, q), (1_000_000, 200_000, 4))

    def test_db_roundtrip_and_report(self):
        database.log_google_usage("gemini-4-hydrogen", 300, 900, 800, 4)
        database.log_google_usage("gemini-4-hydrogen", 300, 100, 100, 2)
        rows = database.get_google_usage(0)
        self.assertEqual(len(rows), 2)
        with patch.object(bot, "GOOGLE_SEARCH_FREE_PER_MONTH", 5):
            report = bot._google_cost_report()
        self.assertIn("Admin only", report)
        self.assertIn("2 searches", report)
        self.assertIn("6 of 5 free search queries", report)
        self.assertIn("about $0.01 for the rest", report)  # 1 billable query x $0.014

    def test_cleanup_keeps_recent_usage(self):
        database.log_google_usage("m", 1, 1, 1, 1)
        database.cleanup_old_records(retention_days=30)
        self.assertEqual(len(database.get_google_usage(0)), 1)

    @patch("bot._send")
    @patch("bot._react")
    @patch.object(bot, "GOOGLE_USER_DAILY_LIMIT", 0)
    @patch.object(bot, "GOOGLE_DAILY_LIMIT", 0)
    @patch.object(bot, "GOOGLE_CACHE_MINUTES", 60)
    @patch("bot._is_dc_admin", return_value=False)
    @patch("bot._google_answer")
    def test_usage_logged_once_and_not_for_cached_answer(self, mock_answer, _adm, _react, _send):
        ans = bot.AIText("A", "gemini-4-hydrogen")
        ans.usage = {"prompt": 10, "output": 20, "thoughts": 30, "queries": 2}
        mock_answer.return_value = ans
        bot._do_google_query(MagicMock(), 1, 5, 1, 9, "same question")
        bot._do_google_query(MagicMock(), 1, 5, 2, 9, "same question")  # served from the cache
        self.assertEqual(mock_answer.call_count, 1)
        self.assertEqual(len(database.get_google_usage(0)), 1)


class TestOpenRouterModelChoice(unittest.TestCase):
    @staticmethod
    def _resp(body):
        m = MagicMock()
        m.__enter__.return_value.read.return_value = json.dumps(body).encode()
        return m

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_MODELS", ["openrouter/free"])
    @patch.object(bot, "OPENROUTER_API_KEY", "or_key")
    @patch.object(bot, "GEMINI_API_KEY", "")
    @patch("bot._urlopen")
    def test_safety_classifier_answer_is_discarded(self, mock_urlopen, _log):
        mock_urlopen.side_effect = [
            self._resp({"model": "nvidia/nemotron-3.5-content-safety:free", "choices": [{"message": {"content": "User Safety: safe"}}]}),
            self._resp({"model": "nvidia/nemotron-3-super-120b-a12b:free", "choices": [{"message": {"content": "Real answer."}}]}),
        ]
        res = bot._call_gemini_api("hi")
        self.assertEqual(res, "Real answer.")
        self.assertEqual(res.model, "nvidia/nemotron-3-super-120b-a12b:free")

    @patch("bot.database.log_api_call")
    @patch.object(bot, "OPENROUTER_MODELS", ["blocked:free", "good:free"])
    @patch.object(bot, "OPENROUTER_API_KEY", "or_key")
    @patch.object(bot, "GEMINI_API_KEY", "")
    @patch("bot._urlopen")
    def test_403_moves_to_next_model(self, mock_urlopen, _log):
        import urllib.error
        mock_urlopen.side_effect = [
            urllib.error.HTTPError("u", 403, "Forbidden", {}, None),
            self._resp({"model": "good:free", "choices": [{"message": {"content": "OK answer."}}]}),
        ]
        self.assertEqual(bot._call_gemini_api("hi").model, "good:free")

    def test_default_list_ends_with_router(self):
        self.assertEqual(bot._DEFAULT_OPENROUTER_MODELS.split(",")[-1], "openrouter/free")


if __name__ == "__main__":
    unittest.main()
