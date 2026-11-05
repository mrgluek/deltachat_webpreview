"""Private administrator warnings for exhausted Jina Reader tokens."""
import os
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import bot
import database


class TestJinaBalanceAlert(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(patch.object(database, "DB_PATH", os.path.join(self.tmp.name, "test.db")))
        database.init_db()
        database.set_config("admin_dc_email", "admin@example.org")
        self.client = MagicMock()
        self.client.rpc.create_contact.return_value = 42
        self.client.rpc.create_chat_by_contact_id.return_value = 77
        self.enterContext(patch.object(bot, "dc_bot_instance", self.client))
        self.enterContext(patch.object(bot, "dc_accid", 1))
        self.send = self.enterContext(patch("bot._send", return_value=123))
        self.now = self.enterContext(patch("bot.time.time", return_value=1000000))
        self.enterContext(patch("bot._check_jina_balance"))
        self.urlopen = self.enterContext(patch("bot._urlopen"))
        self.enterContext(patch("bot.database.log_api_call"))

    def fetch_error(self, code=402, return_html=False):
        self.urlopen.side_effect = urllib.error.HTTPError(
            "https://r.jina.ai/https://example.org", code, "error", {}, None
        )
        self.assertEqual(bot._fetch_from_jina("https://example.org", return_html), (None,) * 4)

    def test_402_warns_admin_privately(self):
        self.fetch_error()
        self.client.rpc.create_contact.assert_called_once_with(1, "admin@example.org", None)
        self.client.rpc.create_chat_by_contact_id.assert_called_once_with(1, 42)
        self.send.assert_called_once()
        args = self.send.call_args.args
        self.assertEqual(args[:3], (self.client, 1, 77))
        self.assertIn("402", args[3])
        self.assertIn("/jina", args[3])
        self.assertNotIn(bot.JINA_API_KEY or "secret-key", args[3])

    def test_low_warning_does_not_suppress_exhaustion(self):
        bot._notify_jina_balance(999999)
        self.fetch_error()
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("999,999", self.send.call_args_list[0].args[3])
        self.assertIn("402", self.send.call_args_list[1].args[3])
        bot._notify_jina_balance(500000)
        self.fetch_error()
        self.assertEqual(self.send.call_count, 2)

    def test_reader_request_checks_balance(self):
        called = threading.Event()
        bot._check_jina_balance.side_effect = called.set
        self.fetch_error()
        self.assertTrue(called.wait(5))

    def test_slow_balance_check_does_not_delay_reader_request(self):
        release = threading.Event()
        self.addCleanup(release.set)
        bot._check_jina_balance.side_effect = lambda: release.wait(5)
        self.urlopen.return_value.__enter__.return_value.read.return_value = b"<html>ok</html>"
        self.assertEqual(bot._fetch_from_jina("https://example.org", True)[2], "<html>ok</html>")
        self.assertFalse(release.is_set())

    def test_html_request_warns_too(self):
        self.fetch_error(return_html=True)
        self.send.assert_called_once()

    def test_other_errors_do_not_warn(self):
        for code in (401, 403, 429, 500):
            self.fetch_error(code)
        self.urlopen.side_effect = TimeoutError("timeout")
        bot._fetch_from_jina("https://example.org")
        self.send.assert_not_called()

    def test_success_does_not_warn(self):
        self.urlopen.return_value.__enter__.return_value.read.return_value = b"<html>ok</html>"
        self.assertEqual(bot._fetch_from_jina("https://example.org", True)[2], "<html>ok</html>")
        self.send.assert_not_called()

    def test_cooldown_uses_persisted_timestamp(self):
        database.set_config("jina_balance_alert_sent_at", "999999")
        self.fetch_error()
        self.send.assert_not_called()
        self.now.return_value = 999999 + 86400
        self.fetch_error()
        self.send.assert_called_once()

    def test_concurrent_failures_send_once(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda _: bot._notify_jina_balance(), range(8)))
        self.send.assert_called_once()

    def test_failed_send_can_retry(self):
        self.send.return_value = None
        self.fetch_error()
        self.assertIsNone(database.get_config("jina_balance_alert_sent_at"))
        self.send.return_value = 123
        self.fetch_error()
        self.assertEqual(self.send.call_count, 2)
        self.assertIsNotNone(database.get_config("jina_balance_alert_sent_at"))

    def test_rpc_error_does_not_break_fetch(self):
        self.client.rpc.create_chat_by_contact_id.side_effect = RuntimeError("offline")
        self.fetch_error()
        self.send.assert_not_called()
        self.assertIsNone(database.get_config("jina_balance_alert_sent_at"))

    def test_no_admin_skips_warning(self):
        database.set_config("admin_dc_email", "")
        self.fetch_error()
        self.send.assert_not_called()

    def test_fingerprint_only_admin(self):
        database.set_config("admin_dc_email", "")
        database.set_admin_fingerprint("AABBCC")
        self.client.rpc.get_contact_ids.return_value = [11, 42]
        with patch("bot._is_dc_admin", side_effect=lambda client, accid, cid: cid == 42):
            self.fetch_error()
        self.client.rpc.create_chat_by_contact_id.assert_called_once_with(1, 42)
        self.send.assert_called_once()

    def test_admin_with_email_and_fingerprint_gets_encrypted_chat(self):
        # /initadmin stores both; create_contact would give a keyless, unencrypted chat
        database.set_admin_fingerprint("AABBCC")
        self.client.rpc.get_contact_ids.return_value = [11, 42]
        with patch("bot._is_dc_admin", side_effect=lambda client, accid, cid: cid == 42):
            self.fetch_error()
        self.client.rpc.create_contact.assert_not_called()
        self.client.rpc.create_chat_by_contact_id.assert_called_once_with(1, 42)
        self.send.assert_called_once()

    def test_email_only_admin_prefers_key_contact(self):
        self.client.rpc.get_contact_ids.return_value = [42]
        with patch("bot._is_dc_admin", return_value=True):
            self.fetch_error()
        self.client.rpc.get_contact_ids.assert_called_once_with(1, 0, "admin@example.org")
        self.client.rpc.create_contact.assert_not_called()

    def test_fingerprint_without_contact_never_goes_unencrypted(self):
        database.set_admin_fingerprint("AABBCC")
        self.client.rpc.get_contact_ids.return_value = []
        self.fetch_error()
        self.client.rpc.create_contact.assert_not_called()
        self.send.assert_not_called()

    def test_no_matching_fingerprint_skips_warning(self):
        database.set_config("admin_dc_email", "")
        database.set_admin_fingerprint("AABBCC")
        self.client.rpc.get_contact_ids.return_value = [11]
        with patch("bot._is_dc_admin", return_value=False):
            self.fetch_error()
        self.send.assert_not_called()

    def test_bot_not_started_skips_warning(self):
        with patch.object(bot, "dc_bot_instance", None):
            self.fetch_error()
        self.send.assert_not_called()


class TestJinaBalanceCheck(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(bot, "JINA_API_KEY", "secret&key"))
        self.enterContext(patch.object(bot, "dc_bot_instance", MagicMock()))
        self.enterContext(patch.object(bot, "dc_accid", 1))
        self.enterContext(patch.object(bot, "_jina_balance_last_check", None))
        self.clock = self.enterContext(patch("bot.time.monotonic", return_value=10000))
        self.urlopen = self.enterContext(patch("bot._urlopen"))
        self.notify = self.enterContext(patch("bot._notify_jina_balance"))

    def response(self, balance):
        import json
        self.urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
            {"wallet": {"total_balance": balance}}
        ).encode()

    def test_warns_strictly_below_million(self):
        self.response(999999)
        bot._check_jina_balance()
        self.notify.assert_called_once_with(999999)
        self.assertIn("api_key=secret%26key", self.urlopen.call_args.args[0].full_url)

    def test_threshold_and_higher_do_not_warn(self):
        for balance in (1000000, 2000000):
            with self.subTest(balance=balance):
                bot._jina_balance_last_check = None
                self.response(balance)
                bot._check_jina_balance()
        self.notify.assert_not_called()

    def test_zero_warns_about_exhaustion(self):
        self.response(0)
        bot._check_jina_balance()
        self.notify.assert_called_once_with(0)

    def test_checks_at_most_once_per_hour(self):
        self.response(999999)
        bot._check_jina_balance()
        self.clock.return_value += 3599
        bot._check_jina_balance()
        self.urlopen.assert_called_once()
        self.clock.return_value += 1
        bot._check_jina_balance()
        self.assertEqual(self.urlopen.call_count, 2)

    def test_failed_check_is_throttled_and_does_not_leak_key(self):
        self.urlopen.side_effect = RuntimeError("secret&key")
        with self.assertLogs("webpreview_bot", level="WARNING") as logs:
            bot._check_jina_balance()
            bot._check_jina_balance()
        self.urlopen.assert_called_once()
        self.notify.assert_not_called()
        self.assertNotIn("secret&key", " ".join(logs.output))

    def test_missing_balance_does_not_trigger_exhaustion_warning(self):
        self.urlopen.return_value.__enter__.return_value.read.return_value = b'{"wallet": {}}'
        bot._check_jina_balance()
        self.notify.assert_not_called()

    def test_invalid_balance_does_not_warn(self):
        for balance in (None, "unknown", True):
            bot._jina_balance_last_check = None
            self.response(balance)
            bot._check_jina_balance()
        self.notify.assert_not_called()

    def test_no_key_does_not_check(self):
        with patch.object(bot, "JINA_API_KEY", ""):
            bot._check_jina_balance()
        self.urlopen.assert_not_called()

    def test_parallel_request_does_not_wait_for_balance_check(self):
        with bot._jina_balance_check_lock:
            bot._check_jina_balance()
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
