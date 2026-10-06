import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from verbatim import state
from verbatim.mcp_http import build_app, require_token
from verbatim.mcp_server import parse_transport_args

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


def test_non_ascii_presented_token_is_refused_not_crashed():
    # hmac.compare_digest raises TypeError the moment either `str` argument has a non-ASCII
    # character -- and Starlette decodes request headers as latin-1, so a header byte like 0xe9
    # reaches `_presented`/`require_token` as exactly this kind of str, from any unauthenticated
    # caller. This has to come back False, not raise -- a crash here is a 500 with a traceback an
    # unauthenticated caller does not get to see, not a 401.
    assert require_token("s3cret", "s3cr\xe9t") is False


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


def test_non_ascii_authorization_header_is_401_not_500():
    """The same bug as the unit test above, through the real ASGI app rather than the pure function.

    Starlette's `TestClient` (and `httpx2.AsyncClient`) refuse to *send* a non-ASCII header value
    themselves -- their own request encoders raise before anything reaches the server -- so the
    only way to reproduce what an actual unauthenticated caller can do on the wire is to hand the
    app a raw ASGI scope with the header already as the bytes Starlette would have received, and
    call it directly.
    """
    app = build_app("s3cret")

    async def round_trip() -> list[dict]:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer s3cr\xe9t")],
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
        state_box = {"sent": False}

        async def receive():
            if not state_box["sent"]:
                state_box["sent"] = True
                return {"type": "http.request", "body": b"{}", "more_body": False}
            return {"type": "http.disconnect"}

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        async with app.router.lifespan_context(app):
            await app(scope, receive, send)
        return messages

    messages = asyncio.run(round_trip())
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    assert status == 401


def test_health_endpoint_answers_unauthenticated():
    # /health deliberately takes no token (adjudicated: healthchecks must work with no credential,
    # and the only thing it reveals -- that the process is up -- is also inferable from getting a
    # 401 at all; do not gate it). Nothing else in this suite calls the route, so deleting it
    # entirely left the rest of the suite green.
    client = TestClient(build_app("s3cret"))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "transport": "streamable-http"}


def test_x_api_key_header_is_accepted_as_a_fallback_credential():
    # The README documents `X-Api-Key` as a supported credential alongside `Authorization: Bearer`.
    # Every other test in this file authenticates with Bearer, so dropping the X-Api-Key branch in
    # `_presented` left the suite green until this test existed.
    #
    # Needs the `with` block (not a bare `TestClient(...)`): this is a request that gets past auth
    # and reaches `manager.handle_request`, which requires the app's lifespan (entered by `with`) to
    # have called `manager.run()` first -- unlike the 401-before-auth tests above, which never touch
    # the manager at all and so never needed it.
    with TestClient(build_app("s3cret")) as client:
        r = client.post(
            "/mcp",
            headers={"X-Api-Key": "s3cret"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert r.status_code == 200


# The seven tools mcp_server registers, in order. Asserted by equality, not membership: a
# membership check (`"list_commitments" in tool_names`) stays green while six of the seven
# silently disappear, which is not a property worth asserting.
_EXPECTED_TOOLS = [
    "list_commitments",
    "list_decisions",
    "list_open_questions",
    "list_blockers",
    "recent_sessions",
    "verbatim_stats",
    "resolve_entity",
]


@asynccontextmanager
async def _mcp_session(token: str = "s3cret") -> AsyncIterator[ClientSession]:
    """An initialised MCP client session speaking to the app `build_app` returns.

    Not a stand-in and not a direct call into the manager: the real Starlette app, driven by the mcp
    SDK's own streamable-http client over `httpx2.ASGITransport` (in-process, no real socket).

    The app's lifespan is entered explicitly via `app.router.lifespan_context`, because
    `StreamableHTTPSessionManager.run()` is a required lifecycle context manager -- its own docstring
    says it "creates and manages the task group for all session operations", and
    `manager.handle_request` raises `RuntimeError: Task group is not initialized` without it. A suite
    that only exercises the 401 paths never calls `handle_request` on a request that gets past auth,
    so it never discovers that. Starlette's `TestClient` would send the startup/shutdown messages
    only inside a `with TestClient(app)` block, and the mcp client needs an async httpx2 client
    rather than TestClient's sync one -- hence invoking the same lifespan directly.
    """
    app = build_app(token)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {token}"},
        ) as http_client:
            async with streamable_http_client("http://testserver/mcp", http_client=http_client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


def _seed_db(tmp_db_path, extraction, diagnostics, monkeypatch) -> None:
    """Point the tools at a throwaway DB holding one extraction.

    Setting `$VERBATIM_DB_PATH` is load-bearing, not tidiness: `mcp_server._call_tool` calls
    `state.open_db()` with no argument, which resolves that variable and otherwise falls back to the
    developer's real `~/.verbatim/state.db`. Same idiom as tests/test_store.py.

    Seeded rather than left empty on purpose. An all-zeros stats payload is also what a tool that
    reaches no database at all would produce, so an empty DB cannot distinguish a working tool from a
    silent one. The counts below are what conftest's `sample_extraction` contains.
    """
    monkeypatch.setenv("VERBATIM_DB_PATH", str(tmp_db_path))
    conn = state.open_db(tmp_db_path)
    try:
        state.save_extraction(conn, extraction, diagnostics, source_path="meeting.txt")
    finally:
        conn.close()


def test_tools_list_returns_exactly_the_seven_tools():
    """A real MCP round trip over HTTP: initialize, then tools/list."""

    async def round_trip() -> list[str]:
        async with _mcp_session() as session:
            result = await session.list_tools()
            return [t.name for t in result.tools]

    assert asyncio.run(round_trip()) == _EXPECTED_TOOLS


def test_tool_call_returns_that_tools_own_content(
    tmp_db_path, sample_extraction, sample_diagnostics, monkeypatch
):
    """tools/call, not just tools/list -- and asserted on the content, not on a response arriving.

    `verbatim_stats` because its output is `json.dumps` of the stats row: an exact, parseable
    assertion with no coupling to prose formatting. The numbers are the ones conftest's
    `sample_extraction` seeds, so they are specific to this tool having actually run its SQL against
    the seeded database -- the point a `tools/list`-only suite misses entirely, since
    `tools/list` keeps working with `on_call_tool=` unregistered.
    """
    _seed_db(tmp_db_path, sample_extraction, sample_diagnostics, monkeypatch)

    async def round_trip():
        async with _mcp_session() as session:
            return await session.call_tool("verbatim_stats", {})

    result = asyncio.run(round_trip())

    assert result.is_error is False
    stats = json.loads(result.content[0].text)
    assert stats["sessions"] == 1
    assert stats["commitments_open"] == 2
    assert stats["decisions_open"] == 1
    assert stats["open_questions_open"] == 1
    assert stats["blockers_open"] == 1


def test_tool_call_arguments_reach_the_query(
    tmp_db_path, sample_extraction, sample_diagnostics, monkeypatch
):
    """Arguments have to survive the transport, not just the tool name.

    `sample_extraction` holds two open commitments -- Alice's at high confidence, Carol's at medium.
    `min_confidence: "high"` must therefore return Alice's and not Carol's: a result that is wrong
    in both directions if the argument is dropped (two commitments) or the filter misapplied (none).
    Also asserts the verbatim quote comes back, since quote-sourcing is the guarantee the tool
    exists to provide. Entity ids are uuids, so they are deliberately not asserted on.
    """
    _seed_db(tmp_db_path, sample_extraction, sample_diagnostics, monkeypatch)

    async def round_trip():
        async with _mcp_session() as session:
            return await session.call_tool("list_commitments", {"min_confidence": "high", "limit": 50})

    text = asyncio.run(round_trip()).content[0].text

    assert "1 commitment(s):" in text
    assert "**Alice** — v0 of Verbatim CLI by EOD Wednesday" in text
    assert "I'll have a working version by end of day Wednesday." in text
    assert "Carol" not in text


def test_bad_tool_arguments_come_back_as_a_tool_error_not_a_client_exception(
    tmp_db_path, sample_extraction, sample_diagnostics, monkeypatch
):
    """`resolve_entity` with no `entity_id` must surface as CallToolResult(isError=True), not raise.

    mcp 1.x's `@server.call_tool()` decorator caught handler exceptions into
    `CallToolResult(isError=True, ...)`; mcp 2.x's `on_call_tool=` constructor kwarg does not, so an
    uncaught `KeyError` from `args["entity_id"]` used to become an `mcp.shared.exceptions.MCPError`
    that the *client* raises -- a tool error the model could have recovered from (missing argument,
    try again) instead becomes a hard failure of the whole call. Removing `_call_tool`'s try/except
    around `_dispatch_tool` reproduces exactly that: this test raises instead of asserting.
    """
    _seed_db(tmp_db_path, sample_extraction, sample_diagnostics, monkeypatch)

    async def round_trip():
        async with _mcp_session() as session:
            return await session.call_tool("resolve_entity", {})

    result = asyncio.run(round_trip())

    assert result.is_error is True
    assert "entity_id" in result.content[0].text


# ----------------------- parse_transport_args: the entry point's transport switch -----------------------
#
# The important assertion is the one about the DEFAULT. Claude Code and Cursor invoke `verbatim-mcp`
# with no arguments today, and this task must not change what that does.


def test_default_is_stdio_so_existing_clients_are_untouched():
    assert parse_transport_args([]).transport == "stdio"


def test_http_is_opt_in():
    args = parse_transport_args(["--transport", "http", "--port", "9001"])
    assert args.transport == "http"
    assert args.port == 9001


def test_http_port_has_a_default():
    assert parse_transport_args(["--transport", "http"]).port == 8081


def test_http_host_defaults_to_local_only():
    # A developer running `verbatim-mcp --transport http` on a laptop to try it out should not
    # bind the LAN by default -- that is the Dockerfile CMD's job (`--host 0.0.0.0`, explicit,
    # because binding every interface is the point in a container). Nothing previously asserted
    # this value at all, so it silently defaulted to 0.0.0.0 even for the CLI.
    assert parse_transport_args(["--transport", "http"]).host == "127.0.0.1"
