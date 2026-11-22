"""Unit tests for session_timer.store (no server needed)."""

import pytest

from session_timer import config
from session_timer.store import (
    Event,
    Session,
    SessionLimitError,
    SessionStore,
    StaleGenerationError,
    format_service_time,
    parse_service_time,
)


def make_session(*events) -> Session:
    session = Session(name="test")
    for name, start, duration in events:
        session.add_event(name, start, duration)
    return session


# --------------------------------------------------------------- store


def test_get_never_creates():
    store = SessionStore()
    assert store.get("a") is None


def test_get_or_create_is_lazy_and_stable():
    store = SessionStore()
    session = store.get_or_create("a")
    assert session is store.get("a")
    assert session is store.get_or_create("a")
    assert session.generation == 0
    assert session.events == []


# ------------------------------------------------------- generation


def test_mutations_bump_generation():
    session = Session(name="t")
    event = session.add_event("a", 100, 10)
    assert session.generation == 1
    session.delete_event(event.id)
    assert session.generation == 2


def test_check_generation():
    session = make_session(("a", 100, 10))
    assert session.generation == 1
    session.check_generation(1)  # ok
    with pytest.raises(StaleGenerationError):
        session.check_generation(0)
    with pytest.raises(StaleGenerationError):
        session.check_generation(None)


def test_failed_mutation_does_not_bump_generation():
    session = make_session(("a", 100, 10))
    with pytest.raises(ValueError):
        session.add_event("b", 100, 0)  # duration must be positive
    assert session.generation == 1
    assert len(session.events) == 1


# ------------------------------------------------------- add / delete


def test_add_event_defaults_and_ids():
    session = make_session(("a", 100, 10), (None, 200, 5))
    assert session.events[0].name == "a"
    assert session.events[1].name == ""  # name is optional
    assert session.events[0].id != session.events[1].id
    assert session.events[0].end_time == 110


def test_delete_event_missing_raises():
    session = make_session(("a", 100, 10))
    with pytest.raises(KeyError):
        session.delete_event("nope")


# ------------------------------------------------------- update_event


def test_update_event_end_time_sets_duration():
    session = make_session(("a", 100, 10))
    event = session.events[0]
    session.update_event(event.id, end_time=125)
    assert event.duration == 25
    assert event.end_time == 125


def test_update_event_remaining_sets_start():
    session = make_session(("a", 100, 10))
    event = session.events[0]
    session.update_event(event.id, remaining=60, now=1000.0)
    assert event.start_time == 1060
    assert event.duration == 10  # unchanged


def test_update_event_precedence_duration_over_end():
    session = make_session(("a", 100, 10))
    event = session.events[0]
    # end_time derives duration=25, then explicit duration=30 overrides
    session.update_event(event.id, end_time=125, duration=30)
    assert event.duration == 30
    # remaining overrides explicit start_time
    session.update_event(event.id, start_time=999, remaining=1, now=2000.0)
    assert event.start_time == 2001


def test_update_event_rejects_bad_values():
    session = make_session(("a", 100, 10))
    event = session.events[0]
    with pytest.raises(ValueError):
        session.update_event(event.id, duration=0)
    with pytest.raises(ValueError):
        session.update_event(event.id, end_time=90)  # before start
    assert event.duration == 10  # unchanged


# ------------------------------------------------------------- shift


def test_shift_start_time_cascade():
    session = make_session(("a", 100, 10), ("b", 200, 10), ("c", 300, 10))
    a = session.events[0]
    session.shift(a.id, "start_time", 50, cascade=True)
    starts = [e.start_time for e in session.events]
    assert starts == [150, 250, 350]


def test_shift_duration_cascade_delays_later_events():
    session = make_session(("a", 100, 10), ("b", 200, 10), ("c", 300, 10))
    a = session.events[0]
    session.shift(a.id, "duration", 30, cascade=True)
    assert a.duration == 40
    assert [e.start_time for e in session.events] == [100, 230, 330]


def test_shift_without_cascade():
    session = make_session(("a", 100, 10), ("b", 200, 10))
    a = session.events[0]
    session.shift(a.id, "start_time", 50, cascade=False)
    assert [e.start_time for e in session.events] == [150, 200]


def test_shift_earlier_event_does_not_move_later_events():
    # Events starting before the shifted one are unaffected even when
    # the shift moves it before/after them.
    session = make_session(("a", 100, 10), ("b", 200, 10))
    b = session.events[1]
    session.shift(b.id, "start_time", -150, cascade=True)
    assert [e.start_time for e in session.events] == [100, 50]


def test_shift_rejects_bad_field_and_negative_duration():
    session = make_session(("a", 100, 10))
    with pytest.raises(ValueError):
        session.shift(session.events[0].id, "name", 5)
    with pytest.raises(ValueError):
        session.shift(session.events[0].id, "duration", -100)


# ------------------------------------------------- current / overlaps


def test_current_event_none_before_first_start():
    session = make_session(("a", 100, 10))
    assert session.current_event(now=99) is None
    assert session.current_event(now=100) is session.events[0]


def test_current_event_persists_until_next_starts():
    session = make_session(("a", 100, 10), ("b", 200, 10))
    a, b = session.events
    assert session.current_event(now=150) is a  # ended, counting negative
    assert session.current_event(now=200) is b
    assert session.current_event(now=1000) is b  # last event stays


def test_current_event_overlap_most_recent_wins():
    session = make_session(("a", 100, 60), ("b", 120, 10))
    assert session.current_event(now=130) is session.events[1]


def test_overlapping_ids():
    session = make_session(("a", 100, 60), ("b", 120, 10), ("c", 300, 10))
    a, b, c = session.events
    assert session.overlapping_ids() == {a.id, b.id}


# --------------------------------------------------------------- CSV


def test_csv_roundtrip():
    session = make_session(("first", 100, 10), ("", 50, 5), ("last, with comma", 200, 30))
    text = session.to_csv()
    lines = text.strip().splitlines()
    assert lines[0] == "start_time,duration_sec,name"
    assert "0:01:40,10,first" in lines
    new_session = Session(name="other")
    count = new_session.replace_from_csv(text)
    assert count == 3
    assert [(e.name, e.start_time, e.duration) for e in new_session.events] == [
        ("", 50, 5),
        ("first", 100, 10),
        ("last, with comma", 200, 30),
    ]


def test_replace_from_csv_replaces_all():
    session = make_session(("a", 100, 10))
    session.replace_from_csv("start_time,duration_sec,name\n0:08:20,10,only\n")
    assert [(e.name, e.start_time) for e in session.events] == [("only", 500)]
    assert session.generation == 2  # initial add + replace


def test_replace_from_csv_without_header_and_blank_lines():
    session = Session(name="t")
    count = session.replace_from_csv("0:01:40,10,one\n\n0:03:20,20,two\n")
    assert count == 2
    assert [e.start_time for e in session.events] == [100, 200]


def test_replace_from_csv_validation():
    session = Session(name="t")
    with pytest.raises(ValueError):
        session.replace_from_csv("not-a-number,10,x\n")
    with pytest.raises(ValueError):
        session.replace_from_csv("0:00:60,10,x\n")  # invalid seconds
    with pytest.raises(ValueError):
        session.replace_from_csv("500,10,x\n")  # plain integers rejected
    with pytest.raises(ValueError):
        session.replace_from_csv("0:01:40,0,x\n")  # duration must be positive
    with pytest.raises(ValueError):
        session.replace_from_csv("0:01:40\n")  # missing duration
    assert session.events == []
    assert session.generation == 0  # nothing changed


def test_replace_from_csv_strips_utf8_bom():
    """Regression: Excel's 'CSV UTF-8' writes a BOM; upload must work."""
    session = Session(name="t")
    count = session.replace_from_csv("﻿start_time,duration_sec,name\n0:01:40,60,café ☕\n")
    assert count == 1
    assert session.events[0].name == "café ☕"
    assert session.events[0].start_time == 100


# ------------------------------------------------------ limits


def test_event_name_length_limit():
    session = Session(name="t")
    ok = "x" * 128
    session.add_event(ok, 100, 60)
    assert session.events[0].name == ok
    with pytest.raises(ValueError):
        session.add_event("x" * 129, 100, 60)
    # update_event goes through the same check
    with pytest.raises(ValueError):
        session.update_event(session.events[0].id, name="y" * 129)
    # None means "keep existing" and is fine
    session.update_event(session.events[0].id, name=None)
    assert session.events[0].name == ok


def test_event_name_length_limit_in_csv():
    session = Session(name="t")
    long_name = "x" * 129
    with pytest.raises(ValueError):
        session.replace_from_csv(f"0:01:40,60,{long_name}\n")
    assert session.events == []
    assert session.generation == 0


def test_max_events_per_session():
    session = Session(name="t")
    for i in range(128):
        session.add_event(f"e{i}", i * 60, 60)
    with pytest.raises(ValueError):
        session.add_event("one too many", 99999, 60)
    assert len(session.events) == 128
    assert session.generation == 128  # rejected add did not bump


def test_max_events_per_session_in_csv():
    session = Session(name="t")
    session.add_event("keep", 0, 60)
    rows = "\n".join(f"{i // 60}:{i % 60:02d},60,x{i}" for i in range(129))
    with pytest.raises(ValueError):
        session.replace_from_csv(rows + "\n")
    # rejected upload left the old events untouched
    assert [e.name for e in session.events] == ["keep"]
    assert session.generation == 1


def test_max_events_per_session_csv_exact_fit():
    session = Session(name="t")
    rows = "\n".join(f"{i // 60}:{i % 60:02d},60,x{i}" for i in range(128))
    assert session.replace_from_csv(rows + "\n") == 128


# ------------------------------------------------------ session cap


def test_session_cap_refuses_when_all_active():
    store = SessionStore()
    for i in range(1024):
        store.get_or_create(f"s{i:04d}")
    with pytest.raises(SessionLimitError):
        store.get_or_create("one-more")
    assert len(store.sessions) == 1024


def test_session_cap_evicts_idle_oldest_first():
    store = SessionStore()
    for i in range(1024):
        store.get_or_create(f"s{i:04d}")
    # make exactly two sessions eviction-eligible (idle > 1 h)
    store.sessions["s0000"].last_used -= 7200
    store.sessions["s0001"].last_used -= 3601
    store.get_or_create("fresh")
    assert "fresh" in store.sessions
    assert "s0000" not in store.sessions  # oldest idle evicted first
    assert "s0001" in store.sessions      # only one slot was needed
    assert len(store.sessions) == 1024


def test_session_cap_eviction_runs_expiry_first():
    store = SessionStore()
    for i in range(1024):
        store.get_or_create(f"s{i:04d}")
    # one session long expired, but still "recent" enough to evade the
    # eviction idle cutoff — the expiry pass inside get_or_create
    # must free it anyway
    store.sessions["s0420"].last_used -= 40 * 24 * 3600
    store.get_or_create("fresh")
    assert "s0420" not in store.sessions
    assert "fresh" in store.sessions


# ------------------------------------------------- snapshot persistence


def without_server_time(d: dict) -> dict:
    """Strip the serialization timestamp (not session state)."""
    return {k: v for k, v in d.items() if k != "server_time"}


def test_snapshot_roundtrip_preserves_everything(tmp_path):
    store = SessionStore()
    session = store.get_or_create("demo")
    session.set_timezone("Europe/Berlin")
    e1 = session.add_event("café ☕", 100, 60)
    session.add_event("second", 200, 30)
    session.shift(e1.id, "duration", 10)
    session.set_font_scale(1.73)
    session.set_dim_level(0.5)
    original = session.to_dict()

    path = tmp_path / "sessions.json"
    store.save_snapshot(path)

    restored = SessionStore()
    assert restored.load_snapshot(path) is True
    rsession = restored.get("demo")
    assert rsession is not None
    assert without_server_time(rsession.to_dict()) == without_server_time(original)
    assert rsession.timezone == "Europe/Berlin"
    # event ids survive so admin rows stay valid across restarts
    assert rsession.find_event(e1.id).name == "café ☕"
    # the restored store keeps working (generation continues)
    rsession.add_event("after restart", 300, 60)
    assert rsession.generation == original["generation"] + 1


def test_load_snapshot_missing_file_is_empty_start(tmp_path):
    store = SessionStore()
    assert store.load_snapshot(tmp_path / "missing.json") is False
    assert store.sessions == {}


def test_load_snapshot_corrupt_file_quarantined(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{not json at all", encoding="utf-8")
    store = SessionStore()
    assert store.load_snapshot(path) is False
    assert store.sessions == {}
    assert not path.exists()
    assert (tmp_path / "sessions.json.corrupt").exists()


def test_load_snapshot_wrong_version_quarantined(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text('{"version": 999, "sessions": {}}', encoding="utf-8")
    store = SessionStore()
    assert store.load_snapshot(path) is False
    assert (tmp_path / "sessions.json.corrupt").exists()


def test_snapshot_roundtrip_unicode(tmp_path):
    store = SessionStore()
    s = store.get_or_create("ünïcode")
    s.set_timezone("Europe/Berlin")
    s.add_event("日本語", 0, 60)
    path = tmp_path / "sessions.json"
    store.save_snapshot(path)
    # file is valid UTF-8 JSON with readable text
    assert "日本語" in path.read_text(encoding="utf-8")
    restored = SessionStore()
    restored.load_snapshot(path)
    assert restored.get("ünïcode").events[0].name == "日本語"


def test_snapshot_of_empty_store_roundtrips(tmp_path):
    store = SessionStore()
    path = tmp_path / "sessions.json"
    store.save_snapshot(path)
    restored = SessionStore()
    assert restored.load_snapshot(path) is True
    assert restored.sessions == {}


# ------------------------------------------------------ service times


def test_format_service_time():
    assert format_service_time(0) == "0:00"
    assert format_service_time(52200) == "14:30"
    assert format_service_time(52237) == "14:30:37"
    assert format_service_time(91800) == "25:30"  # just past midnight


def test_parse_service_time():
    assert parse_service_time("14:30") == 52200
    assert parse_service_time("09:05:07") == 32707
    assert parse_service_time("25:30") == 91800
    with pytest.raises(ValueError):
        parse_service_time("1719000000")  # unixtime-looking: rejected loudly
    with pytest.raises(ValueError):
        parse_service_time("12:60")
    with pytest.raises(ValueError):
        parse_service_time("48:00")  # beyond the two-service-day window
    with pytest.raises(ValueError):
        parse_service_time("")


# ------------------------------------------------- start-time range

def test_add_event_rejects_start_outside_one_day():
    session = Session(name="t")
    with pytest.raises(ValueError, match="0:00"):
        session.add_event("", -1, 60)
    with pytest.raises(ValueError, match="midnight"):
        session.add_event("", 86400, 60)
    with pytest.raises(ValueError, match="midnight"):
        session.add_event("", 90000, 60)
    assert session.events == []
    assert session.generation == 0  # rejected mutations change nothing


def test_update_event_rejects_start_outside_one_day():
    session = make_session(("a", 86300, 60))
    event = session.events[0]
    with pytest.raises(ValueError, match="midnight"):
        session.update_event(event.id, start_time=86400)
    # a "remaining" that lands past midnight is rejected too
    with pytest.raises(ValueError, match="midnight"):
        session.update_event(event.id, remaining=600, now=86300)
    # ...and nothing changed
    assert event.start_time == 86300
    assert session.generation == 1


def test_shift_rejects_result_outside_one_day():
    session = make_session(("a", 86300, 60), ("b", 86400 - 90, 30))
    a, b = session.events
    with pytest.raises(ValueError, match="'a'"):
        session.shift(a.id, "start_time", 100, cascade=False)
    # cascading past midnight: the *later* event is the offender
    with pytest.raises(ValueError, match="cascade"):
        session.shift(a.id, "start_time", 100, cascade=True)
    assert [e.start_time for e in session.events] == [86300, 86310]
    assert session.generation == 2  # make_session added 2; shifts all rejected


def test_shift_cascade_ok_when_result_stays_in_range():
    session = make_session(("a", 3600, 600), ("b", 7200, 600))
    a = session.events[0]
    session.shift(a.id, "start_time", 600, cascade=True)
    assert [e.start_time for e in session.events] == [4200, 7800]


# ----------------------------------------------------- timezone


def test_set_timezone_sets_and_bumps():
    session = Session(name="t")
    assert session.timezone is None
    assert session.set_timezone("Europe/Berlin") is True
    assert session.timezone == "Europe/Berlin"
    assert session.generation == 1


def test_set_timezone_first_wins():
    session = Session(name="t")
    session.set_timezone("Europe/Berlin")
    assert session.set_timezone("UTC") is False
    assert session.timezone == "Europe/Berlin"
    assert session.generation == 1  # no-op did not bump


def test_set_timezone_validates():
    session = Session(name="t")
    with pytest.raises(ValueError):
        session.set_timezone(12345)
    with pytest.raises(ValueError):
        session.set_timezone("has spaces")
    with pytest.raises(ValueError):
        session.set_timezone("x" * 65)
    assert session.timezone is None
    assert session.generation == 0  # nothing changed


def test_set_timezone_force_rebases_zone_but_keeps_events():
    session = make_session(("a", 3600, 600), ("b", 7200, 600))
    session.set_timezone("Europe/Berlin")
    generation = session.generation

    # Without force: still first-write-wins.
    assert session.set_timezone("UTC") is False
    assert session.timezone == "Europe/Berlin"
    assert session.generation == generation

    # Forced re-set: day-seconds untouched, generation bumps, and a
    # repeat with the same value is a no-op (no spurious broadcast).
    assert session.set_timezone("UTC", force=True) is True
    assert session.timezone == "UTC"
    assert [e.start_time for e in session.events] == [3600, 7200]
    assert session.generation == generation + 1
    assert session.set_timezone("UTC", force=True) is False
    assert session.generation == generation + 1


def test_resolved_utc_offset():
    session = Session(name="t")
    assert session.set_timezone("UTC") is True
    assert session.resolved_utc_offset() == 0
    # Unresolvable names fail open (None), they do not raise.
    session.set_timezone("Etc/NotAZone", force=True)
    assert session.resolved_utc_offset() is None
    # No timezone at all: falls back to config.DEFAULT_TZ (or None).
    session.timezone = None
    assert session.resolved_utc_offset() is None
    config.DEFAULT_TZ = "UTC"
    try:
        assert session.resolved_utc_offset() == 0
    finally:
        config.DEFAULT_TZ = None


def test_to_dict_includes_timezone_and_server_time():
    session = Session(name="t")
    data = session.to_dict()
    assert data["timezone"] is None
    assert data["utc_offset"] is None
    assert isinstance(data["server_time"], float)
    session.set_timezone("UTC")
    data = session.to_dict()
    assert data["timezone"] == "UTC"
    assert data["utc_offset"] == 0


def test_update_event_remaining_requires_now():
    session = make_session(("a", 100, 10))
    with pytest.raises(ValueError):
        session.update_event(session.events[0].id, remaining=60)


# ------------------------------------------------------------- expiry


def test_expire():
    store = SessionStore()
    store.get_or_create("old")
    store.sessions["old"].last_used = 0.0
    store.get_or_create("new")
    assert store.expire(max_age_seconds=100, now=1000.0) == ["old"]
    assert store.get("old") is None
    assert store.get("new") is not None


# ------------------------------------------------- audit regressions


def test_duration_upper_bound():
    session = Session(name="t")
    with pytest.raises(ValueError):
        session.add_event("too long", 0, 86401)
    session.add_event("all day", 0, 86400)  # boundary is fine
    with pytest.raises(ValueError):
        session.update_event(session.events[0].id, duration=10**9)
    event = session.events[0]
    session.update_event(event.id, end_time=86400, start_time=0)  # ok
    with pytest.raises(ValueError):
        session.update_event(event.id, remaining=10**9, now=100)


def test_shift_duration_cap():
    session = Session(name="t")
    event = session.add_event("A", 0, 86400)
    with pytest.raises(ValueError):
        session.shift(event.id, "duration", 1, cascade=False)
    assert event.duration == 86400


def test_shift_duration_is_all_or_nothing():
    """Regression: a duration shift whose cascade pushes a later event
    past midnight must leave the anchor's duration unchanged.  The old
    code mutated the duration before validating the cascade."""
    session = Session(name="t")
    a = session.add_event("A", 86300, 60)
    b = session.add_event("B", 86390, 60)
    with pytest.raises(ValueError):
        session.shift(a.id, "duration", 30, cascade=True)
    assert a.duration == 60
    assert b.start_time == 86390
    assert session.generation == 2  # rejected shift did not bump


def test_csv_roundtrip_preserves_special_names():
    session = Session(name="t")
    names = [
        "comma, name",
        chr(34) + "quoted" + chr(34) + " name",
        "line\nbreak".replace(chr(92) + "n", chr(10)),
        "unicode éü中",
        "",
    ]
    for i, name in enumerate(names):
        session.add_event(name, i * 60, 60)
    text = session.to_csv()
    fresh = Session(name="t2")
    fresh.replace_from_csv(text)
    assert [e.name for e in fresh.events] == names


def test_csv_parses_crlf_line_endings():
    session = Session(name="t")
    n = session.replace_from_csv("0:00,60,a\r\n0:01,60,b\r\n")
    assert n == 2
    assert [e.start_time for e in session.events] == [0, 60]
