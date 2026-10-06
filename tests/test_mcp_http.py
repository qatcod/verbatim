import asyncio

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from verbatim.mcp_http import build_app, require_token

# `build_app` lazily imports the real `server` from `verbatim.mcp_server` (see mcp_http.py) so that
# `require_token` stays importable and testable on its own. That import used to raise
# `AttributeError: 'Server' object has no attribute 'list_tools'`: mcp_server.py registered its tools
# with the 1.x `@server.list_tools()`/`@server.call_tool()` decorators, which mcp 2.0 removed in
# favour of `on_list_tools=`/`on_call_tool=` constructor kwargs (SEP-2577, 2026-07-28), and
# `pyproject.toml` admitted 2.x via an uncapped `mcp>=1.0.0`. The three tests below were
# `xfail(strict=True)` against that defect. mcp_server.py has since been migrated to the constructor
# API and the dependency capped at `mcp>=2.3.0,<3`, so they run for real -- a full MCP round trip
# through the app `build_app` returns, against the same `Server` instance the stdio path serves.


# ----------------------- require_token: pure-function auth -----------------------
#
# Auth is pulled out as a pure function deliberately: it is the thing most worth testing
# and least worth testing through a web server.


def test_rejects_a_missing_token():
    # An HTTP listener has no process-level trust the way stdio did. No header means no.
    assert require_token("s3cret", None) is False


def test_rejects_a_wrong_token_of_the_same_length():
    # Same length on purpose: a length comparison would pass a differing token of equal length,
    # and that mutation has to fail a test.
    assert require_token("s3cret", "s3crNO") is False


def test_accepts_the_configured_token():
    assert require_token("s3cret", "s3cret") is True


def test_refuses_to_authorise_when_no_token_is_configured():
    # The dangerous direction. An unset token must deny everything, never serve anonymously.
    assert require_token("", "anything") is False
    assert require_token("", "") is False
    assert require_token("", None) is False


# ----------------------- build_app: the HTTP transport itself -----------------------


def test_refuses_to_build_without_a_token():
    # Starting an anonymous listener over a team's state is the failure mode worth a hard stop.
    # This must fail before build_app ever touches mcp_server's tool registration.
    with pytest.raises(ValueError, match="token"):
        build_app("")


def test_unauthenticated_request_is_refused():
    # An HTTP listener has no header at all here -- no Authorization, no X-Api-Key.
    client = TestClient(build_app("s3cret"))
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_wrong_token_is_refused():
    # Same length as "s3cret" (6 chars) -- a length-only comparison would let this through.
    client = TestClient(build_app("s3cret"))
    r = client.post(
        "/mcp",
        headers={"Authorization": "Bearer nope12"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 401


def test_real_tool_call_through_the_transport():
    """A real MCP round trip over HTTP: initialize, then tools/list, through the SAME Starlette
    app `build_app` returns -- not a stand-in, not a direct call into the manager.

    This is the test the brief's 401-only suite was missing. `StreamableHTTPSessionManager.run()`
    is a required lifecycle context manager (its own docstring: "this creates and manages the task
    group for all session operations"); `manager.handle_request` raises `RuntimeError` without it.
    A suite that only exercises the 401 paths never calls `handle_request` on a real request and so
    never discovers that -- exactly the gap this test closes.

    Uses the mcp SDK's own streamable-http client against the app's ASGI callable via
    `httpx2.ASGITransport` (in-process, no real socket), with the app's lifespan entered explicitly
    via `app.router.lifespan_context` -- the same lifespan `build_app` wires up for a real server,
    just invoked directly instead of through a running event loop's startup/shutdown messages
    (Starlette's `TestClient` only sends those inside a `with TestClient(app) as client:` block, and
    the mcp client needs an async httpx2 client, not TestClient's sync one).
    """

    async def round_trip() -> list[str]:
        app = build_app("s3cret")
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                headers={"Authorization": "Bearer s3cret"},
            ) as http_client:
                async with streamable_http_client("http://testserver/mcp", http_client=http_client) as (
                    read,
                    write,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        return [t.name for t in result.tools]

    tool_names = asyncio.run(round_trip())
    assert "list_commitments" in tool_names
