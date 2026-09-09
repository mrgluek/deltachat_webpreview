"""
Tests for database operations in deltachat_webpreview.
Adheres to AGENTS.md database unit testing conventions.
"""
import os
import time
import unittest
from unittest.mock import MagicMock
import sys

# Ensure root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database

TEST_DB_PATH = "test_webpreview.db"


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = TEST_DB_PATH
        # Reset transport stats buffer
        with database._transport_stats_lock:
            database._transport_stats_buffer.clear()
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        # Clean up database and any WAL/SHM sidecar files
        for f in (TEST_DB_PATH, f"{TEST_DB_PATH}-wal", f"{TEST_DB_PATH}-shm"):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

    # ── Config Tests ────────────────────────────────────────────────────────
    def test_config_set_get_roundtrip(self):
        database.set_config("test_key", "test_value")
        self.assertEqual(database.get_config("test_key"), "test_value")

    def test_config_overwrite(self):
        database.set_config("test_key", "v1")
        database.set_config("test_key", "v2")
        self.assertEqual(database.get_config("test_key"), "v2")

    def test_config_missing_key(self):
        self.assertIsNone(database.get_config("nonexistent_key_12345"))

    # ── Admin Fingerprint Tests ─────────────────────────────────────────────
    def test_admin_fingerprint_get_set(self):
        self.assertIsNone(database.get_admin_fingerprint())
        database.set_admin_fingerprint("A1B2C3D4E5F6")
        self.assertEqual(database.get_admin_fingerprint(), "A1B2C3D4E5F6")

    # ── Resilient Mode Flag Tests ───────────────────────────────────────────
    def test_resilient_flag_toggle(self):
        self.assertIsNone(database.get_config("resilient"))
        database.set_config("resilient", "1")
        self.assertEqual(database.get_config("resilient"), "1")
        database.set_config("resilient", "0")
        self.assertEqual(database.get_config("resilient"), "0")

    # ── Transport Statistics Tests (Buffered) ───────────────────────────────
    def test_transport_stats_accumulation_and_flush(self):
        addr1 = "bot1@example.com"
        addr2 = "bot2@example.com"

        # Increment sent and received in memory buffer
        database.increment_transport_sent(addr1)
        database.increment_transport_sent(addr1)
        database.increment_transport_received(addr1)

        database.increment_transport_sent(addr2)

        # Before flush, buffer has counts
        with database._transport_stats_lock:
            self.assertEqual(database._transport_stats_buffer[addr1]["sent"], 2)
            self.assertEqual(database._transport_stats_buffer[addr1]["recv"], 1)
            self.assertEqual(database._transport_stats_buffer[addr2]["sent"], 1)

        # get_all_transport_stats automatically triggers flush_transport_stats()
        stats = database.get_all_transport_stats()
        self.assertEqual(len(stats), 2)

        stats_map = {s["addr"]: s for s in stats}
        self.assertEqual(stats_map[addr1]["msgs_sent"], 2)
        self.assertEqual(stats_map[addr1]["msgs_received"], 1)
        self.assertEqual(stats_map[addr2]["msgs_sent"], 1)
        self.assertEqual(stats_map[addr2]["msgs_received"], 0)

        # Subsequent increments accumulate properly in database
        database.increment_transport_sent(addr1)
        database.flush_transport_stats()

        stats_after = database.get_all_transport_stats()
        stats_map_after = {s["addr"]: s for s in stats_after}
        self.assertEqual(stats_map_after[addr1]["msgs_sent"], 3)

    def test_transport_stats_ignores_invalid_addresses(self):
        database.increment_transport_sent("")
        database.increment_transport_sent(None)
        database.increment_transport_sent("invalid_address_without_at")
        database.flush_transport_stats()
        self.assertEqual(len(database.get_all_transport_stats()), 0)

    # ── Preview Stats & Record Retention Tests ──────────────────────────────
    def test_preview_stats_and_get_stats(self):
        database.add_preview_log(10, 100, "https://example.com/1", "Test 1", 1024, False)
        database.add_preview_log(10, 101, "https://example.com/2", "Test 2", 2048, True)

        stats = database.get_stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["last_24h"], 2)
        self.assertEqual(stats["total_size"], 3072)

    def test_cleanup_old_records(self):
        now = int(time.time())
        old_time = now - (35 * 86400)  # 35 days ago (outside 30-day window)
        recent_time = now - 3600       # 1 hour ago

        conn = database._connect()
        cursor = conn.cursor()
        # Insert 1 old and 1 recent preview stat
        cursor.execute(
            "INSERT INTO preview_stats (chat_id, from_id, url, title, filesize, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (1, 1, "https://old.com", "Old", 100, old_time)
        )
        cursor.execute(
            "INSERT INTO preview_stats (chat_id, from_id, url, title, filesize, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (1, 1, "https://recent.com", "Recent", 100, recent_time)
        )
        # Insert 1 old and 1 recent api_log
        cursor.execute("INSERT INTO api_log (service, created_at) VALUES (?, ?)", ("jina", old_time))
        cursor.execute("INSERT INTO api_log (service, created_at) VALUES (?, ?)", ("jina", recent_time))
        conn.commit()
        conn.close()

        # Prune older than 30 days
        pruned = database.cleanup_old_records(retention_days=30)
        self.assertEqual(pruned["preview_stats"], 1)
        self.assertEqual(pruned["api_log"], 1)

        # Verify only recent rows remain
        conn = database._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM preview_stats")
        self.assertEqual(cursor.fetchone()[0], 1)
        cursor.execute("SELECT COUNT(*) FROM api_log")
        self.assertEqual(cursor.fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
