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


if __name__ == "__main__":
    unittest.main()
