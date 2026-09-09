"""
Tests for URL validation helpers (_is_internal_or_invalid_url).
"""
import unittest
import os
import sys

# Ensure the project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


class TestUrlValidation(unittest.TestCase):
    """Tests for _is_internal_or_invalid_url."""

    def _check(self, url: str, expected: bool):
        result = bot._is_internal_or_invalid_url(url)
        self.assertEqual(
            result,
            expected,
            msg=f"URL '{url}' -> expected is_invalid={expected}, got {result}",
        )

    # ── valid external URLs ─────────────────────────────────────────────────
    def test_valid_domain_ru(self):
        self._check("https://yandex.ru/games/", False)

    def test_valid_domain_com(self):
        self._check("http://google.com", False)

    def test_valid_cyrillic_domain(self):
        self._check("https://президент.рф/", False)

    def test_valid_subdomain(self):
        self._check("https://sub.domain.co.uk/path", False)

    # ── invalid / internal hostnames ────────────────────────────────────────
    def test_invalid_no_dot(self):
        self._check("https://юрл", True)

    def test_invalid_bare_hostname(self):
        self._check("http://test", True)

    def test_invalid_localhost(self):
        self._check("http://localhost", True)

    def test_invalid_example_com(self):
        # example.com is explicitly blocked in the bot
        self._check("https://example.com", True)

    # ── local domain suffixes ───────────────────────────────────────────────
    def test_local_suffix_local(self):
        self._check("http://myhost.local", True)

    def test_local_suffix_lan(self):
        self._check("http://server.lan", True)

    def test_local_suffix_home(self):
        self._check("http://router.home", True)

    def test_local_suffix_onion(self):
        self._check("https://secret.onion", True)

    # ── private IP addresses ─────────────────────────────────────────────────
    def test_loopback_ipv4(self):
        self._check("http://127.0.0.1", True)

    def test_private_ipv4(self):
        self._check("http://192.168.1.1", True)

    def test_loopback_ipv6(self):
        self._check("http://[::1]", True)

    def test_documentation_ipv6(self):
        self._check("http://[2001:db8::1]", True)

    def test_unspecified_ipv4(self):
        self._check("http://0.0.0.0", True)

    # ── DNS resolution / rebinding SSRF protection ───────────────────────────
    @unittest.mock.patch("socket.getaddrinfo")
    def test_dns_resolving_to_private_ip(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("192.168.1.50", 80))
        ]
        self._check("https://attacker-domain.com", True)

    @unittest.mock.patch("socket.getaddrinfo")
    def test_dns_resolving_to_loopback_ip(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("127.0.0.1", 80))
        ]
        self._check("https://nip.io", True)

    @unittest.mock.patch("socket.getaddrinfo")
    def test_dns_resolving_to_public_ip(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 80))
        ]
        self._check("https://public-site.org", False)

    # ── SafeRedirectHandler Tests ───────────────────────────────────────────
    def test_safe_redirect_blocks_private_url(self):
        handler = bot.SafeRedirectHandler()
        req = unittest.mock.MagicMock()
        req.full_url = "https://public-site.com/redirect"
        with self.assertRaises(bot.urllib.error.HTTPError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/admin")

    def test_safe_redirect_blocks_relative_redirect_to_internal(self):
        handler = bot.SafeRedirectHandler()
        req = unittest.mock.MagicMock()
        req.full_url = "http://127.0.0.1/landing"
        with self.assertRaises(bot.urllib.error.HTTPError):
            handler.redirect_request(req, None, 302, "Found", {}, "/secret")


if __name__ == "__main__":
    unittest.main()
