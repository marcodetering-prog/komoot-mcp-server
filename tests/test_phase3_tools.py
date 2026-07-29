"""Phase 3 tests — 20 new tools that bypass kompy via direct REST.

Each tool wraps a ``client.py`` method that hits Komoot directly with
``requests``. We monkeypatch ``requests.request`` (the single
underlying call all ``_http_request`` helpers route through) so a
single tiny fixture covers GET/POST/PATCH/DELETE.

Tests exercise the *tool* layer (not just the client) because that's
where the LLM-friendly rendering lives — and where docstring contracts
("returns a bullet list", "logs the URL on 404") become user-visible.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import pytest

from komoot_mcp.auth import AuthManager
from komoot_mcp.context import (
    clear_request_state,
    reset_auth_manager,
    set_auth_manager,
)


@pytest.fixture(autouse=True)
def _reset_context():
    clear_request_state()
    yield
    clear_request_state()


class _NoLimit:
    async def acquire(self):
        return None


def _make_response(*, status=200, json_body=None, text="ok"):
    """Mimic the requests.Response surface our helpers care about."""
    resolved_text = text if json_body is None else "{}"
    is_ok = 200 <= status < 300
    body = json_body

    class _Resp:
        status_code = status
        ok = is_ok
        headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}
        text = resolved_text

        def json(self):
            if body is None:
                raise ValueError("no json")
            return body

    return _Resp()


@pytest.fixture
def registered_tools():
    """Register all Phase 3 tools onto a recorder MCP and return the map."""
    registry: dict[str, callable] = {}

    class _Mcp:
        def tool(self):
            def decorator(fn):
                registry[fn.__name__] = fn
                return fn
            return decorator

    from komoot_mcp.tools import (
        browse_tools,
        collection_tools,
        discover_tools,
        highlight_tools,
        share_tools,
        write_tools,
    )
    browse_tools.register(_Mcp())
    write_tools.register(_Mcp())
    highlight_tools.register(_Mcp())
    discover_tools.register(_Mcp())
    collection_tools.register(_Mcp())
    share_tools.register(_Mcp())
    return registry


@pytest.fixture
def auth_token():
    am = AuthManager(email="t@x.com", password="pw")
    am.user_id = "123"
    am.token = "tok"
    token = set_auth_manager(am)
    yield token
    reset_auth_manager(token)


def _patch_request(json_body=None, text="ok", status=200):
    """Patch ``requests.request`` to return a canned response.

    All Phase 3 helpers go through ``requests.request``, so this single
    patch covers GET / POST / PATCH / DELETE.
    """
    resp = _make_response(status=status, json_body=json_body, text=text)
    return patch("komoot_mcp.client.requests.request", return_value=resp)


def _stub_kompy_auth(client):
    """Skip kompy login by injecting a stub authenticated connector."""
    client._api = SimpleNamespace(
        authentication=SimpleNamespace(
            get_username=lambda: "user123",
            get_password=lambda: "tokenABC",
        )
    )


# ----------------------------------------------------------------
# Tour metadata tools (5)
# ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_tour_photos(registered_tools, auth_token):
    body = {
        "_embedded": {"items": [
            {"id": 1, "src": "https://img.komoot.de/x/{width}/{height}/{crop}",
             "rating": 4.7},
            {"id": 2, "src": "https://img.komoot.de/y/{width}/{height}/{crop}"},
        ]}
    }
    with _patch_request(json_body=body):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_get_tour_photos"](tour_id=42)
    assert "Tour 42 photos" in out
    assert "[1]" in out
    assert "{width}" not in out  # templated placeholder must be resolved
    assert "/800/" in out
    assert "rating=4.7" in out


@pytest.mark.asyncio
async def test_get_tour_line(registered_tools, auth_token):
    # Live shape (live-probed 2026-05-18): the response uses
    # ``geometry`` (not ``coordinates``).
    body = {
        "tour_id": 42,
        "geometry": [
            {"lat": 47.1, "lng": 11.4, "alt": 600},
            {"lat": 47.2, "lng": 11.5, "alt": 650},
        ],
    }
    with _patch_request(json_body=body) as mock_req:
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_get_tour_line"](tour_id=42)
    # Confirm we hit the corrected URL on www.komoot.com (not
    # api.komoot.de/.../line, which 404s).
    called_url = mock_req.call_args.kwargs.get("url")
    assert "www.komoot.com/api/v007/tours/42/tour_line" in called_url
    assert "Tour 42 line" in out
    assert "2 points" in out
    assert "lat=47.1" in out


@pytest.mark.asyncio
async def test_create_share_link(registered_tools, auth_token):
    body = {"token": "abc123"}
    with _patch_request(json_body=body):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_create_share_link"](tour_id=42)
    assert "Token: abc123" in out
    assert "share_token=abc123" in out
    assert "/tour/42" in out


@pytest.mark.asyncio
async def test_revoke_share_link(registered_tools, auth_token):
    # DELETE often returns 204 No Content — patch the response shape.
    resp = _make_response(status=204, text="")
    with patch("komoot_mcp.client.requests.request", return_value=resp):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_revoke_share_link"](tour_id=42)
    assert "revoked" in out.lower()


@pytest.mark.asyncio
async def test_modify_tour_extended(registered_tools, auth_token):
    body = {"id": 42, "name": "new", "description": "d"}
    with _patch_request(json_body=body):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_modify_tour_extended"](
            tour_id=42, name="new", description="d",
        )
    assert "updated" in out
    assert "name" in out
    assert "description" in out


# ----------------------------------------------------------------
# Highlight tools (3)
# ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_highlight_images(registered_tools, auth_token):
    body = {"_embedded": {"items": [
        {"id": 11, "src": "https://img.komoot.de/h/{width}/{height}/{crop}",
         "attribution": "Anna"},
    ]}}
    with _patch_request(json_body=body):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_get_highlight_images"](
            highlight_id=99,
        )
    assert "Highlight 99 images" in out
    assert "[11]" in out
    assert "by Anna" in out


@pytest.mark.asyncio
async def test_get_highlight_tips(registered_tools, auth_token):
    body = {"_embedded": {"items": [
        {"id": 5, "text": "Bring water!",
         "creator": {"display_name": "Bob"}},
    ]}}
    with _patch_request(json_body=body):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_get_highlight_tips"](
            highlight_id=99,
        )
    assert "tips" in out.lower()
    assert "Bob: Bring water!" in out


@pytest.mark.asyncio
async def test_list_user_highlights(registered_tools, auth_token):
    body = {"_embedded": {"items": [
        {"id": 7, "name": "Marienplatz", "sports": "hike",
         "category": "viewpoint"},
    ]}}
    with _patch_request(json_body=body):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_list_user_highlights"](
            user_id="123",
        )
    assert "Marienplatz" in out
    assert "[7]" in out


# ----------------------------------------------------------------
# Discover / smart tour tools (5)
# ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_smart_tours_near(registered_tools, auth_token):
    body = {"_embedded": {"items": [
        {"id": 100, "name": "Alps Loop", "sport": "mountainbike",
         "distance": 25000, "elevation_up": 1200},
    ]}}
    with _patch_request(json_body=body) as mock_req:
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_smart_tours_near"](
            lat=47.0, lng=11.0, sport="mountainbike",
        )
    # Confirm we hit the corrected from_location URL.
    called_url = mock_req.call_args.kwargs.get("url")
    assert "discover_tours/from_location" in called_url
    params = mock_req.call_args.kwargs.get("params") or {}
    assert params.get("sport") == "mountainbike"
    assert params.get("max_distance") == 20000
    assert "Smart Tours" in out
    assert "Alps Loop" in out
    assert "25.0 km" in out


@pytest.mark.asyncio
async def test_smart_tour_for_highlight(registered_tools, auth_token):
    body = {"_embedded": {"items": [
        {"id": 200, "name": "Round trip", "sport": "hike",
         "distance": 8000},
    ]}}
    with _patch_request(json_body=body) as mock_req:
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_smart_tour_for_highlight"](
            highlight_id=99, lat=47.0, lng=11.0, sport="hike",
        )
    # Confirm we hit the corrected from_location URL with
    # highlight_id as a query param (no for_highlight path exists).
    called_url = mock_req.call_args.kwargs.get("url")
    assert "discover_tours/from_location" in called_url
    params = mock_req.call_args.kwargs.get("params") or {}
    assert params.get("highlight_id") == 99
    assert "Round trip" in out
    assert "Suggested tours for highlight 99" in out


@pytest.mark.asyncio
async def test_discover_with_attributes(registered_tools, auth_token):
    body = {"_embedded": {"items": [
        {"id": 400, "name": "Scenic ridge", "sport": "hike",
         "distance": 12000},
    ]}}
    with _patch_request(json_body=body):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_discover_with_attributes"](
            lat=47.0, lng=11.0, attributes="scenic",
        )
    assert "Scenic ridge" in out
    assert "scenic" in out


@pytest.mark.asyncio
async def test_route_attribute_options(registered_tools, auth_token):
    # Live shape (live-probed 2026-05-18): plain {"route_attributes":
    # [...]} — and ALL four query params are required.
    body = {"route_attributes": ["waterfalls", "lakes_rivers", "cafe"]}
    with _patch_request(json_body=body) as mock_req:
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_route_attribute_options"](
            lat=47.9959, lng=7.8522, sport="mountainbike",
        )
    params = mock_req.call_args.kwargs.get("params") or {}
    # All four required by the Komoot endpoint — missing any returns 400.
    assert params.get("lat") == 47.9959
    assert params.get("lng") == 7.8522
    assert params.get("sport") == "mountainbike"
    assert params.get("max_distance") == 20000
    assert "waterfalls" in out
    assert "lakes_rivers" in out


# ----------------------------------------------------------------
# Collection tools (3)
# ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_collection(registered_tools, auth_token):
    body = {
        "id": 500, "name": "Best gravel rides", "sport": "touringbicycle",
        "number_of_tours": 12,
        "creator": {"display_name": "Lena"},
        "description": "Curated mix of rolling backroads",
    }
    with _patch_request(json_body=body):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_get_collection"](
            collection_id=500,
        )
    assert "Best gravel rides" in out
    assert "Lena" in out
    assert "Tours: 12" in out


@pytest.mark.asyncio
async def test_get_collection_tours(registered_tools, auth_token):
    body = {"_embedded": {"items": [
        {"_embedded": {"tour": {
            "id": 11, "name": "Saturday loop", "sport": "racebike",
            "distance": 60000,
        }}},
    ]}}
    with _patch_request(json_body=body):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_get_collection_tours"](
            collection_id=500,
        )
    assert "Saturday loop" in out
    assert "60.0 km" in out


# ----------------------------------------------------------------
# Resolvers (1)
# ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_share_url(registered_tools, auth_token):
    body = {
        "id": 12345, "name": "Shared loop", "sport": "hike",
        "distance": 9500, "elevation_up": 300,
        "status": "public", "date": "2026-05-01T10:00:00Z",
    }
    with _patch_request(json_body=body):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_resolve_share_url"](
            share_url="https://www.komoot.com/tour/12345?share_token=abc",
        )
    assert "tour 12345" in out
    assert "Shared loop" in out
    assert "Share token: abc" in out


# ----------------------------------------------------------------
# Error path + client unit checks
# ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_tools_render_errors_not_exceptions(
    registered_tools, auth_token,
):
    """If Komoot returns 500, the tool layer renders an error string."""
    resp = _make_response(status=500, text="boom")
    with patch("komoot_mcp.client.requests.request", return_value=resp):
        from komoot_mcp.context import get_client
        _stub_kompy_auth(get_client())
        out = await registered_tools["komoot_get_tour_line"](tour_id=42)
        assert "Error" in out
        out2 = await registered_tools["komoot_modify_tour_extended"](
            tour_id=42, name="x",
        )
        assert "Error" in out2


@pytest.mark.asyncio
async def test_client_modify_tour_extended_empty_body_raises(auth_token):
    """No fields → KomootAPIError so callers don't accidentally PATCH nothing."""
    from komoot_mcp.client import KomootAPIError
    from komoot_mcp.context import get_client
    client = get_client()
    _stub_kompy_auth(client)
    client.rl = _NoLimit()
    with pytest.raises(KomootAPIError):
        await client.modify_tour_extended(tour_id=42)
