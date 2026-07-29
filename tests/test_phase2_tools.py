"""Tests for the four phase-2 tools added in feat/phase2-...

The new client methods (``get_tour_full``, ``get_highlight``,
``get_tour_weather``, ``discover_near``) all issue a GET with Basic auth
pulled off the kompy-built ``api.authentication``. Those now go through the
per-client Session, so we patch ``requests.Session.get``. Patching the
unbound method with a ``MagicMock`` injects no ``self``, which keeps the
fakes' signatures unchanged.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from komoot_mcp.auth import AuthManager
from komoot_mcp.client import KomootAPIError, KomootClient


class _NoLimit:
    async def acquire(self):
        return None


@pytest.fixture
def client():
    am = AuthManager(email="t@x.com", password="pw")
    c = KomootClient(am, _NoLimit())
    # Pre-seed ``_api`` so ``_basic_auth`` doesn't try to construct a
    # real kompy connector. Mirrors the pattern used by
    # ``test_client_tour_methods._install_api_stub``.
    api = MagicMock()
    auth = MagicMock()
    auth.get_username.return_value = "12345"
    auth.get_password.return_value = "long-lived-token"
    api.authentication = auth
    c._api = api
    return c


def _resp(status=200, json_body=None, text=""):
    """Build a fake ``requests.Response``-shaped object."""
    r = SimpleNamespace()
    r.status_code = status
    r.text = text

    def _json():
        if json_body is None:
            raise ValueError("no json")
        return json_body

    r.json = _json
    return r


# ---------------------------------------------------------------- get_tour_full

class TestGetTourFull:
    @pytest.mark.asyncio
    async def test_hits_embed_url_with_all_embeds(self, client):
        captured = {}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _resp(200, {
                "id": 42,
                "name": "Feldberg loop",
                "sport": "hike",
                "_embedded": {
                    "coordinates": {"items": [{}] * 2081},
                    "way_types": {"items": [{"type": "trail", "amount": 0.7}]},
                    "surfaces": {"items": [{"type": "gravel", "amount": 0.6}]},
                    "directions": {"items": [{}] * 12},
                    "timeline": {"items": []},
                },
            })

        with patch("requests.Session.get", side_effect=fake_get):
            out = await client.get_tour_full(42)

        assert captured["url"] == "https://api.komoot.de/v007/tours/42"
        params = captured["kwargs"]["params"]
        # All the embeds the planner spec called for
        assert "coordinates" in params["_embedded"]
        assert "way_types" in params["_embedded"]
        assert "surfaces" in params["_embedded"]
        assert "directions" in params["_embedded"]
        assert "timeline" in params["_embedded"]
        assert "cover_images" in params["_embedded"]
        assert params["directions"] == "v2"
        # Basic auth threaded through from kompy.authentication
        assert captured["kwargs"]["auth"] == ("12345", "long-lived-token")
        assert out["name"] == "Feldberg loop"

    @pytest.mark.asyncio
    async def test_404_raises_friendly_error(self, client):
        with patch(
            "requests.Session.get",
            return_value=_resp(404, text="not found"),
        ):
            with pytest.raises(KomootAPIError, match="not found"):
                await client.get_tour_full(999)


# ---------------------------------------------------------------- get_highlight

class TestGetHighlight:
    @pytest.mark.asyncio
    async def test_metadata_only_hits_one_url(self, client):
        urls = []

        def fake_get(url, **kwargs):
            urls.append(url)
            return _resp(200, {"id": 98160, "name": "Feldberg Summit"})

        with patch("requests.Session.get", side_effect=fake_get):
            out = await client.get_highlight(98160)

        assert urls == ["https://api.komoot.de/v007/highlights/98160"]
        assert out["metadata"]["name"] == "Feldberg Summit"
        assert "tips" not in out
        assert "recommenders" not in out

    @pytest.mark.asyncio
    async def test_with_tips_and_recommenders_hits_three_urls(self, client):
        urls = []

        def fake_get(url, **kwargs):
            urls.append(url)
            if url.endswith("/tips/"):
                return _resp(200, {"items": [{"text": "Great view!"}]})
            if url.endswith("/recommenders/"):
                return _resp(200, {"items": [{"id": "u1"}, {"id": "u2"}]})
            return _resp(200, {"id": 98160, "name": "Feldberg Summit"})

        with patch("requests.Session.get", side_effect=fake_get):
            out = await client.get_highlight(
                98160, with_tips=True, with_recommenders=True,
            )

        assert len(urls) == 3
        assert any(u.endswith("/tips/") for u in urls)
        assert any(u.endswith("/recommenders/") for u in urls)
        assert out["tips"]["items"][0]["text"] == "Great view!"
        assert len(out["recommenders"]["items"]) == 2


# ---------------------------------------------------------------- get_tour_weather

class TestGetTourWeather:
    @pytest.mark.asyncio
    async def test_hits_weather_service_host(self, client):
        captured = {}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            return _resp(200, {"forecast": [
                {"time": "2026-05-18T08:00", "temperature": 12, "condition": "clear"},
            ]})

        with patch("requests.Session.get", side_effect=fake_get):
            out = await client.get_tour_weather(42)

        assert (
            captured["url"]
            == "https://weather-along-tour-api.komoot.de/v1/weather"
        )
        assert captured["params"] == {"tour_id": 42}
        assert out["forecast"][0]["temperature"] == 12

    @pytest.mark.asyncio
    async def test_tool_surfaces_error_on_bad_request(self, client):
        """Tool layer wraps any client error into a friendly message
        so the LLM never sees a raw stack trace."""
        registered = {}

        class _Mcp:
            def tool(self):
                def decorator(fn):
                    registered[fn.__name__] = fn
                    return fn
                return decorator

        from komoot_mcp.context import (
            clear_request_state,
            reset_auth_manager,
            set_auth_manager,
        )
        from komoot_mcp.tools import browse_tools

        browse_tools.register(_Mcp())

        am = AuthManager(email="t@x.com", password="pw")
        # Pre-seed kompy auth so the tool's client doesn't try to log in.
        clear_request_state()
        token = set_auth_manager(am)
        try:
            from komoot_mcp.context import get_client
            c = get_client()
            api = MagicMock()
            auth = MagicMock()
            auth.get_username.return_value = "12345"
            auth.get_password.return_value = "tok"
            api.authentication = auth
            c._api = api

            with patch(
                "requests.Session.get",
                return_value=_resp(400, text="bad request"),
            ):
                out = await registered["komoot_tour_weather"](42)
            assert "Error getting weather" in out
            # Hint to the maintainer is included
            assert "best-guess" in out
        finally:
            reset_auth_manager(token)
            clear_request_state()


# ---------------------------------------------------------------- discover_near

class TestDiscoverNear:
    @pytest.mark.asyncio
    async def test_builds_lat_lng_path_segment(self, client):
        captured = {}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            return _resp(200, {"_embedded": {"items": [
                {"type": "tour", "name": "Sample", "sport": "hike", "id": 1},
            ]}})

        with patch("requests.Session.get", side_effect=fake_get):
            out = await client.discover_near(
                lat=47.87, lng=8.0, sport="hike", limit=5,
            )

        assert (
            captured["url"]
            == "https://api.komoot.de/v007/discover/47.87,8.0/elements/"
        )
        assert captured["params"]["_embedded"] == "main_tour,summary"
        assert captured["params"]["sport"] == "hike"
        assert captured["params"]["limit"] == 5
        assert out["_embedded"]["items"][0]["name"] == "Sample"

    @pytest.mark.asyncio
    async def test_omits_sport_when_none(self, client):
        captured = {}

        def fake_get(url, **kwargs):
            captured["params"] = kwargs.get("params")
            return _resp(200, {"_embedded": {"items": []}})

        with patch("requests.Session.get", side_effect=fake_get):
            await client.discover_near(lat=50, lng=10)

        assert "sport" not in captured["params"]


# ---------------------------------------------------------------- tool renderers

@pytest.fixture(autouse=True)
def _reset_request_state():
    from komoot_mcp.context import clear_request_state
    clear_request_state()
    yield
    clear_request_state()


def _register_tools(modname):
    """Helper: register a tools module against a recorder ``_Mcp``."""
    registered = {}

    class _Mcp:
        def tool(self):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

    mod = __import__(f"komoot_mcp.tools.{modname}", fromlist=[modname])
    mod.register(_Mcp())
    return registered


def _install_client_with_fake_kompy_auth():
    """Shared setup: pin a client whose ``_basic_auth`` works without
    talking to kompy. Returns the AuthManager token so the test can
    reset it."""
    from komoot_mcp.context import get_client, set_auth_manager

    am = AuthManager(email="t@x.com", password="pw")
    token = set_auth_manager(am)
    c = get_client()
    api = MagicMock()
    auth = MagicMock()
    auth.get_username.return_value = "12345"
    auth.get_password.return_value = "tok"
    api.authentication = auth
    c._api = api
    return token


class TestToolRenderers:
    @pytest.mark.asyncio
    async def test_tour_full_renders_summary(self):
        from komoot_mcp.context import reset_auth_manager
        registered = _register_tools("browse_tools")
        token = _install_client_with_fake_kompy_auth()
        try:
            body = {
                "id": 42,
                "name": "Feldberg loop",
                "sport": "hike",
                "status": "private",
                "distance": 12345,
                "duration": 3600,
                "elevation_up": 800,
                "elevation_down": 800,
                "difficulty": {"grade": "moderate"},
                "start_point": {"lat": 47.87, "lng": 8.0, "alt": 1493},
                "_embedded": {
                    "coordinates": {"items": [{}] * 2081},
                    "way_types": {
                        "items": [
                            {"type": "trail", "amount": 0.6},
                            {"type": "road", "amount": 0.4},
                        ]
                    },
                    "surfaces": {
                        "items": [{"type": "gravel", "amount": 1.0}]
                    },
                    "directions": {"items": [{}] * 12},
                    "timeline": {"items": [
                        {"_embedded": {"reference": {"id": 98160, "name": "Feldberg Summit"}}}
                    ]},
                    "cover_images": {"items": [{}]},
                },
            }
            with patch(
                "requests.Session.get", return_value=_resp(200, body),
            ):
                out = await registered["komoot_get_tour_full"](42)
            assert "Feldberg loop" in out
            assert "2081 points" in out
            assert "Way types" in out
            assert "trail 60%" in out
            assert "Surfaces" in out
            assert "12 steps" in out
            assert "highlight 98160" in out
        finally:
            reset_auth_manager(token)

    @pytest.mark.asyncio
    async def test_highlight_renders_metadata_only(self):
        from komoot_mcp.context import reset_auth_manager
        registered = _register_tools("browse_tools")
        token = _install_client_with_fake_kompy_auth()
        try:
            body = {
                "id": 98160,
                "name": "Feldberg Summit",
                "category": "summit",
                "sports": "hike",
                "score": 95,
                "location": {"lat": 47.87, "lng": 8.0},
            }
            with patch(
                "requests.Session.get", return_value=_resp(200, body),
            ):
                out = await registered["komoot_get_highlight"](98160)
            assert "Highlight 98160" in out
            assert "Feldberg Summit" in out
            assert "summit" in out
            assert "Tips" not in out
        finally:
            reset_auth_manager(token)

    @pytest.mark.asyncio
    async def test_discover_renders_items(self):
        from komoot_mcp.context import reset_auth_manager
        registered = _register_tools("discover_tools")
        token = _install_client_with_fake_kompy_auth()
        try:
            body = {"_embedded": {"items": [
                {
                    "type": "tour",
                    "name": "Black Forest loop",
                    "sport": "hike",
                    "distance": 12000,
                    "id": 777,
                },
                {
                    "type": "collection",
                    "name": "Best of Feldberg",
                    "_embedded": {
                        "main_tour": {
                            "id": 888,
                            "name": "Feldberg summit",
                            "sport": "hike",
                            "distance": 9000,
                        }
                    },
                },
            ]}}
            with patch(
                "requests.Session.get", return_value=_resp(200, body),
            ):
                out = await registered["komoot_recommend_tours_near"](
                    lat=47.87, lng=8.0, sport="hike", limit=5,
                )
            assert "Discovered 2 items" in out
            assert "Black Forest loop" in out
            assert "Best of Feldberg" in out
            assert "12.0 km" in out
            assert "https://www.komoot.com/tour/777" in out
        finally:
            reset_auth_manager(token)
