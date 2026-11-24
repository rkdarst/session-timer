"""API tests using Starlette's TestClient (HTTP + WebSocket)."""

import time

import pytest
from fastapi.testclient import TestClient

import asyncio

from session_timer.main import Hub, app, store
from session_timer.store import Session

NAME = "apitest"  # unique per run; module-level store is in-memory


@pytest.fixture()
def client():
    with TestClient(app) as testclient:
        store.sessions.pop(NAME, None)  # fresh session per test
        yield testclient


def gen(client_generation):
    return {"X-Generation": str(client_generation)}


def add_event(client, generation, name, start, duration):
    return client.post(
        f"/api/{NAME}/events",
        json={"name": name, "start_time": start, "duration": duration},
        headers=gen(generation),
    )


# --------------------------------------------------- limits via the API


def test_event_name_too_long_rejected(client):
    client.post(f"/api/{NAME}/timezone", json={"timezone": "Europe/Berlin"})
    r = add_event(client, 1, "x" * 129, 100, 60)
    assert r.status_code == 400
    state = client.get(f"/api/{NAME}/state").json()
    assert state["events"] == []
    # boundary: exactly 128 is fine
    r = add_event(client, 1, "x" * 128, 100, 60)
    assert r.status_code == 200


def test_max_events_rejected_via_api(client):
    for i in range(128):
        r = add_event(client, i, f"e{i}", i * 60, 60)
        assert r.status_code == 200
    r = add_event(client, 128, "one too many", 99999, 60)
    assert r.status_code == 400
    state = client.get(f"/api/{NAME}/state").json()
    assert len(state["events"]) == 128


def test_max_events_rejected_via_csv_upload(client):
    add_event(client, 0, "keep", 0, 60)
    rows = "\n".join(f"{i // 60}:{i % 60:02d},60,x{i}" for i in range(129))
    r = client.put(
        f"/api/{NAME}/events.csv",
        content=rows + "\n",
        headers={**gen(1), "Content-Type": "text/csv"},
    )
    assert r.status_code == 400
    state = client.get(f"/api/{NAME}/state").json()
    assert [e["name"] for e in state["events"]] == ["keep"]


def test_add_event_start_range(client):
    # 24:00:00 and later are next-day early hours: accepted as data,
    # never "current".
    r = add_event(client, 0, "after midnight", 86400, 60)
    assert r.status_code == 200
    assert r.json()["events"][-1]["start_time"] == 86400
    assert add_event(client, 1, "also bad", -60, 60).status_code == 400
    # the two-service-day window is the hard bound
    r = add_event(client, 1, "way too late", 172800, 60)
    assert r.status_code == 400
    assert "47:59:59" in r.json()["detail"]
    # 172799 = 47:59:59, the last valid second of the two-day window
    assert add_event(client, 1, "just fits", 172799, 1).status_code == 200


def test_csv_upload_accepts_next_day_times(client):
    # 24:00:00 and later are next-day early hours (GTFS-style): kept as
    # data, never "current".  Pre-2026 anchor-based exports load again.
    r = client.put(
        f"/api/{NAME}/events.csv",
        content="24:00:00,60,after midnight\n25:30,60,early hours\n",
        headers={**gen(0), "Content-Type": "text/csv"},
    )
    assert r.status_code == 200
    assert [e["start_time"] for e in r.json()["events"]] == [86400, 91800]
    # ...but the 48-hour window still bounds the data (the CSV time
    # parser itself rejects hours > 47).
    r = client.put(
        f"/api/{NAME}/events.csv",
        content="48:00:00,60,beyond two days\n",
        headers={**gen(1), "Content-Type": "text/csv"},
    )
    assert r.status_code == 400
    assert "out of range" in r.json()["detail"]
    # all-or-nothing: the accepted upload is unchanged
    assert [e.start_time for e in store.get(NAME).events] == [86400, 91800]


def test_csv_upload_accepts_23_59_59(client):
    r = client.put(
        f"/api/{NAME}/events.csv",
        content="23:59:59,1,last one\n",
        headers={**gen(0), "Content-Type": "text/csv"},
    )
    assert r.status_code == 200
    assert [e["start_time"] for e in r.json()["events"]] == [86399]


def test_shift_endpoint_cascade_into_next_day(client):
    """Cascades may push events past 24:00:00; the two-day window is
    still all-or-nothing."""
    first = add_event(client, 0, "a", 86300, 10).json()["events"][0]
    add_event(client, 1, "b", 86350, 10)
    r = client.post(
        f"/api/{NAME}/events/{first['id']}/shift",
        json={"field": "start_time", "delta": 100, "cascade": True},
        headers=gen(2),
    )
    assert r.status_code == 200
    assert [e["start_time"] for e in r.json()["events"]] == [86400, 86450]
    # Pushing past 47:59:59 rejects the whole cascade.
    r = client.post(
        f"/api/{NAME}/events/{first['id']}/shift",
        json={"field": "start_time", "delta": 90000, "cascade": True},
        headers=gen(3),
    )
    assert r.status_code == 400
    assert "47:59:59" in r.json()["detail"]
    state = client.get(f"/api/{NAME}/state").json()
    assert [e["start_time"] for e in state["events"]] == [86400, 86450]


def test_session_cap_returns_503(client):
    from session_timer.store import Session

    from session_timer import config

    # fill the store directly (1024 HTTP round-trips would be slow);
    # MAX_SESSIONS fillers leave no room, so creation must be refused
    # (fillers get a fresh last_used, so they are not eviction-eligible)
    for i in range(config.MAX_SESSIONS):
        store.sessions[f"filler{i:04d}"] = Session(name=f"filler{i:04d}")
    try:
        r = client.post("/api/cap-test-session/events",
                        json={"name": "x", "start_time": 0, "duration": 60},
                        headers=gen(0))
        assert r.status_code == 503
        assert "cap-test-session" not in store.sessions  # nothing was created
        # read-only state stays a non-creating 200-with-empty-state
        state = client.get("/api/cap-test-session/state").json()
        assert state["generation"] is None
        assert state["events"] == []
    finally:
        # always clean up: leaked fillers would 503 every later test
        for filler in [n for n in store.sessions if n.startswith("filler")]:
            del store.sessions[filler]


def test_state_empty_session_does_not_create(client):
    resp = client.get(f"/api/{NAME}/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == NAME
    assert data["generation"] is None
    assert data["timezone"] is None
    assert data["utc_offset"] is None
    assert isinstance(data["server_time"], float)
    assert data["font_scale"] == 1.0
    assert data["dim_level"] == 0.0
    assert data["events"] == []
    assert store.get(NAME) is None  # read-only path never creates


def test_add_event_creates_session_and_returns_state(client):
    resp = add_event(client, 0, "intro", 1000, 600)
    assert resp.status_code == 200
    data = resp.json()
    assert data["generation"] == 1
    assert len(data["events"]) == 1
    assert data["events"][0]["name"] == "intro"
    assert store.get(NAME) is not None  # mutation path creates


def test_stale_generation_rejected_with_409(client):
    add_event(client, 0, "", 1000, 600)
    resp = add_event(client, 0, "", 2000, 600)  # now stale: server at gen 1
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "stale"
    assert detail["generation"] == 1


def test_first_mutation_on_nonexistent_session_uses_generation_zero(client):
    """Regression: admin UI on a fresh URL has generation null and must
    be able to create the session by sending X-Generation: 0."""
    resp = add_event(client, 0, "first ever", 1000, 600)
    assert resp.status_code == 200
    assert resp.json()["generation"] == 1


def test_stale_mutation_on_nonexistent_session_does_not_create(client):
    """Regression: a rejected (stale) request must not create the session
    as a side effect; only a correctly-generational mutation does."""
    resp = add_event(client, 5, "should not exist", 1000, 600)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "stale"
    assert detail["generation"] == 0  # what a fresh session would have
    # The session was NOT created by the rejected request.
    resp = client.get(f"/api/{NAME}/state")
    assert resp.json()["generation"] is None
    assert resp.json()["events"] == []


def test_missing_generation_header_rejected(client):
    resp = client.post(
        f"/api/{NAME}/events",
        json={"name": "", "start_time": 1000, "duration": 600},
    )
    assert resp.status_code == 422  # required header missing


def test_patch_event_end_time(client):
    event_id = add_event(client, 0, "", 1000, 600).json()["events"][0]["id"]
    resp = client.patch(
        f"/api/{NAME}/events/{event_id}",
        json={"end_time": 1300},
        headers=gen(1),
    )
    assert resp.status_code == 200
    event = resp.json()["events"][0]
    assert event["duration"] == 300


def test_shift_cascade_endpoint(client):
    event_id = add_event(client, 0, "a", 100, 10).json()["events"][0]["id"]
    add_event(client, 1, "b", 200, 10)
    resp = client.post(
        f"/api/{NAME}/events/{event_id}/shift",
        json={"field": "start_time", "delta": 50, "cascade": True},
        headers=gen(2),
    )
    assert [e["start_time"] for e in resp.json()["events"]] == [150, 250]


def test_delete_event_and_404(client):
    event_id = add_event(client, 0, "", 1000, 600).json()["events"][0]["id"]
    assert client.delete(f"/api/{NAME}/events/{event_id}", headers=gen(1)).status_code == 200
    resp = client.delete(f"/api/{NAME}/events/{event_id}", headers=gen(2))
    assert resp.status_code == 404


def test_event_now_creates_event_starting_now(client):
    assert (
        client.post(f"/api/{NAME}/timezone", json={"timezone": "UTC"}).status_code
        == 200
    )
    resp = client.post(
        f"/api/{NAME}/events/now",
        json={"name": "now", "duration": 600},
        headers=gen(1),
    )
    assert resp.status_code == 200
    event = resp.json()["events"][0]
    # start_time is wall-clock seconds of day in the session timezone
    # (UTC here): the current minute, rounded backwards so the event is
    # already running and the HH:MM column stays clean.
    assert event["start_time"] % 60 == 0
    # ...and the duration is extended so the event ends a full 600 s
    # from the exact request time: the seconds squeezed out of the
    # floored start are added to the duration.
    assert 600 <= event["duration"] < 601 + 60
    now_utc = time.gmtime()
    day_seconds = now_utc.tm_hour * 3600 + now_utc.tm_min * 60 + now_utc.tm_sec
    # ~now, with slack for a minute boundary crossed between the two
    # reads (floor adds up to 60 s); a midnight wrap between them shows
    # up as a huge difference.
    assert abs(event["start_time"] - day_seconds) < 120 or abs(
        event["start_time"] - day_seconds
    ) > 86280  # midnight wrap between the two reads
    # End = start + duration ≈ now + 600: immune to minute-boundary
    # crossings (both sides shift equally); a midnight wrap between the
    # reads shows up as a huge difference.
    end = event["start_time"] + event["duration"]
    diff = abs(end - (day_seconds + 600))
    assert diff < 120 or diff > 86280


def test_event_now_without_timezone_400(client):
    resp = client.post(
        f"/api/{NAME}/events/now",
        json={"name": "now", "duration": 600},
        headers=gen(0),
    )
    assert resp.status_code == 400


def test_patch_remaining_uses_timezone(client):
    client.post(f"/api/{NAME}/timezone", json={"timezone": "UTC"})
    event_id = add_event(client, 1, "", 100, 600).json()["events"][0]["id"]
    resp = client.patch(
        f"/api/{NAME}/events/{event_id}",
        json={"remaining": 60},
        headers=gen(2),
    )
    assert resp.status_code == 200
    event = resp.json()["events"][0]
    # start = server's day-seconds-now + 60, so it must be within a
    # minute of the current UTC time of day
    now_utc = time.gmtime()
    day_seconds = now_utc.tm_hour * 3600 + now_utc.tm_min * 60 + now_utc.tm_sec
    delta = (event["start_time"] - (day_seconds + 60)) % 86400
    assert delta < 5


def test_set_timezone_endpoint(client):
    resp = client.post(f"/api/{NAME}/timezone", json={"timezone": "Europe/Berlin"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["timezone"] == "Europe/Berlin"
    assert data["utc_offset"] is not None  # resolved server-side
    assert data["server_time"] > 0
    assert data["generation"] == 1  # creation (0) + timezone bump
    assert store.get(NAME) is not None
    # a second POST is a no-op: the first timezone wins
    resp = client.post(f"/api/{NAME}/timezone", json={"timezone": "UTC"})
    assert resp.json()["timezone"] == "Europe/Berlin"
    assert resp.json()["generation"] == 1
    # invalid names are rejected with a clear 400
    resp = client.post(f"/api/{NAME}/timezone", json={"timezone": "not a zone!"})
    assert resp.status_code == 400



def test_csv_roundtrip_endpoint(client):
    add_event(client, 0, "one", 100, 10)
    resp = client.get(f"/api/{NAME}/events.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    lines = resp.text.strip().splitlines()
    assert lines[0] == "start_time,duration_sec,name"

    # upload (replaces) with a different event set
    resp = client.put(
        f"/api/{NAME}/events.csv",
        content="start_time,duration_sec,name\n0:08:20,10,two\n",
        headers={**gen(1), "Content-Type": "text/csv"},
    )
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["name"] == "two"
    assert events[0]["start_time"] == 500

    # round-trip: download now shows the uploaded state
    assert "0:08:20,10,two" in client.get(f"/api/{NAME}/events.csv").text


def test_csv_upload_bad_content_400(client):
    resp = client.put(
        f"/api/{NAME}/events.csv",
        content="nonsense,not-a-number\n",
        headers={**gen(0), "Content-Type": "text/csv"},
    )
    assert resp.status_code == 400


def test_invalid_session_name_404(client):
    # Page routes answer with an EMPTY 404 (fingerprint-free); API
    # routes may carry a JSON detail.
    for path in (
        "/bad.name/",
        "/bad.name/display",
        "/bad.name/iframe",
        "/bad.name",
        "/api/bad.name/state",
    ):
        r = client.get(path)
        assert r.status_code == 404, path
    for path in ("/bad.name/", "/bad.name/display", "/bad.name/iframe"):
        assert client.get(path).content == b"", path


def test_admin_page_no_trailing_slash_redirects(client):
    r = client.get(f"/{NAME}", follow_redirects=False)
    assert r.status_code == 301
    # origin-form Location: scheme-agnostic (proxy-safe) redirect
    assert r.headers["location"] == f"/{NAME}/"
    # invalid names 404 instead of redirecting
    assert client.get("/bad.name", follow_redirects=False).status_code == 404
    # following the redirect lands on the admin page
    followed = client.get(f"/{NAME}")
    assert followed.status_code == 200
    assert "events-table" in followed.text


def test_pages_served(client):
    for path, marker in [
        (f"/{NAME}/", "events-table"),
        (f"/{NAME}/", "preview-frame"),
        (f"/{NAME}/", ">Gap</th>"),
        (f"/{NAME}/", "toggle-theme"),
        (f"/{NAME}/", "Events are NOT saved long term"),
        (f"/{NAME}/", "sysadmin-message"),
        (f"/{NAME}/", "data-loss-warning"),
        (f"/{NAME}/", "show-help"),
        (f"/{NAME}/", "menu-btn"),
        (f"/{NAME}/", "set-timezone"),
        (f"/{NAME}/", "edit-cascade"),
        (f"/{NAME}/", "tz-indicator"),
        (f"/{NAME}/", "event-now-menu"),
        (f"/{NAME}/", "new-session-modal"),
        (f"/{NAME}/", 'id="clock"'),
        (f"/{NAME}/", ">Speaker view</a>"),  # toolbar link (was menu item)
        (f"/{NAME}/", "config.js"),
        (f"/{NAME}/display", 'id="timer"'),
        (f"/{NAME}/display", "config.js"),
        (f"/{NAME}/display", 'id="clock"'),
        (f"/{NAME}/display", "clock-toggle"),
        (f"/{NAME}/iframe", 'id="bar"'),
        (f"/{NAME}/iframe", "next-bar"),
        (f"/{NAME}/iframe", "iframe.js"),
    ]:
        resp = client.get(path)
        assert resp.status_code == 200
        assert marker in resp.text

    # The sysadmin message (SESSION_TIMER_MESSAGE) sits inside the
    # warning banner, above the data-loss text.
    admin_html = client.get(f"/{NAME}/").text
    assert admin_html.index("sysadmin-message") < admin_html.index(
        "Events are NOT saved"
    )


def test_timezone_reset_force(client):
    """The admin menu's "Set timezone to this browser": re-set keeping
    day-seconds."""
    r = client.post(f"/api/{NAME}/timezone", json={"timezone": "Europe/Berlin"})
    assert r.status_code == 200
    assert add_event(client, 1, "evt", 3600, 600).status_code == 200

    # Plain re-post stays a no-op (first timezone wins)...
    r = client.post(f"/api/{NAME}/timezone", json={"timezone": "UTC"})
    assert r.json()["timezone"] == "Europe/Berlin"

    # ...but force re-sets the zone while keeping every day-second.
    r = client.post(
        f"/api/{NAME}/timezone", json={"timezone": "UTC", "force": True}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["timezone"] == "UTC"
    assert data["utc_offset"] == 0
    assert data["events"][0]["start_time"] == 3600
    assert data["generation"] == 3  # timezone + add_event + re-set

    # Repeat with the same value: no generation bump (no spurious push).
    r = client.post(f"/api/{NAME}/timezone", json={"timezone": "UTC", "force": True})
    assert r.json()["generation"] == 3


def test_persistence_endpoint(client, monkeypatch):
    from session_timer import config

    r = client.get("/api/persistence")
    assert r.status_code == 200
    assert r.json() == {"persisted": bool(config.DATA_DIR)}

    # When the operator sets SESSION_TIMER_DATA_DIR (here: patched after
    # import, which the route reads at request time), the flag flips and
    # admin.js hides the warning banner.
    monkeypatch.setattr(config, "DATA_DIR", "/tmp/session-timer-data")
    r = client.get("/api/persistence")
    assert r.json() == {"persisted": True}


def test_banner_endpoint(client, monkeypatch):
    from session_timer import config

    # Unset (or empty) -> empty string; admin.js keeps the bar hidden.
    r = client.get("/api/banner")
    assert r.status_code == 200
    assert r.json() == {"message": config.ADMIN_MESSAGE or ""}

    # When the operator sets SESSION_TIMER_MESSAGE (here: patched after
    # import, which the route reads at request time), the text is served
    # and admin.js shows it in the sysadmin banner.
    monkeypatch.setattr(config, "ADMIN_MESSAGE", "Maintenance tonight")
    assert client.get("/api/banner").json() == {"message": "Maintenance tonight"}


def test_websocket_initial_state_and_push(client):
    with client.websocket_connect(f"/api/{NAME}/ws") as ws:
        initial = ws.receive_json()
        assert initial["name"] == NAME
        assert initial["generation"] is None
        assert initial["timezone"] is None
        assert initial["utc_offset"] is None
        assert initial["events"] == []
        assert initial["server_time"] > 0

        # a mutation over HTTP is pushed to the connected client
        resp = add_event(client, 0, "pushed", 1000, 600)
        assert resp.status_code == 200
        pushed = ws.receive_json()
        assert pushed["generation"] == 1
        assert pushed["events"][0]["name"] == "pushed"

        # client pings are answered
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}

        # a display syncing its font size is pushed too (so the admin
        # page's embedded preview mirrors it)
        r = client.put(f"/api/{NAME}/font-scale", json={"scale": 2.07})
        assert r.status_code == 200
        assert ws.receive_json()["font_scale"] == 2.07


def test_font_scale_syncs_without_generation_change(client):
    client.post(f"/api/{NAME}/timezone", json={"timezone": "Europe/Berlin"})
    before = client.get(f"/api/{NAME}/state").json()["generation"]
    r = client.put(f"/api/{NAME}/font-scale", json={"scale": 1.73})
    assert r.status_code == 200
    assert r.json()["font_scale"] == 1.73
    state = client.get(f"/api/{NAME}/state").json()
    assert state["font_scale"] == 1.73
    # presentation-only: schedule generation is untouched (displays
    # don't track it, so requiring one would break them)
    assert state["generation"] == before
    # validation
    assert client.put(f"/api/{NAME}/font-scale", json={"scale": 0}).status_code == 400
    assert client.put(
        f"/api/{NAME}/font-scale", json={"scale": -1}
    ).status_code == 400
    assert client.put(f"/api/{NAME}/font-scale", json={"scale": 1.73}).status_code == 200


def test_font_scale_never_creates_session(client):
    assert (
        client.put("/api/nowhere-session/font-scale", json={"scale": 1.2}).status_code
        == 404
    )
    assert store.get("nowhere-session") is None


def test_dim_level_syncs_without_generation_change(client):
    client.post(f"/api/{NAME}/timezone", json={"timezone": "Europe/Berlin"})
    before = client.get(f"/api/{NAME}/state").json()["generation"]
    r = client.put(f"/api/{NAME}/dim", json={"level": 0.5})
    assert r.status_code == 200
    assert r.json()["dim_level"] == 0.5
    state = client.get(f"/api/{NAME}/state").json()
    assert state["dim_level"] == 0.5
    # presentation-only: schedule generation is untouched (displays
    # don't track it, so requiring one would break them)
    assert state["generation"] == before
    # validation: below 0, above the 90% cap, and non-numeric
    assert client.put(f"/api/{NAME}/dim", json={"level": -0.1}).status_code == 400
    assert client.put(f"/api/{NAME}/dim", json={"level": 0.95}).status_code == 400
    assert client.put(f"/api/{NAME}/dim", json={"level": "dark"}).status_code == 422
    # boundary: exactly 0 and 0.9 are fine
    assert client.put(f"/api/{NAME}/dim", json={"level": 0}).status_code == 200
    assert client.put(f"/api/{NAME}/dim", json={"level": 0.9}).status_code == 200


def test_dim_never_creates_session(client):
    assert client.put("/api/nobody-home/dim", json={"level": 0.3}).status_code == 404
    assert store.get("nobody-home") is None


# ------------------------------------------------- hardening regressions


def test_mutation_with_invalid_session_name_is_404_and_creates_nothing(client):
    """Mutations validate the session name: junk names that no page URL
    could ever reach must not squat in the session cap."""
    r = client.post(
        "/api/bad%20name%20%C3%A9/events",
        json={"name": "x", "start_time": 0, "duration": 60},
        headers=gen(0),
    )
    assert r.status_code == 404
    assert "bad name é" not in store.sessions


def test_timezone_creation_at_session_cap_is_503(client):
    """The timezone route creates sessions (admin page stamps on load);
    a full store must surface as 503 like every other creation path,
    not as an unhandled 500."""
    for i in range(1024 - len(store.sessions)):
        store.sessions[f"filler{i:04d}"] = Session(name=f"filler{i:04d}")
    try:
        r = client.post("/api/capfull/timezone", json={"timezone": "UTC"})
        assert r.status_code == 503
        assert "capfull" not in store.sessions
    finally:
        for n in [n for n in store.sessions if n.startswith("filler")]:
            del store.sessions[n]


def test_csv_upload_size_cap(client):
    add_event(client, 0, "keep", 0, 60)
    big = "0:00,60,x\n" * 120_000  # ~1.1 MB, over the 1 MB limit
    r = client.put(
        f"/api/{NAME}/events.csv",
        content=big,
        headers={**gen(1), "Content-Type": "text/csv"},
    )
    assert r.status_code == 413
    state = client.get(f"/api/{NAME}/state").json()
    assert [e["name"] for e in state["events"]] == ["keep"]


def test_csv_upload_duration_upper_bound_rejected(client):
    r = client.put(
        f"/api/{NAME}/events.csv",
        content="0:00,99999999,x\n",
        headers={**gen(0), "Content-Type": "text/csv"},
    )
    assert r.status_code == 400
    assert "24 hours" in r.json()["detail"]


def test_csv_export_neutralizes_formula_prefixes(client):
    """A name starting with '=' would execute as a spreadsheet formula
    on import; the export prefixes a single quote (OWASP mitigation)."""
    add_event(client, 0, '=HYPERLINK("http://evil", "x")', 0, 60)
    add_event(client, 1, "plain name", 300, 60)
    r = client.get(f"/api/{NAME}/events.csv")
    assert r.status_code == 200
    body = r.text
    assert "'=HYPERLINK" in body
    # no un-prefixed formula cell remains
    assert '"=HYPERLINK' not in body
    assert ",plain name" in body


def test_duration_upper_bound_via_api(client):
    r = add_event(client, 0, "too long", 0, 86401)
    assert r.status_code == 400
    # boundary: exactly 24 h is fine
    r = add_event(client, 0, "all day", 0, 86400)
    assert r.status_code == 200
    # patch path (end/remaining conversions funnel through the check)
    eid = client.get(f"/api/{NAME}/state").json()["events"][0]["id"]
    r = client.patch(
        f"/api/{NAME}/events/{eid}", json={"duration": 10**20}, headers=gen(1)
    )
    assert r.status_code == 400


def test_security_headers_added_but_no_frame_restrictions(client):
    """Hardening headers on every response; framing stays unrestricted
    because the display page is meant to be embedded anywhere."""
    r = client.get("/static/style.css")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "x-frame-options" not in r.headers
    # No framework fingerprint: uvicorn's Server header is stripped.
    assert "server" not in r.headers


def test_healthz(client):
    """Liveness probe: 200 with an empty body (fingerprint-free), after
    exercising the real request path and the store.  Both slash forms
    answer identically (the slash form would otherwise hit the admin
    page route and serve an interface for a session named "healthz")."""
    for path in ("/healthz", "/healthz/"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.content == b"", path


def test_root_and_unknown_paths_are_empty_404s(client):
    """/ and every unmatched path answer with an empty 404: no JSON
    framework signature, nothing that hints at what is hosted here.
    Security is URL secrecy — probing the site must reveal nothing.
    (Single-segment valid-name paths are the one exception: they
    301-redirect to their slash form, uniformly, so the redirect itself
    reveals nothing about session existence — see
    test_admin_page_no_trailing_slash_redirects.)"""
    for path in (
        "/",
        "/no.such.page",
        "/x/y/z",
        "/favicon.ico",
        # The OpenAPI schema is disabled (openapi_url=None) and the dot
        # makes it an invalid session name: empty 404, no leak.  ("/docs"
        # would NOT qualify — with docs disabled it is an ordinary valid
        # name and 301-redirects like every other one-segment path.)
        "/openapi.json",
    ):
        r = client.get(path)
        assert r.status_code == 404, path
        assert r.content == b"", path
    # Unknown methods on known paths are equally clueless.
    r = client.delete("/")
    assert r.status_code == 404
    assert r.content == b""


def test_hub_broadcast_keeps_only_newest_for_slow_consumer():
    hub = Hub()
    q = hub.subscribe("hubtest")

    async def flood():
        for i in range(50):
            await hub.broadcast("hubtest", {"i": i})

    asyncio.run(flood())
    # Bounded: never grows past the cap, and the newest snapshot is kept.
    assert q.qsize() < Hub.QUEUE_MAX
    items = [q.get_nowait() for _ in range(q.qsize())]
    assert items[-1] == {"i": 49}
    hub.unsubscribe("hubtest", q)


# --------------------------------------------- problem-hunt regressions


def test_ws_binary_frame_does_not_crash_endpoint(client):
    """A binary frame has no text key, which used to raise an uncaught
    KeyError in receive_text() and kill the endpoint with a traceback.
    The client must now get a clean teardown and the server must stay
    healthy."""
    add_event(client, 0, "e", 0, 60)
    with client.websocket_connect(f"/api/{NAME}/ws") as ws:
        ws.receive_json()  # initial state
        (ws.send_bytes if hasattr(ws, "send_bytes") else ws.send)(b"\x01\x02")
        # Server must tear the connection down with an explicit close
        # (older starlette raises on receive; newer returns the close
        # message as a plain dict).  The failure mode being guarded
        # against is an endpoint exception leaking out and the client
        # never seeing a close at all.
        closed = False
        for _ in range(10):
            try:
                msg = ws.receive()
            except Exception:
                closed = True
                break
            if isinstance(msg, dict) and msg.get("type") == "websocket.close":
                closed = True
                break
        assert closed, "server never closed the connection after a binary frame"
    r = client.get(f"/api/{NAME}/state")
    assert r.status_code == 200


def test_oversized_json_body_rejected_413(client):
    """JSON bodies are bounded like CSV uploads: a 2 MB body must get
    413 before pydantic parses it, not a parse-then-reject 400."""
    r = client.post(
        f"/api/{NAME}/events",
        json={"name": "x" * 2_000_000, "start_time": 0, "duration": 60},
        headers=gen(0),
    )
    assert r.status_code == 413


def test_missing_generation_header_is_422(client):
    r = client.post(
        f"/api/{NAME}/events",
        json={"name": "x", "start_time": 0, "duration": 60},
    )
    assert r.status_code == 422  # a validation error, not a 500


def test_patch_end_time_before_start_rejected(client):
    add_event(client, 0, "e", 100, 60)
    eid = client.get(f"/api/{NAME}/state").json()["events"][0]["id"]
    r = client.patch(
        f"/api/{NAME}/events/{eid}", json={"end_time": 50}, headers=gen(1)
    )
    assert r.status_code == 400
    r = client.patch(
        f"/api/{NAME}/events/{eid}", json={"end_time": 100}, headers=gen(1)
    )
    assert r.status_code == 400  # end == start => zero duration


def test_font_scale_and_dim_bounds_rejected(client):
    client.post(f"/api/{NAME}/timezone", json={"timezone": "UTC"})
    for bad in (100, -1):
        r = client.put(f"/api/{NAME}/font-scale", json={"scale": bad})
        assert r.status_code == 400
    for bad in (0.95, -0.1):
        r = client.put(f"/api/{NAME}/dim", json={"level": bad})
        assert r.status_code == 400
    # boundaries themselves are valid
    r = client.put(f"/api/{NAME}/dim", json={"level": 0.9})
    assert r.status_code == 200


def test_unresolvable_timezone_fails_open(client):
    """A charset-valid but unresolvable zone is stored (fail-open):
    utc_offset is null and server-side now-of-day features explain
    how to fix it."""
    r = client.post(f"/api/{NAME}/timezone", json={"timezone": "Mars/Olympus"})
    assert r.status_code == 200
    assert r.json()["utc_offset"] is None
    r = client.post(
        f"/api/{NAME}/events/now", json={"duration": 60}, headers=gen(1)
    )
    assert r.status_code == 400
    assert "timezone" in r.json()["detail"]


def test_timezone_bumps_generation_only_on_change(client):
    r = client.post(f"/api/{NAME}/timezone", json={"timezone": "UTC"})
    assert r.json()["generation"] == 1
    r = client.post(f"/api/{NAME}/timezone", json={"timezone": "UTC"})
    assert r.json()["generation"] == 1  # same value: no bump


def test_hub_unsubscribes_after_websocket_disconnect(client):
    from session_timer.main import hub

    with client.websocket_connect(f"/api/{NAME}/ws") as ws:
        ws.receive_json()
    deadline = time.time() + 2
    while time.time() < deadline and hub._queues.get(NAME):
        time.sleep(0.05)
    assert NAME not in hub._queues


def test_lax_type_coercion_documented(client):
    """pydantic lax mode: numeric strings coerce, real floats fail."""
    r = client.post(
        f"/api/{NAME}/events",
        json={"name": "x", "start_time": "100", "duration": 60},
        headers=gen(0),
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/{NAME}/events",
        json={"name": "x", "start_time": 100.5, "duration": 60},
        headers=gen(1),
    )
    assert r.status_code == 422
