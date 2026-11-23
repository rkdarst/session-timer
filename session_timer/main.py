"""FastAPI application: HTML pages, JSON API, and WebSocket push.

See ``DESIGN.md`` for the behavioral spec and ``AGENTS.md`` for the
implementation plan.  Run with a *single* worker (in-memory state):

    uvicorn session_timer.main:app --port 8000

Set ``SESSION_TIMER_DATA_DIR`` to persist sessions across restarts via
periodic snapshots (see ``config.py``); unset = memory-only.  Set
``SESSION_TIMER_TZ`` to an IANA zone name to give sessions created
without an explicit timezone a default (clients fall back to their own
local clocks when nothing is resolvable).  Set ``SESSION_TIMER_MESSAGE``
to show a yellow sysadmin message at the top of the admin page (read
once at startup; empty/unset = no bar).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .store import SessionLimitError, SessionStore, StaleGenerationError

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

store = SessionStore()
_snapshot_path: Path | None = (
    Path(config.DATA_DIR) / "sessions.json" if config.DATA_DIR else None
)
_snapshot_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if _snapshot_path is not None:
        loaded = store.load_snapshot(_snapshot_path)
        logger.info(
            "%s snapshot %s (%d sessions)",
            "loaded" if loaded else "no usable",
            _snapshot_path,
            len(store.sessions),
        )
    expiry_task = asyncio.create_task(_expiry_loop())
    snapshot_task = (
        asyncio.create_task(_snapshot_loop()) if _snapshot_path is not None else None
    )
    try:
        yield
    finally:
        expiry_task.cancel()
        if snapshot_task is not None:
            snapshot_task.cancel()
        # Graceful shutdown (SIGTERM): capture everything, including
        # last_used drift since the last timer tick.
        if _snapshot_path is not None:
            await _save_snapshot()


async def _expiry_loop() -> None:
    """Periodically drop idle sessions (in-memory expiry per DESIGN.md)."""
    while True:
        await asyncio.sleep(3600)
        store.expire(config.SESSION_EXPIRY_SECONDS)


async def _snapshot_loop() -> None:
    """Periodically snapshot the store when it changed since last write."""
    saved = store.fingerprint()
    while True:
        await asyncio.sleep(config.SNAPSHOT_INTERVAL_SECONDS)
        if store.fingerprint() == saved:
            continue
        await _save_snapshot()
        saved = store.fingerprint()


async def _save_snapshot() -> None:
    assert _snapshot_path is not None
    async with _snapshot_lock:
        try:
            await asyncio.to_thread(store.save_snapshot, _snapshot_path)
        except OSError as exc:
            logger.error("failed to write snapshot %s: %s", _snapshot_path, exc)


app = FastAPI(title="session-timer", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def request_body_cap(request: Request, call_next):
    """Reject oversized request bodies with 413 before they are parsed.

    Bounded for the same reason as the CSV upload cap (config
    MAX_CSV_UPLOAD_BYTES, the same 1 MB): real payloads are tiny (an
    events table is 128 events x 128 chars), so anything larger is
    either a mistake or a memory-exhaustion attempt.  Starlette reads
    the whole body into memory before pydantic validates it, so route
    validation alone bounds nothing.
    """
    if request.url.path.startswith("/api/"):
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > config.MAX_CSV_UPLOAD_BYTES:
            # Note: return the response rather than raising — a raise
            # in user middleware sits outside the ExceptionMiddleware
            # layer and would surface as a 500.
            return PlainTextResponse(
                f"request body too large (limit "
                f"{config.MAX_CSV_UPLOAD_BYTES} bytes)",
                status_code=413,
            )
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Basic hardening headers on every HTTP response.

    Deliberately NO X-Frame-Options / CSP frame-ancestors: the display
    page is meant to be embedded in arbitrary pages.  Also drops the
    Server header uvicorn would otherwise add ("Server: uvicorn" is a
    fingerprint on a site that is supposed to reveal nothing).
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if "server" in response.headers:
        del response.headers["server"]
    return response


class Hub:
    """Per-session fan-out of state payloads to connected WebSocket clients."""

    # Per-subscriber cap: each queued item is a complete state snapshot,
    # so a stalled client that stops reading is dropped back to just the
    # newest payload instead of accumulating memory without bound.
    QUEUE_MAX = 16

    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, name: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
        self._queues.setdefault(name, set()).add(queue)
        return queue

    def unsubscribe(self, name: str, queue: asyncio.Queue) -> None:
        subscribers = self._queues.get(name)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                del self._queues[name]

    async def broadcast(self, name: str, payload: dict) -> None:
        for queue in list(self._queues.get(name, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow consumer: drop every queued (superseded) snapshot
                # and keep only this newest one.
                while True:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                queue.put_nowait(payload)


hub = Hub()


# --------------------------------------------------------------- helpers


def check_name(name: str) -> None:
    if not config.NAME_RE.fullmatch(name):
        # Deliberately 404 (not 400): bad names just "do not exist".
        raise HTTPException(status_code=404, detail="unknown session")


def empty_state(name: str) -> dict:
    return {
        "name": name,
        "generation": None,
        "timezone": None,
        "utc_offset": None,
        "server_time": time.time(),
        "font_scale": 1.0,
        "dim_level": 0.0,
        "events": [],
    }


async def broadcast_state(name: str, session) -> dict:
    payload = session.to_dict()
    await hub.broadcast(name, payload)
    return payload


def day_seconds_now(session) -> int:
    """The session timezone's current wall-clock seconds of day (0..86400).

    Needs the session's timezone (or the server-wide default): the
    server resolves the zone's current UTC offset, so the client never
    has to.  Without a resolvable zone there is no "now of day" and
    server-side relative features ("event now", "remaining") are
    refused — the admin sets a timezone from any browser.
    """
    offset = session.resolved_utc_offset()
    if offset is None:
        raise ValueError(
            "session timezone is not set (or unrecognized); set it from the "
            "admin page's ☰ menu — 'Set timezone to this browser'"
        )
    return int((time.time() + offset) % (24 * 3600))


def run_mutation(name: str, client_generation: int, mutate):
    """Shared mutation preamble: staleness check + error mapping.

    Per DESIGN.md: updates based on an old generation are rejected with
    409 and the current generation; the client tells the admin to
    refresh.  A *stale* request must not create the session: only a
    correctly-generational mutation does (a nonexistent session has
    generation 0, so the first-ever mutation sends 0).

    The session name is validated here too, so mutations cannot create
    junk-named sessions that no page URL could ever reach.
    """
    check_name(name)
    # Session-cap check first: a full store must surface as 503, not
    # as a misleading 409 (the request is not stale — it is refused).
    if store.get(name) is None and len(store.sessions) >= config.MAX_SESSIONS:
        try:
            store.get_or_create(name)
        except SessionLimitError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    session = store.get(name)
    server_generation = session.generation if session is not None else 0
    if client_generation != server_generation:
        raise HTTPException(
            status_code=409,
            detail={"error": "stale", "generation": server_generation},
        )
    if session is None:
        session = store.get_or_create(name)
    try:
        result = mutate(session)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SessionLimitError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return session, result


# --------------------------------------------------------------- models


class EventIn(BaseModel):
    name: str = ""
    start_time: int
    duration: int


class EventPatch(BaseModel):
    name: str | None = None
    start_time: int | None = None
    end_time: int | None = None
    duration: int | None = None
    remaining: int | None = None


class ShiftIn(BaseModel):
    field: Literal["start_time", "duration"]
    delta: int
    cascade: bool = True


class EventNowIn(BaseModel):
    name: str = ""
    duration: int


class TimezoneIn(BaseModel):
    timezone: str
    force: bool = False  # admin menu "Set timezone to this browser": replace


class FontScaleIn(BaseModel):
    scale: float


class DimIn(BaseModel):
    level: float


# --------------------------------------------------------------- pages


@app.get("/api/persistence")
async def persistence_info():
    """Whether snapshot persistence is enabled (drives the admin warning)."""
    return {"persisted": bool(config.DATA_DIR)}


@app.get("/api/banner")
async def banner_message():
    """Sysadmin message (SESSION_TIMER_MESSAGE) shown on the admin page.

    Static per deployment: the text is read from the environment at
    import (``config.ADMIN_MESSAGE``, stripped and capped at 512
    characters); the empty string means "no message" and admin.js keeps
    the bar hidden.  Read at request time so tests can monkeypatch it.
    """
    return {"message": config.ADMIN_MESSAGE or ""}


@app.get("/", include_in_schema=False)
async def root():
    """Empty 404: the root path gives no clue anything is hosted here.

    Same for every other unmatched path (the catch-all at the end of the
    route table).  FastAPI's default is a JSON body naming the framework,
    exactly the fingerprint the URL-secrecy model does not want to leak.
    """
    return Response(status_code=404)


@app.get("/{name}/", include_in_schema=False)
async def admin_page(name: str):
    check_name(name)
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/{name}", include_in_schema=False)
async def admin_page_no_slash(name: str):
    """Redirect the missing-trailing-slash form of the admin URL.

    The catch-all at the end of the route table would otherwise answer
    with a bare 404 (it matches before Starlette's automatic
    slash-redirect can fire).  An origin-form Location keeps the
    redirect scheme-agnostic — Starlette's automatic 307 bakes the
    request scheme into an absolute URL, which is wrong behind the
    TLS-terminating proxy this deployment assumes — and 301 marks the
    slash form canonical.  Invalid names (dots, etc.) get the same
    empty 404 as the catch-all — probing must reveal nothing — and the
    redirect is uniform for every valid name, so it reveals nothing
    about session existence either.
    """
    if not config.NAME_RE.fullmatch(name):
        # Empty 404 (not check_name's JSON exception): stay
        # fingerprint-free, like the "/" and catch-all handlers.
        return Response(status_code=404)
    return RedirectResponse(f"/{name}/", status_code=301)


@app.get("/{name}/display", include_in_schema=False)
async def display_page(name: str):
    check_name(name)
    return FileResponse(STATIC_DIR / "display.html")


@app.get("/{name}/iframe", include_in_schema=False)
async def iframe_page(name: str):
    """Embeddable progress-only view: full-viewport horizontal progress
    bar and a top-band "next" bar — no countdown text, no controls."""
    check_name(name)
    return FileResponse(STATIC_DIR / "iframe.html")


# --------------------------------------------------------------- state


@app.post("/api/{name}/timezone")
async def set_timezone(name: str, body: TimezoneIn):
    """Set the session timezone, an IANA zone name (admin page does this
    on load).

    Only the admin browsers know which room's clock the schedule
    follows (the server is timezone-agnostic apart from resolving the
    offset), so they post ``Intl``-derived IANA names here.  The first
    timezone wins; later calls are no-ops, but the response always
    carries the canonical state (including the zone's current
    ``utc_offset``).  With ``force`` (the admin menu's "Set timezone to
    this browser" action) the timezone is replaced while keeping every
    event's day-seconds, so the HH:MM schedule is unchanged but now
    means the new zone's wall clock.
    """
    check_name(name)
    session = store.get(name)
    if session is None:
        try:
            session = store.get_or_create(name)
        except SessionLimitError as exc:
            # Same mapping as run_mutation: a full store is 503, not a
            # misleading 409 or an unhandled 500.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        changed = session.set_timezone(body.timezone, force=body.force)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if changed:
        await broadcast_state(name, session)
    return session.to_dict()


@app.get("/api/{name}/state")
async def get_state(name: str):
    check_name(name)
    session = store.get(name)
    if session is None:
        return empty_state(name)
    session.touch()
    return session.to_dict()


@app.put("/api/{name}/font-scale")
async def put_font_scale(name: str, body: FontScaleIn):
    """Sync a display browser's countdown text-size choice.

    The text size is presentation, not schedule state: display browsers
    don't track the generation counter, so this neither sends nor bumps
    the generation — it updates the session and pushes state so embedded
    previews on admin pages mirror the choice.  A nonexistent session is
    a 404: read-only paths never create sessions.
    """
    check_name(name)
    session = store.get(name)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    try:
        session.set_font_scale(body.scale)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await broadcast_state(name, session)


@app.put("/api/{name}/dim")
async def put_dim(name: str, body: DimIn):
    """Sync the room-dimming level (0 = off, 0.9 = 90% black).

    Like the font scale this is presentation, not schedule state: no
    generation is sent or bumped, and a nonexistent session is a 404.
    Displays and admin pages both PUT here; every panel gets the change
    via the state broadcast.
    """
    check_name(name)
    session = store.get(name)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    try:
        session.set_dim_level(body.level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await broadcast_state(name, session)


@app.websocket("/api/{name}/ws")
async def websocket_endpoint(websocket: WebSocket, name: str):
    if not config.NAME_RE.fullmatch(name):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    queue = hub.subscribe(name)
    try:
        session = store.get(name)
        initial = session.to_dict() if session is not None else empty_state(name)
        await websocket.send_json(initial)
        while True:
            receive_task = asyncio.create_task(websocket.receive_text())
            queue_task = asyncio.create_task(queue.get())
            done, pending = await asyncio.wait(
                {receive_task, queue_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            pushed = None
            client_text = None
            disconnect = False
            # Read ALL completed results before acting: if the client
            # disconnects in the same iteration that a state push
            # completed, the push must still be delivered (a naive
            # `break` on the disconnect would drop the already-consumed
            # message).
            for task in done:
                try:
                    if task is queue_task:
                        pushed = task.result()
                    else:
                        client_text = task.result()
                except WebSocketDisconnect:
                    disconnect = True
                except Exception:
                    # Malformed frame (e.g. binary => KeyError inside
                    # receive_text()): this client is speaking a protocol
                    # we do not understand.  Tear the connection down
                    # cleanly instead of crashing the endpoint.
                    disconnect = True
            try:
                if pushed is not None:
                    await websocket.send_json(pushed)
                if client_text == "ping":
                    await websocket.send_json({"type": "pong"})
            except (WebSocketDisconnect, RuntimeError):
                # Client went away mid-send.
                break
            if disconnect:
                # Explicit close: returning without one would leave a
                # still-connected client waiting for a message that
                # never comes (that is exactly what a malformed frame
                # triggers).  A real disconnect simply fails the close
                # attempt, which is caught below.
                try:
                    await websocket.close(code=1008)
                except (WebSocketDisconnect, RuntimeError):
                    pass  # client already gone
                break
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(name, queue)


# --------------------------------------------------------------- mutations


@app.post("/api/{name}/events")
async def add_event(name: str, body: EventIn, x_generation: Annotated[int, Header()]):
    session, _ = run_mutation(
        name,
        x_generation,
        lambda s: s.add_event(body.name, body.start_time, body.duration),
    )
    return await broadcast_state(name, session)


@app.post("/api/{name}/events/now")
async def event_now(
    name: str, body: EventNowIn, x_generation: Annotated[int, Header()]
):
    session, _ = run_mutation(
        name,
        x_generation,
        lambda s: s.add_event(body.name, day_seconds_now(s), body.duration),
    )
    return await broadcast_state(name, session)


@app.patch("/api/{name}/events/{event_id}")
async def patch_event(
    name: str, event_id: str, body: EventPatch, x_generation: Annotated[int, Header()]
):
    fields = body.model_dump(exclude_unset=True)

    def mutate(s):
        # "remaining" is relative to now, which is service-day seconds.
        if "remaining" in fields:
            fields["now"] = day_seconds_now(s)
        return s.update_event(event_id, **fields)

    session, _ = run_mutation(name, x_generation, mutate)
    return await broadcast_state(name, session)


@app.post("/api/{name}/events/{event_id}/shift")
async def shift_event(
    name: str, event_id: str, body: ShiftIn, x_generation: Annotated[int, Header()]
):
    session, _ = run_mutation(
        name,
        x_generation,
        lambda s: s.shift(event_id, body.field, body.delta, body.cascade),
    )
    return await broadcast_state(name, session)


@app.delete("/api/{name}/events/{event_id}")
async def delete_event(
    name: str, event_id: str, x_generation: Annotated[int, Header()]
):
    session, _ = run_mutation(name, x_generation, lambda s: s.delete_event(event_id))
    return await broadcast_state(name, session)


# --------------------------------------------------------------- CSV


@app.get("/api/{name}/events.csv", response_class=PlainTextResponse)
async def get_events_csv(name: str):
    check_name(name)
    session = store.get(name)
    if session is None:
        text = "start_time,duration_sec,name\n"
    else:
        text = session.to_csv()
    return PlainTextResponse(
        text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="session-timer-{name}.csv"'},
    )


@app.put("/api/{name}/events.csv")
async def put_events_csv(
    name: str, request: Request, x_generation: Annotated[int, Header()]
):
    limit = config.MAX_CSV_UPLOAD_BYTES
    detail = f"CSV upload too large (limit {limit} bytes)"
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=detail)
    raw = await request.body()
    if len(raw) > limit:
        # Chunked requests without Content-Length are bounded only here.
        raise HTTPException(status_code=413, detail=detail)
    text = raw.decode("utf-8", errors="replace")
    # Upload *replaces* all events (per DESIGN.md).
    session, _ = run_mutation(name, x_generation, lambda s: s.replace_from_csv(text))
    return await broadcast_state(name, session)


# ------------------------------------------ catch-all: stay fingerprint-free
# Registered LAST, so every real route matches first.  Unknown paths (and
# unknown methods on known paths) get the same empty 404 as "/": no JSON
# framework signature, no route listing, nothing.


@app.api_route(
    "/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def catch_all(rest: str):
    return Response(status_code=404)


# --------------------------------------------------------------- entry point


def run() -> None:
    """Entry point for the `session-timer` console script (pyproject).

    Single worker on purpose: all state is in this process' memory.
    --no-server-header: the site should not advertise what it runs
    (URL-secrecy model; see the root handler's empty 404s).
    """
    import uvicorn

    uvicorn.run(
        "session_timer.main:app",
        host=config.HOST,
        port=config.PORT,
        workers=1,
        server_header=False,
    )

