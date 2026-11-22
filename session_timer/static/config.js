/* Client-side tunables — the counterpart of session_timer/config.py.
   Plain JS, no build step: loaded as a classic script BEFORE common.js
   on both pages, defining a single frozen global `CONFIG`.

   Only values an operator might plausibly tune live here.  Behavioral
   cadence (render tick, polling/retry intervals) and storage key names
   stay in the code that uses them. */
"use strict";

const CONFIG = Object.freeze({
  // Mirrors NAME_MAX_LENGTH in session_timer/config.py (the session-name
  // regex in common.js is built from this).
  nameMaxLength: 128,

  // Room dimming (admin + display pages, synced via the session).
  dimSteps: Object.freeze([0, 0.3, 0.5, 0.7, 0.8, 0.9]),

  // Display page: counter and bar flash (0.5 Hz, CSS) for this long
  // after an event's end, then idle.
  flashLimitSeconds: 5 * 60,
  // "Next speaker" bar: gaps longer than this stay hidden and only fill
  // during this final window before the next event.
  nextWindowSeconds: 10 * 60,
  // Progress bar / counter colors by fraction of the event remaining.
  warnFractionYellow: 0.25,
  warnFractionRed: 0.15,
  // Discrete text-size steps (multipliers of the base font size).
  fontScales: Object.freeze([
    0.33, 0.4, 0.48, 0.58, 0.69, 0.83, 1, 1.2, 1.45, 1.73, 2.07, 2.49, 2.99, 3.58, 4.3,
  ]),

   // Admin page: duration (seconds) of the first "Add event" on an
   // empty session.
   newEventDefaultDuration: 600,

  // Both pages: WebSocket reconnect backoff.  The first retry after a
  // disconnect waits reconnectBaseMs and every further failure doubles
  // the wait up to reconnectMaxMs; a successful connection resets the
  // ladder.  The 5 s /state polling fallback keeps the page updated
  // (and receiving edits) in the meantime.
  reconnectBaseMs: 1000,
  reconnectMaxMs: 5 * 60 * 1000,

  // Both pages: how often to re-fetch the full state over HTTP while
  // connected (the WS already pushes every change).  This re-syncs the
  // server-time skew correction and the session's utc_offset (DST
  // changes) even when nothing mutates for hours.
  resyncIntervalMs: 3600 * 1000,
});