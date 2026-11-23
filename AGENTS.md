# AGENTS.md

Guidance for AI agents (and humans) working on this repository.  The
user-facing specification is in `DESIGN.md` — read it first; this file
describes the implementation plan and conventions.

## Project overview

Web-based session timer per `DESIGN.md`: FastAPI server, in-memory
sessions keyed by hard-to-guess names, admin page (edit events) and
read-only display page (countdown + progress bar), updates pushed over
WebSockets.  Single uvicorn worker (in-memory state).  No persistence,
no auth, no SSL (handled by another layer).

## Implementation status

Phases 1–4 are implemented and verified:

- `session_timer/store.py` — sessions/events/generation counters, CSV
- `session_timer/main.py` — HTTP + JSON API + WebSocket push
- `session_timer/static/` — admin and display UIs (plain HTML/JS/CSS);
  the admin page embeds a live preview of the display page in an
  iframe (`?embed=1`), which mirrors the text size a display browser
  last chose (`PUT /api/{name}/font-scale`, presentation-only — no
  generation bump; included in state payloads and snapshots); the same
  pattern carries the room-dim level (`PUT /api/{name}/dim`, steps up
  to 90% black) with darker/lighter buttons on both pages
- `tests/test_store.py`, `tests/test_api.py` — all tests passing
- Admin toolbar: add/event-now/cascade/preview/dim buttons stay
  visible; overflow actions (display link, QR, CSV download/upload,
  the "Manual edits delay later events" checkbox, dark-mode toggle)
  live behind a ☰ hamburger menu that auto-closes;
  a permanent "Events are NOT saved long term" banner sits at the top
  and a Help button opens a concise popup
- Manual-edit cascade (☰ menu checkbox, off by default): a typed
  start/duration edit then goes to the shift endpoint as
  `delta = new − old` with `cascade: true` (duration edits shift later
  starts) instead of an absolute PATCH — reusing the store's
  all-or-nothing cascade validation; no server changes.  Delta 0
  sends nothing (shift bumps the generation unconditionally); a
  rejected cascade re-renders the table before the error banner, so
  the typed value cannot linger stale.
- Sysadmin banner: `SESSION_TIMER_MESSAGE` (server environment, read
  once at startup, stripped and capped at 512 chars) is served by
  `GET /api/banner` and shown by admin.js inside the yellow warning
  banner on the admin page, above the data-loss text (textContent
  only; hidden when unset; enabling persistence hides only the
  data-loss text — a sysadmin message keeps the banner up, and the box
  vanishes only when both texts are absent; display page unaffected;
  not dynamically updateable — restart to change).  Tests: 109 passing.
- Verified live with uvicorn: pages 200, 409 on stale generation, CSV
  round-trip, WebSocket initial state + push + ping/pong
- Audit fixes (post-timezone-redesign): mutations validate the
  session name (junk names can no longer squat in the session cap); the
  timezone route maps a full store to 503 like every other creation
  path (was an unhandled 500); CSV uploads are capped at 1 MB (413);
  CSV export neutralizes spreadsheet formula prefixes (leading =, +,
  -, @, tab, CR get a single-quote prefix on export only); durations
  are capped at 24 h (config.MAX_DURATION_SECONDS, checked in
  add/update/shift/replace); duration shifts validate before mutating
  (true all-or-nothing cascades); Hub push queues are capped (16) and a
  stalled client keeps only the newest snapshot; every response carries
  X-Content-Type-Options: nosniff + Referrer-Policy: no-referrer, with
  framing deliberately unrestricted (display page is embeddable); the
  compose template documents Docker log rotation so a request flood
  cannot fill the host disk.  Tests: 96 passing.
- Audit fixes round 2 (probe-found): the WebSocket loop catches any
  exception from a receive task (a binary frame used to raise an
  uncaught KeyError in `receive_text()` and crash the endpoint) and
  then always sends an explicit close, so a malformed-frame client gets
  a clean teardown instead of hanging; all `/api/*` JSON request bodies
  are capped at 1 MB by the `request_body_cap` middleware (413, same
  limit as CSV uploads — the response is returned directly, not raised,
  because user middleware sits outside the ExceptionMiddleware layer).
  Tests: 107 passing.
- Times are **wall-clock times of day** in the session timezone
  (seconds of the service day in `[0, 172800)`; sessions are reusable
  daily templates that reset at the zone's midnight): admin start
  column shows/edits `HH:MM` (hours may reach 47), CSV is `H:MM:SS`.
  Starts at/past 24:00:00 are next-day early hours — kept as data,
  sort last, never become "current" (never trigger); the display's
  "Next" bar skips them (`ST.nextEvent`).  Sessions carry an IANA
  `timezone` stamped by the first
  admin browser (`POST /api/{name}/timezone`, force-resettable from
  the menu); the server resolves `utc_offset` (zoneinfo) and
  broadcasts it with `server_time` in every state payload; clients
  compute now-of-day via `ST.dayNow(utc_offset)` (server-time skew
  correction in `ST.noteServerTime`/`ST.serverNow`, hourly `/state`
  re-sync)

Run it:

```
~/.venvs/speaker-timer/bin/python -m uvicorn speaker_timer.main:app --port 8000
```

## Planned file layout

```
speaker-timer/               # (repo folder name; package is session_timer)
├── DESIGN.md          # spec (source of truth for behavior)
├── AGENTS.md          # this file
├── README.md          # user-facing docs (only if asked)
├── pyproject.toml     # project metadata, dependencies (fastapi, uvicorn)
├── Dockerfile         # container image (single uvicorn worker)
├── .dockerignore
├── session_timer/
│   ├── __init__.py
│   ├── main.py        # FastAPI app, routes, websocket endpoint
│   ├── store.py       # in-memory session store + generation counters
│   └── static/
│       ├── admin.html + admin.js     # admin UI (static, no templating)
│       ├── display.html + display.js # display UI (static, no templating)
│       ├── config.js                 # client-side tunables (global CONFIG, frozen)
│       ├── common.js                 # shared websocket + render helpers
│       ├── qrcode.js                 # vendored qrcode-generator 1.4.4 (MIT)
│       └── style.css                 # shared styles
├── deploy/
│   └── docker-compose.yml.sample  # deployment TEMPLATE: copy elsewhere, edit there
└── tests/
    └── test_store.py  # unit tests for the store (no server needed)
```

Keep static assets as plain JS (no build step, no npm) — matches the
spec's simplicity requirement.

**No server-side templating.**  `admin.html` and `display.html` are
plain static files served with `FileResponse` from the dynamic routes
(`/{name}/`, `/{name}/display` — a static mount can't handle the
dynamic name).  All data arrives via the WebSocket/JSON API and is
rendered client-side; the session name is parsed in JS from
`window.location.pathname` (validate it: non-empty, no `/`).  Do not
add Jinja2 or any template engine — there is nothing to inject at
render time.

## Implementation plan (phased)

### Phase 1: server core (`store.py`)

1. `Session` dataclass: `name`, `generation: int`, `events: list[Event]`,
   `timezone: str | None` (IANA zone name; server resolves the current
   UTC offset via `zoneinfo` — fail-open to None when unresolvable or
   missing tzdata; `config.DEFAULT_TZ` from `SESSION_TIMER_TZ` is the
   fallback).
2. `Event` dataclass: `name: str = ""`, `start_time: int` (wall-clock
   seconds of day, validated to `[0, 86400)` on add/update/shift), 
   `duration: int` (seconds).
3. `SessionStore`: dict `name -> Session`, guarded by a single
   `asyncio.Lock` (or plain dict ops — single worker, atomic enough for
   GIL-level ops, but prefer the lock for clarity).
4. Session creation is **lazy**: `get_or_create(name)` used by admin
   routes only; display routes use `get()` which may return None.
5. Every mutation: apply change, `generation += 1`, notify websocket
   listeners.

### Phase 2: HTTP + WebSocket API (`main.py`)

Routes (all JSON where applicable):

- `GET /{name}/` → admin HTML page
- `GET /{name}/display` → display HTML page
- `GET /api/{name}/state` → current `{generation, events}`

- `WS /api/{name}/ws` → push channel; server sends
  `{type: "state", generation, events}` on every change
- Admin mutation endpoints (all check `generation` from client; on
  mismatch return 409 `{error: "stale", generation}` and the client
  shows "state changed, please refresh" — per DESIGN.md line 12):
  - `POST /api/{name}/timezone` — set the session's IANA timezone
    (first value wins; admin page posts `Intl`'s zone name on load;
    `force: true` re-sets it keeping event day-seconds)
  - `POST /api/{name}/events` — add event
  - `DELETE /api/{name}/events/{event_id}`
  - `PATCH /api/{name}/events/{event_id}` — set start/end/duration/
    remaining (server converts between representations; remaining ⇒
    `start_time = now + remaining` with `now` = the session zone's
    current seconds of day, end ⇒ `duration = end - start`)
  - `POST /api/{name}/events/{event_id}/shift` — body
    `{field: "start_time"|"duration", delta: int, cascade: true}`;
    with cascade, apply same delta to all events with greater
    `start_time` (ordering "as found by start time")
  - `POST /api/{name}/events/now` — quick "event now": create event
    starting at now with given duration
  - `GET /api/{name}/events.csv` — download, columns
    `start_time,duration_sec,name` with start_time as `H:MM:SS`
    time of day (header row, this order)
  - `PUT /api/{name}/events.csv` — upload, **replaces** all events

Clients send their known `generation` in the request (header
`X-Generation` or query/body field — pick one and be consistent).


### Phase 3: display UI (`display.html`/`display.js`)

1. Connect WS; fall back to polling `state` every 5 s if WS fails
   (plus an hourly `/state` re-fetch for skew/DST re-sync, from
   `CONFIG.resyncIntervalMs`).  Reconnects use exponential backoff
   (`CONFIG.reconnectBaseMs` doubling to `CONFIG.reconnectMaxMs`,
   1 s → 5 min cap, reset on a successful connection) so a long outage
   is not hammered with 2 s retries; polling covers updates meanwhile.
   Regression test: `node tests/js/test_ws_backoff.mjs`.
2. Local ticking with `requestAnimationFrame`/`setInterval(…, 250)`;
   on every state receipt `ST.noteServerTime(data.server_time)`
   anchors the client clock to the server (extrapolated on monotonic
   `performance.now()`), then render from
   `ST.dayNow(utc_offset)` vs `end_time = start_time + duration`.
   Never count stored seconds down — always derive from server time.
3. Current event: greatest `start_time <= now`; show it until next
   event's `start_time`.  Overlaps: most recently started wins.
4. No current countdown (no event yet, or `FLASH_LIMIT` = 5 min after
   expiry) → idle state: greyed-out 00:00 (`#timer.idle`), no progress
   bar; `applyTimerSize()` still runs so the font size can be set
   beforehand.  (Replaced the old fully-blank page.)
5. Progress bar: fixed div, bottom 20% of viewport, width animates 0→100%
   over the event duration.  Colors: green, yellow at ≤25% remaining,
   red at ≤15% remaining.  After end: bar stays full-width red, both
   bar and counter flash at 0.5 Hz (CSS `blink` keyframes, 2 s cycle —
   on/off every 1 s), for at most 5 minutes after expiry (then the
   idle greyed-00:00 state takes over).
5. "Next speaker" bar: while nothing is running and a future event
   exists (shared `ST.nextEvent` = smallest start_time > now, never one
   already started) — including before the first event of the day — a
   white bar in the next-lowest 10% band (20-30% from the bottom)
   fills until the next event's start, with a "Next: M:SS" countdown
   overlay.  If the gap is longer than 10 minutes (display.js
   `NEXT_WINDOW`), or nothing is running at all, the bar stays hidden
   until 10 minutes before the next event and fills over that window.
6. Display controls cluster, upper-right: fullscreen toggle and
   +/- buttons for the countdown text size (discrete ~×1.2 steps
   0.33–4.3 around the `min(28vw, 40vh)` base, persisted in
   `localStorage` with nearest-step restore, buttons disabled at the
   ends; JS sets the inline `font-size` on every render tick, capping
   it to measured-text fit — 100vw wide, timer-box tall — so the max
   step exactly fills the screen with zero border and no click is a
   no-op; recomputed on resize).
6. Counter format: `H:MM:SS` or `MM:SS`; negative after expiry (counts
   up in magnitude).

### Phase 4: admin UI (`admin.html`/`admin.js`)

1. Same WS connection for live state.
2. Table view: one row per event — name, start (text input,
   "YYYY-MM-DD HH:MM", 24-hour, browser-local timezone), duration, end
   (derived), remaining (live, but only filled for the event the
   display page would currently show — shared `ST.currentEvent` logic;
   other rows blank and the current row gets a `tr.current` highlight
   so it is identifiable at a glance), +/- buttons for duration and
   start (cascade), delete button.  Overlapping events get a CSS
   highlight class, not blocked.
3. "Add event" and "Event now (duration)" buttons; CSV download/upload
   via the API routes (upload = file input → PUT, confirm replaces).
4. "Display page" toolbar link (new tab) and a "QR code" button popping
   up a modal with a QR of the display URL.  QR is generated client-side
   with the vendored `static/qrcode.js` (qrcode-generator 1.4.4, MIT) —
   no build step, and the QR encodes the exact origin the browser sees
   (correct scheme behind the SSL-terminating proxy).  Modal closes via
   the Close button, backdrop click, or Escape.
5. All edits: optimistic UI is *not* used — send request, on 409 show
   refresh banner.  Simpler and matches spec.
6. On WS state message with newer generation, re-render table (preserve
   focus if a row input is being edited; skip re-render of the focused
   row's inputs if possible).

### Phase 5: polish + docs

- `pyproject.toml` with `fastapi`, `uvicorn`; venv at `~/.venvs/speaker-timer`.
- Add project-specific entries to `.gitignore` (e.g. `venva/`, `__pycache__/`, `*.egg-info/`).
- README.md only if the user asks.
- Manual test checklist (below).

## Deployment (Docker)

- `Dockerfile`: python:3.14-slim, installs the package via pip, runs
  unprivileged, single uvicorn worker on 0.0.0.0:8000 with a HEALTHCHECK
  against a static asset (GETs never create a session).
- `deploy/docker-compose.yml.sample` is a **template, not used from this
  repo**: it gets copied to the deployment host and edited there
  (image name, port binding).  It documents the invariants: memory-only
  state by default (optional snapshots via `SESSION_TIMER_DATA_DIR`,
  see below), exactly one replica, TLS termination in a front reverse
  proxy.
- Persistence (opt-in): `SESSION_TIMER_DATA_DIR=/data` + a volume
  enables JSON snapshots every 5 min (dirty-checking) and on graceful
  shutdown; loaded at startup.  Limits live in `session_timer/config.py`
  (128-char session/event names, 128 events/session, 1024 sessions with
  idle-only eviction).  Commented lines are in the compose template.
- The `[project.scripts]` entry `session-timer = session_timer.main:run`
  starts the same single-worker uvicorn locally on 127.0.0.1:8000.
- GitHub Actions (`.github/workflows/build-image.yml`): on every push /
  PR / manual dispatch it runs pytest (uv, same `uv.lock` as the image),
  builds the amd64 image tagged `session-timer:latest` (buildx, GHA
  cache) and smoke-tests the container against a static asset.  On push
  events a second job then cross-builds amd64 + arm64 under QEMU and
  publishes the multi-arch manifest to
  `ghcr.io/rkdarst/session-timer:latest` with the workflow's own
  GITHUB_TOKEN (`packages: write`; the first push creates the package,
  private by default — flip to public in package settings for anonymous
  pulls).  No registry credentials are configured anywhere.

## Testing / validation

- Unit tests (`pytest`) for `store.py`: add/delete/shift-cascade math,
  generation increments, CSV round-trip, stale-generation rejection.
- Run: `uvicorn session_timer.main:app --port 8000` (single worker!).
- Manual checks: open `/{name}/` and `/{name}/display` in two windows;
  verify live push both directions, 409 on simulated stale update
  (edit state via second window while first is mid-request), display
  color thresholds (use a 1-minute event), flash behavior after expiry,
  auto-advance to next event, CSV round-trip, nonexistent session on
  display = blank, admin edits create the session.

## Conventions / constraints

- Python stdlib + FastAPI only; no frontend build tooling.
- Times are **wall-clock times of day** in the session timezone:
  stored in memory and on the wire as seconds of day `[0, 86400)`;
  admin enters/displays `HH:MM`, CSV uses `H:MM:SS` (uploads at/past
  24:00:00 rejected).  The session stores an IANA `timezone` (first
  admin browser stamps it; force-resettable); the server resolves the
  current `utc_offset` with `zoneinfo` and sends `server_time` +
  `utc_offset` in every state payload — clients never resolve zones
  and need no correct local clock (skew correction in common.js).
- Security = URL secrecy only; do not add auth.
- Never persist to disk; never spawn multiple workers.
- Don't modify `README.md` unless asked; keep `DESIGN.md` as the
  behavioral source of truth and update it if behavior changes.

## Open items from spec review (fix or decide during Phase 1–2)

- DESIGN.md line 12: typo "nentwork" → "network".
- Display URL derivability (`/{name}/display` reveals `/{name}/`) —
  currently accepted by design; if that changes, display needs its own
  token.

