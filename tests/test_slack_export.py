"""Slack export connector tests — parsing, filtering, transcript rendering."""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from verbatim.connectors import slack_export

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "slack_export_dir"


# ----- loading -----


def test_load_from_directory() -> None:
    export = slack_export.load(FIXTURE_DIR)
    assert "U001" in export.users
    assert export.users["U001"] == "alice"
    assert export.users["U002"] == "bob"
    # U003 has no display_name, falls back to real_name
    assert export.users["U003"] == "Carol Carter"
    assert "general" in export.channels
    assert "random" in export.channels
    export.close()


def test_load_from_zip(tmp_path: Path) -> None:
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for p in FIXTURE_DIR.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(FIXTURE_DIR))
    export = slack_export.load(zip_path)
    assert "U001" in export.users
    assert "general" in export.channels
    export.close()


def test_load_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        slack_export.load(tmp_path / "nope.zip")


def test_load_rejects_random_file(tmp_path: Path) -> None:
    p = tmp_path / "not_a_slack_export.txt"
    p.write_text("hi")
    with pytest.raises(ValueError):
        slack_export.load(p)


# ----- thread building -----


def test_threads_are_built_with_correct_messages() -> None:
    export = slack_export.load(FIXTURE_DIR)
    units = list(export.iter_units(min_thread_messages=3))
    export.close()
    threads = [u for u in units if u.kind == "thread"]
    assert len(threads) == 1
    t = threads[0]
    assert t.channel == "general"
    assert len(t.messages) == 4
    # messages sorted by timestamp
    assert t.messages[0].text.startswith("Should we use Postgres")
    assert t.messages[-1].text.startswith("Sounds good")


def test_min_thread_messages_filter() -> None:
    export = slack_export.load(FIXTURE_DIR)
    units = list(export.iter_units(min_thread_messages=10))
    export.close()
    # the fixture's only thread has 4 messages, so threshold=10 drops it
    assert all(u.kind != "thread" for u in units)


def test_channel_filter() -> None:
    export = slack_export.load(FIXTURE_DIR)
    units = list(export.iter_units(channels=["general"]))
    export.close()
    assert all(u.channel == "general" for u in units)


def test_since_filter() -> None:
    export = slack_export.load(FIXTURE_DIR)
    # Fixture timestamps decode to 2025-05-18; since=2025-05-19 should drop everything.
    since = datetime(2025, 5, 19, tzinfo=timezone.utc)
    units = list(export.iter_units(since=since))
    export.close()
    assert units == []


def test_skips_noise_subtypes() -> None:
    export = slack_export.load(FIXTURE_DIR)
    units = list(
        export.iter_units(min_thread_messages=3, include_loose_messages=True)
    )
    export.close()
    # The channel_join message must not appear anywhere
    for u in units:
        for m in u.messages:
            assert m.subtype != "channel_join"


# ----- loose-message rollups -----


def test_loose_messages_off_by_default() -> None:
    export = slack_export.load(FIXTURE_DIR)
    units = list(export.iter_units(min_thread_messages=3))
    export.close()
    assert all(u.kind != "channel_day" for u in units)


def test_loose_messages_when_enabled() -> None:
    export = slack_export.load(FIXTURE_DIR)
    units = list(
        export.iter_units(min_thread_messages=3, include_loose_messages=True)
    )
    export.close()
    rollups = [u for u in units if u.kind == "channel_day"]
    # general has 2 non-threaded messages on 2026-05-18; threshold for rollup
    # is 2, so we expect one rollup. random only has 1 loose message, skipped.
    assert len(rollups) == 1
    assert rollups[0].channel == "general"


# ----- transcript rendering -----


def test_transcript_contains_channel_header() -> None:
    export = slack_export.load(FIXTURE_DIR)
    unit = next(export.iter_units(min_thread_messages=3))
    export.close()
    text = unit.transcript
    assert text.startswith("Channel: #general")
    assert "Thread started 2025-05-18" in text


def test_transcript_resolves_user_ids_to_names() -> None:
    export = slack_export.load(FIXTURE_DIR)
    unit = next(export.iter_units(min_thread_messages=3))
    export.close()
    text = unit.transcript
    assert "@alice:" in text
    assert "@bob:" in text
    # in-message @mention <@U001> should resolve too
    assert "<@U001>" not in text
    assert "@alice" in text


def test_source_label_format() -> None:
    export = slack_export.load(FIXTURE_DIR)
    unit = next(export.iter_units(min_thread_messages=3))
    export.close()
    label = unit.source_label
    assert label.startswith("slack://#general/thread/")
    assert "2025-05-18" in label


def test_source_kind_for_thread_and_rollup() -> None:
    export = slack_export.load(FIXTURE_DIR)
    units = list(
        export.iter_units(min_thread_messages=3, include_loose_messages=True)
    )
    export.close()
    kinds = {u.source_kind for u in units}
    assert "slack_thread" in kinds
    assert "slack_channel_day" in kinds


# ----- low-level helpers -----


def test_parse_message_drops_non_message_type() -> None:
    raw = {"type": "channel_marker", "text": "x", "ts": "1.0"}
    assert slack_export._parse_message(raw) is None


def test_parse_message_drops_noise_subtype() -> None:
    raw = {
        "type": "message", "subtype": "channel_join",
        "text": "x", "ts": "1.0",
    }
    assert slack_export._parse_message(raw) is None


def test_parse_message_drops_missing_ts() -> None:
    raw = {"type": "message", "text": "x"}
    assert slack_export._parse_message(raw) is None


def test_user_map_picks_best_name() -> None:
    data = [
        {"id": "U1", "profile": {"display_name_normalized": "alice"}, "name": "alice_lowname"},
        {"id": "U2", "profile": {"real_name": "Bob B"}, "name": "bob_lowname"},
        {"id": "U3", "name": "carol_only"},
        {"id": "U4"},
    ]
    out = slack_export._build_user_map(data)
    assert out["U1"] == "alice"
    assert out["U2"] == "Bob B"
    assert out["U3"] == "carol_only"
    assert out["U4"] == "U4"  # falls back to id


def test_user_map_handles_garbage() -> None:
    assert slack_export._build_user_map("not a list") == {}
    assert slack_export._build_user_map([{"profile": {}}]) == {}  # no id


def test_user_mention_replacement() -> None:
    users = {"U001": "alice"}
    assert slack_export._replace_user_mentions("hey <@U001>", users) == "hey @alice"
    assert slack_export._replace_user_mentions("no mentions", users) == "no mentions"
    assert slack_export._replace_user_mentions("hey <@UNKNOWN>", users) == "hey <@UNKNOWN>"


def test_first_line_truncation() -> None:
    assert slack_export._first_line("short") == "short"
    long = "x" * 200
    assert len(slack_export._first_line(long, max_chars=50)) == 51  # 50 + ellipsis


# ----- malformed input tolerance -----


def test_iter_units_skips_non_list_channel_files(tmp_path: Path) -> None:
    """If a channel file contains garbage (not a JSON array), don't crash."""
    fake = tmp_path / "fake_export"
    fake.mkdir()
    (fake / "users.json").write_text(json.dumps([]))
    chan = fake / "broken"
    chan.mkdir()
    (chan / "2026-05-18.json").write_text(json.dumps({"this": "is not a list"}))
    export = slack_export.load(fake)
    units = list(export.iter_units(min_thread_messages=1))
    export.close()
    assert units == []
