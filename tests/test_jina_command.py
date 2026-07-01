"""
Tests for /jina token balance check command:
  - Authorization check (admin only)
  - Key fallback logic (payload vs JINA_API_KEY env)
  - Handling of missing key configuration
  - Parsing and formatting of the balance stats from Jina API
  - Error handling on network / API failure
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


class MockEvent:
    def __init__(self, from_id=10, chat_id=1, msg_id=100, payload=""):
        self.msg = MagicMock()
        self.msg.from_id = from_id
        self.msg.chat_id = chat_id
        self.msg.id = msg_id
        self.payload = payload


class TestJinaCommand(unittest.TestCase):
    """Tests for the /jina balance checking command."""

    def setUp(self):
        self.original_key = bot.JINA_API_KEY
        bot.JINA_API_KEY = "env_jina_key_123"

    def tearDown(self):
        bot.JINA_API_KEY = self.original_key

    @patch("bot._is_dc_admin")
    @patch("bot._send")
    def test_non_admin_rejected(self, mock_send, mock_is_admin):
        mock_is_admin.return_value = False
        event = MockEvent()

        bot._handle_jina_command(None, 1, event)

        mock_send.assert_called_once_with(
            None, 1, 1, "❌ Only the bot administrator can use /jina."
        )

    @patch("bot._is_dc_admin")
    @patch("bot._send")
    def test_missing_api_key_warning(self, mock_send, mock_is_admin):
        mock_is_admin.return_value = True
        bot.JINA_API_KEY = ""
        event = MockEvent(payload="")

        bot._handle_jina_command(None, 1, event)

        mock_send.assert_called_once()
        self.assertIn("JINA_API_KEY` is not configured", mock_send.call_args[0][3])

    @patch("threading.Thread")
    @patch("bot._is_dc_admin")
    @patch("bot._urlopen")
    @patch("bot._react")
    @patch("bot._send")
    def test_jina_check_success_default_key(self, mock_send, mock_react, mock_urlopen, mock_is_admin, mock_thread):
        mock_is_admin.return_value = True

        # Run thread target synchronously
        def run_sync(target, daemon=True):
            target()
            return MagicMock()
        mock_thread.side_effect = run_sync

        # Mock API Response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "wallet": {
                "total_balance": 9972364,
                "trial_balance": 9972364,
                "regular_balance": 0,
                "trial_start": "2026-06-26T16:57:52.994094+00:00",
                "trial_end": "2036-06-26T16:57:52.994094+00:00"
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        event = MockEvent(payload="")
        bot._handle_jina_command(None, 1, event)

        # Verify key used in URL
        called_url = mock_urlopen.call_args[0][0].full_url
        self.assertIn("api_key=env_jina_key_123", called_url)

        # Verify reactions (pending / done)
        mock_react.assert_any_call(None, 1, 100, "⏳")
        mock_react.assert_any_call(None, 1, 100, "☑️")

        # Verify output message formatting
        mock_send.assert_called_once()
        message = mock_send.call_args[0][3]
        self.assertIn("Jina AI API Key Stats:", message)
        self.assertIn("Total Balance:** 9,972,364", message)
        self.assertIn("Trial Period:** 2026-06-26 to 2036-06-26", message)

    @patch("threading.Thread")
    @patch("bot._is_dc_admin")
    @patch("bot._urlopen")
    @patch("bot._react")
    @patch("bot._send")
    def test_jina_check_success_payload_key(self, mock_send, mock_react, mock_urlopen, mock_is_admin, mock_thread):
        mock_is_admin.return_value = True

        def run_sync(target, daemon=True):
            target()
            return MagicMock()
        mock_thread.side_effect = run_sync

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "wallet": {
                "total_balance": 50000,
                "trial_balance": 10000,
                "regular_balance": 40000
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Pass custom key as payload
        event = MockEvent(payload="custom_jina_key_xyz")
        bot._handle_jina_command(None, 1, event)

        called_url = mock_urlopen.call_args[0][0].full_url
        self.assertIn("api_key=custom_jina_key_xyz", called_url)

        mock_send.assert_called_once()
        message = mock_send.call_args[0][3]
        self.assertIn("Total Balance:** 50,000", message)
        self.assertIn("Regular Balance:** 40,000", message)

    @patch("threading.Thread")
    @patch("bot._is_dc_admin")
    @patch("bot._urlopen")
    @patch("bot._react")
    @patch("bot._send")
    def test_jina_check_failure(self, mock_send, mock_react, mock_urlopen, mock_is_admin, mock_thread):
        mock_is_admin.return_value = True

        def run_sync(target, daemon=True):
            target()
            return MagicMock()
        mock_thread.side_effect = run_sync

        # Mock exception
        mock_urlopen.side_effect = Exception("HTTP 401 Unauthorized")

        event = MockEvent()
        bot._handle_jina_command(None, 1, event)

        mock_react.assert_any_call(None, 1, 100, "⏳")
        mock_react.assert_any_call(None, 1, 100, "❌")

        mock_send.assert_called_once()
        self.assertIn("Failed to check Jina API key balance", mock_send.call_args[0][3])
        self.assertIn("HTTP 401 Unauthorized", mock_send.call_args[0][3])


if __name__ == "__main__":
    unittest.main()
