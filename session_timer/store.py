"""In-memory session store for the web-based session timer.

See ``DESIGN.md`` for the behavioral spec and ``AGENTS.md`` for
implementation notes.  Event times follow the GTFS "service time"
convention: ``start_time`` is seconds since "noon minus 12 hours" of
the session's day (local midnight, except unambiguous on DST-change
nights); each session stores an ``anchor`` (the unixtime of its
"noon minus 12h") so clients can convert to absolute times.

The store is synchronous and is used from a single asyncio worker (one
uvicorn process, per DESIGN.md).  Request handlers never await between
reading and writing a session, so plain dict/list operations are
sufficient and no locking is required.

Sessions are created lazily by admin activity; displays use ``get()``
which never creates anything.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

CSV_COLUMNS = ("start_time", "duration_sec", "name")

SNAPSHOT_VERSION = 1


class StaleGenerationError(Exception):
    """A client submitted a change based on an outdated generation."""


class SessionLimitError(Exception):
    """The session cap is full and nothing may be evicted."""


def _as_int(value: object, what: str) -> int:
    """Coerce strict integers (JSON numbers); reject bools and fractions."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{what} must be an integer")
    return int(value)


_SERVICE_TIME_RE = re.compile(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$")


def format_service_time(day_seconds: int) -> str:
    """Service-day seconds -> "H:MM" (or "H:MM:SS" with seconds).

    Hours are not zero-padded and may exceed 23 for events just past
    midnight (GTFS-style service times).
    """
    total = int(day_seconds)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if seconds:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{hours}:{minutes:02d}"


def parse_service_time(text: str) -> int:
    """Parse "H:MM" or "H:MM:SS" (24h) -> service-day seconds.

    Raises ValueError on anything else — including plain integers, so
    old unixtime CSVs fail loudly instead of importing garbage.
    """
    cleaned = text.strip()
    match = _SERVICE_TIME_RE.fullmatch(cleaned)
    if match is None:
        raise ValueError(f"invalid start_time {cleaned!r}: use H:MM[:SS]")
    hours, minutes, seconds = int(match[1]), int(match[2]), int(match[3] or 0)
    if hours > 47 or minutes > 59 or seconds > 59:
        raise ValueError(f"invalid start_time {cleaned!r}: out of range")
    return hours * 3600 + minutes * 60 + seconds


def _check_name(name: object) -> str:
    if name is None:
        return ""
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    cleaned = name.strip()
    if len(cleaned) > config.EVENT_NAME_MAX_LENGTH:
        raise ValueError(
            f"name must be at most {config.EVENT_NAME_MAX_LENGTH} characters"
        )
    return cleaned


@dataclass
class Event:
    """One timed item in a session."""

    name: str = ""
    start_time: int = 0  # service-day seconds since the session's noon-12h
    duration: int = 0  # seconds
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def end_time(self) -> int:
        return self.start_time + self.duration

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "start_time": self.start_time,
            "duration": self.duration,
        }

@dataclass
class Session:
    """A named collection of events plus a generation counter.

    The generation counter is the staleness marker from DESIGN.md: the
    server holds canonical state, pushes the new generation to clients
    on every change, and rejects client updates carrying an older
    generation.
    """

    name: str
    generation: int = 0
    events: list[Event] = field(default_factory=list)
    last_used: float = field(default_factory=time.time)
    # Unixtime of the session's "noon minus 12h" (day-second 0).  Set
    # once by the first admin browser (only the browser knows the
    # intended local day); None until then.
    anchor: int | None = None
    # Countdown text-size scale chosen on a display browser (one of
    # display.js's FONT_SCALES steps).  Synced to the session so the
    # admin page's embedded preview can mirror it; presentation-only
    # state (never bumps the generation).
    display_font_scale: float = 1.0
    # Room-dimming overlay level (0 = off, 0.9 = 90% black), chosen via
    # the darker/lighter buttons on any panel.  Synced so an admin can
    # dim displays they cannot physically reach; presentation-only
    # state (never bumps the generation).
    dim_level: float = 0.0

    # ------------------------------------------------- bookkeeping

    def touch(self) -> None:
        self.last_used = time.time()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "generation": self.generation,
            "anchor": self.anchor,
            "font_scale": self.display_font_scale,
            "dim_level": self.dim_level,
            "events": [event.to_dict() for event in self.events],
        }

    def set_font_scale(self, scale: object) -> None:
        """Set the countdown text-size scale a display browser chose.

        Presentation-only state: the generation counter is untouched
        (this is not schedule state, and display browsers don't track
        generations), but the change is broadcast in the state payload
        and persisted via snapshots.
        """
        if isinstance(scale, bool) or not isinstance(scale, (int, float)):
            raise ValueError("scale must be a number")
        scale = float(scale)
        if not 0 < scale <= 16:
            raise ValueError("scale must be between 0 and 16")
        self.display_font_scale = scale
        self.touch()

    def set_dim_level(self, level: object) -> None:
        """Set the room-dimming overlay level (0 = off, 0.9 = 90% black).

        Presentation-only state like ``display_font_scale``: no
        generation bump, broadcast in the state payload, persisted via
        snapshots.  Capped at 0.9 so the screen never goes fully black.
        """
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise ValueError("level must be a number")
        level = float(level)
        if not 0 <= level <= 0.9:
            raise ValueError("level must be between 0 and 0.9")
        self.dim_level = level
        self.touch()

    def set_anchor(self, anchor: object) -> bool:
        """Set the service-day anchor; returns True when it changed.

        Only the first value wins: later calls are no-ops so a second
        admin cannot move the day out from under a running session.
        """
        anchor = _as_int(anchor, "anchor")
        if anchor <= 0:
            raise ValueError("anchor must be a positive unixtime")
        if self.anchor is not None:
            return False
        self.anchor = anchor
        self._bump()
        return True

    def check_generation(self, client_generation: object) -> None:
        if client_generation != self.generation:
            raise StaleGenerationError(
                f"stale generation: client has {client_generation!r}, "
                f"server has {self.generation}"
            )

    def _bump(self) -> None:
        self.generation += 1
        self.touch()

    # ------------------------------------------------- lookups

    def find_event(self, event_id: str) -> Event:
        for event in self.events:
            if event.id == event_id:
                return event
        raise KeyError(f"no event with id {event_id!r}")

    def current_event(self, now: float) -> Event | None:
        """The event the display should show.

        The current event is the one with the greatest ``start_time``
        that has already started (``start_time <= now``); overlapping
        events therefore resolve to the most recently started one.  An
        event stays current (counting up negative) until the next event
        starts.  Before the first event starts there is no current
        event.  ``now`` must be in the same time base as the events
        (service-day seconds, or absolute — only comparisons matter).
        """
        current: Event | None = None
        for event in self.events:
            if event.start_time <= now and (
                current is None or event.start_time > current.start_time
            ):
                current = event
        return current

    def overlapping_ids(self) -> set[str]:
        """IDs of all events whose time windows overlap another event."""
        ids: set[str] = set()
        events = self.events
        for i, a in enumerate(events):
            for b in events[i + 1 :]:
                if a.start_time < b.end_time and b.start_time < a.end_time:
                    ids.add(a.id)
                    ids.add(b.id)
        return ids

    # ------------------------------------------------- mutations
    # Every successful mutation bumps the generation and refreshes
    # last_used.  Validation happens before any state changes, so a
    # rejected mutation leaves the generation untouched.

    def add_event(self, name: str, start_time: object, duration: object) -> Event:
        clean_name = _check_name(name)
        start_time = _as_int(start_time, "start_time")
        duration = _as_int(duration, "duration")
        if duration <= 0:
            raise ValueError("duration must be positive")
        if len(self.events) >= config.MAX_EVENTS_PER_SESSION:
            raise ValueError(
                f"session already has the maximum of "
                f"{config.MAX_EVENTS_PER_SESSION} events"
            )
        event = Event(name=clean_name, start_time=start_time, duration=duration)
        self.events.append(event)
        self._bump()
        return event

    def delete_event(self, event_id: str) -> None:
        self.find_event(event_id)  # raises KeyError if missing
        self.events = [event for event in self.events if event.id != event_id]
        self._bump()

    def update_event(
        self,
        event_id: str,
        *,
        name: object = None,
        start_time: object = None,
        end_time: object = None,
        duration: object = None,
        remaining: object = None,
        now: float | None = None,
    ) -> Event:
        """Update one event, converting between time representations.

        Order of application (a later argument overrides an earlier
        one), matching the admin controls in DESIGN.md:

        1. ``start_time`` (service-day seconds)
        2. ``remaining``: ``start_time = now + remaining`` (``now`` is
           required and must be in the same time base)
        3. ``end_time``: ``duration = end_time - start_time``
        4. ``duration`` (absolute seconds)
        """
        event = self.find_event(event_id)

        new_name = event.name
        if name is not None:
            new_name = _check_name(name)

        new_start = event.start_time
        if start_time is not None:
            new_start = _as_int(start_time, "start_time")
        if remaining is not None:
            if now is None:
                raise ValueError("now is required when updating remaining")
            new_start = int(now) + _as_int(remaining, "remaining")

        new_duration = event.duration
        if end_time is not None:
            new_duration = _as_int(end_time, "end_time") - new_start
        if duration is not None:
            new_duration = _as_int(duration, "duration")
        if new_duration <= 0:
            raise ValueError("duration must be positive")

        event.name = new_name
        event.start_time = new_start
        event.duration = new_duration
        self._bump()
        return event


    def shift(
        self, event_id: str, field_name: str, delta: object, cascade: bool = True
    ) -> None:
        """Increase/decrease ``start_time`` or ``duration`` of an event.

        With ``cascade`` (the "delaying subsequent events" control in
        DESIGN.md) the same ``delta`` is applied to the start time of
        every event that starts strictly later than the shifted event's
        original start time ("subsequent events, as found by start
        time").
        """
        if field_name not in ("start_time", "duration"):
            raise ValueError("field must be 'start_time' or 'duration'")
        delta = _as_int(delta, "delta")
        event = self.find_event(event_id)
        anchor_start = event.start_time
        if field_name == "start_time":
            event.start_time += delta
        else:
            if event.duration + delta <= 0:
                raise ValueError("duration must stay positive")
            event.duration += delta
        if cascade:
            for other in self.events:
                if other.id != event.id and other.start_time > anchor_start:
                    other.start_time += delta
        self._bump()

    def replace_events(self, items: list[dict]) -> int:
        """Replace all events (used by CSV upload, which *replaces*)."""
        if len(items) > config.MAX_EVENTS_PER_SESSION:
            raise ValueError(
                f"at most {config.MAX_EVENTS_PER_SESSION} events per session "
                f"(got {len(items)})"
            )
        events: list[Event] = []
        for i, item in enumerate(items):
            clean_name = _check_name(item.get("name"))
            start_time = _as_int(item.get("start_time"), f"item {i}: start_time")
            duration = _as_int(item.get("duration"), f"item {i}: duration")
            if duration <= 0:
                raise ValueError(f"item {i}: duration must be positive")
            events.append(
                Event(name=clean_name, start_time=start_time, duration=duration)
            )
        self.events = events
        self._bump()
        return len(events)


    # ------------------------------------- CSV (download/upload)

    def to_csv(self) -> str:
        """Serialize as CSV: start_time (H:MM[:SS]),duration_sec,name."""
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for event in sorted(self.events, key=lambda e: e.start_time):
            writer.writerow(
                [format_service_time(event.start_time), event.duration, event.name]
            )
        return buf.getvalue()

    def replace_from_csv(self, text: str) -> int:
        """Replace all events from CSV text; returns the event count.

        Accepts an optional header row.  Columns are
        ``start_time,duration_sec,name`` (name optional); start_time is
        "H:MM[:SS]" service-day time (GTFS-style).  A leading UTF-8 BOM
        (as written by Excel's "CSV UTF-8" on Windows) is tolerated.
        """
        # Without stripping it, the first cell becomes "\ufeffstart_time"
        # (or "\ufeff0:00") and fails to parse.
        if text.startswith("﻿"):
            text = text[1:]
        rows = list(csv.reader(io.StringIO(text)))
        if rows and rows[0] and rows[0][0].strip().lower().startswith("start_time"):
            rows = rows[1:]
        items: list[dict] = []
        for lineno, row in enumerate(rows, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) < 2:
                raise ValueError(
                    f"CSV line {lineno}: expected start_time and duration columns"
                )
            try:
                start_time = parse_service_time(row[0])
                duration = int(row[1].strip())
            except ValueError as exc:
                raise ValueError(f"CSV line {lineno}: {exc}") from exc
            name = row[2].strip() if len(row) > 2 else ""
            if duration <= 0:
                raise ValueError(f"CSV line {lineno}: duration must be positive")
            items.append(
                {"name": name, "start_time": start_time, "duration": duration}
            )
        return self.replace_events(items)


class SessionStore:
    """All sessions, in memory only (nothing survives a restart)."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}

    def get(self, name: str) -> Session | None:
        """Existing session or None; never creates (display path)."""
        return self.sessions.get(name)

    def get_or_create(self, name: str) -> Session:
        """Existing session, or a new empty one (admin path).

        Enforces ``config.MAX_SESSIONS``: an immediate expiry pass frees
        long-idle slots first, then sessions idle beyond
        ``config.SESSION_EVICTION_IDLE_SECONDS`` are evicted oldest-first;
        if the store is still full, creation is refused (an active
        session is never evicted).
        """
        session = self.sessions.get(name)
        if session is not None:
            return session
        if len(self.sessions) >= config.MAX_SESSIONS:
            # Free obviously-stale slots first (also refreshes the set
            # we are about to scan for eviction candidates).
            self.expire(config.SESSION_EXPIRY_SECONDS)
            cutoff = time.time() - config.SESSION_EVICTION_IDLE_SECONDS
            idle = sorted(
                (s for s in self.sessions.values() if s.last_used <= cutoff),
                key=lambda s: s.last_used,
            )
            need = len(self.sessions) - config.MAX_SESSIONS + 1
            if len(idle) < need:
                raise SessionLimitError(
                    f"session limit of {config.MAX_SESSIONS} reached"
                )
            for evicted in idle[:need]:
                del self.sessions[evicted.name]
                logger.info("evicted idle session %r (session cap)", evicted.name)
        session = Session(name=name)
        self.sessions[name] = session
        return session

    def fingerprint(self) -> frozenset:
        """Cheap change marker: names + generations of all sessions.

        Every state change bumps some session's generation, and
        expiry/eviction change the key set, so comparing fingerprints
        tells whether the store changed since the last snapshot.
        (``last_used`` updates do not count — snapshotting them up to
        one interval late is harmless.)
        """
        return frozenset(
            (name, session.generation, session.display_font_scale, session.dim_level)
            for name, session in self.sessions.items()
        )

    def expire(self, max_age_seconds: float, now: float | None = None) -> list[str]:
        """Drop sessions unused for ``max_age_seconds``; returns names."""
        if now is None:
            now = time.time()
        expired = [
            name
            for name, session in self.sessions.items()
            if now - session.last_used > max_age_seconds
        ]
        for name in expired:
            del self.sessions[name]
        return expired

    # ------------------------------------- snapshot persistence (opt-in)

    def save_snapshot(self, path: str | Path) -> None:
        """Atomically write all sessions to ``path`` (JSON).

        Writes to a temp file in the same directory, fsyncs, then
        ``os.replace()``s into place, so a crash mid-write leaves the
        previous snapshot intact.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": SNAPSHOT_VERSION,
            "sessions": {
                name: {
                    "generation": session.generation,
                    "anchor": session.anchor,
                    "last_used": session.last_used,
                    "font_scale": session.display_font_scale,
                    "dim_level": session.dim_level,
                    "events": [
                        {
                            "id": event.id,
                            "name": event.name,
                            "start_time": event.start_time,
                            "duration": event.duration,
                        }
                        for event in session.events
                    ],
                }
                for name, session in self.sessions.items()
            },
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def load_snapshot(self, path: str | Path) -> bool:
        """Load sessions from ``path``; returns True when loaded.

        A missing file is a normal first start (returns False).  A
        corrupt or wrong-version file is renamed aside (``*.corrupt``)
        and the store starts empty rather than crash-looping.
        """
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        try:
            loaded = self._parse_snapshot(text)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            quarantine = path.with_suffix(path.suffix + ".corrupt")
            try:
                os.replace(path, quarantine)
            except OSError:
                pass
            logger.warning(
                "ignoring corrupt snapshot %s (%s); starting empty; saved "
                "aside as %s",
                path,
                exc,
                quarantine,
            )
            return False
        self.sessions = loaded
        return True

    def _parse_snapshot(self, text: str) -> dict[str, Session]:
        data = json.loads(text)
        if data.get("version") != SNAPSHOT_VERSION:
            raise ValueError(f"unsupported snapshot version {data.get('version')!r}")
        sessions_data = data["sessions"]
        if not isinstance(sessions_data, dict):
            raise ValueError("sessions must be an object")
        loaded: dict[str, Session] = {}
        for name, sd in sessions_data.items():
            if not isinstance(name, str) or not isinstance(sd, dict):
                raise ValueError("bad session entry")
            session = Session(
                name=name,
                generation=int(sd["generation"]),
                anchor=sd["anchor"],
                last_used=float(sd["last_used"]),
                # snapshots predating font-scale sync lack the key
                display_font_scale=float(sd.get("font_scale", 1.0)),
                # ... and those predating room-dimming lack this one
                dim_level=float(sd.get("dim_level", 0.0)),
            )
            for ed in sd["events"]:
                session.events.append(
                    Event(
                        name=str(ed["name"]),
                        start_time=int(ed["start_time"]),
                        duration=int(ed["duration"]),
                        id=str(ed["id"]),
                    )
                )
            loaded[name] = session
        return loaded
