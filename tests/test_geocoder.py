"""Tests for Geocoder (Photon API wrapper)."""
import json
import threading
import time
from unittest.mock import patch, MagicMock
from komoot_mcp.geocoder import Geocoder

class TestGeocoder:
    def test_forward_parses_response(self):
        geo = Geocoder()
        mock_response = {
            "features": [{
                "geometry": {"coordinates": [13.404954, 52.520008]},
                "properties": {"name": "Berlin", "city": "Berlin", "country": "Germany", "type": "city", "osm_id": 12345}
            }]
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

class TestGeocoderThrottleThreadSafety:
    """One process-wide Geocoder, so ``_wait`` must lock its read-modify-write.

    Unlocked, several threads read the same stale ``_last_call`` and fire
    together, multiplying the outbound rate to Photon.
    """

    N_THREADS = 6
    INTERVAL = 0.05

    def _run_concurrently(self, fn, n):
        start = threading.Barrier(n)
        errors = []

        def worker():
            start.wait(timeout=10)
            try:
                fn()
            except Exception as exc:  # pragma: no cover - surfaces real bugs
                errors.append(exc)

        pool = [threading.Thread(target=worker) for _ in range(n)]
        for t in pool:
            t.start()
        for t in pool:
            t.join(timeout=30)
            assert not t.is_alive(), "geocoder throttle deadlocked"
        assert not errors, f"worker raised: {errors!r}"

    def test_concurrent_calls_are_spaced_by_min_interval(self):
        geo = Geocoder()
        geo._min_interval = self.INTERVAL

        fire_times = []
        times_lock = threading.Lock()

        def fake_urlopen(url, timeout=None):
            # Stamp the moment the (would-be) network call happens; the
            # throttle is what must keep these apart. No real request is
            # ever made.
            with times_lock:
                fire_times.append(time.monotonic())
            resp = MagicMock()
            resp.read.return_value = b'{"features":[]}'
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self._run_concurrently(lambda: geo.forward("Berlin"), self.N_THREADS)

        assert len(fire_times) == self.N_THREADS
        fire_times.sort()
        gaps = [b - a for a, b in zip(fire_times, fire_times[1:])]
        # Generous margin: we only care that the throttle serialises the
        # calls, not that sleep timing is exact on a loaded CI box.
        floor = self.INTERVAL * 0.7
        assert all(g >= floor for g in gaps), (
            f"calls fired inside the throttle interval: gaps={gaps} "
            f"(min allowed {floor})"
        )

    def test_only_one_thread_is_ever_inside_the_throttle(self):
        """Mutual exclusion, checked directly by watching for overlap.

        Replaces an earlier "stamps stay monotonic" test that passed with the
        lock deleted: the unsynchronised window is one bytecode wide, so a lost
        update essentially never landed. Counting threads inside the sleep
        fails every run instead.
        """
        geo = Geocoder()
        geo._min_interval = self.INTERVAL

        inside = 0
        max_inside = 0
        bookkeeping = threading.Lock()
        real_sleep = time.sleep

        def tracking_sleep(duration):
            nonlocal inside, max_inside
            with bookkeeping:
                inside += 1
                max_inside = max(max_inside, inside)
            real_sleep(duration)
            with bookkeeping:
                inside -= 1

        with patch.object(time, "sleep", tracking_sleep):
            self._run_concurrently(geo._wait, self.N_THREADS)

        assert max_inside == 1, f"{max_inside} threads inside the throttle at once"
