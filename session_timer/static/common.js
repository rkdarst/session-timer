/* Shared helpers for admin and display pages.  Plain JS, no build step.
   Served as a classic (non-module) script defining the global `ST`. */
"use strict";

const ST = (() => {
  // Mirrors NAME_RE in session_timer/config.py; the length limit comes
  // from config.js so client and server stay in sync.
  const NAME_RE = new RegExp("^[A-Za-z0-9_-]{1," + CONFIG.nameMaxLength + "}$");

  /** Session name from the URL path: "/NAME/" or "/NAME/display". */
  function sessionName() {
    const parts = window.location.pathname.split("/").filter(Boolean);
    return parts.length > 0 ? decodeURIComponent(parts[0]) : null;
  }

  function validName(name) {
    return typeof name === "string" && NAME_RE.test(name);
  }

  function apiUrl(name, suffix) {
    return "/api/" + encodeURIComponent(name) + suffix;
  }

  /**
   * Connect to the session's WebSocket push channel.  Calls onState
   * with the current state payload ({name, generation, events, ...})
   * whenever the server pushes new state.  Falls back to 5 s polling
   * of /state while disconnected and reconnects automatically; an
   * hourly /state re-fetch re-syncs the server-time skew and the
   * session's utc_offset (DST) even when no mutation happens for
   * hours.  Server-time sync (noteServerTime) is applied centrally so
   * both pages and every payload path benefit.
   */
  function connect(name, onState) {
    let ws = null;
    let reconnectTimer = null;
    let pollTimer = null;
    let resyncTimer = null;

    function receive(data) {
      if (data && Array.isArray(data.events)) {
        noteServerTime(data.server_time);
        onState(data);
      }
    }

    async function fetchState() {
      try {
        const resp = await fetch(apiUrl(name, "/state"));
        if (resp.ok) receive(await resp.json());
      } catch (err) {
        /* offline; retry on next tick */
      }
    }

    function startPolling() {
      if (pollTimer) return;
      pollTimer = setInterval(fetchState, 5000);
    }

    function stopPolling() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function scheduleReconnect() {
      if (reconnectTimer) return;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        open();
      }, 2000);
    }

    function open() {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      try {
        ws = new WebSocket(
          proto + "://" + window.location.host + "/api/" + encodeURIComponent(name) + "/ws"
        );
      } catch (err) {
        ws = null;
        startPolling();
        scheduleReconnect();
        return;
      }
      ws.onopen = stopPolling;
      ws.onmessage = (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (err) {
          return;
        }
        receive(data);
      };
      ws.onclose = () => {
        ws = null;
        startPolling();
        scheduleReconnect();
      };
      ws.onerror = () => {
        if (ws) ws.close();
      };
    }

    open();
    resyncTimer = setInterval(fetchState, CONFIG.resyncIntervalMs);
    return {
      close() {
        stopPolling();
        if (reconnectTimer) clearTimeout(reconnectTimer);
        if (resyncTimer) clearInterval(resyncTimer);
        if (ws) ws.close();
      },
    };
  }

  /**
   * Send a state-changing request with the client's generation.  On 409
   * (stale generation) the thrown error has `.stale = true` and
   * `.serverGeneration`; the admin UI shows the "please refresh" banner.
   */
  async function mutate(name, path, method, body, generation) {
    const resp = await fetch(apiUrl(name, path), {
      method: method,
      headers: {
        "Content-Type": "application/json",
        "X-Generation": String(generation),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (resp.status === 409) {
      let detail = {};
      try {
        detail = (await resp.json()).detail || {};
      } catch (err) {
        /* ignore */
      }
      const err = new Error("stale generation");
      err.stale = true;
      err.serverGeneration = detail.generation;
      throw err;
    }
    if (!resp.ok) {
      let message = resp.statusText;
      try {
        const data = await resp.json();
        if (data.detail) {
          message =
            typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
        }
      } catch (err) {
        /* keep statusText */
      }
      const err = new Error(message);
      err.status = resp.status;
      throw err;
    }
    if (resp.status === 204) return null;
    return resp.json();
  }

  /**
   * The next event that has not started yet at time `now`: the event
   * with the smallest start_time > now (ties: first in the list).
   * Returns null when every event has already started.  Shared by the
   * display page's "next speaker" bar.
   */
  function nextEvent(events, now) {
    let next = null;
    for (const ev of events || []) {
      if (ev.start_time > now && (next === null || ev.start_time < next.start_time)) {
        next = ev;
      }
    }
    return next;
  }

  /**
   * The event a display page would show at time `now`: the event with
   * the greatest start_time <= now; overlapping events resolve to the
   * most recently started one (DESIGN.md).  Stays current (counting
   * negative) until the next event starts.  Returns null when nothing
   * has started yet.  Shared by the display and admin pages.
   */
  function currentEvent(events, now) {
    let current = null;
    for (const ev of events || []) {
      if (ev.start_time <= now && (current === null || ev.start_time > current.start_time)) {
        current = ev;
      }
    }
    return current;
  }

  /** Format seconds as M:SS / H:MM:SS; negative when totalSeconds < 0. */
  function fmtDuration(totalSeconds) {
    const negative = totalSeconds < 0;
    const secs = Math.abs(negative ? Math.floor(totalSeconds) : Math.ceil(totalSeconds));
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    const pad = (n) => String(n).padStart(2, "0");
    const core = h > 0 ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
    return (negative ? "-" : "") + core;
  }

  /** Service-day seconds -> "H:MM" (or "H:MM:SS" when seconds are
      non-zero).  Hours are not zero-padded and may exceed 23 for
      events just past midnight (GTFS-style service times; see
      DESIGN.md). */
  function fmtServiceTime(daySeconds) {
    const total = Math.floor(daySeconds);
    const h = Math.floor(total / 3600);
    const pad = (n) => String(n).padStart(2, "0");
    const m = pad(Math.floor((total % 3600) / 60));
    const s = total % 60;
    return s !== 0 ? h + ":" + m + ":" + pad(s) : h + ":" + m;
  }

  /** "H:MM" or "H:MM:SS" (24-hour) -> service-day seconds, or null if
      invalid.  Lenient about leading zeros; hours may be 0-47 so a
      time just past midnight can be written as e.g. "24:30". */
  function parseServiceTime(value) {
    if (typeof value !== "string") return null;
    const match = value.trim().match(/^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/);
    if (!match) return null;
    const h = +match[1];
    const m = +match[2];
    const s = match[3] !== undefined ? +match[3] : 0;
    if (h > 47 || m > 59 || s > 59) return null;
    return h * 3600 + m * 60 + s;
  }

  /**
   * Server-time skew correction: every state payload carries
   * `server_time` (unixtime); call noteServerTime() with it on every
   * state receipt, then serverNow() returns the server's idea of
   * "now", extrapolated with the monotonic performance.now() clock.
   * Displays with a dead RTC battery self-correct, and an NTP step
   * mid-session causes no glitch (only drift between syncs, which the
   * hourly re-sync bounds).
   */
  let serverAtSync = null;
  let perfAtSync = 0;

  function perfNow() {
    return window.performance ? window.performance.now() : Date.now();
  }

  function noteServerTime(t) {
    if (typeof t === "number" && Number.isFinite(t)) {
      serverAtSync = t;
      perfAtSync = perfNow();
    }
  }

  function serverNow() {
    if (serverAtSync === null) return Date.now() / 1000;
    return serverAtSync + (perfNow() - perfAtSync) / 1000;
  }

  /**
   * Current wall-clock time of the session's day, in seconds
   * [0, 86400).  Uses the server-provided utc_offset when available
   * (so tz-database-less devices work); falls back to this browser's
   * own offset otherwise.  Wraps at midnight, which restarts the daily
   * template at "before the first event".
   */
  function dayNow(utcOffset) {
    const offset =
      typeof utcOffset === "number" && Number.isFinite(utcOffset)
        ? utcOffset
        : -new Date().getTimezoneOffset() * 60;
    return (((serverNow() + offset) % 86400) + 86400) % 86400;
  }

  return {
    sessionName,
    validName,
    connect,
    mutate,
    currentEvent,
    nextEvent,
    fmtDuration,
    fmtServiceTime,
    parseServiceTime,
    noteServerTime,
    serverNow,
    dayNow,
  };
})();
