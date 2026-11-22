"""API tests using Starlette's TestClient (HTTP + WebSocket)."""

import time

import pytest
from fastapi.testclient import TestClient

from session_timer.main import app, store

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
    client.post(f"/api/{NAME}/anchor", json={"anchor": 1700000000})
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
    assert resp.json() == {
        "name": NAME,
        "generation": None,
        "anchor": None,
        "font_scale": 1.0,
        "events": [],
    }
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
    anchor = 1700000000
    assert client.post(f"/api/{NAME}/anchor", json={"anchor": anchor}).status_code == 200
    resp = client.post(
        f"/api/{NAME}/events/now",
        json={"name": "now", "duration": 600},
        headers=gen(1),
    )
    assert resp.status_code == 200
    event = resp.json()["events"][0]
    # start_time is service-day seconds: now minus the day anchor
    assert abs(event["start_time"] - (time.time() - anchor)) < 5


def test_event_now_without_anchor_400(client):
    resp = client.post(
        f"/api/{NAME}/events/now",
        json={"name": "now", "duration": 600},
        headers=gen(0),
    )
    assert resp.status_code == 400


def test_patch_remaining_uses_anchor(client):
    anchor = 1700000000
    client.post(f"/api/{NAME}/anchor", json={"anchor": anchor})
    event_id = add_event(client, 1, "", 100, 600).json()["events"][0]["id"]
    resp = client.patch(
        f"/api/{NAME}/events/{event_id}",
        json={"remaining": 60},
        headers=gen(2),
    )
    assert resp.status_code == 200
    event = resp.json()["events"][0]
    assert abs(event["start_time"] - (time.time() - anchor + 60)) < 5


def test_set_anchor_endpoint(client):
    resp = client.post(f"/api/{NAME}/anchor", json={"anchor": 1700000000})
    assert resp.status_code == 200
    data = resp.json()
    assert data["anchor"] == 1700000000
    assert data["generation"] == 1  # creation (0) + anchor bump
    assert store.get(NAME) is not None
    # a second POST is a no-op: the first anchor wins
    resp = client.post(f"/api/{NAME}/anchor", json={"anchor": 1800000000})
    assert resp.json()["anchor"] == 1700000000
    assert resp.json()["generation"] == 1



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
    assert client.get("/api/bad.name/state").status_code == 404
    assert client.get("/api/bad.name/display").status_code == 404


def test_pages_served(client):
    for path, marker in [
        (f"/{NAME}/", "events-table"),
        (f"/{NAME}/", "preview-frame"),
        (f"/{NAME}/display", 'id="timer"'),
    ]:
        resp = client.get(path)
        assert resp.status_code == 200
        assert marker in resp.text


def test_websocket_initial_state_and_push(client):
    with client.websocket_connect(f"/api/{NAME}/ws") as ws:
        initial = ws.receive_json()
        assert initial == {
            "name": NAME,
            "generation": None,
            "anchor": None,
            "font_scale": 1.0,
            "events": [],
        }

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
    client.post(f"/api/{NAME}/anchor", json={"anchor": 1700000000})
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
