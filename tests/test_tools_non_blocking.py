"""Regression tests: routing tools must not block the event loop.

Reverting any single offload in ``routing_tools`` fails at least one test here,
so keep exactly one call slow per test and the rest instant. Checked via a
heartbeat coroutine, plus wall-clock overlap for the plan calls; geocodes are
exempt from overlap since they share one dedicated thread on purpose.

Timings are coarse (0.2 s sleeps, generous margins) for loaded CI boxes.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from komoot_mcp.context import clear_request_state


# How long one synchronous "network" call takes: 2x if serialized, ~1x if not.
BLOCK_SECONDS = 0.2


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


class _SlowGeocoder:
    """Stand-in for the real synchronous Geocoder."""

    DELAY = BLOCK_SECONDS

    def forward(self, query, limit=5):
        time.sleep(self.DELAY)
        return [{
            "display_name": query, "city": "Freiburg", "country": "DE",
            "lat": 47.99, "lon": 7.85, "type": "city",
        }]

    def reverse(self, lat, lon):
        time.sleep(self.DELAY)
        return {
            "display_name": "somewhere", "city": "Freiburg",
            "country": "DE", "lat": lat, "lon": lon, "type": "city",
        }


class _InstantGeocoder(_SlowGeocoder):
    """Free geocoding, so a test measures only the call it targets."""

    DELAY = 0.0


async def _heartbeat(stop: asyncio.Event) -> int:
    """Count how many times the loop gets back to us while a tool runs."""
    ticks = 0
    while not stop.is_set():
        await asyncio.sleep(BLOCK_SECONDS / 20)
        ticks += 1
    return ticks


async def _ticks_during(coro):
    """Run ``coro`` and return (result, heartbeat ticks observed)."""
    stop = asyncio.Event()
    beat = asyncio.create_task(_heartbeat(stop))
    try:
        result = await coro
    finally:
        stop.set()
    return result, await beat


class TestGeocodeToolDoesNotBlock:
    @pytest.mark.asyncio
    async def test_concurrent_geocodes_leave_loop_responsive(self, monkeypatch):
        """Serialized by design (one thread); the loop must still stay free."""
        from komoot_mcp.tools import routing_tools
        registered = _build_tool_registry(routing_tools)
        monkeypatch.setattr(routing_tools, "get_geocoder",
                            lambda: _SlowGeocoder())

        outs, ticks = await _ticks_during(asyncio.gather(
            registered["komoot_geocode"]("Freiburg"),
            registered["komoot_geocode"]("Karlsruhe"),
        ))

        assert all("Geocoding results" in o for o in outs)
        assert ticks >= 10, f"event loop stalled during geocodes ({ticks} ticks)"

    @pytest.mark.asyncio
    async def test_reverse_geocode_leaves_loop_responsive(self, monkeypatch):
        from komoot_mcp.tools import routing_tools
        registered = _build_tool_registry(routing_tools)
        monkeypatch.setattr(routing_tools, "get_geocoder",
                            lambda: _SlowGeocoder())

        out, ticks = await _ticks_during(
            registered["komoot_geocode"]("47.99,7.85"),
        )

        assert "Location:" in out
        # Blocking would let through 0-1 ticks.
        assert ticks >= 5, f"event loop stalled during reverse geocode ({ticks} ticks)"


class TestGeocodeIsolatedFromTheDefaultPool:
    """Real Geocoder with only ``urlopen`` stubbed, so ``_wait`` really runs."""

    THROTTLE = 0.1

    @pytest.mark.asyncio
    async def test_burst_keeps_spacing_and_leaves_the_default_pool_free(
        self, monkeypatch,
    ):
        from komoot_mcp.geocoder import Geocoder
        from komoot_mcp.tools import routing_tools

        registered = _build_tool_registry(routing_tools)

        geo = Geocoder()
        geo._min_interval = self.THROTTLE
        monkeypatch.setattr(routing_tools, "get_geocoder", lambda: geo)

        fetches = []

        def fake_urlopen(url, timeout=None):
            fetches.append(time.monotonic())
            resp = MagicMock()
            resp.read.return_value = b'{"features":[]}'
            holder = MagicMock()
            holder.__enter__.return_value = resp
            return holder

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            burst = [
                asyncio.create_task(registered["komoot_geocode"](f"q{i}"))
                for i in range(8)
            ]
            await asyncio.sleep(0.02)

            # An unrelated blocking call, i.e. any kompy request in client.py.
            started = time.monotonic()
            await asyncio.to_thread(lambda: "tours")
            unrelated_wait = time.monotonic() - started

            await asyncio.gather(*burst)

        assert len(fetches) == 8
        gaps = [b - a for a, b in zip(fetches, fetches[1:])]
        assert all(g >= self.THROTTLE * 0.7 for g in gaps), (
            f"throttle bypassed by concurrent threads: gaps={gaps}"
        )
        # Without a dedicated executor the burst occupies default-pool workers
        # and this wait grows with the queue.
        assert unrelated_wait < self.THROTTLE * 2, (
            f"default pool starved by geocode burst ({unrelated_wait:.2f}s)"
        )


class TestPlanRouteToolDoesNotBlock:
    @staticmethod
    def _install(monkeypatch, routing_tools):
        fake_result = {
            "gpx": "<gpx><trk><name>plan</name></trk></gpx>",
            "distance_km": 5.5,
            "elevation_gain_m": 120.0,
            "duration_minutes": 90.0,
            "waypoints": [(47.99, 7.85), (49.0, 8.4)],
        }

        class _SlowRouting:
            def plan_route(self, **kwargs):
                time.sleep(BLOCK_SECONDS)
                return fake_result

        monkeypatch.setattr(routing_tools, "get_geocoder",
                            lambda: _InstantGeocoder())
        monkeypatch.setattr(routing_tools, "get_routing_manager",
                            lambda: _SlowRouting())

    @pytest.mark.asyncio
    async def test_two_plans_overlap(self, monkeypatch):
        from komoot_mcp.tools import routing_tools
        registered = _build_tool_registry(routing_tools)
        self._install(monkeypatch, routing_tools)

        started = time.monotonic()
        outs = await asyncio.gather(
            registered["komoot_plan_route"](start="Freiburg", end="Karlsruhe"),
            registered["komoot_plan_route"](start="Freiburg", end="Karlsruhe"),
        )
        elapsed = time.monotonic() - started

        assert all("Route planned successfully" in o for o in outs)
        # Geocoding is instant here, so only the two plan calls cost anything:
        # 2x serial, ~1x concurrent.
        assert elapsed < BLOCK_SECONDS * 1.5, (
            f"concurrent route plans serialized ({elapsed:.2f}s)"
        )

    @pytest.mark.asyncio
    async def test_plan_leaves_loop_responsive(self, monkeypatch):
        from komoot_mcp.tools import routing_tools
        registered = _build_tool_registry(routing_tools)
        self._install(monkeypatch, routing_tools)

        out, ticks = await _ticks_during(
            registered["komoot_plan_route"](start="Freiburg", end="Karlsruhe"),
        )

        assert "Route planned successfully" in out
        assert ticks >= 10, f"event loop stalled during plan_route ({ticks} ticks)"


class TestPlanAndUploadToolDoesNotBlock:
    """One slow call per test, else the others supply enough ticks to hide it."""

    @staticmethod
    def _install(monkeypatch, routing_tools, *, auth_delay, plan_delay):
        fake_route = {
            "distance": 12300.0, "duration": 4000,
            "elevation_up": 250.0, "elevation_down": 220.0,
        }

        class _Planner:
            def __init__(self, auth_pair, **kwargs):
                pass

            def plan(self, **kwargs):
                time.sleep(plan_delay)
                return fake_route

        class _FakeClient:
            _session = None

            def _basic_auth(self):
                # kompy builds its connector here, logging in over blocking
                # requests with no timeout set.
                time.sleep(auth_delay)
                return ("uid", "tok")

            async def save_planned_tour(self, route_response, name,
                                        status="private"):
                return {"id": 1234, "status": "saved"}

        monkeypatch.setattr(routing_tools, "KomootNativePlanner", _Planner)
        monkeypatch.setattr(routing_tools, "get_geocoder",
                            lambda: _InstantGeocoder())
        monkeypatch.setattr(routing_tools, "get_client", lambda: _FakeClient())

    async def _run(self, registered):
        return await _ticks_during(
            registered["komoot_plan_and_upload"](
                start="Freiburg", end="Karlsruhe",
            ),
        )

    @pytest.mark.asyncio
    async def test_native_plan_is_offloaded(self, monkeypatch):
        from komoot_mcp.tools import routing_tools
        registered = _build_tool_registry(routing_tools)
        self._install(monkeypatch, routing_tools,
                      auth_delay=0.0, plan_delay=BLOCK_SECONDS)

        out, ticks = await self._run(registered)

        assert "1234" in out
        assert ticks >= 10, f"loop stalled during the planner POST ({ticks} ticks)"

    @pytest.mark.asyncio
    async def test_auth_login_is_offloaded(self, monkeypatch):
        from komoot_mcp.tools import routing_tools
        registered = _build_tool_registry(routing_tools)
        self._install(monkeypatch, routing_tools,
                      auth_delay=BLOCK_SECONDS, plan_delay=0.0)

        out, ticks = await self._run(registered)

        assert "1234" in out
        assert ticks >= 10, f"loop stalled during the kompy login ({ticks} ticks)"
