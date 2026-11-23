# Web-based session timer

This is a web-based session timer, that has a web server that can accept multiple clients at different `/name` URLs.  Pages are responsive, and changes instantly sync to the server.

As a basic usage, there is a web timer for one event that counts down until an admin resets the timer (admin mode shortcut for one event).  However, there is support for pre-programming multiple events and each counts down, and once a new event starts, the old one disappears from the display view.  This way a session organizer can pre-plan the day and the timer runs automatically.  Sessions expire 31 days after last use (or of course on server restart).


## Data model

There are **sessions** at URL `/SESSION_NAME/`.  Simply navigating to a URL returns the empty session and making some active changes in the browser creates a session that gets synced (opening the admin page also stamps the session's timezone, which creates it).  Multiple admins (and displays) should be able to access at the same time and updates get pushed to all connected clients dynamically and immediately.

The server holds canonical state, and any old state by clients is ignored.  Add a generation counter to detect old state by clients.  State is fully dynamic.  Admin clients shouldn't have old state, since updates get pushed to them immediately (if someone is currently editing, sorry).  So, if the state changes under an admin, it gets a push with the new generation number.  Any updates with this old state get rejected (should be infrequent - only on network difficulties) with a message to the admin and being told to refresh.

Each session has multiple **events** which have a {name (optional), start_time, duration}.  Of course, there can be UI for setting other things, for example duration and end time.  Times are **wall-clock times of day** in the session's timezone: `start_time` is seconds of day in `[0, 86400)` (i.e. between 0:00:00 and 23:59:59); events cannot start at or past midnight.  Times are entered and displayed as `HH:MM` (24-hour); CSV uses `H:MM:SS`.  Durations are plain seconds and `end = start + duration`.

Each session carries a **timezone**: an IANA zone name (e.g. `Europe/Berlin`), stamped by the first admin browser that opens the session (only the browser knows which room's clock the schedule follows) and fixed from then on.  The *server* resolves the zone's current UTC offset (via stdlib `zoneinfo`) at each serialization and broadcasts it as `utc_offset` in every state payload, together with `server_time` (the server's unixtime).  Clients need no timezone database and no correct clock of their own: they compute the current time of day as `(server_time + utc_offset) mod 86400`, anchoring to `server_time` and extrapolating on the monotonic `performance.now()` clock — so a display with a dead RTC battery self-corrects, and an NTP step mid-session causes no glitch.  An hourly `/state` re-fetch re-syncs the skew and picks up DST changes.

Sessions are **reusable daily templates**: at the session timezone's local midnight the day wraps back to 0 and the session returns to "before the first event", so the same schedule runs again the next day without any edit.  This is why times are times of day rather than dates: the session must remember *which clock it follows*, not *which day it is*.  Accepted consequences: on DST-change nights an event in the repeated/missing hour occurs once per wall-clock label (whatever the zone's current offset makes of it); a post-expiry flash is cut off at midnight; and old CSV exports with starts at or past 24:00:00 are rejected with an explanatory error.  If the server cannot resolve the zone name (e.g. no tz database installed) it fails open: the timezone is still stored, but `utc_offset` is sent as null and clients fall back to their own local clocks.  A deployment-wide default can be set with the `SESSION_TIMER_TZ` environment variable (used when a session has no timezone of its own).

The timezone can be replaced from the admin menu ("Set timezone to this browser"): the HH:MM schedule is kept, but it now means the new zone's wall clock (absolute times shift by the zone difference).  Setting the timezone is silent — no banner, prompt, or hint: the first admin browser stamps the session automatically, and a mismatch (e.g. a laptop in the organizer's home timezone editing a venue schedule in another zone) is harmless by design since the schedule's HH:MM numbers never change; the menu item is the explicit fix if the first admin's browser was in the wrong zone.


## Interface

### Users

There are no users.  Security is by hard-to-guess URLs.

### Admin

Allows one to view the events in the session and modify them.  There are controls to set and update combinations of start, end, duration, remaining time.  These controls will be refined later, choose something for now that makes sense.

* Table-like view
* Add event
* Delete event
* Set event start / end / duration / remaining times (not affecting
  other events — unless the ☰ menu's "Manual edits delay later events"
  checkbox is on: then a typed start/duration change shifts every later
  event by the same delta, through the same all-or-nothing cascade the
  +/− buttons use; off by default since it is rarely wanted)
* Increase/decrease duration of event (also then delaying subsequent events, as found by start time)
* Increase/decrease start time of event (also then delaying subsequent events)
* Highlight overlapping events but not otherwise blocking them.
* Show the remaining time only for the event currently being displayed
  (same current-event rule as the display page: greatest start_time <=
  now, overlaps to the most recently started) and highlight that row in
  the table, so the current event is identifiable at a glance.
* Show the gap between each event and the next one (as found by start
  time) in a column: blank for the last event, negative (and red) when
  events overlap.
* Quick "event now" setup.
* Room dimming: "darker"/"lighter" buttons that fade the display
  screens toward black (steps up to 90% black), synced to every panel
  via the session — so an admin can dim displays that are not
  physically reachable (e.g. when the room lights go down).  The admin
  page itself stays full-brightness; its preview strip shows the
  dimmed result.
* Dark mode for the admin screen, toggled with a 🌓 button and
  remembered per browser (following the OS preference until first
  toggled).  Unlike the room dimming this is local to one admin's
  screen and never synced.
* A permanent warning banner at the top ("Events are NOT saved long
  term") — shown only when snapshot persistence is disabled (queried
  from `GET /api/persistence`) — and a Help button opening a concise
  popup with the main points (URL = access, times-of-day semantics,
  cascade, display colors, memory-only data).
* Toolbar overflow behind a ☰ hamburger menu (top right): display
  page link, QR code, CSV download/upload, a "Manual edits delay later
  events" checkbox (off by default: when on, typing a new start or
  duration shifts every later event by the same delta — the manual-edit
  counterpart of the +/− cascade), "Set timezone to this
  browser" (forced re-set of the session timezone: HH:MM schedule
  kept, absolute times shift by the zone difference), "Show raw
  state" (the JSON as this client sees it, including `server_time` —
  a handy clock-skew diagnostic), and the dark-mode toggle (shown as
  "Dark mode: on/off").  The menu auto-closes after any action, on
  Escape, or on a click anywhere outside it (the checkbox toggles
  without closing).
* Download events as csv (`start_time,duration_sec,name`, with
  start_time as `H:MM:SS` time of day; starts at or past 24:00:00 are
  rejected on upload — sessions cannot span midnight)
* Upload events as csv (replacing all current)
* Link to the display page and a QR-code popup with the display URL
  (so a phone can be pointed at the display).
* Live preview of the display page in the admin toolbar: a tiny
  always-on strip that is an iframe of the real display page
  (`?embed=1`), so it renders exactly like the projector — same
  WebSocket and the same render loop — at all times.  In embedded mode
  the display page hides its fullscreen and text-size controls and
  never writes the per-browser font-size setting; instead it mirrors
  the countdown text size last chosen on a display browser (synced via
  the server, capped to the preview's own viewport).


### Display (`/SESSION_NAME/display`)

Read-only.  Show a countdown timer until end time and progress bar along the bottom.  If there is no current countdown — before the first event starts, or 5 minutes after the last event ended — the display shows a greyed-out 00:00 with no progress bar (so the countdown text size can be judged and adjusted in advance) rather than a fully blank screen.  If there are overlapping events, it shows the one that started most recently.

The countdown progress bar takes the bottom 20% of the screen and slowly moves from left to right until it covers the whole duration.  It starts green, then turns yellow at 25% left, then red at 15% left.  The size of the main countdown text can be adjusted with +/- buttons in the upper-right corner (next to the fullscreen toggle; each click changes it by about 20%) and the choice is remembered per browser and synced to the session (via `PUT /api/{SESSION_NAME}/font-scale`, without changing the schedule's generation counter, so displays — which don't track generations — can send it) so the admin page's embedded preview mirrors it.  The screen can also be dimmed for dark rooms with darker/lighter buttons (same corner): a black overlay fades in steps up to 90% black, and the level is session state synced to every panel via `PUT /api/{SESSION_NAME}/dim` (again without affecting the generation), so an admin can dim displays remotely; the overlay sits beneath the control buttons so a human at a dimmed display can still brighten it.  The text size is capped so it never exceeds the screen.  After expiry, the counter continues to count up negative numbers while flashing (0.5Hz), the progress bar flashes (0.5Hz); 5 minutes after the event ended the countdown area reverts to the greyed-out 00:00 idle state.  While nothing is running and a future event exists (start_time after now) — including before the first event of the day — a white bar in the next-lowest 10% of the screen (20-30% from the bottom) fills until the next event starts, with a countdown of the time remaining until then — so the current speaker can see how much time is left until the next speaker should get started.  If the gap is longer than 10 minutes (no event starts within 10 minutes of the current event's end, or nothing is running at all), the white bar stays hidden until 10 minutes before the next event and fills across that window; shorter gaps fill from the moment the current event ends.

Pages stay current over one WebSocket push channel, falling back to a
5-second `/api/{SESSION_NAME}/state` poll (plus an hourly re-fetch to
re-sync clock skew and DST) whenever the socket is down.  Reconnect
attempts use exponential backoff: the first retry waits 1 second, each
further failure doubles the wait up to a 5-minute cap, and a successful
connection resets the ladder (tunables `CONFIG.reconnectBaseMs` /
`CONFIG.reconnectMaxMs`).  While disconnected, a page keeps rendering
the most recently received schedule — the countdown extrapolates server
time locally, so it keeps ticking correctly even during a full outage —
and catches up on edits as soon as any path (push, poll, or reconnect)
delivers new state.


## Tools

Python, simple web server that listens on a port.  SSL etc. will be handled in another layer.  If a framework is needed, use FastAPI.  Websockets are made to push updates to all connected clients.  In-memory state means there must be one worker.

The server stores sessions in memory only.  Optionally, persistence
across restarts can be enabled by setting the `SESSION_TIMER_DATA_DIR`
environment variable: the server then writes a JSON snapshot of all
sessions to `$SESSION_TIMER_DATA_DIR/sessions.json` every 5 minutes
(when something changed) and on graceful shutdown, and loads it at
startup.  A hard kill can lose the last few minutes of edits; a missing
or corrupt snapshot is ignored (saved aside as `*.corrupt`) and the
server starts empty.  With the variable unset (the default), nothing is
ever written to disk.

The admin page can show a deployment-wide **sysadmin message**: the
`SESSION_TIMER_MESSAGE` environment variable is rendered inside the
yellow warning banner on every admin page, above the data-loss warning
text (when snapshot persistence is enabled the data-loss text hides,
but a sysadmin message keeps the banner up).  It is read once at server
start (restart to change), stripped and capped at 512 characters, and
fetched once per page load from `GET /api/banner`; unset or empty means
no message.  The display page is not affected.

Limits (see `session_timer/config.py`): session names are at most 128
ASCII characters; event names at most 128 characters; a session holds
at most 128 events (larger adds/CSV uploads are rejected); at most 1024
sessions are kept — when the cap is hit, an immediate expiry pass runs,
then only sessions idle for over an hour are evicted oldest-first, and
if nothing qualifies, new sessions are refused with 503 (a live session
is never evicted).  Durations are at most 24 hours; CSV uploads are
capped at 1 MB (rejected with 413).

Hardening: every response carries X-Content-Type-Options: nosniff
and Referrer-Policy: no-referrer; framing is deliberately *not*
restricted so the display page can be embedded anywhere.  CSV export
prefixes a ' to names beginning with =, +, -, @ (or tab/CR) so
spreadsheets do not treat them as formulas (re-uploading an export
keeps the quote).  Mutations validate the session name, so junk names
cannot occupy the session cap; WebSocket push queues are capped -- a
stalled client is fed only the newest state (each payload is a full
snapshot); the WebSocket loop survives malformed frames (e.g. binary)
with a clean explicit close, and all `/api/*` JSON request bodies are
capped at 1 MB like CSV uploads (rejected with 413 before parsing).


Design with simplicity and security in mind.
