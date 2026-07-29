"""Issue #19 / native-planner — ``komoot_plan_and_upload``.

The user-facing workflow "plan a route and add it to my Komoot tours"
previously required two MCP tool calls with a ~100k-token GPX flowing
through the LLM in between. The current tool runs both server-side and
returns only the tour ID + URL.

History: an earlier shape of this tool went through OpenRouteService +
``upload_gpx_capture_id``. That always wrote a ``tour_recorded``
(activity) record in Komoot — even with ``?type=tour_planned`` on the
query string — which made the user's planned-route saves show up as
"I just rode this 71km MTB ride today" in their feed (issue #21
follow-up). The tool now uses Komoot's own native planner
(``POST /api/routing/tour``) + ``save_planned_tour``, which honors the
``type=tour_planned`` field in the JSON body. The ORS-based
``komoot_plan_route`` tool still exists for callers that just want raw
GPX output without saving anything.

Also covers the lower-level ``upload_gpx_capture_id`` client helper —
kept around because ``komoot_upload_tour`` still uses it for the
"upload a GPS recording" path where the user really did ride the
route.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from komoot_mcp.auth import AuthManager
from komoot_mcp.client import KomootAPIError, KomootClient
from komoot_mcp.context import clear_request_state

GPX_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>inline-track</name>
    <trkseg>
      <trkpt lat="49.0" lon="8.4"><ele>120</ele></trkpt>
      <trkpt lat="49.01" lon="8.41"><ele>125</ele></trkpt>
    </trkseg>
  </trk>
</gpx>
"""


class _NoLimit:
    async def acquire(self):
        return None


@pytest.fixture(autouse=True)
def _reset():
    clear_request_state()
    yield
    clear_request_state()


@pytest.fixture
def client():
    am = AuthManager(email="t@x.com", password="pw")
    return KomootClient(am, _NoLimit())


def _register_routing_tools():
    from komoot_mcp.tools import routing_tools

    registered: dict[str, callable] = {}

    class _Mcp:
        def tool(self):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn

            return deco

    routing_tools.register(_Mcp())
    return registered, routing_tools


class _FakeGeocoder:
    def forward(self, q, limit=1):
        return [
            {
                "lat": 49.0094,
                "lon": 8.4001,
                "display_name": q,
                "city": "X",
                "country": "DE",
                "type": "place",
            }
        ]

    def reverse(self, lat, lon):  # pragma: no cover - unused
        return {}


# ---------------------------------------------------------------------
# The new komoot_plan_and_upload tool
# ---------------------------------------------------------------------


class TestPlanAndUploadTool:
    """The combo tool plans via Komoot's native planner and saves the
    response as ``tour_planned``. It must:

    * register correctly,
    * thread the public-sport → Komoot-sport name through the planner,
    * surface plan + save errors clearly (no "successfully: False"
      from #17),
    * not leak the planner's giant route blob to the LLM,
    * default tour status to ``private``.
    """

    @pytest.mark.asyncio
    async def test_tool_is_registered(self):
        registered, _ = _register_routing_tools()
        assert "komoot_plan_and_upload" in registered

    @pytest.mark.asyncio
    async def test_happy_path_returns_id_and_url(self, monkeypatch):
        registered, routing_tools = _register_routing_tools()

        fake_route = {
            # The native planner returns hundreds of KB of coordinates
            # under _embedded.coordinates — we use a short stub here
            # but assert the giant blob never reaches the user-facing
            # response.
            "type": "tour_planned",
            "distance": 12345.6,
            "duration": 5400,
            "elevation_up": 250.0,
            "elevation_down": 220.0,
            "path": [{"location": {"lat": 49.0, "lng": 8.4}}],
            "segments": [{"type": "Routed", "from": 0, "to": 1}],
            "_embedded": {
                "coordinates": {
                    "items": [
                        {"lat": 49.0, "lng": 8.4, "alt": 100, "t": 0},
                    ]
                    * 5000
                }
            },
        }

        plan_calls = []

        class _FakePlanner:
            def __init__(self, auth_pair):
                self.auth_pair = auth_pair

            def plan(self, waypoints, sport_komoot, constitution=3):
                plan_calls.append(
                    {
                        "waypoints": waypoints,
                        "sport_komoot": sport_komoot,
                        "constitution": constitution,
                    }
                )
                return fake_route

        save_calls = []

        class _FakeClient:
            def _basic_auth(self):
                return ("uid", "tok")

            async def save_planned_tour(self, route_response, name, status="private"):
                save_calls.append(
                    {
                        "name": name,
                        "status": status,
                        "route_response_id": id(route_response),
                    }
                )
                return {"id": 7654321, "status": "saved"}

        monkeypatch.setattr(
            routing_tools,
            "KomootNativePlanner",
            _FakePlanner,
        )
        monkeypatch.setattr(routing_tools, "get_geocoder", lambda: _FakeGeocoder())
        monkeypatch.setattr(routing_tools, "get_client", lambda: _FakeClient())

        out = await registered["komoot_plan_and_upload"](
            start="Freiburg",
            end="Karlsruhe",
            sport="mountain_bike",
            tour_name="MTB cross-country",
            tour_status="private",
        )

        # The giant coordinates blob MUST NOT be in the response.
        assert '"t": 0' not in out
        assert '"_embedded"' not in out
        # Tour ID + URL ARE in the response.
        assert "7654321" in out
        assert "https://www.komoot.com/tour/7654321" in out
        # Plan summary surfaces (distance in km, derived from m).
        assert "12.35 km" in out
        # Sport mapping: mountain_bike → mtb on the native planner side.
        assert plan_calls[0]["sport_komoot"] == "mtb"
        # Custom tour name flows through.
        assert save_calls[0]["name"] == "MTB cross-country"
        # Privacy defaults to private and surfaces in the response.
        assert save_calls[0]["status"] == "private"

    @pytest.mark.asyncio
    async def test_save_failure_does_not_claim_success(self, monkeypatch):
        registered, routing_tools = _register_routing_tools()

        fake_route = {
            "distance": 12300.0,
            "duration": 4000,
            "elevation_up": 250.0,
            "elevation_down": 220.0,
        }

        class _FakePlanner:
            def __init__(self, auth_pair):
                pass

            def plan(self, **kwargs):
                return fake_route

        class _FakeClient:
            def _basic_auth(self):
                return ("uid", "tok")

            async def save_planned_tour(self, **kwargs):
                raise KomootAPIError(
                    "Komoot rejected save_planned_tour (HTTP 400). Response body (first 300 chars): bad body"
                )

        monkeypatch.setattr(
            routing_tools,
            "KomootNativePlanner",
            _FakePlanner,
        )
        monkeypatch.setattr(routing_tools, "get_geocoder", lambda: _FakeGeocoder())
        monkeypatch.setattr(routing_tools, "get_client", lambda: _FakeClient())

        out = await registered["komoot_plan_and_upload"](
            start="Freiburg",
            end="Karlsruhe",
            sport="hike",
        )
        assert "successfully: False" not in out
        assert "save to Komoot failed" in out
        assert "400" in out
        # The plan distance still surfaces so the user knows the route
        # itself was valid.
        assert "12.3 km" in out

    @pytest.mark.asyncio
    async def test_plan_failure_does_not_attempt_save(self, monkeypatch):
        registered, routing_tools = _register_routing_tools()
        save_calls = []

        class _FailingPlanner:
            def __init__(self, auth_pair):
                pass

            def plan(self, **kwargs):
                # Use RoutingError to mirror what the real planner raises.
                from komoot_mcp.routing import RoutingError

                raise RoutingError("Komoot planner request failed (HTTP 500)")

        class _FakeClient:
            def _basic_auth(self):
                return ("uid", "tok")

            async def save_planned_tour(self, **kwargs):
                save_calls.append(kwargs)
                return {"id": 1, "status": "saved"}

        monkeypatch.setattr(
            routing_tools,
            "KomootNativePlanner",
            _FailingPlanner,
        )
        monkeypatch.setattr(routing_tools, "get_geocoder", lambda: _FakeGeocoder())
        monkeypatch.setattr(routing_tools, "get_client", lambda: _FakeClient())

        out = await registered["komoot_plan_and_upload"](
            start="Freiburg",
            end="Karlsruhe",
            sport="hike",
        )
        assert "Route planning failed" in out
        assert "HTTP 500" in out
        # The save path must not have been reached.
        assert save_calls == []

    @pytest.mark.asyncio
    async def test_roundtrip_requires_waypoints(self, monkeypatch):
        registered, routing_tools = _register_routing_tools()

        class _NeverCalledPlanner:
            def __init__(self, auth_pair):
                pass

            def plan(self, **kwargs):  # pragma: no cover - assertion fails first
                raise AssertionError("planner should not be called")

        class _FakeClient:
            def _basic_auth(self):
                return ("uid", "tok")

            async def save_planned_tour(self, **kwargs):  # pragma: no cover
                raise AssertionError("save should not be called")

        monkeypatch.setattr(
            routing_tools,
            "KomootNativePlanner",
            _NeverCalledPlanner,
        )
        monkeypatch.setattr(routing_tools, "get_geocoder", lambda: _FakeGeocoder())
        monkeypatch.setattr(routing_tools, "get_client", lambda: _FakeClient())

        out = await registered["komoot_plan_and_upload"](
            start="Freiburg",
            roundtrip=True,
            sport="mountain_bike",
        )
        # A zero-distance roundtrip is the degenerate case — must
        # error with a clear message, not call the planner.
        assert "Roundtrip" in out
        assert "waypoints" in out

    @pytest.mark.asyncio
    async def test_roundtrip_with_waypoints_threads_through(self, monkeypatch):
        registered, routing_tools = _register_routing_tools()
        captured = {}

        class _FakePlanner:
            def __init__(self, auth_pair):
                pass

            def plan(self, waypoints, sport_komoot, constitution=3):
                captured["waypoints"] = waypoints
                captured["sport"] = sport_komoot
                return {"distance": 5000, "duration": 1800, "elevation_up": 100, "elevation_down": 100}

        class _FakeClient:
            def _basic_auth(self):
                return ("uid", "tok")

            async def save_planned_tour(self, **kwargs):
                return {"id": 42, "status": "saved"}

        monkeypatch.setattr(
            routing_tools,
            "KomootNativePlanner",
            _FakePlanner,
        )
        monkeypatch.setattr(routing_tools, "get_geocoder", lambda: _FakeGeocoder())
        monkeypatch.setattr(routing_tools, "get_client", lambda: _FakeClient())

        out = await registered["komoot_plan_and_upload"](
            start="Freiburg",
            roundtrip=True,
            sport="mountain_bike",
            waypoints="47.99,7.85|47.97,7.88",
        )
        # The waypoints became start → vias → start.
        wps = captured["waypoints"]
        assert len(wps) == 4
        assert wps[0] == wps[-1]
        # Intermediate vias preserved in order.
        assert wps[1] == (47.99, 7.85)
        assert wps[2] == (47.97, 7.88)
        assert "42" in out

    @pytest.mark.asyncio
    async def test_gravel_maps_to_touringbicycle(self, monkeypatch):
        """Komoot has no separate gravel profile — we fold to the
        all-rounder bike profile rather than failing or sending an
        unknown sport token."""
        registered, routing_tools = _register_routing_tools()
        captured = {}

        class _FakePlanner:
            def __init__(self, auth_pair):
                pass

            def plan(self, waypoints, sport_komoot, constitution=3):
                captured["sport"] = sport_komoot
                return {"distance": 1, "duration": 1, "elevation_up": 0, "elevation_down": 0}

        class _FakeClient:
            def _basic_auth(self):
                return ("uid", "tok")

            async def save_planned_tour(self, **kwargs):
                return {"id": 1, "status": "saved"}

        monkeypatch.setattr(
            routing_tools,
            "KomootNativePlanner",
            _FakePlanner,
        )
        monkeypatch.setattr(routing_tools, "get_geocoder", lambda: _FakeGeocoder())
        monkeypatch.setattr(routing_tools, "get_client", lambda: _FakeClient())

        await registered["komoot_plan_and_upload"](
            start="Freiburg",
            end="Karlsruhe",
            sport="gravel_ride",
        )
        assert captured["sport"] == "touringbicycle"


# ---------------------------------------------------------------------
# upload_gpx_capture_id — the client-side helper used by #19
# ---------------------------------------------------------------------


class _FakeKomootResponse:
    def __init__(self, status_code=201, body=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class TestUploadGpxCaptureId:
    """Direct test of the client-side upload helper that pulls the ID
    out of the Komoot response (kompy throws it away)."""

    @pytest.mark.asyncio
    async def test_201_returns_id_and_uploaded_status(self, client, monkeypatch):
        # Stub the kompy connector so _get_api() works.
        api = MagicMock()
        api.authentication = MagicMock()
        api.authentication.get_email_address = MagicMock(return_value="t@x.com")
        api.authentication.get_password = MagicMock(return_value="pw")
        client._api = api

        from komoot_mcp import client as client_mod

        def fake_post(url, auth=None, headers=None, params=None, data=None, **kwargs):
            assert "tours" in url
            assert params["data_type"] == "gpx"
            # Default for this helper is now tour_planned — uploading
            # a planned route should NOT show up as a completed
            # activity in the user's Komoot tour list.
            assert params["type"] == "tour_planned"
            return _FakeKomootResponse(status_code=201, body={"id": 555})

        monkeypatch.setattr(client_mod.requests, "post", fake_post)

        out = await client.upload_gpx_capture_id(
            gpx_content=GPX_SAMPLE,
            sport="hike",
            tour_name="x",
        )
        assert out == {"id": 555, "status": "uploaded"}

    @pytest.mark.asyncio
    async def test_tour_type_override_passes_through(self, client, monkeypatch):
        """A caller can still request ``tour_recorded`` (e.g. for a
        future "upload my real GPS recording" path) — the helper must
        forward whatever ``tour_type`` it's given, not hard-code one."""
        api = MagicMock()
        api.authentication = MagicMock()
        api.authentication.get_email_address = MagicMock(return_value="t@x.com")
        api.authentication.get_password = MagicMock(return_value="pw")
        client._api = api

        from komoot_mcp import client as client_mod

        seen = {}

        def fake_post(url, auth=None, headers=None, params=None, data=None, **kwargs):
            seen.update(params)
            return _FakeKomootResponse(status_code=201, body={"id": 1})

        monkeypatch.setattr(client_mod.requests, "post", fake_post)

        await client.upload_gpx_capture_id(
            gpx_content=GPX_SAMPLE,
            sport="hike",
            tour_type="tour_recorded",
        )
        assert seen["type"] == "tour_recorded"

    @pytest.mark.asyncio
    async def test_202_marks_duplicate(self, client, monkeypatch):
        api = MagicMock()
        api.authentication = MagicMock()
        api.authentication.get_email_address = MagicMock(return_value="t@x.com")
        api.authentication.get_password = MagicMock(return_value="pw")
        client._api = api

        from komoot_mcp import client as client_mod

        def fake_post(*a, **kw):
            return _FakeKomootResponse(status_code=202, body={"id": 999})

        monkeypatch.setattr(client_mod.requests, "post", fake_post)

        out = await client.upload_gpx_capture_id(
            gpx_content=GPX_SAMPLE,
            sport="hike",
        )
        assert out == {"id": 999, "status": "duplicate"}

    @pytest.mark.asyncio
    async def test_400_raises_with_status_in_message(self, client, monkeypatch):
        api = MagicMock()
        api.authentication = MagicMock()
        api.authentication.get_email_address = MagicMock(return_value="t@x.com")
        api.authentication.get_password = MagicMock(return_value="pw")
        client._api = api

        from komoot_mcp import client as client_mod

        def fake_post(*a, **kw):
            return _FakeKomootResponse(
                status_code=400,
                body={},
                text="bad gpx",
            )

        monkeypatch.setattr(client_mod.requests, "post", fake_post)

        with pytest.raises(KomootAPIError) as ei:
            await client.upload_gpx_capture_id(
                gpx_content=GPX_SAMPLE,
                sport="hike",
            )
        msg = str(ei.value)
        assert "400" in msg
        # Helpful diagnosis hint for the most common 400 cause.
        assert "track format" in msg or "trk" in msg
