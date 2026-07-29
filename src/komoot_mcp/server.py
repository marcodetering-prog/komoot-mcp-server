"""Komoot MCP Server — browse, download, upload, and plan routes.

Runs as either:

* **stdio** for local single-user use (creds via env vars).
* **streamable-HTTP** behind the Eric AI platform gateway. The gateway
  forwards per-user creds via the ``x-user-credentials`` header and a
  shared ``Authorization: Bearer <INTERNAL_SECRET>`` token. See
  ``middleware.py`` for the wire format.
"""

import os

from mcp.server import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from komoot_mcp.tools import (
    auth_tools,
    browse_tools,
    collection_tools,
    data_tools,
    discover_tools,
    highlight_tools,
    routing_tools,
    share_tools,
    write_tools,
)


def create_server(host="127.0.0.1", port=8000):
    """Create and configure the MCP server with all tools registered.

    All per-tenant state (AuthManager, KomootClient) lives in a
    ContextVar, populated per-request by the Starlette middleware.
    Tools resolve dependencies via :mod:`komoot_mcp.context` — there
    is no module-level shared state.
    """
    mcp = FastMCP(
        "Komoot MCP Server",
        host=host,
        port=port,
        streamable_http_path="/mcp",
    )

    # Add health check endpoint
    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    # Register all tools. Tools pull their per-request dependencies
    # from ContextVar at call time, so no wiring step is needed.
    auth_tools.register(mcp)
    browse_tools.register(mcp)
    data_tools.register(mcp)
    write_tools.register(mcp)
    routing_tools.register(mcp)
    discover_tools.register(mcp)
    highlight_tools.register(mcp)
    collection_tools.register(mcp)
    share_tools.register(mcp)

    return mcp


def _run_http(mcp: FastMCP) -> None:
    """Mount middleware and serve via uvicorn.

    ``FastMCP.run(transport='streamable-http')`` builds its own
    Starlette app internally, leaving no seam to install middleware.
    We replicate the small bit of glue here so we can wrap the app in
    the platform-integration middleware stack.
    """
    import asyncio

    import uvicorn

    # Import locally so unit tests that don't need HTTP can skip these.
    from komoot_mcp.middleware import (
        InternalSecretMiddleware,
        UserCredentialsMiddleware,
    )

    app = mcp.streamable_http_app()

    # Order matters — outer middleware runs first. We want the secret
    # check to gate everything (including credential parsing), so it
    # goes on last (Starlette wraps outermost-last).
    app.add_middleware(UserCredentialsMiddleware)
    app.add_middleware(InternalSecretMiddleware)

    config = uvicorn.Config(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


def main():
    """Entry point for the MCP server."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", default="stdio", choices=["stdio", "http"])
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "3007")))
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"))
    args = parser.parse_args()

    mcp = create_server(host=args.host, port=args.port)

    if args.transport == "http":
        _run_http(mcp)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
