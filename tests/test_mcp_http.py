import asyncio

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from verbatim.mcp_http import build_app, require_token

# `build_app` lazily imports the real `server` from `verbatim.mcp_server` (see mcp_http.py) so that
# `require_token` stays importable and testable on its own. That module's tool registration uses the
# decorator API (`@server.list_tools()`, `@server.call_tool()`), which the installed mcp==2.3.0's
# `Server` dropped in favour of constructor-time `on_list_tools=`/`on_call_tool=` callables (its own
# deprecation notice dates the change to 2026-07-28, SEP-2577). Importing `verbatim.mcp_server` on its
# own, with zero HTTP code involved, reproduces the identical `AttributeError: 'Server' object has no
# attribute 'list_tools'` -- confirming this is pre-existing in mcp_server.py and unrelated to the
# transport built here. mcp_server.py is out of this module's file boundary to fix, so every test
# below that calls `build_app` with a non-empty token is marked `xfail(strict=True)` against it: a
# fix to mcp_server.py's tool registration should flip these to unexpected passes, which `strict=True`
# turns into a failure as a reminder to remove the marker. `build_app`'s own mechanics (lifespan/
# `run()`, the auth gate, a real `tools/list` round trip) were separately verified against a
# stand-in `Server` built with the current constructor API, by pre-seeding
# `sys.modules["verbatim.mcp_server"]` before import -- never touching mcp_server.py on disk. See the
# task-2 report for that verification, including mutating away the lifespan's `manager.run()` (the
# round trip then fails with the exact `RuntimeError: Task group is not initialized. Make sure to use
# run().` Task 1 predicted) and mutating away the auth check (the request then reaches the manager
# without a task group and errors, which still fails the 401 assertion).
_BLOCKED_BY_MCP_SERVER_DECORATOR_API = (
    "blocked by a pre-existing defect in verbatim/mcp_server.py (out of this module's file "
    "boundary): its @server.list_tools()/@server.call_tool() decorators raise AttributeError "
    "against the installed mcp==2.3.0, which replaced them with on_list_tools=/on_call_tool= "
    "constructor kwargs. Reproduces with `import verbatim.mcp_server` alone, no HTTP involved."
)


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


@pytest.mark.xfail(strict=True, reason=_BLOCKED_BY_MCP_SERVER_DECORATOR_API)
def test_unauthenticated_request_is_refused():
    # An HTTP listener has no header at all here -- no Authorization, no X-Api-Key.
    client = TestClient(build_app("s3cret"))
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


@pytest.mark.xfail(strict=True, reason=_BLOCKED_BY_MCP_SERVER_DECORATOR_API)
def test_wrong_token_is_refused():
    # Same length as "s3cret" (6 chars) -- a length-only comparison would let this through.
    client = TestClient(build_app("s3cret"))
    r = client.post(
        "/mcp",
        headers={"Authorization": "Bearer nope12"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 401


@pytest.mark.xfail(strict=True, reason=_BLOCKED_BY_MCP_SERVER_DECORATOR_API)
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
