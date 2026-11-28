"""Tests for proxy credential redaction in logs."""
import os
import sys
import unittest
import urllib.request
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


class TestRedactUrl(unittest.TestCase):
    def test_no_userinfo_unchanged(self):
        self.assertEqual(bot.redact_url("http://proxy.host:8888"), "http://proxy.host:8888")

    def test_user_and_password(self):
        self.assertEqual(bot.redact_url("http://USER:PASS@proxy.host:8888"), "http://***@proxy.host:8888")

    def test_user_only(self):
        out = bot.redact_url("http://tokenuser@proxy.host:8888")
        self.assertEqual(out, "http://***@proxy.host:8888")

    def test_https_and_socks5(self):
        self.assertEqual(bot.redact_url("https://u:p@h.example:443/x?y=1"), "https://***@h.example:443/x?y=1")
        self.assertEqual(bot.redact_url("socks5://u:p@10.0.0.1:1080"), "socks5://***@10.0.0.1:1080")

    def test_percent_encoded_password(self):
        out = bot.redact_url("http://user:p%40ss%3Aw0rd@proxy.host:8888")
        self.assertEqual(out, "http://***@proxy.host:8888")
        self.assertNotIn("w0rd", out)

    def test_raw_at_in_password(self):
        out = bot.redact_url("http://user:p@ss:word@proxy.host:8888")
        self.assertNotIn("ss", out.split("@")[0])
        self.assertNotIn("word", out)
        self.assertTrue(out.endswith("@proxy.host:8888"))

    def test_garbage_never_leaks(self):
        for raw in ["user:secret@host", "::::secret@@@", "http://[bad:secret@host", "secret@"]:
            out = bot.redact_url(raw)
            self.assertNotIn("secret", out, raw)

    def test_empty_and_none(self):
        self.assertEqual(bot.redact_url(""), "")
        self.assertEqual(bot.redact_url(None), "")

    def test_redact_text(self):
        msg = "Tunnel failed via http://u:secret@proxy.host:8888 timeout"
        out = bot.redact_text(msg)
        self.assertNotIn("secret", out)
        self.assertIn("proxy.host:8888", out)


class TestProxyLogsHaveNoCredentials(unittest.TestCase):
    def test_routing_and_failure_logs(self):
        creds = "http://USER:SECRETPW@proxy.host:8888"
        opener = MagicMock()
        opener.open.side_effect = OSError(f"cannot reach {creds}")
        with patch.object(bot, "PROXY_URL", creds), \
             patch.object(bot, "PROXY_DOMAINS", [".ru"]), \
             patch.object(bot, "JINA_PROXY_URL", ""), \
             patch.object(bot, "INSTAGRAM_PROXY_URL", ""), \
             patch.object(bot, "ARCHIVE_TODAY_PROXY_URL", ""), \
             patch("urllib.request.build_opener", return_value=opener), \
             patch("urllib.request.urlopen", return_value=MagicMock()):
            with self.assertLogs(bot.logger, level="DEBUG") as cm:
                bot._urlopen(urllib.request.Request("https://example.ru/page"), timeout=5)
        text = "\n".join(cm.output)
        self.assertIn("through proxy", text)
        self.assertIn("proxy.host:8888", text)
        self.assertNotIn("SECRETPW", text)
        self.assertNotIn("USER", text)


if __name__ == "__main__":
    unittest.main()
