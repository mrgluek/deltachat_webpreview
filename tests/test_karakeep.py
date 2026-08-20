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
        try:
            parsed_payload = json.loads(req_create.data.decode("utf-8"))
            self.assertEqual(parsed_payload, {"type": "link", "url": "https://example.com/page"})
        except json.JSONDecodeError as e:
            self.fail(f"Failed to parse create-bookmark payload: {e}")

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
        self.assertEqual(req_tags.full_url, "https://keep.example.com/api/v1/bookmarks/bm_xyz/tags")
        try:
            parsed_tags = json.loads(req_tags.data.decode("utf-8"))
            self.assertEqual(parsed_tags, {"tags": [{"tagName": "deltachat"}, {"tagName": "bot"}]})
        except json.JSONDecodeError as e:
            self.fail(f"Failed to parse attach-tags payload: {e}")



class TestSaveToWebArchive(unittest.TestCase):
    """Tests for _save_to_web_archive API call logic (mocked HTTP)."""

    @patch("urllib.request.urlopen")
    def test_success_returns_redirected_url(self, mock_urlopen):
        """Test that a successful Web Archive save returns the redirected URL."""
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
    def test_failure_returns_http_error(self, mock_urlopen):
        """Test that an HTTP error returns the appropriate failure response."""
        from urllib.error import HTTPError
        from http.client import HTTPMessage
        
        # urlopen raises HTTPError for HTTP error status codes
        mock_urlopen.side_effect = HTTPError(
            url="https://web.archive.org/save/https://example.com/page",
            code=503,
            msg="Service Unavailable",
            hdrs=HTTPMessage(),
            fp=None
        )

        success, result = bot._save_to_web_archive("https://example.com/page")

        self.assertFalse(success)
        self.assertIn("HTTP 503", result)


class TestSaveToArchiveToday(unittest.TestCase):
    """Tests for _save_to_archive_today API call logic and mirror fallbacks."""

    @patch("bot._urlopen")
    def test_success_returns_redirected_url(self, mock_urlopen):
        """Test successful save returning redirected snapshot URL."""
        mock_get_resp = MagicMock()
        mock_get_resp.read.return_value = b'<html><form><input type="hidden" name="submitid" value="sid123"></form></html>'

        mock_post_resp = MagicMock()
        mock_post_resp.geturl.return_value = "https://archive.ph/snap123"
        mock_post_resp.read.return_value = b""

        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=mock_get_resp)),
            MagicMock(__enter__=MagicMock(return_value=mock_post_resp)),
        ]

        with patch("bot.ARCHIVE_TODAY_MIRRORS", ["https://archive.ph"]):
            success, result = bot._save_to_archive_today("https://example.com/page")

        self.assertTrue(success)
        self.assertEqual(result, "https://archive.ph/snap123")

    @patch("bot._urlopen")
    def test_success_meta_refresh(self, mock_urlopen):
        """Test successful save via meta refresh in response body."""
        mock_get_resp = MagicMock()
        mock_get_resp.read.return_value = b'<html></html>'

        mock_post_resp = MagicMock()
        mock_post_resp.geturl.return_value = "https://archive.ph/submit/"
        mock_post_resp.read.return_value = b'<html><head><meta http-equiv="refresh" content="0;url=https://archive.ph/wip/456"></head></html>'

        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=mock_get_resp)),
            MagicMock(__enter__=MagicMock(return_value=mock_post_resp)),
        ]

        with patch("bot.ARCHIVE_TODAY_MIRRORS", ["https://archive.ph"]):
            success, result = bot._save_to_archive_today("https://example.com/page")

        self.assertTrue(success)
        self.assertEqual(result, "https://archive.ph/wip/456")

    @patch("bot._urlopen")
    def test_success_js_redirect(self, mock_urlopen):
        """Test successful save via JavaScript redirection."""
        mock_get_resp = MagicMock()
        mock_get_resp.read.return_value = b'<html></html>'

        mock_post_resp = MagicMock()
        mock_post_resp.geturl.return_value = "https://archive.ph/submit/"
        mock_post_resp.read.return_value = b'<script>location.replace("https://archive.ph/789xyz");</script>'

        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=mock_get_resp)),
            MagicMock(__enter__=MagicMock(return_value=mock_post_resp)),
        ]

        with patch("bot.ARCHIVE_TODAY_MIRRORS", ["https://archive.ph"]):
            success, result = bot._save_to_archive_today("https://example.com/page")

        self.assertTrue(success)
        self.assertEqual(result, "https://archive.ph/789xyz")

    @patch("bot._urlopen")
    def test_mirror_fallback_sequence(self, mock_urlopen):
        """Test that failure on the first mirror falls back to the next mirror."""
        from urllib.error import HTTPError
        from http.client import HTTPMessage

        # Mirror 1 (archive.ph) GET fails with 503
        err503 = HTTPError("https://archive.ph/", 503, "Service Unavailable", HTTPMessage(), None)

        # Mirror 2 (archive.is) GET succeeds, POST succeeds
        mock_get_resp2 = MagicMock()
        mock_get_resp2.read.return_value = b'<input name="submitid" value="sid999">'
        mock_post_resp2 = MagicMock()
        mock_post_resp2.geturl.return_value = "https://archive.is/fallback_ok"

        mock_urlopen.side_effect = [
            err503,
            MagicMock(__enter__=MagicMock(return_value=mock_get_resp2)),
            MagicMock(__enter__=MagicMock(return_value=mock_post_resp2)),
        ]

        with patch("bot.ARCHIVE_TODAY_MIRRORS", ["https://archive.ph", "https://archive.is"]):
            success, result = bot._save_to_archive_today("https://example.com/page")

        self.assertTrue(success)
        self.assertEqual(result, "https://archive.is/fallback_ok")

    @patch("bot._urlopen")
    def test_all_mirrors_fail(self, mock_urlopen):
        """Test failure when all configured mirrors fail."""
        mock_urlopen.side_effect = Exception("Connection refused")

        with patch("bot.ARCHIVE_TODAY_MIRRORS", ["https://archive.ph", "https://archive.is"]):
            success, result = bot._save_to_archive_today("https://example.com/page")

        self.assertFalse(success)
        self.assertIn("Connection refused", result)


class TestDoKeep(unittest.TestCase):
    """Tests for _do_keep sequential flow (KaraKeep -> Web Archive -> Archive.today)."""

    @patch("bot._is_dc_admin")
    @patch("bot._karakeep_enabled")
    @patch("bot._save_to_karakeep")
    @patch("bot._save_to_web_archive")
    @patch("bot._save_to_archive_today")
    @patch("bot._react")
    @patch("bot._send")
    def test_do_keep_admin_karakeep_and_webarchive_success(
        self, mock_send, mock_react, mock_archivetoday, mock_webarchive, mock_karakeep, mock_keep_enabled, mock_is_admin
    ):
        mock_is_admin.return_value = True
        mock_keep_enabled.return_value = True
        mock_karakeep.return_value = (True, "bm_123")
        mock_webarchive.return_value = (True, "https://web.archive.org/web/123/https://example.com")

        mock_bot = MagicMock()
        mock_bot.rpc.create_chat_by_contact_id.return_value = 99

        bot._do_keep(mock_bot, 1, 10, 100, 5, "https://example.com")

        # KaraKeep called first
        mock_karakeep.assert_called_once_with("https://example.com")
        # Private message sent to admin chat (chat 99)
        mock_send.assert_any_call(mock_bot, 1, 99, "🔖 Saved to KaraKeep!\n🔗 https://example.com\n📎 https://keep.example.com/dashboard/preview/bm_123")
        # WebArchive called second
        mock_webarchive.assert_called_once_with("https://example.com")
        # Public confirmation sent to source chat (chat 10)
        mock_send.assert_any_call(mock_bot, 1, 10, "🏛️ Saved to Web Archive!\n🔗 https://example.com\n📎 https://web.archive.org/web/123/https://example.com")
        # Success reaction
        mock_react.assert_called_once_with(mock_bot, 1, 100, "☑️")
        # Archive.today fallback not called
        mock_archivetoday.assert_not_called()

    @patch("bot._is_dc_admin")
    @patch("bot._karakeep_enabled")
    @patch("bot._save_to_karakeep")
    @patch("bot._save_to_web_archive")
    @patch("bot._save_to_archive_today")
    @patch("bot._react")
    @patch("bot._send")
    def test_do_keep_webarchive_fails_falls_back_to_archive_today(
        self, mock_send, mock_react, mock_archivetoday, mock_webarchive, mock_karakeep, mock_keep_enabled, mock_is_admin
    ):
        mock_is_admin.return_value = False
        mock_keep_enabled.return_value = False
        mock_webarchive.return_value = (False, "HTTP 503 Service Unavailable")
        mock_archivetoday.return_value = (True, "https://archive.ph/abc12")

        mock_bot = MagicMock()

        bot._do_keep(mock_bot, 1, 10, 100, 5, "https://example.com")

        # KaraKeep not called for regular user
        mock_karakeep.assert_not_called()
        # WebArchive called
        mock_webarchive.assert_called_once_with("https://example.com")
        # Archive.today called as fallback
        mock_archivetoday.assert_called_once_with("https://example.com")
        # Public confirmation with Archive.today sent to chat
        mock_send.assert_called_once_with(mock_bot, 1, 10, "🏛️ Saved to Archive.today!\n🔗 https://example.com\n📎 https://archive.ph/abc12")
        mock_react.assert_called_once_with(mock_bot, 1, 100, "☑️")

    @patch("bot._is_dc_admin")
    @patch("bot._karakeep_enabled")
    @patch("bot._save_to_karakeep")
    @patch("bot._save_to_web_archive")
    @patch("bot._save_to_archive_today")
    @patch("bot._react")
    @patch("bot._send")
    def test_do_keep_both_fail_reports_error(
        self, mock_send, mock_react, mock_archivetoday, mock_webarchive, mock_karakeep, mock_keep_enabled, mock_is_admin
    ):
        mock_is_admin.return_value = False
        mock_keep_enabled.return_value = False
        mock_webarchive.return_value = (False, "timed out")
        mock_archivetoday.return_value = (False, "All archive.today mirrors failed")

        mock_bot = MagicMock()

        bot._do_keep(mock_bot, 1, 10, 100, 5, "https://example.com")

        mock_react.assert_called_once_with(mock_bot, 1, 100, "❌")
        mock_send.assert_called_once_with(
            mock_bot, 1, 10,
            "❌ Failed to archive URL.\n• Web Archive: timed out\n• Archive.today: All archive.today mirrors failed"
        )


if __name__ == "__main__":
    unittest.main()
