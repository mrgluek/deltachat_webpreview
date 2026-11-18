"""Tests for Google Maps link previews (coordinates parsing, geocoding fallback, pseudo image URL)."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot


class TestGoogleMaps(unittest.TestCase):
    def test_is_google_maps_url(self):
        for u in (
            "https://maps.app.goo.gl/abc123",
            "https://goo.gl/maps/abc",
            "https://www.google.com/maps/place/Foo",
            "https://www.google.de/maps?q=1,2",
            "https://maps.google.com/?q=1,2",
        ):
            self.assertTrue(bot._is_google_maps_url(u), u)
        for u in ("https://www.google.com/search?q=maps", "https://example.com/maps", "https://goo.gl/abc"):
            self.assertFalse(bot._is_google_maps_url(u), u)

    def test_parse_coordinates_and_zoom(self):
        lat, lon, zoom, name = bot._parse_google_maps_url(
            "https://www.google.com/maps/place/Foo+Bar/@51.5,-0.1,17z/data=!3d51.51!4d-0.12"
        )
        self.assertEqual((lat, lon, zoom, name), (51.51, -0.12, 17, "Foo Bar"))

    def test_parse_query_coordinates(self):
        lat, lon, zoom, _ = bot._parse_google_maps_url("https://maps.google.com/?q=48.1,11.5&z=12")
        self.assertEqual((lat, lon, zoom), (48.1, 11.5, 12))

    def test_parse_name_only(self):
        lat, lon, _, name = bot._parse_google_maps_url("https://www.google.com/maps/place/Foo,+8+Bar+St,+London/data=!4m2")
        self.assertIsNone(lat)
        self.assertEqual(name, "Foo, 8 Bar St, London")

    def test_out_of_range_coordinates_dropped(self):
        lat, lon, _, _ = bot._parse_google_maps_url("https://maps.google.com/?q=95.0,11.5")
        self.assertIsNone(lat)

    @patch.object(bot, "_geocode_place", return_value=(51.5, 0.01))
    def test_fetch_uses_geocoder_for_names(self, _geo):
        resp = MagicMock()
        resp.geturl.return_value = "https://www.google.com/maps/place/Foo,+London/data=!4m2"
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=resp)
        cm.__exit__ = MagicMock(return_value=False)
        with patch.object(bot, "_urlopen", return_value=cm):
            title, img, md = bot._fetch_google_maps_og_data("https://maps.app.goo.gl/x")
        self.assertEqual(title, "Google Maps: Foo, London")
        self.assertEqual(img, "osmmap:51.500000,0.010000,15")


if __name__ == "__main__":
    unittest.main()
