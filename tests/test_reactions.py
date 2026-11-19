"""Tests for emoji reaction actions (delete / save / tldr / preview / webxdc)."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import database


class TestReactionActions(unittest.TestCase):
    def setUp(self):
        bot._seen_reactions.clear()

    def test_variation_selector_and_mapping(self):
        self.assertEqual(bot._new_reaction_actions(1, 5, ["🗑️"]), ["delete"])
        self.assertEqual(bot._new_reaction_actions(2, 5, ["🏛"]), ["archive"])

    def test_only_new_reactions_fire(self):
        self.assertEqual(bot._new_reaction_actions(1, 5, ["⚡"]), ["tldr"])
        self.assertEqual(bot._new_reaction_actions(1, 5, ["⚡"]), [])
        self.assertEqual(bot._new_reaction_actions(1, 5, []), [])
        self.assertEqual(bot._new_reaction_actions(1, 5, ["⚡"]), ["tldr"])

    def test_unknown_emoji_ignored(self):
        self.assertEqual(bot._new_reaction_actions(1, 5, ["👍"]), [])


class TestReactionHandling(unittest.TestCase):
    def setUp(self):
        self._old_db = database.DB_PATH
        database.DB_PATH = "test_reactions.db"
        database.init_db()
        self.bot = MagicMock()

    def tearDown(self):
        database.DB_PATH = self._old_db
        for f in ("test_reactions.db", "test_reactions.db-wal", "test_reactions.db-shm"):
            if os.path.exists(f):
                os.remove(f)

    def test_bot_msg_tracking(self):
        database.add_bot_msg(10, 3, 7, "https://example.com/")
        self.assertEqual(database.get_bot_msg(10),
                         {"chat_id": 3, "requester_id": 7, "url": "https://example.com/"})
        self.assertIsNone(database.get_bot_msg(11))

    @patch("bot._is_dc_admin", return_value=False)
    def test_delete_only_by_requester(self, _):
        database.add_bot_msg(10, 3, 7, None)
        self.bot.rpc.get_message.return_value = MagicMock(from_id=bot.DC_CONTACT_ID_SELF)
        bot._handle_reaction(self.bot, 1, 3, 10, 8, "delete")
        self.bot.rpc.delete_messages_for_all.assert_not_called()
        bot._handle_reaction(self.bot, 1, 3, 10, 7, "delete")
        self.bot.rpc.delete_messages_for_all.assert_called_once_with(1, [10])

    @patch("bot._is_dc_admin", return_value=False)
    def test_delete_ignores_foreign_messages(self, _):
        self.bot.rpc.get_message.return_value = MagicMock(from_id=99)
        bot._handle_reaction(self.bot, 1, 3, 10, 7, "delete")
        self.bot.rpc.delete_messages_for_all.assert_not_called()

    @patch("bot._save_to_web_archive", return_value=(True, "https://web.archive.org/x"))
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._is_dc_admin", return_value=False)
    @patch("bot._is_internal_or_invalid_url", return_value=False)
    def test_archive_reacts_without_message(self, *_):
        bot._do_reaction_archive(self.bot, 1, 3, 10, 7, "https://example.com/")
        self.bot.rpc.send_reaction.assert_called_with(1, 10, ["🏛️"])
        self.bot.rpc.send_msg.assert_not_called()

    @patch("bot._save_to_karakeep", return_value=(True, "id1"))
    def test_karakeep_reacts_without_message(self, _):
        bot._do_reaction_karakeep(self.bot, 1, 3, 10, 7, "https://example.com/")
        self.bot.rpc.send_reaction.assert_called_with(1, 10, ["☑️"])
        self.bot.rpc.send_msg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
