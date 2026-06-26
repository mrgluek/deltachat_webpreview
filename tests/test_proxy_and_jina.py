"""
Tests for proxy and Jina AI Reader integration:
  - _should_use_proxy
  - _urlopen proxy routing (direct / PROXY_URL / JINA_PROXY_URL)
  - _fetch_from_jina headers (Authorization, X-Proxy-Url)
  - _download_cached_image SVG skip logic
  - _check_url_headers octet-stream allow/reject
  - _save_jina_preview_to_cache
  - _get_og_preview_data Jina fallback
  - _do_preview skips network pre-checks on fresh cache hit
"""
import os
import sys
import time
import unittest
import urllib.request
from unittest.mock import MagicMock, patch

# Set env vars BEFORE importing bot
os.environ["JINA_API_KEY"] = "testjina123"
os.environ["PROXY_URL"] = "http://127.0.0.1:8080"
os.environ["PROXY_DOMAINS"] = ".ru, blocked.com"
os.environ["JINA_PROXY_URL"] = ""  # start with no Jina proxy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot

# Override module-level constants that were already evaluated at import time
# (they may have been set by a previous test module in the same process)
bot.JINA_API_KEY = "testjina123"
bot.PROXY_URL = "http://127.0.0.1:8080"
bot.PROXY_DOMAINS = [".ru", "blocked.com"]
bot.JINA_PROXY_URL = ""


class TestShouldUseProxy(unittest.TestCase):
    """Tests for _should_use_proxy."""

    def test_matching_ru_domain(self):
        self.assertTrue(bot._should_use_proxy("https://yandex.ru/"))

    def test_matching_subdomain_of_ru(self):
        self.assertTrue(bot._should_use_proxy("http://some.rbc.ru/news"))

    def test_matching_explicit_domain(self):
        self.assertTrue(bot._should_use_proxy("http://blocked.com/path"))

    def test_non_matching_domain(self):
        self.assertFalse(bot._should_use_proxy("https://google.com/"))

    def test_jina_reader_url_not_proxied(self):
        # Jina reader URLs should never go through general PROXY_URL
        self.assertFalse(bot._should_use_proxy("https://r.jina.ai/https://yandex.ru/"))
        self.assertFalse(bot._should_use_proxy("https://r.jina.ai/http://blocked.com/path"))


class TestUrlopen(unittest.TestCase):
    """Tests for _urlopen proxy routing logic."""

    @patch("urllib.request.urlopen")
    def test_direct_for_non_proxy_domain(self, mock_urlopen):
        bot._urlopen("https://google.com")
        mock_urlopen.assert_called_once_with("https://google.com", timeout=None)

    @patch("urllib.request.build_opener")
    @patch("urllib.request.urlopen")
    def test_proxied_for_proxy_domain(self, mock_urlopen, mock_build_opener):
        mock_opener = MagicMock()
        mock_build_opener.return_value = mock_opener

        bot._urlopen("https://yandex.ru", timeout=10)

        mock_build_opener.assert_called_once()
        mock_opener.open.assert_called_once_with("https://yandex.ru", timeout=10)
        mock_urlopen.assert_not_called()

    @patch("urllib.request.build_opener")
    @patch("urllib.request.urlopen")
    def test_jina_direct_when_no_jina_proxy(self, mock_urlopen, mock_build_opener):
        bot.JINA_PROXY_URL = ""
        bot._urlopen("https://r.jina.ai/https://yandex.ru", timeout=15)
        mock_urlopen.assert_called_once_with(
            "https://r.jina.ai/https://yandex.ru", timeout=15
        )
        mock_build_opener.assert_not_called()

    @patch("urllib.request.build_opener")
    @patch("urllib.request.urlopen")
    def test_jina_via_dedicated_proxy(self, mock_urlopen, mock_build_opener):
        mock_opener = MagicMock()
        mock_build_opener.return_value = mock_opener
        bot.JINA_PROXY_URL = "http://127.0.0.1:9000"
        try:
            bot._urlopen("https://r.jina.ai/https://yandex.ru", timeout=15)

            mock_build_opener.assert_called_once()
            handler = mock_build_opener.call_args[0][0]
            self.assertIsInstance(handler, urllib.request.ProxyHandler)
            self.assertEqual(
                handler.proxies,
                {
                    "http": "http://127.0.0.1:9000",
                    "https": "http://127.0.0.1:9000",
                },
            )
            mock_opener.open.assert_called_once_with(
                "https://r.jina.ai/https://yandex.ru", timeout=15
            )
            mock_urlopen.assert_not_called()
        finally:
            bot.JINA_PROXY_URL = ""


class TestFetchFromJina(unittest.TestCase):
    """Tests for _fetch_from_jina request headers."""

    @patch("bot._urlopen")
    def test_auth_header_added(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b"Title: Test\nMarkdown Content: test"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        bot._fetch_from_jina("https://example.com")

        req = mock_urlopen.call_args[0][0]
        self.assertIsInstance(req, urllib.request.Request)
        self.assertEqual(req.get_header("Authorization"), "Bearer testjina123")

    @patch("bot._urlopen")
    def test_x_proxy_url_header_for_proxy_domain(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b"Title: Test\nMarkdown Content: test"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        bot._fetch_from_jina("https://yandex.ru/news")

        req = mock_urlopen.call_args[0][0]
        self.assertIsInstance(req, urllib.request.Request)
        self.assertEqual(req.get_header("X-proxy-url"), "http://127.0.0.1:8080")


class TestDownloadCachedImage(unittest.TestCase):
    """Tests for SVG skip logic in _download_cached_image."""

    def test_skips_svg_url_by_extension(self):
        self.assertIsNone(
            bot._download_cached_image("https://example.com/logo.svg", "testsvg")
        )

    def test_skips_svg_url_with_query_params(self):
        self.assertIsNone(
            bot._download_cached_image("https://example.com/logo.svg?v=123", "testsvg")
        )

    @patch("bot._urlopen")
    def test_skips_svg_by_content_type(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.headers = {"Content-Type": "image/svg+xml"}
        mock_response.read.return_value = b"<svg>...</svg>"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        self.assertIsNone(
            bot._download_cached_image("https://example.com/logo", "testsvg")
        )


class TestCheckUrlHeaders(unittest.TestCase):
    """Tests for _check_url_headers octet-stream allow/reject logic."""

    @patch("bot._urlopen")
    def test_allows_octet_stream_for_webpage_url(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.headers = {"Content-Type": "application/octet-stream"}
        mock_urlopen.return_value.__enter__.return_value = mock_response

        err_type, val, ctype = bot._check_url_headers(
            "https://www.rbc.ru/rbcfreenews/6a3d65df9a79476e9ceec22b"
        )
        self.assertIsNone(err_type)
        self.assertEqual(ctype, "application/octet-stream")

    @patch("bot._urlopen")
    def test_rejects_octet_stream_for_binary_extension(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.headers = {"Content-Type": "application/octet-stream"}
        mock_urlopen.return_value.__enter__.return_value = mock_response

        err_type, val, ctype = bot._check_url_headers(
            "https://example.com/files/archive.zip"
        )
        self.assertEqual(err_type, "BINARY_TYPE")


class TestSaveJinaPreviewToCache(unittest.TestCase):
    """Tests for _save_jina_preview_to_cache."""

    @patch("database.add_cached_preview")
    def test_saves_to_database(self, mock_add):
        with (
            unittest.mock.patch("builtins.open", unittest.mock.mock_open()) as mock_file,
            unittest.mock.patch("os.path.getsize", return_value=1234),
        ):
            bot._save_jina_preview_to_cache(
                "https://example.com/page",
                "testcachehash",
                "My Test Title",
                "# Header 1\nThis is a *test* article.",
            )
            mock_file.assert_called_once()
            mock_add.assert_called_once()
            self.assertEqual(mock_add.call_args[0][0], "testcachehash_readability")
            self.assertEqual(mock_add.call_args[0][2], "My Test Title")


class TestInlineSoupImages(unittest.TestCase):
    """Tests for _inline_soup_images."""

    @patch("bot._download_image_bytes")
    def test_inlines_and_removes_srcset(self, mock_download):
        from bs4 import BeautifulSoup
        mock_download.return_value = b"fake image bytes"
        
        html = '<img src="https://example.com/img.png" srcset="https://example.com/img.png 2x" sizes="100vw" data-src="https://example.com/img.png" />'
        soup = BeautifulSoup(html, "html.parser")
        
        bot._inline_soup_images(soup, "https://example.com")
        
        img = soup.find("img")
        self.assertIsNotNone(img)
        self.assertTrue(img["src"].startswith("data:image/"))
        self.assertNotIn("srcset", img.attrs)
        self.assertNotIn("sizes", img.attrs)
        self.assertNotIn("data-src", img.attrs)

    @patch("bot._download_image_bytes")
    def test_decomposes_on_failure(self, mock_download):
        from bs4 import BeautifulSoup
        mock_download.return_value = None
        
        html = '<img src="https://example.com/img.png" />'
        soup = BeautifulSoup(html, "html.parser")
        
        bot._inline_soup_images(soup, "https://example.com")
        
        self.assertIsNone(soup.find("img"))


class TestGetOgPreviewData(unittest.TestCase):
    """Tests for _get_og_preview_data Jina fallback."""

    @patch("bot._fetch_from_jina")
    @patch("bot._urlopen")
    def test_falls_back_to_jina_on_standard_error(
        self, mock_urlopen, mock_fetch_from_jina
    ):
        mock_fetch_from_jina.return_value = ("Jina Title", None, "Jina MD", None)
        mock_urlopen.side_effect = Exception("Standard Blocked")

        title, image_url, is_invidious, warning, jina_markdown = (
            bot._get_og_preview_data("https://example.com/article")
        )

        self.assertEqual(title, "Jina Title")
        self.assertEqual(jina_markdown, "Jina MD")
        self.assertIsNone(image_url)


class TestDoPreviewCacheOptimization(unittest.TestCase):
    """Tests that _do_preview skips network pre-checks when og_cache is fresh."""

    @patch("database.get_cached_og")
    @patch("database.get_cached_preview")
    @patch("bot._detect_and_get_file_info")
    @patch("bot._check_url_headers")
    @patch("bot._send")
    @patch("bot._react")
    @patch("bot._generate_readability_preview")
    def test_skips_network_on_fresh_cache(
        self,
        mock_gen,
        mock_react,
        mock_send,
        mock_check_headers,
        mock_detect_file,
        mock_get_cached_preview,
        mock_get_cached_og,
    ):
        mock_get_cached_og.return_value = {
            "title": "Fresh Cached Webpage",
            "image_path": None,
            "warning": None,
            "jina_markdown": None,
            "created_at": time.time(),
        }
        mock_get_cached_preview.return_value = None

        def side_effect(url, out_path):
            with open(out_path, "w") as f:
                f.write("<html></html>")
            return True, "Generated Title"

        mock_gen.side_effect = side_effect

        bot._do_preview(
            MagicMock(), 1, 69, 100, 200, "https://google.com/fresh-webpage", "readability"
        )

        # Network pre-checks MUST NOT be called
        mock_detect_file.assert_not_called()
        mock_check_headers.assert_not_called()
        # Readability generation should have been called
        mock_gen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
