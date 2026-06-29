"""
Tests for KaraKeep integration (_save_to_karakeep, _karakeep_enabled).
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Set env vars BEFORE importing bot so the module picks them up
os.environ["KARAKEEP_URL"] = "https://keep.example.com"
os.environ["KARAKEEP_API_KEY"] = "mockkey"
os.environ["KARAKEEP_TAGS"] = "deltachat, bot"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot

# Override module-level constants that were already evaluated at import time
# (they may have been set by a previous test module in the same process)
bot.KARAKEEP_URL = "https://keep.example.com"
bot.KARAKEEP_API_KEY = "mockkey"
bot.KARAKEEP_TAGS = ["deltachat", "bot"]


class TestKaraKeepEnabled(unittest.TestCase):
    """Tests for _karakeep_enabled and env-var parsing."""

    def test_env_vars_parsed(self):
        self.assertEqual(bot.KARAKEEP_URL, "https://keep.example.com")
        self.assertEqual(bot.KARAKEEP_API_KEY, "mockkey")
        self.assertEqual(bot.KARAKEEP_TAGS, ["deltachat", "bot"])

    def test_karakeep_enabled(self):
        self.assertTrue(bot._karakeep_enabled())


class TestSaveToKaraKeep(unittest.TestCase):
    """Tests for _save_to_karakeep API call logic (mocked HTTP)."""

    @patch("urllib.request.urlopen")
    def test_success_creates_bookmark_and_attaches_tags(self, mock_urlopen):
        # First call: create bookmark → returns bookmark id
        mock_create = MagicMock()
        mock_create.read.return_value = b'{"id": "bookmark_12345"}'

        # Second call: attach tags
        mock_tags = MagicMock()
        mock_tags.read.return_value = b'{"success": true}'

        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=mock_create)),
            MagicMock(__enter__=MagicMock(return_value=mock_tags)),
        ]

        success, result = bot._save_to_karakeep("https://example.com/page")

        self.assertTrue(success)
        self.assertEqual(result, "bookmark_12345")
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_create_bookmark_request_fields(self, mock_urlopen):
        mock_create = MagicMock()
        mock_create.read.return_value = b'{"id": "bm_abc"}'
        mock_tags = MagicMock()
        mock_tags.read.return_value = b'{}'

        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=mock_create)),
            MagicMock(__enter__=MagicMock(return_value=mock_tags)),
        ]

        bot._save_to_karakeep("https://example.com/page")

        # Verify create-bookmark call
        req_create = mock_urlopen.call_args_list[0][0][0]
        self.assertEqual(req_create.full_url, "https://keep.example.com/api/v1/bookmarks")
        self.assertEqual(req_create.get_header("Authorization"), "Bearer mockkey")
        self.assertEqual(
            json.loads(req_create.data.decode("utf-8")),
            {"type": "link", "url": "https://example.com/page"},
        )

    @patch("urllib.request.urlopen")
    def test_attach_tags_request_fields(self, mock_urlopen):
        mock_create = MagicMock()
        mock_create.read.return_value = b'{"id": "bm_xyz"}'
        mock_tags = MagicMock()
        mock_tags.read.return_value = b'{}'

        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=mock_create)),
            MagicMock(__enter__=MagicMock(return_value=mock_tags)),
        ]

        bot._save_to_karakeep("https://example.com/page")

        # Verify attach-tags call
        req_tags = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(
            req_tags.full_url,
            "https://keep.example.com/api/v1/bookmarks/bm_xyz/tags",
        )
        self.assertEqual(
            json.loads(req_tags.data.decode("utf-8")),
            {"tags": [{"tagName": "deltachat"}, {"tagName": "bot"}]},
        )



class TestSaveToWebArchive(unittest.TestCase):
    """Tests for _save_to_web_archive API call logic (mocked HTTP)."""

    @patch("urllib.request.urlopen")
    def test_success_returns_redirected_url(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.geturl.return_value = "https://web.archive.org/web/20260629/https://example.com/page"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        success, result = bot._save_to_web_archive("https://example.com/page")

        self.assertTrue(success)
        self.assertEqual(result, "https://web.archive.org/web/20260629/https://example.com/page")

        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://web.archive.org/save/https://example.com/page")
        self.assertEqual(req.get_header("User-agent"), bot.STANDARD_USER_AGENT)

    @patch("urllib.request.urlopen")
    def test_failure_returns_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("HTTP 503 Service Unavailable")

        success, result = bot._save_to_web_archive("https://example.com/page")

        self.assertFalse(success)
        self.assertIn("HTTP 503", result)


class TestDoKeep(unittest.TestCase):
    """Tests for _do_keep routing logic between KaraKeep and Web Archive."""

    @patch("bot._is_dc_admin")
    @patch("bot._karakeep_enabled")
    @patch("bot._save_to_karakeep")
    @patch("bot._save_to_web_archive")
    @patch("bot._react")
    @patch("bot._send")
    def test_do_keep_admin_karakeep_enabled(self, mock_send, mock_react, mock_webarchive, mock_karakeep, mock_keep_enabled, mock_is_admin):
        mock_is_admin.return_value = True
        mock_keep_enabled.return_value = True
        mock_karakeep.return_value = (True, "bm_123")
        mock_webarchive.return_value = (True, "https://web.archive.org/web/123/https://example.com")
        
        mock_bot = MagicMock()
        mock_bot.rpc.create_chat_by_contact_id.return_value = 999

        bot._do_keep(mock_bot, 1, 10, 100, 20, "https://example.com")

        mock_karakeep.assert_called_once_with("https://example.com")
        mock_webarchive.assert_called_once_with("https://example.com")
        mock_react.assert_called_once_with(mock_bot, 1, 100, "☑️")
        
        # Check that we sent a success reply containing the Web Archive URL to the main chat
        # and KaraKeep link to the private chat 999
        self.assertEqual(mock_send.call_count, 2)
        
        # First send: Web Archive to main chat (10)
        self.assertEqual(mock_send.call_args_list[0][0][2], 10)
        self.assertIn("Saved to Web Archive", mock_send.call_args_list[0][0][3])
        
        # Second send: KaraKeep to private chat (999)
        self.assertEqual(mock_send.call_args_list[1][0][2], 999)
        self.assertIn("Saved to KaraKeep", mock_send.call_args_list[1][0][3])

    @patch("bot._is_dc_admin")
    @patch("bot._karakeep_enabled")
    @patch("bot._save_to_karakeep")
    @patch("bot._save_to_web_archive")
    @patch("bot._react")
    @patch("bot._send")
    def test_do_keep_admin_karakeep_disabled(self, mock_send, mock_react, mock_webarchive, mock_karakeep, mock_keep_enabled, mock_is_admin):
        mock_is_admin.return_value = True
        mock_keep_enabled.return_value = False
        mock_webarchive.return_value = (True, "https://web.archive.org/web/123/https://example.com")

        bot._do_keep(None, 1, 10, 100, 20, "https://example.com")

        mock_karakeep.assert_not_called()
        mock_webarchive.assert_called_once_with("https://example.com")
        mock_react.assert_called_once_with(None, 1, 100, "☑️")
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][2], 10)
        self.assertIn("Saved to Web Archive", mock_send.call_args[0][3])

    @patch("bot._is_dc_admin")
    @patch("bot._karakeep_enabled")
    @patch("bot._save_to_karakeep")
    @patch("bot._save_to_web_archive")
    @patch("bot._react")
    @patch("bot._send")
    def test_do_keep_regular_user(self, mock_send, mock_react, mock_webarchive, mock_karakeep, mock_keep_enabled, mock_is_admin):
        mock_is_admin.return_value = False
        mock_keep_enabled.return_value = True
        mock_webarchive.return_value = (True, "https://web.archive.org/web/123/https://example.com")

        bot._do_keep(None, 1, 10, 100, 20, "https://example.com")

        mock_karakeep.assert_not_called()
        mock_webarchive.assert_called_once_with("https://example.com")
        mock_react.assert_called_once_with(None, 1, 100, "☑️")
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][2], 10)
        self.assertIn("Saved to Web Archive", mock_send.call_args[0][3])


if __name__ == "__main__":
    unittest.main()
