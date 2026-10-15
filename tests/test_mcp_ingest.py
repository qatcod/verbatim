"""Tests for the `ingest_transcript` MCP tool — the one WRITE tool.

The seven read tools are microsecond SQLite queries. This one makes a paid LLM call that takes tens
of seconds and is called by automation that retries, which is why idempotence and serialisation are
most of what is tested here rather than output formatting.

ON THE FAKE EXTRACTOR, because the fake is the part that decides whether these tests can fail.
`fake_extract` counts its own calls and sleeps. The count is what makes "did not re-extract"
observable at all: without it, a replay that silently re-ran the model would still return the same
counts from the database and every assertion would pass. The sleep is what makes the concurrency
test real: four coroutines that each complete instantly can serialise by accident on the event loop
and the test would pass against a missing lock.
"""
import asyncio
import pathlib
import sqlite3
import time

import pytest

from verbatim import mcp_server, store
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Blocker,
    Commitment,
    Confidence,
    Decision,
    ExtractionResult,
    OpenQuestion,
    SourceReference,
)


def _source(quote: str = "I'll ship it") -> SourceReference:
    return SourceReference(verbatim_quote=quote, rationale="explicit")


def _result(*, empty: bool = False) -> ExtractionResult:
    if empty:
        # A meeting where nobody committed to anything. Ordinary, not an error.
        return ExtractionResult(meeting_summary="chat", participants=["ana"])
    return ExtractionResult(
        meeting_summary="m",
        participants=["ana", "dan"],
        commitments=[
            Commitment(
                actor="ana", deliverable="ship it", confidence=Confidence.HIGH, sources=[_source()]
            )
        ],
        decisions=[
            Decision(topic="db", outcome="postgres", confidence=Confidence.HIGH, sources=[_source()])
        ],
        open_questions=[
            OpenQuestion(topic="scope", question="how big?", confidence=Confidence.MEDIUM,
                         sources=[_source()])
        ],
        blockers=[
            Blocker(blocked_thing="the export", blocked_by="no access",
                    confidence=Confidence.HIGH, sources=[_source()])
        ],
    )


class FakeExtractor:
    """Counts calls, and can be made slow or made to fail."""

    def __init__(self, *, delay: float = 0.0, empty: bool = False, boom: bool = False) -> None:
        self.calls = 0
        self._delay = delay
        self._empty = empty
        self._boom = boom

    def __call__(self, transcript: str, **kwargs):
        self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        if self._boom:
            raise RuntimeError("model unreachable")
        diag = ExtractionDiagnostics(
            model="fake", input_tokens=1, output_tokens=1,
            stop_reason="end_turn", transcript_chars=len(transcript),
        )
        return _result(empty=self._empty), diag


@pytest.fixture()
def conn(tmp_path: pathlib.Path):
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


def _patch(monkeypatch, fake: FakeExtractor) -> None:
    # Patched on the module the tool imports FROM, because `_ingest_transcript` does
    # `from .extractor import extract` at call time — patching a name on mcp_server would not be
    # seen by that import.
    monkeypatch.setattr("verbatim.extractor.extract", fake)


async def _ingest_text(conn, args) -> str:
    """The prose half of the result.

    `_ingest_transcript` returns (text, structured) since structured output was added. These tests
    were written against the text and still assert the right things about it; the structured half has
    its own section at the end of this file.
    """
    text, _structured = await mcp_server._ingest_transcript(conn, args)
    return text


def _session_id(output: str) -> str:
    return output.splitlines()[0].split()[-1].rstrip(".")


# ----------------------- the tool is registered -----------------------


def test_ingest_transcript_is_advertised_with_a_schema():
    async def _run():
        # The HTTP transport is useless for ingestion if the tool is not in the list a client reads.
        result = await mcp_server._list_tools(None, None)
        tool = next((t for t in result.tools if t.name == "ingest_transcript"), None)
        assert tool is not None, "ingest_transcript must be advertised"
        assert tool.input_schema["required"] == ["text"]
        assert set(tool.input_schema["properties"]) >= {"text", "external_id", "source_label"}

    asyncio.run(_run())


# ----------------------- idempotence -----------------------


def test_a_repeat_with_the_same_external_id_does_not_re_extract(conn, monkeypatch):
    async def _run():
        # The assertion that matters most. Convaa's ingest hook is at-least-once by construction: a
        # lease that expires while an extraction is still running produces a second call for the same
        # transcript. Without this, that writes a second session plus a full duplicate set of entities.
        fake = FakeExtractor()
        _patch(monkeypatch, fake)

        first = await _ingest_text(conn, {"text": "t", "external_id": "tx-1"})
        second = await _ingest_text(conn, {"text": "t", "external_id": "tx-1"})

        assert fake.calls == 1, "the second ingest must not pay for another model call"
        assert _session_id(first) == _session_id(second)
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        # And no duplicate entities behind the identical session id.
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 4

    asyncio.run(_run())


def test_a_repeat_reports_the_first_run_s_counts(conn, monkeypatch):
    async def _run():
        fake = FakeExtractor()
        _patch(monkeypatch, fake)
        await _ingest_text(conn, {"text": "t", "external_id": "tx-1"})
        replay = await _ingest_text(conn, {"text": "t", "external_id": "tx-1"})

        # Not an error: the caller's retry was correct behaviour, it could not know the first had
        # landed. So it gets the original answer, marked as a replay.
        assert "Already ingested" in replay
        assert "commitments: 1" in replay
        assert "decisions: 1" in replay

    asyncio.run(_run())


def test_without_an_external_id_each_ingest_is_its_own_session(conn, monkeypatch):
    async def _run():
        # A human running `verbatim ingest` twice means it twice. Idempotence is opt-in, via the id.
        fake = FakeExtractor()
        _patch(monkeypatch, fake)
        a = await _ingest_text(conn, {"text": "t"})
        b = await _ingest_text(conn, {"text": "t"})
        assert fake.calls == 2
        assert _session_id(a) != _session_id(b)

    asyncio.run(_run())


def test_different_external_ids_are_different_sessions(conn, monkeypatch):
    async def _run():
        fake = FakeExtractor()
        _patch(monkeypatch, fake)
        a = await _ingest_text(conn, {"text": "t", "external_id": "tx-1"})
        b = await _ingest_text(conn, {"text": "t", "external_id": "tx-2"})
        assert fake.calls == 2
        assert _session_id(a) != _session_id(b)

    asyncio.run(_run())


def test_a_replay_answers_without_waiting_for_an_extraction_in_flight(conn, monkeypatch):
    """A known external_id is answered BEFORE the lock is taken, not behind it.

    This is what the pre-lock check buys, and why it is not redundant with the identical check
    inside the lock: correctness is the same either way, but without it every retry queues behind
    whatever extraction is running. Convaa's hook retries on a timer across many transcripts, so
    "replays wait for the current model call" means a retry storm serialises behind real work.
    """
    fake = FakeExtractor(delay=0.6)
    _patch(monkeypatch, fake)

    async def _run():
        # One transcript already ingested.
        first = await _ingest_text(conn, {"text": "t", "external_id": "done"})

        # A different, slow ingest now holds the lock.
        inflight = asyncio.create_task(
            _ingest_text(conn, {"text": "t2", "external_id": "other"})
        )
        await asyncio.sleep(0.1)          # let it get inside the lock
        assert not inflight.done()

        started = time.monotonic()
        replay = await _ingest_text(conn, {"text": "t", "external_id": "done"})
        waited = time.monotonic() - started

        await inflight
        assert "Already ingested" in replay
        assert _session_id(replay) == _session_id(first)
        # The in-flight extraction still has ~0.5s to run. Anything close to that means the replay
        # queued behind it.
        assert waited < 0.2, f"the replay waited {waited:.2f}s for the lock"

    asyncio.run(_run())


# ----------------------- serialisation -----------------------


def test_concurrent_ingests_of_one_id_extract_exactly_once(conn, monkeypatch):
    async def _run():
        # The pre-check cannot catch this: all four coroutines reach it before any of them has saved.
        # Only the re-check INSIDE the lock collapses them. The extractor sleeps so the four really do
        # overlap — with an instant fake they could serialise by accident and pass against no lock.
        fake = FakeExtractor(delay=0.2)
        _patch(monkeypatch, fake)

        outs = await asyncio.gather(
            *[_ingest_text(conn, {"text": "t", "external_id": "same"}) for _ in range(4)]
        )

        assert fake.calls == 1, f"4 concurrent ingests caused {fake.calls} extractions"
        assert len({_session_id(o) for o in outs}) == 1
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1

    asyncio.run(_run())


def test_concurrent_ingests_of_different_ids_do_not_overlap(conn, monkeypatch):
    async def _run():
        # Serialisation is the point, not just dedupe: two DIFFERENT transcripts must still take the
        # 4-slot host one at a time. Measured by wall clock, which is the only thing that shows it.
        fake = FakeExtractor(delay=0.25)
        _patch(monkeypatch, fake)

        started = time.monotonic()
        await asyncio.gather(
            _ingest_text(conn, {"text": "a", "external_id": "x"}),
            _ingest_text(conn, {"text": "b", "external_id": "y"}),
        )
        elapsed = time.monotonic() - started

        assert fake.calls == 2
        # Serial is ~0.50s, parallel ~0.25s. 0.4 separates them with room for a slow machine.
        assert elapsed >= 0.4, f"the two extractions overlapped ({elapsed:.2f}s)"

    asyncio.run(_run())


# ----------------------- ordinary and failing outcomes -----------------------


def test_a_meeting_with_nothing_to_extract_is_not_an_error(conn, monkeypatch):
    async def _run():
        # The spec is explicit: "a meeting with no commitments is ordinary". Counts are printed as
        # explicit zeros so a caller can tell this from a failure.
        _patch(monkeypatch, FakeExtractor(empty=True))
        out = await _ingest_text(conn, {"text": "just chatting"})
        assert "commitments: 0" in out
        assert "blockers: 0" in out

    asyncio.run(_run())


def test_an_extraction_failure_leaves_no_session_behind(conn, monkeypatch):
    async def _run():
        # Half-written state is the one outcome that must not happen: the caller will retry, and a
        # partial session would make the retry a duplicate.
        _patch(monkeypatch, FakeExtractor(boom=True))
        with pytest.raises(RuntimeError):
            await _ingest_text(conn, {"text": "t", "external_id": "tx-1"})
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    asyncio.run(_run())


def test_a_failed_ingest_can_be_retried(conn, monkeypatch):
    async def _run():
        # The external_id must not be burned by a failure — otherwise the first transient model error
        # would permanently poison that transcript.
        _patch(monkeypatch, FakeExtractor(boom=True))
        with pytest.raises(RuntimeError):
            await _ingest_text(conn, {"text": "t", "external_id": "tx-1"})

        good = FakeExtractor()
        _patch(monkeypatch, good)
        out = await _ingest_text(conn, {"text": "t", "external_id": "tx-1"})
        assert good.calls == 1
        assert "Ingested 4 item(s)" in out

    asyncio.run(_run())


# ----------------------- argument validation -----------------------
#
# By hand, because mcp 2.x does NOT validate against inputSchema — the same gap documented on
# _call_tool for exceptions.


@pytest.mark.parametrize(
    "args",
    [
        {},                                    # text missing entirely
        {"text": ""},                          # empty
        {"text": "   \n  "},                   # whitespace only
        {"text": None},                        # explicit null, which a JSON client can send
        {"text": 42},                          # wrong type
        {"text": "ok", "external_id": 5},      # wrong type on the identity field
        {"text": "ok", "source_label": 7},
    ],
)
def test_bad_arguments_are_rejected(conn, monkeypatch, args):
    async def _run():
        fake = FakeExtractor()
        _patch(monkeypatch, fake)
        with pytest.raises(ValueError):
            await _ingest_text(conn, args)
        assert fake.calls == 0, "a bad request must not reach the model"

    asyncio.run(_run())


# ----------------------- a cross-process write -----------------------


def test_a_race_lost_to_another_process_returns_that_run_s_result(conn, tmp_path, monkeypatch):
    """The in-process lock cannot see another container; the unique index is the backstop.

    Simulated by writing the row from inside the extraction, which is where a second replica's
    write would land: after this ingest's lock-held re-check and before its own save.

    The interloper opens its OWN connection, inside the thread, because the extraction runs via
    asyncio.to_thread and sqlite3 connections cannot cross threads (check_same_thread defaults to
    True). That is the same constraint the production code is shaped around -- a first version of
    this test reused the fixture's connection and died on exactly that ProgrammingError.
    """
    db = tmp_path / "state.db"

    class Interloper(FakeExtractor):
        def __call__(self, transcript: str, **kwargs):
            other = store.connect(db)
            try:
                store.insert_session(
                    other, source_path=None, source_kind="transcript", model="other",
                    meeting_summary="theirs", participants=[], external_id="tx-1",
                )
            finally:
                other.close()
            return super().__call__(transcript, **kwargs)

    fake = Interloper()
    _patch(monkeypatch, fake)

    async def _run():
        out = await _ingest_text(conn, {"text": "t", "external_id": "tx-1"})
        assert "Already ingested" in out, "losing the race must return the winner's result, not raise"

    asyncio.run(_run())
    assert fake.calls == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_the_unique_index_exists_on_a_fresh_database(tmp_path):
    c = store.connect(tmp_path / "fresh.db")
    try:
        names = {r["name"] for r in c.execute("PRAGMA index_list(sessions)")}
        assert "idx_sessions_external_id" in names
    finally:
        c.close()


def test_an_existing_database_is_migrated_on_open(tmp_path):
    """The path that broke first: the index must be created by _migrate, not by SCHEMA.

    connect() runs executescript(SCHEMA) BEFORE _migrate(), so on a pre-existing database the
    CREATE TABLE is a no-op and a CREATE INDEX in SCHEMA references a column that does not exist
    yet -- `no such column: external_id`, on every open, for every existing install.

    The old database is built by executing the HISTORICAL `sessions` DDL, not by adding the column
    and dropping it again. That shortcut is what a real old database is not, and it was also
    version-fragile: `ALTER TABLE ... DROP COLUMN` rewrites the stored DDL text, and on CI's older
    SQLite the rewrite left this table's inline column comment swallowing the closing paren
    (`error in table sessions after drop column: incomplete input`). It passed on macOS's SQLite
    3.53 and failed on all three CI versions.
    """
    p = tmp_path / "old.db"

    # Exactly the sessions table as it stood before external_id existed.
    old_ddl = """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        source_path TEXT,
        source_kind TEXT NOT NULL,
        model TEXT NOT NULL,
        meeting_summary TEXT,
        participants_json TEXT NOT NULL DEFAULT '[]',
        extracted_at TEXT NOT NULL,
        transcript_chars INTEGER,
        input_tokens INTEGER,
        output_tokens INTEGER
    );
    """
    raw = sqlite3.connect(p)
    try:
        raw.executescript(old_ddl)
        raw.execute(
            "INSERT INTO sessions (id, source_kind, model, meeting_summary, extracted_at) "
            "VALUES ('s1', 'transcript', 'm', 'pre-existing', '2026-01-01T00:00:00Z')"
        )
        raw.commit()
        cols = {r[1] for r in raw.execute("PRAGMA table_info(sessions)")}
        assert "external_id" not in cols, "the fixture must start WITHOUT the column"
    finally:
        raw.close()

    c = store.connect(p)  # must not raise, and must migrate in place
    try:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
        names = {r["name"] for r in c.execute("PRAGMA index_list(sessions)")}
        assert "external_id" in cols
        assert "idx_sessions_external_id" in names
        # The existing row survives the migration.
        assert c.execute("SELECT meeting_summary FROM sessions").fetchone()[0] == "pre-existing"
        # And uniqueness is enforced on the migrated DB, not only a fresh one.
        store.insert_session(c, source_path=None, source_kind="transcript", model="m",
                             meeting_summary="s", participants=[], external_id="dup")
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_session(c, source_path=None, source_kind="transcript", model="m",
                                 meeting_summary="s", participants=[], external_id="dup")
    finally:
        c.close()


def test_a_hand_run_ingest_never_collides_on_a_null_external_id(tmp_path):
    # The index is partial for this reason: without the WHERE clause the second hand-run ingest
    # would fail on a duplicate NULL in a stricter engine, and the semantics would be wrong anyway.
    c = store.connect(tmp_path / "n.db")
    try:
        for _ in range(3):
            store.insert_session(c, source_path=None, source_kind="transcript", model="m",
                                 meeting_summary="s", participants=[], external_id=None)
        assert c.execute(
            "SELECT COUNT(*) FROM sessions WHERE external_id IS NULL"
        ).fetchone()[0] == 3
    finally:
        c.close()


def test_counts_are_zero_filled(tmp_path):
    # A replay comparing counts must not see a missing key where a kind was simply empty.
    c = store.connect(tmp_path / "z.db")
    try:
        sid = store.insert_session(c, source_path=None, source_kind="transcript", model="m",
                                   meeting_summary="s", participants=[])
        assert store.count_entities_by_kind(c, sid) == {
            "commitment": 0, "decision": 0, "open_question": 0, "blocker": 0,
        }
    finally:
        c.close()

# ----------------------- structured output -----------------------
#
# The reason this exists: the consumer is a PROGRAM (Convaa's thread renderer), and the thing it must
# do before posting an item to a team is check that the item's quote is a real substring of the
# transcript. The read tools return Markdown prose for a model to read -- regex-parsing an owner, a
# deadline and a quote back out of a rendered sentence is not a foundation for that check.


def test_the_structured_half_carries_this_session_s_items(conn, monkeypatch):
    fake = FakeExtractor()
    _patch(monkeypatch, fake)

    async def _run():
        _text, structured = await mcp_server._ingest_transcript(conn, {"text": "t", "external_id": "x"})
        assert structured["replayed"] is False
        assert structured["session_id"]
        # All four kinds, under stable wire names a consumer's parser is keyed on.
        assert set(structured) >= {"session_id", "replayed", "commitments", "decisions", "open_questions", "blockers"}
        c = structured["commitments"][0]
        # Fields, not a sentence.
        assert c["actor"] == "ana"
        assert c["deliverable"] == "ship it"
        # Lowercase, which is how it is stored -- worth asserting rather than normalising,
        # because a consumer branching on confidence has to match what actually arrives.
        assert c["confidence"] == "high"
        # And the quotes, which are the whole point — an item whose quote is not in the transcript
        # has to be droppable by the consumer.
        assert c["quotes"] == ["I'll ship it"]

    asyncio.run(_run())


def test_a_replay_returns_the_same_structured_items(conn, monkeypatch):
    # A retry must give the consumer the same items, not an empty set: the thread renderer may be
    # running on the retry, and "verbatim already has it" must not mean "there is nothing to post".
    fake = FakeExtractor()
    _patch(monkeypatch, fake)

    async def _run():
        _t1, first = await mcp_server._ingest_transcript(conn, {"text": "t", "external_id": "x"})
        _t2, replay = await mcp_server._ingest_transcript(conn, {"text": "t", "external_id": "x"})
        assert fake.calls == 1
        assert replay["replayed"] is True
        assert replay["session_id"] == first["session_id"]
        assert replay["commitments"] == first["commitments"]
        assert replay["blockers"] == first["blockers"]

    asyncio.run(_run())


def test_an_empty_meeting_returns_empty_lists_not_missing_keys(conn, monkeypatch):
    # Same reasoning as the zero-filled counts: a consumer reading `structured["commitments"]` must
    # not have to distinguish "no commitments" from "the key is gone".
    _patch(monkeypatch, FakeExtractor(empty=True))

    async def _run():
        _text, structured = await mcp_server._ingest_transcript(conn, {"text": "just chatting"})
        for key in ("commitments", "decisions", "open_questions", "blockers"):
            assert structured[key] == [], key

    asyncio.run(_run())


def test_the_items_are_scoped_to_this_session(conn, monkeypatch):
    # The gap this closes: the read tools filter by actor/confidence/status but NOT by session, so
    # "the items from THIS meeting" was not a question the MCP surface could answer. Two ingests must
    # not see each other's items.
    fake = FakeExtractor()
    _patch(monkeypatch, fake)

    async def _run():
        _t1, a = await mcp_server._ingest_transcript(conn, {"text": "t", "external_id": "m1"})
        _t2, b = await mcp_server._ingest_transcript(conn, {"text": "t", "external_id": "m2"})
        assert a["session_id"] != b["session_id"]
        assert len(a["commitments"]) == 1
        assert len(b["commitments"]) == 1
        assert a["commitments"][0]["id"] != b["commitments"][0]["id"]

    asyncio.run(_run())


def test_the_tool_result_carries_structured_content(conn, monkeypatch, tmp_path):
    # Through _call_tool, which is what an MCP client actually receives — the handler returning a
    # tuple is useless if the result does not carry it.
    import mcp.types as types

    monkeypatch.setenv("VERBATIM_DB_PATH", str(tmp_path / "s.db"))
    _patch(monkeypatch, FakeExtractor())

    async def _run():
        params = types.CallToolRequestParams(
            name="ingest_transcript", arguments={"text": "t", "external_id": "x"}
        )
        result = await mcp_server._call_tool(None, params)
        assert not result.is_error   # mcp 2.x field name; `isError` is only a serialisation alias
        assert result.structured_content is not None
        assert result.structured_content["commitments"][0]["actor"] == "ana"
        # The prose is kept as well, so a model calling the same tool still gets something readable.
        assert "Ingested" in result.content[0].text

    asyncio.run(_run())


def test_a_read_tool_carries_no_structured_content(conn, monkeypatch, tmp_path):
    # structured_content is initialised per call; a read tool must not inherit the previous call's.
    import mcp.types as types

    monkeypatch.setenv("VERBATIM_DB_PATH", str(tmp_path / "s.db"))

    async def _run():
        params = types.CallToolRequestParams(name="verbatim_stats", arguments={})
        result = await mcp_server._call_tool(None, params)
        assert result.structured_content is None

    asyncio.run(_run())

def test_an_item_merged_into_an_older_canonical_is_still_reported(conn, monkeypatch):
    """Reconciliation must not make a meeting's own item vanish from its thread render.

    This is what `canonical_only=False` buys, and why the default view is wrong here. The read tools
    show canonical entities only — the right view for "what is live team-wide", because it folds
    duplicates. But this result answers a different question: "what did THIS extraction produce".
    If a commitment from this meeting gets merged into an older canonical (verbatim does that when a
    similar item already exists), the canonical view hides it — and the thread would silently post
    one item fewer than the meeting actually contained, with nothing logged.

    Mutation-tested: flipping canonical_only to True fails only this test, which is why it is here.
    """
    fake = FakeExtractor()
    _patch(monkeypatch, fake)

    async def _run():
        _t, first = await mcp_server._ingest_transcript(conn, {"text": "t", "external_id": "m1"})
        item_id = first["commitments"][0]["id"]

        # A second, unrelated session whose commitment becomes the canonical, then merge this
        # meeting's item into it — exactly the shape auto-reconcile produces.
        other = store.insert_session(
            conn, source_path=None, source_kind="transcript", model="m",
            meeting_summary="older", participants=[],
        )
        canonical = store.insert_entity(
            conn, session_id=other, kind="commitment", confidence="high",
            payload={"actor": "ana", "deliverable": "ship it"},
            primary_actor="ana", primary_topic="ship it",
        )
        store.set_canonical(conn, item_id, canonical)

        # Re-read through the replay path, which is what a retry (or the sweep) would do.
        _t2, replay = await mcp_server._ingest_transcript(conn, {"text": "t", "external_id": "m1"})
        ids = [c["id"] for c in replay["commitments"]]
        assert item_id in ids, "a merged item disappeared from its own meeting's result"

    asyncio.run(_run())
