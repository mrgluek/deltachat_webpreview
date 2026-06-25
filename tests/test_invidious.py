"""
Tests for Invidious helpers:
  - _extract_youtube_id_from_invidious
  - _clean_domain
  - database helpers: add/remove/list invidious domains
"""
import unittest
import os
import sys
import tempfile

# Ensure the project root is importable and use an isolated DB
_TEST_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TEST_DB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import database

database.init_db()


class TestExtractYouTubeId(unittest.TestCase):
    """Tests for _extract_youtube_id_from_invidious."""

    def test_watch_query_param(self):
        vid = bot._extract_youtube_id_from_invidious(
            "https://yewtu.be/watch?v=dQw4w9WgXcQ"
        )
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_embed_path(self):
        vid = bot._extract_youtube_id_from_invidious(
            "https://invidious.snopyta.org/embed/dQw4w9WgXcQ"
        )
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_v_path(self):
        vid = bot._extract_youtube_id_from_invidious(
            "https://invidious.kavin.rocks/v/dQw4w9WgXcQ"
        )
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_no_video_id(self):
        vid = bot._extract_youtube_id_from_invidious("https://invidious.io/")
        self.assertIsNone(vid)


class TestCleanDomain(unittest.TestCase):
    """Tests for _clean_domain."""

    def test_bare_domain(self):
        self.assertEqual(bot._clean_domain("inv.nadeko.net"), "inv.nadeko.net")

    def test_strips_whitespace_and_lowercases(self):
        self.assertEqual(bot._clean_domain("  INV.nadeko.net  "), "inv.nadeko.net")

    def test_full_https_url(self):
        self.assertEqual(
            bot._clean_domain("https://inv.nadeko.net/watch?v=123"),
            "inv.nadeko.net",
        )

    def test_protocol_relative_url(self):
        self.assertEqual(
            bot._clean_domain("//inv.nadeko.net:8080/watch"),
            "inv.nadeko.net",
        )

    def test_url_with_trailing_slash(self):
        self.assertEqual(bot._clean_domain("http://yewtu.be/"), "yewtu.be")


class TestDatabaseInvidiousDomains(unittest.TestCase):
    """Tests for database Invidious domain management helpers."""

    TEST_DOMAIN = "test-invidious.example"

    def setUp(self):
        database.remove_invidious_domain(self.TEST_DOMAIN)

    def tearDown(self):
        database.remove_invidious_domain(self.TEST_DOMAIN)

    def test_domain_not_listed_before_add(self):
        domains = database.list_invidious_domains()
        self.assertNotIn(self.TEST_DOMAIN, domains)

    def test_add_domain(self):
        database.add_invidious_domain(self.TEST_DOMAIN)
        domains = database.list_invidious_domains()
        self.assertIn(self.TEST_DOMAIN, domains)

    def test_config_key_set_on_add(self):
        database.add_invidious_domain(self.TEST_DOMAIN)
        val = database.get_config(f"invidious_domain_{self.TEST_DOMAIN}")
        self.assertEqual(val, "1")

    def test_remove_domain(self):
        database.add_invidious_domain(self.TEST_DOMAIN)
        database.remove_invidious_domain(self.TEST_DOMAIN)
        domains = database.list_invidious_domains()
        self.assertNotIn(self.TEST_DOMAIN, domains)


if __name__ == "__main__":
    unittest.main()
