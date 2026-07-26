"""
Tests for /webxdc command and WebXDC packaging functionality.
"""
import os
import sys
import tempfile
import zipfile
import unittest
from unittest.mock import MagicMock, patch

_TEST_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TEST_DB

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import database

database.init_db()


class MockEvent:
    def __init__(self, from_id=10, chat_id=1, msg_id=100, payload="", text=""):
        self.msg = MagicMock()
        self.msg.from_id = from_id
        self.msg.chat_id = chat_id
        self.msg.id = msg_id
        self.msg.text = text
        self.msg.is_bot = False
        self.msg.is_info = False
        self.payload = payload


class TestWebXDC(unittest.TestCase):
    def setUp(self):
        database.init_db()
        bot.dc_accid = 1

    def tearDown(self):
        try:
            os.remove(_TEST_DB)
        except Exception:
            pass

    def test_package_webxdc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            html_file = os.path.join(tmpdir, "test.html")
            with open(html_file, "w", encoding="utf-8") as f:
                f.write("<html><body><h1>Test WebXDC</h1></body></html>")

            xdc_file = os.path.join(tmpdir, "output.xdc")
            res = bot._package_webxdc(html_file, xdc_file, "Test Page Title", "https://example.com/test")
            self.assertTrue(res)
            self.assertTrue(os.path.exists(xdc_file))

            # Verify zip archive contents
            with zipfile.ZipFile(xdc_file, "r") as zf:
                namelist = zf.namelist()
                self.assertIn("index.html", namelist)
                self.assertIn("manifest.toml", namelist)

                manifest_text = zf.read("manifest.toml").decode("utf-8")
                self.assertIn('name = "Test Page Title"', manifest_text)
                self.assertIn('source_code_url = "https://example.com/test"', manifest_text)

                index_text = zf.read("index.html").decode("utf-8")
                self.assertIn("Test WebXDC", index_text)

    @patch("bot._handle_preview_command")
    @patch("bot._is_bot_blocked", return_value=False)
    def test_webxdc_command_trigger(self, mock_blocked, mock_handle):
        mock_bot = MagicMock()
        event = MockEvent(chat_id=1, payload="https://example.com", text="/webxdc https://example.com")
        bot.webxdc_command(mock_bot, 1, event)

        mock_handle.assert_called_once_with(mock_bot, 1, event, mode="webxdc")

    @patch("bot._do_preview")
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._is_bot_blocked", return_value=False)
    def test_dynamic_webxdc_trigger(self, mock_blocked, mock_rate_limit, mock_do_preview):
        urlhash = database.get_or_create_url_hash("https://example.com/dynamic")
        mock_bot = MagicMock()

        event = MockEvent(chat_id=1, text=f"/webxdc_{urlhash}")
        bot.on_new_message(mock_bot, 1, event)

        # Give background thread time or check call
        mock_do_preview.assert_called_once()
        args, kwargs = mock_do_preview.call_args
        self.assertEqual(args[5], "https://example.com/dynamic")
        self.assertEqual(args[6], "webxdc")


    def test_process_soup_links(self):
        from bs4 import BeautifulSoup
        html = '<p>Check <a href="https://example.com/foo">link 1</a> and <a href="https://example.com/bar" target="_self">link 2</a></p>'
        soup = BeautifulSoup(html, "html.parser")
        bot._process_soup_links(soup)
        
        for a in soup.find_all("a"):
            self.assertEqual(a.get("target"), "_blank")
            self.assertEqual(a.get("rel"), "noopener noreferrer")


if __name__ == "__main__":
    unittest.main()
