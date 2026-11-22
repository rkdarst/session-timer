"""FastAPI application: HTML pages, JSON API, and WebSocket push.

See ``DESIGN.md`` for the behavioral spec and ``AGENTS.md`` for the
implementation plan.  Run with a *single* worker (in-memory state):

    uvicorn session_timer.main:app --port 8000

Set ``SESSION_TIMER_DATA_DIR`` to persist sessions across restarts via
periodic snapshots (see ``config.py``); unset = memory-only.
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
from fastapi.responses import FileResponse, PlainTextResponse
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


class Hub:
    """Per-session fan-out of state payloads to connected WebSocket clients."""

    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, name: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
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
        "anchor": None,
        "font_scale": 1.0,
        "events": [],
    }


async def broadcast_state(name: str, session) -> dict:
    payload = session.to_dict()
    await hub.broadcast(name, payload)
    return payload


def now_in_day(session) -> int:
    """Current time as service-day seconds (needs the session anchor)."""
    if session.anchor is None:
        raise ValueError(
            "session has no day anchor; open the admin page first to set it"
        )
    return int(time.time()) - session.anchor


def run_mutation(name: str, client_generation: int, mutate):
    """Shared mutation preamble: staleness check + error mapping.

    Per DESIGN.md: updates based on an old generation are rejected with
    409 and the current generation; the client tells the admin to
    refresh.  A *stale* request must not create the session: only a
    correctly-generational mutation does (a nonexistent session has
    generation 0, so the first-ever mutation sends 0).
    """
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


class AnchorIn(BaseModel):
    anchor: int


class FontScaleIn(BaseModel):
    scale: float


# --------------------------------------------------------------- pages


@app.get("/{name}/", include_in_schema=False)
async def admin_page(name: str):
    check_name(name)
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/{name}/display", include_in_schema=False)
async def display_page(name: str):
    check_name(name)
    return FileResponse(STATIC_DIR / "display.html")


# --------------------------------------------------------------- state


@app.post("/api/{name}/anchor")
async def set_anchor(name: str, body: AnchorIn):
    """Set the session's service-day anchor (admin page does this on load).

    Only the admin browser knows the intended local day (the server is
    timezone-agnostic), so it computes "local noon minus 12h" and posts
    it here.  The first anchor wins; later calls are no-ops, but the
    response always carries the canonical state.
    """
    check_name(name)
    if body.anchor <= 0:
        raise HTTPException(status_code=400, detail="anchor must be a positive unixtime")
    session = store.get(name)
    if session is None:
        session = store.get_or_create(name)
    if session.set_anchor(body.anchor):
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
            try:
                if pushed is not None:
                    await websocket.send_json(pushed)
                if client_text == "ping":
                    await websocket.send_json({"type": "pong"})
            except (WebSocketDisconnect, RuntimeError):
                # Client went away mid-send.
                break
            if disconnect:
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
        lambda s: s.add_event(body.name, now_in_day(s), body.duration),
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
            fields["now"] = now_in_day(s)
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
    text = (await request.body()).decode("utf-8", errors="replace")
    # Upload *replaces* all events (per DESIGN.md).
    session, _ = run_mutation(name, x_generation, lambda s: s.replace_from_csv(text))
    return await broadcast_state(name, session)


# --------------------------------------------------------------- entry point


def run() -> None:
    """Entry point for the `session-timer` console script (pyproject).

    Single worker on purpose: all state is in this process' memory.
    """
    import uvicorn

    uvicorn.run(
        "session_timer.main:app", host=config.HOST, port=config.PORT, workers=1
    )

