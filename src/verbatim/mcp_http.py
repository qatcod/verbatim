"""HTTP transport for verbatim's MCP server.

Separate from mcp_server.py on purpose. That module owns the tools and the stdio path, which is how
Claude Code and Cursor use verbatim today; this module owns being reachable over a network, which is
a different concern with a different threat model. Keeping them apart means an HTTP change cannot
break stdio, and auth has exactly one home.
"""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send


def require_token(expected: str, presented: str | None) -> bool:
    """Whether a request may proceed.

    Constant-time comparison because this gates network access to a team's extracted state. And an
    EMPTY `expected` denies everything: an unset token must mean the service refuses, never that it
    serves anonymously. That is the direction this gets wrong when it gets wrong.
    """
    if not expected or presented is None:
        return False
    return hmac.compare_digest(expected, presented)


def _presented(headers: Headers) -> str | None:
    auth = headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[len("bearer ") :]
    return headers.get("x-api-key")


class _MCPEndpoint:
    """Raw-ASGI endpoint gating `manager.handle_request` behind `require_token`.

    Deliberately a callable class instance rather than a plain function. Starlette's `Route` treats a
    plain `async def handler(request)` as `func(request) -> Response` and wraps it in
    `request_response`, which awaits the function and then calls the *return value* as
    `response(scope, receive, send)`. But `manager.handle_request` already writes the ASGI response
    messages itself and returns `None` -- wrapped that way, Starlette would try to call `None(...)`
    once the request got past auth. A class instance takes Starlette's "endpoint is a class, treat it
    as ASGI" branch instead, handing us the raw `(scope, receive, send)` so we can either answer 401
    ourselves or hand the request straight to the manager.
    """

    def __init__(self, token: str, manager: StreamableHTTPSessionManager) -> None:
        self._token = token
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = Headers(scope=scope)
        if not require_token(self._token, _presented(headers)):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self._manager.handle_request(scope, receive, send)


async def _health(_request: Request) -> Response:
    return JSONResponse({"status": "ok", "transport": "streamable-http"})


def build_app(token: str) -> Starlette:
    """A Starlette app serving verbatim's MCP tools over HTTP.

    Raises if no token is configured. The caller cannot opt out of auth: there is no code path here
    that serves anonymously, because the one that would is the one somebody eventually ships.
    """
    if not token:
        raise ValueError("an HTTP MCP transport requires a token; refusing to serve anonymously")

    # Imported here, not at module level: this module (and require_token in particular) should be
    # importable -- and testable -- without pulling in mcp_server's tool registration at all.
    from .mcp_server import server  # the SAME Server instance the stdio path serves

    manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        # manager.run() creates the task group every session operation needs, and its own docstring
        # says it "can only be called once per instance" -- so it is entered exactly once here and
        # held open for the app's lifetime, not re-entered per request.
        async with manager.run():
            yield

    return Starlette(
        routes=[
            Route("/mcp", _MCPEndpoint(token, manager), methods=["POST", "GET"]),
            Route("/health", _health, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
