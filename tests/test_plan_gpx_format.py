"""Issue #18 — ORS rte -> Komoot trk GPX conversion.

Without this, every ``komoot_plan_route`` output is rejected by the
Komoot upload endpoint with HTTP 400. The fix lives in
``routing._ors_rte_to_trk_gpx`` and is called from
``RoutingManager.plan_route`` before returning.
"""
from __future__ import annotations

from typing import Any

import pytest

from komoot_mcp.routing import RoutingManager, _ors_rte_to_trk_gpx

# An ORS-shaped GPX with <rte>/<rtept> + per-point elevation. Pared
# down to three points so the test stays readable but preserving the
# format quirks Komoot rejects.
ORS_RTE_GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1"
     creator="OpenRouteService">
  <metadata><name>ORS route</name></metadata>
  <rte>
    <name>ORS route</name>
    <rtept lat="49.0" lon="8.4"><ele>120</ele></rtept>
    <rtept lat="49.01" lon="8.41"><ele>125</ele></rtept>
    <rtept lat="49.02" lon="8.42"><ele>130</ele></rtept>
  </rte>
</gpx>
"""


class TestOrsRteToTrkGpx:
    """The conversion function itself: in => <rte>, out => <trk>."""

    def test_converts_rte_to_trk(self):
        out = _ors_rte_to_trk_gpx(ORS_RTE_GPX)
        # All route-point markers must be gone.
        assert "<rte>" not in out
        assert "<rtept" not in out
        # Track-point markers must be present.
        assert "<trk>" in out
        assert "<trkseg>" in out
        assert "<trkpt" in out

    def test_preserves_point_count(self):
        out = _ors_rte_to_trk_gpx(ORS_RTE_GPX)
        # Three input <rtept> → three output <trkpt>.
        assert out.count("<trkpt") == 3

    def test_preserves_coordinates_and_elevation(self):
        out = _ors_rte_to_trk_gpx(ORS_RTE_GPX)
        import gpxpy

        parsed = gpxpy.parse(out)
        assert len(parsed.tracks) == 1
        seg = parsed.tracks[0].segments[0]
        # Coordinates round-trip exactly.
        assert [(p.latitude, p.longitude) for p in seg.points] == [
            (49.0, 8.4), (49.01, 8.41), (49.02, 8.42),
        ]
        # Elevation is preserved when present.
        assert [p.elevation for p in seg.points] == [120.0, 125.0, 130.0]

    def test_creator_marks_conversion_source(self):
        out = _ors_rte_to_trk_gpx(ORS_RTE_GPX)
        assert "komoot-mcp-server" in out

    def test_empty_input_emits_empty_gpx(self):
        out = _ors_rte_to_trk_gpx(
            '<?xml version="1.0"?><gpx version="1.1" '
            'xmlns="http://www.topografix.com/GPX/1/1"></gpx>'
        )
        assert "<rte>" not in out
        # No tracks emitted is fine — just no rtept on the wire.
        assert "<rtept" not in out


class _FakeOrsClient:
    """Tracks calls to client.directions; returns a fixed GeoJSON shape."""

    def __init__(self, key=None):
        self.key = key
        self._base_url = "https://api.openrouteservice.org"
        self._timeout = 60
        self.calls: list[dict[str, Any]] = []

    def directions(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "features": [{
                "properties": {"summary": {
                    "distance": 1234.5, "ascent": 67.8, "duration": 909,
                }},
                "geometry": {"coordinates": [[8.4, 49.0], [8.41, 49.01]]},
            }]
        }


class _OrsResp:
    """A requests.Response-like stub for the raw GPX path."""

    def __init__(self, status_code=200, text=ORS_RTE_GPX):
        self.status_code = status_code
        self.text = text

    def json(self):
        raise ValueError("not json")


@pytest.fixture
def routing_manager(monkeypatch):
    """A RoutingManager wired with a fake ORS client + fake GPX HTTP."""
    monkeypatch.setenv("ORS_API_KEY", "k")
    m = RoutingManager()
    m.client = _FakeOrsClient(key="k")

    import komoot_mcp.routing as routing_mod

    def fake_post(url, data=None, headers=None, timeout=None, **kwargs):
        return _OrsResp(status_code=200, text=ORS_RTE_GPX)

    monkeypatch.setattr(routing_mod.requests, "post", fake_post)
    return m


class TestPlanRouteConvertsBeforeReturning:
    """End-to-end on RoutingManager: ORS gives <rte>, plan_route returns <trk>."""

    def test_plan_route_output_is_track_format(self, routing_manager):
        result = routing_manager.plan_route(
            start=(49.0, 8.4), end=(49.01, 8.41), sport="hike",
        )
        gpx = result["gpx"]
        assert "<trk>" in gpx
        assert "<rte>" not in gpx
        assert "<rtept" not in gpx
        assert "<trkpt" in gpx

    def test_raw_escape_hatch_returns_ors_xml(self, routing_manager):
        """Power-user toggle: ``_raw_ors_gpx=True`` skips conversion."""
        result = routing_manager.plan_route(
            start=(49.0, 8.4), end=(49.01, 8.41), sport="hike",
            _raw_ors_gpx=True,
        )
        assert "<rte>" in result["gpx"]
        assert "<rtept" in result["gpx"]
