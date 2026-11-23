"""2.32.3: the own fingerprint is read once per account, not on every admin check."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import database
import dc_fingerprint

SELF_FP = "D58B5E6C55482F94F9F5B84B6FB9FE3AD2658082"
PEER_FP = "0ECC037BAD768C41CA99B1F5F0B80E7D5DB13122"
ADMIN_FP = "DEC0E8D11FC4686F2C715C56F8C9AD699BC19AD8"
INFO = (f"Messages are end-to-end encrypted.\nFingerprints:\n\nMe:\n{SELF_FP}\n\n"
        f"Bob:\n{PEER_FP}\n\nRelays:\nbob@example.net")


def make_bot():
    b = MagicMock()
    b.rpc.get_chat_securejoin_qr_code.return_value = f"https://i.delta.chat/#{SELF_FP}&v=3&i=a&s=b"
    b.rpc.get_contact.return_value = SimpleNamespace(address="bob@example.net")
    b.rpc.get_contact_encryption_info.return_value = INFO
    return b


class TestOwnFingerprintCache(unittest.TestCase):
    def setUp(self):
        self._old_db = database.DB_PATH
        database.DB_PATH = "test_fp_cache.db"
        database.init_db()
        database.set_admin_fingerprint(ADMIN_FP)
        dc_fingerprint.forget_self_fingerprint()
        bot._user_rate_limits.clear()

    def tearDown(self):
        database.DB_PATH = self._old_db
        for f in ("test_fp_cache.db", "test_fp_cache.db-wal", "test_fp_cache.db-shm"):
            if os.path.exists(f):
                os.remove(f)

    def test_helper_asks_the_core_once(self):
        rpc = make_bot().rpc
        for _ in range(1000):
            self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)
        self.assertEqual(rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_failures_are_not_cached_and_accounts_are_separate(self):
        rpc = MagicMock()
        rpc.get_chat_securejoin_qr_code.side_effect = [Exception("down"), f"https://i.delta.chat/#{SELF_FP}&v=3"]
        self.assertIsNone(dc_fingerprint.get_self_fingerprint(rpc, 1))
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)
        rpc.get_chat_securejoin_qr_code.side_effect = None
        rpc.get_chat_securejoin_qr_code.return_value = f"https://i.delta.chat/#{PEER_FP}&v=3"
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 2), PEER_FP)
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)
        dc_fingerprint.forget_self_fingerprint(rpc, 1)
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), PEER_FP)

    def test_1000_rate_limit_checks_of_non_admins_cost_at_most_one_qr_call(self):
        b = make_bot()
        for sender in range(100, 1100):  # distinct senders, so none is rate limited
            self.assertFalse(bot._is_rate_limited(b, 1, sender))
        self.assertLessEqual(b.rpc.get_chat_securejoin_qr_code.call_count, 1)
        self.assertEqual(b.rpc.get_contact_encryption_info.call_count, 1000)  # per-contact info stays

    def test_1000_admin_checks_cost_at_most_one_qr_call(self):
        b = make_bot()
        for _ in range(1000):
            self.assertFalse(bot._is_dc_admin(b, 1, 50))
        self.assertLessEqual(b.rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_admin_still_recognised_by_fingerprint(self):
        b = make_bot()
        b.rpc.get_contact_encryption_info.return_value = INFO.replace(PEER_FP, ADMIN_FP)
        for _ in range(20):
            self.assertTrue(bot._is_dc_admin(b, 1, 50))
        self.assertEqual(b.rpc.get_chat_securejoin_qr_code.call_count, 1)


if __name__ == "__main__":
    unittest.main()
