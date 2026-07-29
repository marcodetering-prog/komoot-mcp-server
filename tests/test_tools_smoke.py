"""Smoke tests for the tool handlers with kompy mocked out.

These exercise the multi-tenant plumbing end-to-end at the tool layer:
- AuthManager is resolved from the ContextVar.
- Two installed AuthManagers don't bleed across requests.
- The login + list_tours + user_profile happy paths return strings.

The kompy stub is installed in ``tests/conftest.py`` so that all
project modules see the same in-memory ``kompy`` module — patching
attributes on it from here works because the reference is shared.
"""

from __future__ import annotations

from unittest.mock import patch

import kompy  # the conftest stub
import pytest

from komoot_mcp.auth import AuthManager
from komoot_mcp.context import (
    clear_request_state,
    reset_auth_manager,
    set_auth_manager,
)


@pytest.fixture(autouse=True)
def _reset():
    clear_request_state()
    yield
    clear_request_state()


class TestPerRequestAuthManager:
    @pytest.mark.asyncio
    async def test_client_uses_contextvar_auth(self):
        """The client built from ContextVar AM hits the right kompy creds."""
        am = AuthManager(email="alice@x.com", password="pw")
        token = set_auth_manager(am)
        try:
            from komoot_mcp.context import get_client

            client = get_client()
            assert client.auth.email == "alice@x.com"
            result = await client.list_tours()
            assert result["tours"][0]["name"] == "tour-for-alice@x.com"
        finally:
            reset_auth_manager(token)

    @pytest.mark.asyncio
    async def test_two_scopes_get_different_clients(self):
        """Two contexts must build clients pinned to their own AM."""
        import contextvars

        async def run_as(email):
            ctx = contextvars.copy_context()

            async def inner():
                am = AuthManager(email=email, password="pw")
                set_auth_manager(am)
                from komoot_mcp.context import get_client

                client = get_client()
                result = await client.list_tours()
                return result["tours"][0]["name"]

            # Run in a copied context so changes don't leak.
            import asyncio

            return await asyncio.create_task(inner(), context=ctx)

        # Run sequentially in distinct contexts. Each should see its email.
        a = await run_as("alice@x.com")
        b = await run_as("bob@x.com")
        assert a == "tour-for-alice@x.com"
        assert b == "tour-for-bob@x.com"

    @pytest.mark.asyncio
    async def test_list_tours_tool_renders_string(self):
        """The actual tool handler returns a human-readable string."""
        # Build a minimal "FastMCP-like" recorder.
        registered: dict[str, callable] = {}

        class _Mcp:
            def tool(self):
                def decorator(fn):
                    registered[fn.__name__] = fn
                    return fn

                return decorator

        from komoot_mcp.tools import browse_tools

        browse_tools.register(_Mcp())

        am = AuthManager(email="carol@x.com", password="pw")
        token = set_auth_manager(am)
        try:
            out = await registered["komoot_list_tours"]()
            assert "tour-for-carol@x.com" in out
        finally:
            reset_auth_manager(token)

    @pytest.mark.asyncio
    async def test_login_tool_uses_contextvar_auth(self):
        """komoot_login should drive the ContextVar AM, not a global one."""
        registered: dict[str, callable] = {}

        class _Mcp:
            def tool(self):
                def decorator(fn):
                    registered[fn.__name__] = fn
                    return fn

                return decorator

        from komoot_mcp.tools import auth_tools

        auth_tools.register(_Mcp())

        am = AuthManager(email="dave@x.com", password="pw")
        # Force "already authenticated" path so we don't hit network.
        am.user_id = "dave"
        am.token = "tok"
        token = set_auth_manager(am)
        try:
            out = await registered["komoot_login"]()
            assert "dave" in out
        finally:
            reset_auth_manager(token)

    @pytest.mark.asyncio
    async def test_user_profile_display_falls_back_when_username_unknown(self):
        """When kompy returns the literal 'unknown' placeholder for the
        display name, the profile dict + rendered tool string should fall
        back to the email so users don't see 'Profile: unknown'."""
        registered: dict[str, callable] = {}

        class _Mcp:
            def tool(self):
                def decorator(fn):
                    registered[fn.__name__] = fn
                    return fn

                return decorator

        from komoot_mcp.tools import browse_tools

        browse_tools.register(_Mcp())

        # Drop in a connector whose authentication returns the literal
        # "unknown" username — the placeholder kompy hands back when
        # Komoot's login response omits a display name.
        class _UnknownAuth:
            def get_username(self):
                return "unknown"

            def get_email_address(self):
                return "frank@x.com"

        class _UnknownConnector:
            def __init__(self, email, password):
                self.authentication = _UnknownAuth()

        am = AuthManager(email="frank@x.com", password="pw")
        token = set_auth_manager(am)
        try:
            with patch.object(kompy, "KomootConnector", _UnknownConnector):
                from komoot_mcp.context import get_client

                # Direct client check: dict carries the fallback explicitly.
                profile = await get_client().get_user_profile()
                assert profile["display_name"] == "frank@x.com"
                assert profile["username"] == "unknown"
                assert profile["email"] == "frank@x.com"

                # Tool-handler check: rendered string must not say "Profile: unknown".
                out = await registered["komoot_get_user_profile"]()
            assert "Profile: unknown" not in out
            assert "frank@x.com" in out
        finally:
            reset_auth_manager(token)

    @pytest.mark.asyncio
    async def test_user_profile_tool_no_silent_swallow(self):
        """get_user_profile should propagate kompy errors via the handler."""
        registered: dict[str, callable] = {}

        class _Mcp:
            def tool(self):
                def decorator(fn):
                    registered[fn.__name__] = fn
                    return fn

                return decorator

        from komoot_mcp.tools import browse_tools

        browse_tools.register(_Mcp())

        am = AuthManager(email="eve@x.com", password="pw")
        token = set_auth_manager(am)
        try:
            # Patch the KomootConnector stub to raise on construction —
            # handler should catch and produce an error string (not the
            # silent fallback dict). Patch on the shared kompy module
            # object so the reference in client.py also sees the change.
            with patch.object(
                kompy,
                "KomootConnector",
                side_effect=RuntimeError("boom"),
            ):
                out = await registered["komoot_get_user_profile"]()
            assert "Error getting profile" in out
            assert "boom" in out
        finally:
            reset_auth_manager(token)
