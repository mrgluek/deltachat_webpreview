"""
Tests for /webpreview command and auto-preview toggle logic.
"""
import os
import sys
import unittest
import tempfile
from unittest.mock import MagicMock, patch

# Use an isolated test database
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


class TestWebPreviewToggle(unittest.TestCase):
    """Tests for the /webpreview toggle command and logic."""

    def setUp(self):
        # Ensure database is initialized since tearDown might delete it
        database.init_db()
        # Reset DB state for chat_id=1 and chat_id=2
        database.set_webpreview_disabled(1, False)
        database.set_webpreview_disabled(2, False)
        database.set_config("greeted_10", "1")
        bot.dc_accid = 1

    def tearDown(self):
        try:
            os.remove(_TEST_DB)
        except Exception:
            pass

    def test_database_disabled_state(self):
        # Default is enabled (disabled=False)
        self.assertFalse(database.is_webpreview_disabled(1))

        # Disable it
        database.set_webpreview_disabled(1, True)
        self.assertTrue(database.is_webpreview_disabled(1))

        # Re-enable it
        database.set_webpreview_disabled(1, False)
        self.assertFalse(database.is_webpreview_disabled(1))

    @patch("bot._send")
    def test_webpreview_command_toggle(self, mock_send):
        mock_bot = MagicMock()

        # 1. Turn OFF
        event_off = MockEvent(chat_id=1, payload="off", text="/webpreview off")
        bot.webpreview_command(mock_bot, 1, event_off)
        self.assertTrue(database.is_webpreview_disabled(1))
        mock_send.assert_called_with(
            mock_bot, 1, 1, "🔇 WebPreview has been disabled for this chat. I will no longer preview links automatically. You can still use `/preview`, `/archive`, or `/keep` on links manually."
        )

        # 2. Turn ON
        event_on = MockEvent(chat_id=1, payload="on", text="/webpreview on")
        bot.webpreview_command(mock_bot, 1, event_on)
        self.assertFalse(database.is_webpreview_disabled(1))
        mock_send.assert_called_with(
            mock_bot, 1, 1, "🔊 WebPreview has been enabled for this chat. Links will be parsed automatically."
        )

        # 3. Status check when enabled
        event_status = MockEvent(chat_id=1, payload="", text="/webpreview")
        bot.webpreview_command(mock_bot, 1, event_status)
        mock_send.assert_called_with(
            mock_bot, 1, 1,
            "WebPreview is currently enabled 🔊 for this chat.\n\n"
            "Usage:\n"
            "• `/webpreview off` (or `0`, `false`) — Disable automatic link previews\n"
            "• `/webpreview on` (or `1`, `true`) — Enable automatic link previews"
        )

        # 4. Status check when disabled
        database.set_webpreview_disabled(1, True)
        bot.webpreview_command(mock_bot, 1, event_status)
        mock_send.assert_called_with(
            mock_bot, 1, 1,
            "WebPreview is currently disabled 🔇 for this chat.\n\n"
            "Usage:\n"
            "• `/webpreview off` (or `0`, `false`) — Disable automatic link previews\n"
            "• `/webpreview on` (or `1`, `true`) — Enable automatic link previews"
        )

    @patch("bot._is_private_chat")
    @patch("threading.Thread")
    @patch("bot._is_bot_blocked")
    @patch("bot._is_rate_limited")
    @patch("bot._is_yt_bot_in_chat")
    def test_on_new_message_respects_disabled_state(
        self, mock_yt_bot, mock_rate_limit, mock_blocked, mock_thread, mock_is_private
    ):
        mock_blocked.return_value = False
        mock_rate_limit.return_value = False
        mock_yt_bot.return_value = False
        mock_bot = MagicMock()
        
        # Scenario A: Private chat
        mock_is_private.return_value = True

        # When WebPreview is enabled (default), thread should start to parse URL
        event_a1 = MockEvent(chat_id=1, msg_id=101, text="Check this link https://google.com")
        bot.on_new_message(mock_bot, 1, event_a1)
        mock_thread.assert_called_once()
        mock_thread.reset_mock()

        # When WebPreview is disabled, thread should NOT start
        database.set_webpreview_disabled(1, True)
        event_a2 = MockEvent(chat_id=1, msg_id=102, text="Check this link https://google.com")
        bot.on_new_message(mock_bot, 1, event_a2)
        mock_thread.assert_not_called()
        mock_thread.reset_mock()

        # Scenario B: Group chat
        mock_is_private.return_value = False

        # When WebPreview is enabled (default)
        event_b1 = MockEvent(chat_id=2, msg_id=103, text="Look: https://google.com")
        bot.on_new_message(mock_bot, 1, event_b1)
        mock_thread.assert_called_once()
        mock_thread.reset_mock()

        # When WebPreview is disabled
        database.set_webpreview_disabled(2, True)
        event_b2 = MockEvent(chat_id=2, msg_id=104, text="Look: https://google.com")
        bot.on_new_message(mock_bot, 1, event_b2)
        mock_thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
