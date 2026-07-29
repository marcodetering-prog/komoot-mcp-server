"""Tests for Geocoder (Photon API wrapper)."""

import json
from unittest.mock import MagicMock, patch

from komoot_mcp.geocoder import Geocoder


class TestGeocoder:
    def test_forward_parses_response(self):
        geo = Geocoder()
        mock_response = {
            "features": [
                {
                    "geometry": {"coordinates": [13.404954, 52.520008]},
                    "properties": {
                        "name": "Berlin",
                        "city": "Berlin",
                        "country": "Germany",
                        "type": "city",
                        "osm_id": 12345,
                    },
                }
            ]
        }
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_response).encode()
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            results = geo.forward("Berlin")
            assert len(results) == 1
            assert results[0]["display_name"] == "Berlin"
            assert results[0]["lat"] == 52.520008
            assert results[0]["lon"] == 13.404954

    def test_forward_empty_results(self):
        geo = Geocoder()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"features":[]}'
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            assert geo.forward("xyznonexistent") == []

    def test_reverse_returns_fallback_on_empty(self):
        geo = Geocoder()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"features":[]}'
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            result = geo.reverse(0.0, 0.0)
            assert result["display_name"] == "Unknown location"
