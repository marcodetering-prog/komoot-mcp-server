"""Per-request execution context for the Komoot MCP server.

Multi-tenancy is achieved with :class:`contextvars.ContextVar` — each
incoming HTTP request runs inside an isolated context so the
``AuthManager`` / ``KomootClient`` it builds never leaks across
tenants. The Starlette middleware in :mod:`komoot_mcp.middleware`
populates the context before the MCP handler runs and resets it
afterwards.

For stdio/local-dev mode (no middleware), the helpers below fall back
to env vars and a process-wide ``RateLimiter`` so behaviour is
unchanged for single-user setups.
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Optional

from komoot_mcp.auth import AuthManager
from komoot_mcp.client import KomootClient
from komoot_mcp.rate_limiter import RateLimiter
from komoot_mcp.geocoder import Geocoder
# RoutingManager is imported lazily — its only hard dep is the
# openrouteservice client, which we don't want to require for unit
# tests that exercise auth/tour tools alone.


# Per-request state. Tools must NEVER look at module globals — always
# resolve through ``get_*`` helpers so multi-tenant isolation holds.
_auth_var: ContextVar[Optional[AuthManager]] = ContextVar(
    "komoot_auth_manager", default=None
)
_client_var: ContextVar[Optional[KomootClient]] = ContextVar(
    "komoot_client", default=None
)
# OpenRouteService API key — per-org credential plumbed via x-user-credentials.
# Tools that hit ORS (currently komoot_plan_route) MUST read through
# ``get_ors_api_key`` so two concurrent tenants never share a key.
_ors_api_key_var: ContextVar[Optional[str]] = ContextVar(
    "komoot_ors_api_key", default=None
)


# Shared singletons that don't carry tenant identity. Rate limiting is
# per-process (we throttle the whole server, not per-user). Geocoder
# talks to public APIs with no user state.
_rate_limiter: RateLimiter | None = None
_geocoder: Geocoder | None = None


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


def get_geocoder() -> Geocoder:
    global _geocoder
    if _geocoder is None:
        _geocoder = Geocoder()
    return _geocoder


def get_routing_manager():
    """Build a RoutingManager for the current request.

    Reads the ORS API key from the ContextVar (set by middleware from
    ``x-user-credentials``), falling back to ``ORS_API_KEY`` env var for
    stdio mode. Returns ``None`` if neither is set or if the
    ``openrouteservice`` client isn't installed — callers handle the
    ``None`` case with a user-facing error.

    Built per-request (no caching) because the key is per-tenant. The
    ``openrouteservice.Client`` is cheap to construct.
    """
    try:
        # Lazy import — see top of file.
        from komoot_mcp.routing import RoutingError, RoutingManager
    except ImportError:
        return None

    api_key = get_ors_api_key()
    try:
        return RoutingManager(api_key=api_key)
    except RoutingError:
        return None


def set_ors_api_key(api_key: str) -> object:
    """Install an ORS API key for the current context.

    Returns a token for :func:`reset_ors_api_key`. Middleware MUST reset
    after the request so the key doesn't leak across tenants.
    """
    return _ors_api_key_var.set(api_key)


def reset_ors_api_key(token: object) -> None:
    _ors_api_key_var.reset(token)  # type: ignore[arg-type]


def get_ors_api_key() -> Optional[str]:
    """Return the current request's ORS API key.

    Resolution order:
    1. ContextVar (set by middleware from ``x-user-credentials``).
    2. ``ORS_API_KEY`` env var (stdio/local-dev fallback).

    Returns ``None`` if neither is set.
    """
    key = _ors_api_key_var.get()
    if key:
        return key
    return os.environ.get("ORS_API_KEY") or None


def set_auth_manager(auth: AuthManager) -> object:
    """Install an :class:`AuthManager` for the current context.

    Returns a token that can be passed to :func:`reset_auth_manager`
    to undo the change. Middleware MUST reset to avoid leaking creds
    across requests served by the same worker.
    """
    return _auth_var.set(auth)


def reset_auth_manager(token: object) -> None:
    _auth_var.reset(token)  # type: ignore[arg-type]


def get_auth_manager() -> AuthManager:
    """Return the current request's AuthManager.

    Lazily builds one from env vars if none has been installed —
    preserves stdio/local-dev behaviour for single-user setups.
    """
    am = _auth_var.get()
    if am is None:
        am = AuthManager()
        _auth_var.set(am)
        # The lazy fallback is not reset — stdio mode is single-tenant
        # by design, so we keep the auth for the life of the process.
    return am


def get_client() -> KomootClient:
    """Return the current request's KomootClient, building lazily."""
    c = _client_var.get()
    if c is None:
        c = KomootClient(get_auth_manager(), get_rate_limiter())
        _client_var.set(c)
    return c


def clear_request_state() -> None:
    """Reset auth/client/ORS ContextVars to their defaults.

    Middleware calls this in a ``finally`` block to make absolutely
    sure no tenant state is held by the worker after a response.

    The client close is best-effort and usually a no-op: tools build the
    client lazily via ``get_client()`` inside the MCP handler's own context,
    and a ``ContextVar.set`` there does not propagate back out to the
    middleware, so ``_client_var`` is normally None by the time we get here.
    Session sockets are then released by GC. The call stays because it is
    correct whenever the client *is* visible, and because building the
    client eagerly in middleware would otherwise leak sockets silently.
    """
    client = _client_var.get()
    if client is not None:
        client.close()
    _auth_var.set(None)
    _client_var.set(None)
    _ors_api_key_var.set(None)
