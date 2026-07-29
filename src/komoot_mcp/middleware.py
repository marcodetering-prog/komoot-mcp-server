"""Starlette middleware for the Eric AI platform integration.

Two concerns:

1. **Internal-secret auth.** When ``INTERNAL_SECRET`` is set in the
   environment, every incoming request (except ``/health``) must
   carry a matching ``Authorization: Bearer <secret>`` header. This
   stops random internet traffic from reaching the MCP endpoint when
   it's exposed behind the platform gateway. When ``INTERNAL_SECRET``
   is unset (local dev) the check is skipped. The exact wire format
   matches the Bitrix reference server so the gateway can use a single
   code path for all backends.

2. **Per-tenant credentials.** The gateway forwards the calling user's
   Komoot creds as JSON in ``x-user-credentials``:

       {"email": "user@example.com", "password": "...", "ors_api_key": "..."}

   The middleware parses that header, builds an :class:`AuthManager`,
   pushes it into the request-local ContextVar via
   :func:`komoot_mcp.context.set_auth_manager`, and resets after the
   downstream handler returns. The optional ``ors_api_key`` field is
   pushed into a separate ContextVar via
   :func:`komoot_mcp.context.set_ors_api_key` so routing tools can pick
   it up per-request. If the header is absent (e.g. stdio mode), the
   env-var fallback in :mod:`komoot_mcp.context` kicks in.
"""
from __future__ import annotations

import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from komoot_mcp.auth import AuthManager
from komoot_mcp.context import (
    clear_request_state,
    reset_auth_manager,
    reset_ors_api_key,
    set_auth_manager,
    set_ors_api_key,
)

logger = logging.getLogger(__name__)


INTERNAL_SECRET_HEADER = "authorization"
BEARER_PREFIX = "Bearer "
USER_CREDENTIALS_HEADER = "x-user-credentials"
HEALTH_PATH = "/health"


class InternalSecretMiddleware:
    """Reject non-internal traffic when ``INTERNAL_SECRET`` is set.

    Accepts two Authorization header formats emitted by the platform gateway:

    * ``Bearer <INTERNAL_SECRET>`` — legacy/direct form.
    * ``Bearer Internal-gateway:<GATEWAY_SECRET>`` — gateway-prefixed form
      (used when ``GATEWAY_SECRET`` is set on the gateway side).

    On rejection, returns a valid JSON-RPC 2.0 error body with code -32001
    (server-defined unauthorized, mirrors Bitrix's -32000 shape).

    Env vars are read at request time so credential rotation works without
    restart.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Read at request time so rotation works without restart.
        secret = os.environ.get("INTERNAL_SECRET")
        gateway_secret = os.environ.get("GATEWAY_SECRET")

        if scope["type"] != "http" or not secret:
            await self.app(scope, receive, send)
            return

        # Allow health checks without auth so the orchestrator can probe.
        if scope.get("path") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return

        # Header names are case-insensitive (HTTP/1.1 RFC 7230 §3.2); lowercase
        # them for lookup. The value must match the Bearer prefix case-sensitively
        # to mirror the Bitrix reference (`auth !== \`Bearer ${secret}\``).
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        provided = headers.get(INTERNAL_SECRET_HEADER)

        expected_direct = f"{BEARER_PREFIX}{secret}"
        expected_gateway = (
            f"{BEARER_PREFIX}Internal-gateway:{gateway_secret}"
            if gateway_secret
            else None
        )

        if provided == expected_direct or (expected_gateway is not None and provided == expected_gateway):
            pass
        else:
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32001,
                        "message": "unauthorized: missing or invalid Authorization header",
                    },
                },
                status_code=401,
                media_type="application/json",
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class UserCredentialsMiddleware:
    """Extract ``x-user-credentials`` and install it in the ContextVar."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Health endpoint never needs creds — skip the parse work.
        if scope.get("path") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        creds_header = request.headers.get(USER_CREDENTIALS_HEADER)
        auth_token = None
        ors_token = None
        if creds_header:
            try:
                creds = json.loads(creds_header)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON in %s header", USER_CREDENTIALS_HEADER)
                creds = None
            if isinstance(creds, dict):
                email = creds.get("email")
                password = creds.get("password")
                if email and password:
                    auth = AuthManager(email=email, password=password)
                    auth_token = set_auth_manager(auth)
                ors_api_key = creds.get("ors_api_key")
                if ors_api_key:
                    ors_token = set_ors_api_key(ors_api_key)

        try:
            await self.app(scope, receive, send)
        finally:
            # Always reset — never let one tenant's creds outlive the request.
            if auth_token is not None:
                try:
                    reset_auth_manager(auth_token)
                except (ValueError, LookupError):
                    # ContextVar reset can fail across task boundaries; fall
                    # back to a hard clear so we don't leak state.
                    clear_request_state()
            if ors_token is not None:
                try:
                    reset_ors_api_key(ors_token)
                except (ValueError, LookupError):
                    clear_request_state()
            # Belt-and-braces: also drop the lazily-built KomootClient so the
            # next request doesn't see a stale instance bound to old creds.
            clear_request_state()
