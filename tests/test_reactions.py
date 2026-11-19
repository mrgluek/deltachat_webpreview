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
        bot._save_results.clear()
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
                         {"chat_id": 3, "requester_id": 7, "url": "https://example.com/", "req_msg_id": None})
        self.assertIsNone(database.get_bot_msg(11))

    @patch("bot._is_dc_admin", return_value=False)
    def test_delete_only_by_requester(self, _):
        database.add_bot_msg(10, 3, 7, None, 5)
        self.bot.rpc.get_message.return_value = MagicMock(from_id=bot.DC_CONTACT_ID_SELF)
        bot._handle_reaction(self.bot, 1, 3, 10, 8, "delete")
        self.bot.rpc.delete_messages_for_all.assert_not_called()
        bot._handle_reaction(self.bot, 1, 3, 10, 7, "delete")
        self.bot.rpc.delete_messages_for_all.assert_called_once_with(1, [10])
        self.bot.rpc.send_reaction.assert_called_with(1, 5, ["🗑️ Deleted by user request"])

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
        self.bot.rpc.send_reaction.assert_called_with(1, 10, ["🏛️ Saved to Web Archive!"])
        self.bot.rpc.send_msg.assert_not_called()

    @patch("bot._save_to_karakeep", return_value=(True, "id1"))
    def test_karakeep_reacts_without_message(self, _):
        bot._do_reaction_karakeep(self.bot, 1, 3, 10, 7, "https://example.com/")
        self.bot.rpc.send_reaction.assert_called_with(1, 10, ["💾 Saved to KaraKeep!"])
        self.bot.rpc.send_msg.assert_not_called()

    def test_count_downvotes_any_skin_tone(self):
        by = {"1": ["👎"], "5": ["👎🏻"], "6": ["👎🏿"], "7": ["👍"], "8": ["👎️"]}
        self.assertEqual(bot._count_downvotes(by), 3)

    @patch("bot._get_admin_chat_id", return_value=77)
    def test_downvotes_notify_admin_once_over_threshold(self, _):
        database.add_bot_msg(20, 3, 7, "https://bad.example.com/page")
        by = {str(i): ["👎"] for i in range(10, 12)}
        bot._check_downvotes(self.bot, 1, 20, by)
        self.bot.rpc.send_msg.assert_not_called()
        by["12"] = ["👎🏽"]
        bot._check_downvotes(self.bot, 1, 20, by)
        bot._check_downvotes(self.bot, 1, 20, by)
        self.assertEqual(self.bot.rpc.send_msg.call_count, 1)
        self.assertIn("/preview_exclude bad.example.com",
                      self.bot.rpc.send_msg.call_args[0][2].text)

    @patch("bot._get_admin_chat_id", return_value=77)
    def test_downvotes_ignore_untracked_messages(self, _):
        bot._check_downvotes(self.bot, 1, 99, {str(i): ["👎"] for i in range(10, 20)})
        self.bot.rpc.send_msg.assert_not_called()


    def test_save_reactions_combine(self):
        bot._save_results.clear()
        bot._react_save_result(self.bot, 1, 40, "karakeep", True)
        self.bot.rpc.send_reaction.assert_called_with(1, 40, ["💾 Saved to KaraKeep!"])
        bot._react_save_result(self.bot, 1, 40, "archive", True)
        self.bot.rpc.send_reaction.assert_called_with(1, 40, ["💾 KaraKeep · 🏛️ Archive"])
        bot._react_save_result(self.bot, 1, 40, "archive", False)
        self.bot.rpc.send_reaction.assert_called_with(1, 40, ["💾 KaraKeep · ❌ Archive"])


if __name__ == "__main__":
    unittest.main()
