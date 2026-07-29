"""Tool-layer regression tests for the three issues fixed in PR #8.

Covers:
  * Issue #9 — ``komoot_get_tour_gpx`` returns the GPX XML inline in a
    fenced code block rather than a server-side filesystem path.
  * Issue #10 — ``komoot_get_tour_way_types`` renders the dict shape
    produced by the client as ``<name>: <pct>%`` instead of raw object
    reprs.
  * Issue #11 — ``komoot_plan_route`` happy path returns the planned
    summary AND the GPX body inline; the ORS GPX-format request goes
    through the raw HTTP path (no more spurious "HTTP Error: 200").

The tests stand alone — they don't touch the real ``kompy`` connector
or ``openrouteservice`` HTTP API; everything is mocked via the kompy /
openrouteservice stubs installed by ``conftest.py`` plus per-test
``monkeypatch``.
"""
from __future__ import annotations

import pytest

from komoot_mcp.context import clear_request_state


@pytest.fixture(autouse=True)
def _reset():
    clear_request_state()
    yield
    clear_request_state()


def _build_tool_registry(module):
    registered: dict[str, callable] = {}

    class _Mcp:
        def tool(self):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

    module.register(_Mcp())
    return registered


class TestGetTourGpxToolReturnsInline:
    """Issue #9: GPX content must come back in the tool response."""

    @pytest.mark.asyncio
    async def test_renders_gpx_in_fenced_block(self, monkeypatch):
        from komoot_mcp.tools import data_tools
        registered = _build_tool_registry(data_tools)

        # Monkeypatch the client.get_tour_gpx to return a known string.
        gpx_payload = "<gpx><trk><name>x</name></trk></gpx>"

        from komoot_mcp import context as ctx_mod

        class _FakeClient:
            async def get_tour_gpx(self, tour_id):
                return gpx_payload

        monkeypatch.setattr(ctx_mod, "get_client", lambda: _FakeClient())
        # Tool module imports get_client at module load — patch there too.
        monkeypatch.setattr(data_tools, "get_client", lambda: _FakeClient())

        out = await registered["komoot_get_tour_gpx"](tour_id=42)
        # The response must contain the GPX content, a byte-count, and a
        # fenced XML code block — and must NOT report a server-side path.
        assert "<gpx>" in out
        assert "```xml" in out
        assert f"({len(gpx_payload)} bytes)" in out
        assert "/tmp/" not in out
        assert "saved to" not in out

    @pytest.mark.asyncio
    async def test_returns_large_gpx_in_full(self, monkeypatch):
        """Real-world planned routes are 300–500 KB. The tool must
        return the full content — no truncation, no cap. Regression
        test for the bug where PR #12's 200 KB cap silently chopped
        real routes in half.
        """
        from komoot_mcp.tools import data_tools

        registered = _build_tool_registry(data_tools)

        # Fabricate a >400 KB GPX body — well above the old 200 KB
        # cap and roughly the size of a typical 70 km planned route.
        body = "x" * 400_000
        gpx_payload = f"<gpx>{body}</gpx>"
        assert len(gpx_payload) > 300_000  # sanity check

        class _FakeClient:
            async def get_tour_gpx(self, tour_id):
                return gpx_payload

        monkeypatch.setattr(data_tools, "get_client", lambda: _FakeClient())

        out = await registered["komoot_get_tour_gpx"](tour_id=42)
        # No truncation language anywhere in the output.
        assert "truncated" not in out
        assert "omitted" not in out
        # The full byte count is reported.
        assert f"({len(gpx_payload)} bytes)" in out
        # The full body is present verbatim, fenced.
        assert "```xml" in out
        assert gpx_payload in out


class TestGetTourWayTypesRendering:
    """Issue #10: the tool must render dicts as ``name: pct%``."""

    @pytest.mark.asyncio
    async def test_renders_percentages(self, monkeypatch):
        from komoot_mcp.tools import data_tools
        registered = _build_tool_registry(data_tools)

        class _FakeClient:
            async def get_tour_way_types(self, tour_id):
                return [
                    {"way_type": "trail", "fraction": 0.42},
                    {"way_type": "road", "fraction": 0.58},
                ]

        monkeypatch.setattr(data_tools, "get_client", lambda: _FakeClient())

        out = await registered["komoot_get_tour_way_types"](tour_id=42)
        # The output must be human-readable, not a raw Python repr.
        assert "trail: 42.0%" in out
        assert "road: 58.0%" in out
        assert "Waypoint object at" not in out


class TestPlanRouteHappyPathReturnsGpxInline:
    """Issue #11 + Issue #9: ``komoot_plan_route`` must succeed end-to-end
    on the happy path AND embed the GPX inline (no server-side path).

    Reproduces the original bug shape: when the ORS GPX-format request
    was routed through ``client.directions``, the Python client tried
    ``response.json()`` on XML, raised ``HTTPError(200)``, and the tool
    surfaced "Route planning failed: HTTP Error: 200". The fix routes
    the GPX request through a raw ``requests.post``; this test stubs
    that out and asserts the success path.
    """

    @pytest.mark.asyncio
    async def test_happy_path_returns_summary_and_gpx_inline(self, monkeypatch):
        from komoot_mcp.tools import routing_tools

        registered = _build_tool_registry(routing_tools)

        # Provide a planning result that mimics what RoutingManager
        # returns after both ORS calls succeed.
        fake_result = {
            "gpx": "<gpx><trk><name>plan</name></trk></gpx>",
            "distance_km": 5.5,
            "elevation_gain_m": 120.0,
            "duration_minutes": 90.0,
            "waypoints": [(49.0, 8.4), (49.01, 8.41)],
        }

        class _FakeRouting:
            def plan_route(self, **kwargs):
                # Capture so we can sanity-check shape.
                self.last_kwargs = kwargs
                return fake_result

        class _FakeGeocoder:
            def forward(self, query, limit=1):
                return [{"lat": 49.0094, "lon": 8.4001,
                         "display_name": "X", "city": "Y",
                         "country": "DE", "type": "place"}]

            def reverse(self, lat, lon):  # pragma: no cover - unused here
                return {}

        monkeypatch.setattr(
            routing_tools, "get_routing_manager", lambda: _FakeRouting(),
        )
        monkeypatch.setattr(
            routing_tools, "get_geocoder", lambda: _FakeGeocoder(),
        )

        out = await registered["komoot_plan_route"](
            start="49.0094,8.4001", end="49.0136,8.4045", sport="hike",
        )
        # No spurious "HTTP Error: 200" or "Route planning failed".
        assert "Route planning failed" not in out
        assert "HTTP Error" not in out
        # Summary lines present.
        assert "Distance: 5.5 km" in out
        assert "Elevation gain: 120.0 m" in out
        # GPX embedded inline — no server-side path leak.
        assert "```xml" in out
        assert "<gpx>" in out
        assert "/tmp/" not in out
        assert "GPX saved to" not in out
        assert "komoot_upload_tour('/" not in out  # no path passed back


class TestRoutingManagerGpxFetchAvoidsHttp200Bug:
    """Direct test for issue #11 root cause.

    The previous ``RoutingManager.plan_route`` called
    ``client.directions(..., format="gpx")``. That triggered
    ``openrouteservice.exceptions.HTTPError(200)`` because the GPX body
    is XML and the ORS client unconditionally JSON-decodes responses.

    The fix sidesteps the client for the GPX format and POSTs to
    ``/v2/directions/{profile}/gpx`` directly. This test mocks both
    ``client.directions`` and ``requests.post`` to assert that:
      * the GPX request lands as a raw HTTP POST (not via the client),
      * the success path doesn't surface "HTTP Error: 200",
      * the returned dict carries the XML body verbatim.
    """

    @pytest.mark.asyncio
    async def test_gpx_path_uses_raw_http_post(self, monkeypatch):
        monkeypatch.setenv("ORS_API_KEY", "k")
        import komoot_mcp.routing as routing_mod
        from komoot_mcp.routing import RoutingManager

        manager = RoutingManager()

        # Track every call site so we can prove the GPX call did NOT
        # touch the client.
        client_calls: list[dict] = []

        def fake_directions(**kwargs):
            client_calls.append(kwargs)
            assert kwargs.get("format") != "gpx", (
                "GPX must not flow through the ORS Python client — "
                "that's the issue #11 bug."
            )
            return {
                "features": [{
                    "properties": {"summary": {
                        "distance": 5500, "ascent": 120, "duration": 5400,
                    }},
                    "geometry": {"coordinates": [[8.4, 49.0], [8.41, 49.01]]},
                }]
            }

        manager.client.directions = fake_directions
        manager.client._base_url = "https://api.openrouteservice.org"
        manager.client._timeout = 60

        gpx_body = "<gpx><trk><name>ok</name></trk></gpx>"

        class _Resp:
            status_code = 200
            text = gpx_body

            def json(self):  # pragma: no cover - error path only
                raise ValueError("not json")

        captured: dict = {}

        def fake_post(url, data=None, headers=None, timeout=None, **kwargs):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
            return _Resp()

        monkeypatch.setattr(routing_mod.requests, "post", fake_post)

        out = manager.plan_route(
            start=(49.0, 8.4), end=(49.01, 8.41), sport="hike",
        )
        # As of issue #18, plan_route post-processes the ORS GPX into
        # track format before returning. The body therefore won't match
        # ``gpx_body`` byte-for-byte. Verify the route data round-trips
        # through the transform: input had one <trk>, output still has
        # at least one <trk>.
        assert "<trk>" in out["gpx"]
        # And opt-in raw mode returns the original ORS body verbatim.
        raw = manager.plan_route(
            start=(49.0, 8.4), end=(49.01, 8.41), sport="hike",
            _raw_ors_gpx=True,
        )
        assert raw["gpx"] == gpx_body
        assert out["distance_km"] == 5.5
        assert captured["url"].endswith("/v2/directions/foot-hiking/gpx")
        assert captured["headers"]["Authorization"] == "k"
