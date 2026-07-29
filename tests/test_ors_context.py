"""ContextVar isolation and middleware tests for the per-org ORS API key.

These tests exist alongside ``test_context_isolation.py`` (which covers
the Komoot AuthManager) and prove the same invariants for the
OpenRouteService key plumbed via ``x-user-credentials``:

* Two concurrent ContextVar scopes hold independent keys.
* The Starlette middleware extracts ``ors_api_key`` from the JSON header
  and pushes it into the ContextVar for the lifetime of the request.
* Routing tools return a clear error when no key is configured for the
  org and no env-var fallback is set.
* The ``ORS_API_KEY`` env var keeps working as a stdio-mode fallback.
"""

import asyncio
import json

import pytest

from komoot_mcp.context import (
    clear_request_state,
    get_ors_api_key,
    get_routing_manager,
    reset_ors_api_key,
    set_ors_api_key,
)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Each test runs in a fresh context with no ORS_API_KEY env var."""
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    clear_request_state()
    yield
    clear_request_state()


class TestOrsApiKeyContextVar:
    def test_set_and_get_returns_same_key(self):
        token = set_ors_api_key("key-alpha")
        try:
            assert get_ors_api_key() == "key-alpha"
        finally:
            reset_ors_api_key(token)

    def test_reset_restores_default_none(self):
        token = set_ors_api_key("key-beta")
        assert get_ors_api_key() == "key-beta"
        reset_ors_api_key(token)
        # After reset and with no env var, the key resolves to None.
        assert get_ors_api_key() is None

    def test_env_var_is_fallback_when_contextvar_empty(self, monkeypatch):
        monkeypatch.setenv("ORS_API_KEY", "env-fallback-key")
        clear_request_state()
        assert get_ors_api_key() == "env-fallback-key"

    def test_contextvar_wins_over_env_var(self, monkeypatch):
        """Per-tenant key must override the process-wide env var."""
        monkeypatch.setenv("ORS_API_KEY", "env-fallback-key")
        token = set_ors_api_key("tenant-key")
        try:
            assert get_ors_api_key() == "tenant-key"
        finally:
            reset_ors_api_key(token)

    @pytest.mark.asyncio
    async def test_concurrent_tasks_see_different_keys(self):
        """Smoking gun: two concurrent coroutines must NOT share keys."""
        results: dict[str, str | None] = {}
        ready = asyncio.Event()
        proceed = asyncio.Event()

        async def tenant(label: str, key: str):
            token = set_ors_api_key(key)
            try:
                if label == "alice":
                    ready.set()
                    await proceed.wait()
                else:
                    await ready.wait()
                    proceed.set()
                results[label] = get_ors_api_key()
            finally:
                reset_ors_api_key(token)

        await asyncio.gather(
            tenant("alice", "alice-ors-key"),
            tenant("bob", "bob-ors-key"),
        )

        assert results["alice"] == "alice-ors-key"
        assert results["bob"] == "bob-ors-key"


class TestUserCredentialsMiddlewareOrs:
    """The middleware must extract ``ors_api_key`` from the JSON header."""

    @pytest.mark.asyncio
    async def test_middleware_installs_ors_key(self):
        from komoot_mcp.middleware import UserCredentialsMiddleware

        captured: dict[str, str | None] = {"key": None}

        async def downstream(scope, receive, send):
            captured["key"] = get_ors_api_key()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = UserCredentialsMiddleware(downstream)
        creds = {
            "email": "tenant@example.com",
            "password": "secret",
            "ors_api_key": "ors-tenant-key",
        }
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [
                (b"x-user-credentials", json.dumps(creds).encode("latin-1")),
            ],
        }

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            pass

        await mw(scope, receive, send)

        assert captured["key"] == "ors-tenant-key"
        # After the request, state must be cleared so the next tenant
        # doesn't see this key.
        assert get_ors_api_key() is None

    @pytest.mark.asyncio
    async def test_middleware_handles_missing_ors_key(self):
        """A request with email/password but no ors_api_key is valid —
        only routing tools should fail downstream, not the request itself.
        """
        from komoot_mcp.middleware import UserCredentialsMiddleware

        captured: dict[str, str | None] = {"key": "unset-sentinel"}

        async def downstream(scope, receive, send):
            captured["key"] = get_ors_api_key()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = UserCredentialsMiddleware(downstream)
        # Header has Komoot creds but no ors_api_key field.
        creds = {"email": "tenant@example.com", "password": "secret"}
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [
                (b"x-user-credentials", json.dumps(creds).encode("latin-1")),
            ],
        }

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            pass

        await mw(scope, receive, send)

        # No ORS key was provided → ContextVar resolves to None (no env fallback in this test).
        assert captured["key"] is None

    @pytest.mark.asyncio
    async def test_middleware_resets_ors_key_after_request(self):
        """Two sequential requests with different keys must not bleed."""
        from komoot_mcp.middleware import UserCredentialsMiddleware

        seen: list[str | None] = []

        async def downstream(scope, receive, send):
            seen.append(get_ors_api_key())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = UserCredentialsMiddleware(downstream)

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(_msg):
            pass

        async def make_request(key: str):
            creds = {"email": "u@x.com", "password": "p", "ors_api_key": key}
            scope = {
                "type": "http",
                "path": "/mcp",
                "headers": [
                    (b"x-user-credentials", json.dumps(creds).encode("latin-1")),
                ],
            }
            await mw(scope, receive, send)

        await make_request("first-key")
        await make_request("second-key")

        assert seen == ["first-key", "second-key"]
        # After both requests, no key should be lingering.
        assert get_ors_api_key() is None


class TestRoutingManagerPerRequest:
    """``get_routing_manager`` must use the per-request key, not a cached
    process-wide instance, and must return ``None`` when no key is set so
    the tool layer can render a friendly error.
    """

    def test_returns_none_when_no_key_anywhere(self):
        # No env var (cleared by fixture), no ContextVar key.
        assert get_routing_manager() is None

    def test_uses_contextvar_key(self):
        token = set_ors_api_key("ctx-key-123")
        try:
            mgr = get_routing_manager()
            assert mgr is not None
            # The stub openrouteservice.Client records the key it got.
            assert mgr.client.key == "ctx-key-123"
        finally:
            reset_ors_api_key(token)

    def test_uses_env_var_when_contextvar_empty(self, monkeypatch):
        monkeypatch.setenv("ORS_API_KEY", "env-stdio-key")
        # Cleared state, no ContextVar — should fall back to env var.
        clear_request_state()
        mgr = get_routing_manager()
        assert mgr is not None
        assert mgr.client.key == "env-stdio-key"

    def test_contextvar_overrides_env_var(self, monkeypatch):
        """When both are set, the per-tenant key wins so we never leak
        a different tenant's key (or a stale dev key from env)."""
        monkeypatch.setenv("ORS_API_KEY", "env-default")
        token = set_ors_api_key("tenant-override")
        try:
            mgr = get_routing_manager()
            assert mgr is not None
            assert mgr.client.key == "tenant-override"
        finally:
            reset_ors_api_key(token)

    def test_two_contextvar_scopes_get_different_managers(self):
        """Each scope sees a manager bound to its own key."""
        keys_seen: list[str] = []

        token_a = set_ors_api_key("scope-a-key")
        keys_seen.append(get_routing_manager().client.key)
        reset_ors_api_key(token_a)

        token_b = set_ors_api_key("scope-b-key")
        keys_seen.append(get_routing_manager().client.key)
        reset_ors_api_key(token_b)

        assert keys_seen == ["scope-a-key", "scope-b-key"]


class TestRoutingToolErrorMessage:
    """The user-facing tool message must point users at the dashboard."""

    @pytest.mark.asyncio
    async def test_plan_route_returns_dashboard_hint_when_unconfigured(self):
        """When no ORS key is configured anywhere, komoot_plan_route must
        return a string error that mentions both the dashboard and the
        signup URL — not a raw exception or env-var-only hint.
        """
        # Build a tiny ad-hoc MCP harness that captures the tool function.
        from komoot_mcp.tools import routing_tools

        captured: dict[str, object] = {}

        class _CapturingMCP:
            def tool(self, *args, **kwargs):
                def decorator(fn):
                    captured[fn.__name__] = fn
                    return fn

                return decorator

        routing_tools.register(_CapturingMCP())
        plan_route = captured["komoot_plan_route"]

        # No ORS key in ContextVar, no env var (fixture cleared it).
        result = await plan_route(start="Berlin")

        assert isinstance(result, str)
        lower = result.lower()
        assert "ors api key not configured" in lower
        assert "dashboard" in lower
        assert "openrouteservice.org" in lower
