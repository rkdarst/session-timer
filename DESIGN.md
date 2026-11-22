# Web-based session timer

This is a web-based session timer, that has a web server that can accept multiple clients at different `/name` URLs.  Pages are responsive, and changes instantly sync to the server.

As a basic usage, there is a web timer for one event that counts down until an admin resets the timer (admin mode shortcut for one event).  However, there is support for pre-programming multiple events and each counts down, and once a new event starts, the old one disappears from the display view.  This way a session organizer can pre-plan the day and the timer runs automatically.  Sessions expire 31 days after last use (or of course on server restart).


## Data model

There are **sessions** at URL `/SESSION_NAME/`.  Simply navigating to a URL returns the empty session and making some active changes in the browser creates a session that gets synced (opening the admin page also fixes the session's day anchor, which creates it).  Multiple admins (and displays) should be able to access at the same time and updates get pushed to all connected clients dynamically and immediately.

The server holds canonical state, and any old state by clients is ignored.  Add a generation counter to detect old state by clients.  State is fully dynamic.  Admin clients shouldn't have old state, since updates get pushed to them immediately (if someone is currently editing, sorry).  So, if the state changes under an admin, it gets a push with the new generation number.  Any updates with this old state get rejected (should be infrequent - only on network difficulties) with a message to the admin and being told to refresh.

Each session has multiple **events** which have a {name (optional), start_time, duration}.  Of course, there can be UI for setting other things, for example duration and end time.  Times follow the GTFS "service time" convention: `start_time` is **seconds since "noon minus 12 hours" of the session's day** — effectively local midnight, but defined via the UTC offset in effect *at noon*, so the hours around a daylight-saving change stay unambiguous (no repeated or missing hour).  Times are entered and displayed as `HH:MM` (24-hour, browser-local); CSV uses `H:MM:SS`.  Durations are plain seconds and `end = start + duration`.

Each session carries an **anchor**: the unixtime of its "noon minus 12h", computed by the first admin browser that opens the session (only the browser knows the intended local day; the server stays timezone-agnostic) and fixed from then on.  Clients convert service times to absolute times with it (`absolute = anchor + start_time`), so countdowns are exact and every viewer sees the admin's schedule.  A session covers a single day; sessions are meant to be created on the day they are used.


## Interface

### Users

There are no users.  Security is by hard-to-guess URLs.

### Admin

Allows one to view the events in the session and modify them.  There are controls to set and update combinations of start, end, duration, remaining time.  These controls will be refined later, choose something for now that makes sense.

* Table-like view
* Add event
* Delete event
* Set event start / end / duration / remaining times (not affecting other events)
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
* Download events as csv (`start_time,duration_sec,name`, with
  start_time as `H:MM:SS` service time)
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

Limits (see `session_timer/config.py`): session names are at most 128
ASCII characters; event names at most 128 characters; a session holds
at most 128 events (larger adds/CSV uploads are rejected); at most 1024
sessions are kept — when the cap is hit, an immediate expiry pass runs,
then only sessions idle for over an hour are evicted oldest-first, and
if nothing qualifies, new sessions are refused with 503 (a live session
is never evicted).


Design with simplicity and security in mind.
