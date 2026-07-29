"""Tests for RateLimiter."""

import asyncio
import os
import time

import pytest

from komoot_mcp.rate_limiter import RateLimiter


class TestRateLimiter:
    def test_default_rate(self):
        os.environ.pop("KOMOOT_RATE_LIMIT", None)
        rl = RateLimiter()
        assert rl.rate == 2.0

    def test_custom_rate(self):
        os.environ["KOMOOT_RATE_LIMIT"] = "5"
        rl = RateLimiter()
        assert rl.rate == 5.0

    @pytest.mark.asyncio
    async def test_acquire_does_not_block_initial_burst(self):
        """The burst capacity (== rate) should be available instantly."""
        os.environ["KOMOOT_RATE_LIMIT"] = "5"
        rl = RateLimiter()
        start = time.monotonic()
        for _ in range(5):  # exactly the burst, no sleep expected
            await rl.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, f"Initial burst took {elapsed:.3f}s — should be ~0"

    @pytest.mark.asyncio
    async def test_rate_limit_enforced_under_sustained_load(self):
        """Going past the burst MUST sleep, proving rate limiting is on.

        With rate=10/s and the token-bucket algorithm in rate_limiter.py,
        20 acquires take roughly 0.5s: the first 10 drain the bucket,
        then about every other subsequent acquire forces a ~0.1s sleep
        while the bucket refills. The lower bound here is set to fail
        loudly if anyone short-circuits ``acquire`` or makes it
        unconditionally non-blocking.
        """
        os.environ["KOMOOT_RATE_LIMIT"] = "10"
        rl = RateLimiter()
        start = time.monotonic()
        for _ in range(20):
            await rl.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.35, (
            f"Sustained-load elapsed={elapsed:.3f}s — rate limiter looks "
            "disabled (expected >=0.35s for 20 acquires @ 10/s)"
        )
        # Upper bound — not pathologically slow.
        assert elapsed < 2.5

    @pytest.mark.asyncio
    async def test_acquire_is_async(self):
        """Sanity check: acquire returns a coroutine that must be awaited."""
        rl = RateLimiter()
        coro = rl.acquire()
        assert asyncio.iscoroutine(coro)
        await coro
