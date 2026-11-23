"use strict";

(function () {
  const name = ST.sessionName();
  const tbody = document.querySelector("#events-table tbody");
  const staleBanner = document.getElementById("stale-banner");
  const errorBanner = document.getElementById("error-banner");
  const cascadeBox = document.getElementById("cascade");
  // ☰ menu: typed edits shift later events too (off by default).
  const editCascadeBox = document.getElementById("edit-cascade");

  let state = {
    name: name,
    generation: null,
    timezone: null,
    utc_offset: null,
    events: [],
  };
  let stale = false;
  let timezoneEnsured = false;

  if (!ST.validName(name)) {
    errorBanner.textContent = "Invalid session URL";
    errorBanner.hidden = false;
    return;
  }
  ST.setPageTitle(name, "admin");

  ST.connect(name, onState);

  // Populate state immediately over HTTP so the very first mutation
  // uses the real generation even if the WS is still connecting (the
  // WS push does the same thing once it arrives).
  fetch("/api/" + encodeURIComponent(name) + "/state")
    .then((resp) => (resp.ok ? resp.json() : null))
    .then((data) => {
      if (data) onState(data);
    })
    .catch(() => {
      /* WS or polling will populate state */
    });

  // -------------------------------------------------- banners / errors

  function error(message) {
    errorBanner.textContent = message;
    errorBanner.hidden = false;
    window.setTimeout(() => {
      errorBanner.hidden = true;
    }, 5000);
  }

  function showStale() {
    stale = true;
    staleBanner.hidden = false;
  }

  /** A nonexistent session has server-side generation 0 (it starts
      counting at 0 on creation), so the first mutation ever sends 0. */
  function generationForRequest() {
    return state.generation === null ? 0 : state.generation;
  }

  function guard(fn) {
    if (stale) {
      showStale();
      return;
    }
    fn().catch((err) => {
      if (err.stale) showStale();
      else error(err.message);
    });
  }

  /** Send a mutation; the response carries the new canonical state. */
  function mutate(path, method, body) {
    return ST.mutate(name, path, method, body, generationForRequest()).then((data) => {
      if (data && data.generation !== null && data.generation !== undefined) {
        state = data;
        stale = false;
        staleBanner.hidden = true;
        renderTable();
      }
      return data;
    });
  }

  function onState(data) {
    state = data;
    ST.noteServerTime(data.server_time);
    if (data.generation !== null && data.generation !== undefined) {
      stale = false;
      staleBanner.hidden = true;
    }
    if (!timezoneEnsured) {
      timezoneEnsured = true;
      ensureTimezone();
    }
    updateDimButtons();
    renderTable();
  }

  // ------------------------------------------------------ timezone

  /** This browser's IANA timezone name (e.g. "Europe/Berlin"), or null
      when the browser cannot report one (old/limited browsers).  The
      server validates and stores it; clients never resolve offsets
      themselves (they get utc_offset in every state payload). */
  function browserTimezone() {
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (typeof tz === "string" && tz && tz !== "Etc/Unknown") return tz;
    } catch (err) {
      /* no Intl: fall through */
    }
    return null;
  }

  /** The session timezone is fixed by the first admin visit; when the
      session has none, stamp this browser's — silently, by design
      (DESIGN.md): the first admin's device is almost always the right
      clock, and anything else is fixable from the ☰ menu.  Only the
      server's "event now"/"remaining" features need it; editing works
      regardless. */
  function ensureTimezone() {
    if (state.generation === null || state.timezone != null) return;
    const tz = browserTimezone();
    if (!tz) return;
    fetch("/api/" + encodeURIComponent(name) + "/timezone", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ timezone: tz }),
    })
      .then((resp) => (resp.ok ? resp.json() : null))
      .then((data) => {
        if (data) onState(data);
      })
      .catch(() => {
        /* WS/polling will refresh; client-side editing works without
           the timezone, only server-side "event now" needs it. */
      });
  }

  /** Now of day in the session timezone (server time + utc_offset,
      wrapped at midnight).  Falls back to local time until the server
      sends an offset. */
  function nowDaySeconds() {
    return Math.floor(ST.dayNow(state.utc_offset));
  }

  // -------------------------------------------------- table rendering

  function sortedEvents() {
    return [...state.events].sort(
      (a, b) => a.start_time - b.start_time || a.id.localeCompare(b.id)
    );
  }

  function overlapIds(events) {
    const ids = new Set();
    for (let i = 0; i < events.length; i++) {
      for (let j = i + 1; j < events.length; j++) {
        const a = events[i];
        const b = events[j];
        if (
          a.start_time < b.start_time + b.duration &&
          b.start_time < a.start_time + a.duration
        ) {
          ids.add(a.id);
          ids.add(b.id);
        }
      }
    }
    return ids;
  }

  function captureFocus() {
    const active = document.activeElement;
    if (!active || !active.dataset || !active.dataset.eid) return null;
    let pos = null;
    let end = null;
    try {
      pos = active.selectionStart;
      end = active.selectionEnd;
    } catch (err) {
      /* non-text input */
    }
    return { eid: active.dataset.eid, field: active.dataset.field, pos, end };
  }

  function restoreFocus(info) {
    if (!info) return;
    const input = tbody.querySelector(
      'input[data-eid="' + info.eid + '"][data-field="' + info.field + '"]'
    );
    if (input) {
      input.focus();
      if (info.pos !== null && info.pos !== undefined) {
        try {
          input.setSelectionRange(info.pos, info.end);
        } catch (err) {
          /* ignore */
        }
      }
    }
  }

  function renderTable() {
    const focus = captureFocus();
    const events = sortedEvents();
    const overlaps = overlapIds(events);
    tbody.textContent = "";
    for (let i = 0; i < events.length; i++) {
      const ev = events[i];
      const tr = document.createElement("tr");
      tr.dataset.eid = ev.id;
      if (overlaps.has(ev.id)) tr.classList.add("overlap");

      tr.appendChild(makeInput(ev, "name", ev.name, "text"));
      tr.appendChild(makeInput(ev, "start_time", ST.fmtServiceTime(ev.start_time), "text"));
      tr.appendChild(makeStepButtons(ev, "start_time", 60));
      tr.appendChild(makeInput(ev, "duration", String(ev.duration), "number"));
      tr.appendChild(makeStepButtons(ev, "duration", 60));
      tr.appendChild(makeReadonlyCell(ST.fmtServiceTime(ev.start_time + ev.duration)));
      tr.appendChild(makeGapCell(ev, events[i + 1] || null));
      tr.appendChild(makeRemainingCell());
      tr.appendChild(makeDeleteButton(ev));
      tbody.appendChild(tr);
    }
    updateRemaining();
    restoreFocus(focus);
  }


  function makeInput(ev, field, value, type) {
    const td = document.createElement("td");
    const input = document.createElement("input");
    input.type = type;
    input.value = value;
    input.dataset.eid = ev.id;
    input.dataset.field = field;
    if (field === "name") input.maxLength = 128; // server enforces too
    if (field === "start_time") input.placeholder = "HH:MM";
    if (field === "duration") input.min = "1";
    input.addEventListener("change", () => commitInput(ev.id, field, input));
    td.appendChild(input);
    return td;
  }

  function commitInput(eid, field, input) {
    guard(async () => {
      if (field === "name") {
        await mutate("/events/" + eid, "PATCH", { name: input.value });
        return;
      }
      // Canonical current values from the last received state (not the
      // DOM): the cascade delta must be relative to what the server
      // actually has.
      const ev = state.events.find((e) => e.id === eid);
      const cascade = editCascadeBox.checked && ev !== undefined;
      if (field === "start_time") {
        const daySecs = ST.parseServiceTime(input.value);
        if (daySecs === null) {
          error("Invalid start time — use HH:MM (24-hour)");
          renderTable();
          return;
        }
        if (cascade) {
          await commitShifted(eid, "start_time", daySecs, ev.start_time);
          return;
        }
        await mutate("/events/" + eid, "PATCH", { start_time: daySecs });
      } else if (field === "duration") {
        const duration = parseInt(input.value, 10);
        if (!Number.isFinite(duration) || duration <= 0) {
          error("Duration must be positive");
          renderTable();
          return;
        }
        if (cascade) {
          await commitShifted(eid, "duration", duration, ev.duration);
          return;
        }
        await mutate("/events/" + eid, "PATCH", { duration: duration });
      }
    });
  }

  /** Manual edit with ☰ "Manual edits delay later events" checked:
      send the delta to the shift endpoint with cascade=true, so every
      event starting later moves by the same amount (a duration edit
      shifts later starts; the server validates the whole cascade
      all-or-nothing).  Delta 0 is skipped: shift bumps the generation
      unconditionally, so a no-op request would push a spurious update
      to every client.  On rejection the table re-renders first, so the
      typed value cannot linger inconsistent with the untouched later
      rows; guard() then shows the server's error. */
  async function commitShifted(eid, field, newValue, oldValue) {
    const delta = newValue - oldValue;
    if (delta === 0) {
      renderTable();
      return;
    }
    try {
      await mutate("/events/" + eid + "/shift", "POST", {
        field: field,
        delta: delta,
        cascade: true,
      });
    } catch (err) {
      renderTable();
      throw err;
    }
  }

  /** +/− buttons for start/duration; cascade per the toolbar checkbox. */
  function makeStepButtons(ev, field, step) {
    const td = document.createElement("td");
    td.className = "step-buttons";
    const buttons = [
      ["−", -step],
      ["+", step],
    ];
    for (const [label, delta] of buttons) {
      const btn = document.createElement("button");
      btn.textContent = label;
      btn.title =
        (delta > 0 ? "Increase " : "Decrease ") + field + " by " + step + "s";
      btn.addEventListener("click", () => {
        guard(() =>
          mutate("/events/" + ev.id + "/shift", "POST", {
            field: field,
            delta: delta,
            cascade: cascadeBox.checked,
          })
        );
      });
      td.appendChild(btn);
    }
    return td;
  }

  function makeReadonlyCell(text) {
    const td = document.createElement("td");
    td.textContent = text;
    return td;
  }

  /** Gap between this event's end and the next event's start (in the
      start-time-sorted order).  Blank for the last event; negative
      (red) when the events overlap.  Re-rendered on every state push,
      so it needs no timer of its own. */
  function makeGapCell(ev, next) {
    const td = document.createElement("td");
    td.className = "gap";
    if (next !== null) {
      const gap = next.start_time - (ev.start_time + ev.duration);
      td.textContent = ST.fmtDuration(gap);
      if (gap < 0) td.classList.add("negative");
    }
    return td;
  }

  function makeRemainingCell() {
    const td = document.createElement("td");
    td.className = "remaining";
    return td;
  }

  function makeDeleteButton(ev) {
    const td = document.createElement("td");
    const btn = document.createElement("button");
    btn.textContent = "✕";
    btn.title = "Delete event";
    btn.addEventListener("click", () => {
      guard(() => mutate("/events/" + ev.id, "DELETE"));
    });
    td.appendChild(btn);
    return td;
  }

  // ---------------------------------------------- live remaining column

  /** The Remaining column mirrors the display page: only the event the
      display would show right now (ST.currentEvent — same shared logic)
      gets a live countdown; every other row's cell is blank, and the
      current row gets a highlight class so it is identifiable at a
      glance.  The countdown goes negative after expiry until the next
      event starts, exactly like the display. */
  function updateRemaining() {
    const now = nowDaySeconds();
    const current = ST.currentEvent(state.events, now);
    const currentId = current ? current.id : null;
    for (const tr of tbody.rows) {
      const isCurrent = tr.dataset.eid === currentId;
      tr.classList.toggle("current", isCurrent);
      const td = tr.querySelector("td.remaining");
      if (!td) continue;
      if (isCurrent) {
        const end = current.start_time + current.duration;
        td.textContent = ST.fmtDuration(end - now);
        td.classList.toggle("past", end <= now);
      } else {
        td.textContent = "";
        td.classList.remove("past");
      }
    }
  }

  setInterval(updateRemaining, 1000);

  // ------------------------------------------- speaker-view link + QR popup

  function displayUrl() {
    return window.location.origin + "/" + encodeURIComponent(name) + "/display";
  }

  document.getElementById("display-link").href = displayUrl();

  // Tiny always-on live preview in the toolbar: an iframe of the real
  // display page (?embed=1) — identical rendering by construction
  // (same WebSocket push, same display.js render loop), including the
  // font size a display browser last chose.
  document.getElementById("preview-frame").src = displayUrl() + "?embed=1";

  // ------------------------------------ room dimming (synced displays)

  // Same session-synced mechanism the display page uses, but the admin
  // page itself stays full-brightness: the admin is operating it, and
  // the preview strip mirrors the dimmed result.
  const DIM_STEPS = CONFIG.dimSteps;
  const dimDarkerBtn = document.getElementById("dim-darker");
  const dimLighterBtn = document.getElementById("dim-lighter");

  function nearestDimIndex() {
    const level = typeof state.dim_level === "number" ? state.dim_level : 0;
    let best = 0;
    for (let i = 1; i < DIM_STEPS.length; i++) {
      if (Math.abs(DIM_STEPS[i] - level) < Math.abs(DIM_STEPS[best] - level)) {
        best = i;
      }
    }
    return best;
  }

  function updateDimButtons() {
    const idx = nearestDimIndex();
    dimDarkerBtn.disabled = idx >= DIM_STEPS.length - 1;
    dimLighterBtn.disabled = idx <= 0;
  }

  function changeDim(delta) {
    const idx = Math.min(
      DIM_STEPS.length - 1,
      Math.max(0, nearestDimIndex() + delta)
    );
    // Fire-and-forget: the WS broadcast updates state.dim_level and
    // re-enables/disables the buttons when the change is confirmed.
    fetch("/api/" + encodeURIComponent(name) + "/dim", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level: DIM_STEPS[idx] }),
    }).catch(() => {});
  }

  dimDarkerBtn.addEventListener("click", () => changeDim(1));
  dimLighterBtn.addEventListener("click", () => changeDim(-1));

  // ------------------------------------ admin dark mode (local, per browser)

  // A per-browser preference, not session state: dark mode affects only
  // this admin screen (the synced room dimming is what affects the
  // display panels).  With no saved choice, follow the OS preference.
  const THEME_KEY = "session-timer:admin-dark";
  const themeBtn = document.getElementById("toggle-theme");

  try {
    const saved = window.localStorage.getItem(THEME_KEY);
    if (
      saved === "1" ||
      (saved === null &&
        window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: dark)").matches)
    ) {
      document.body.classList.add("dark");
    }
  } catch (err) {
    /* localStorage unavailable; default to light */
  }

  themeBtn.addEventListener("click", () => {
    const dark = document.body.classList.toggle("dark");
    try {
      window.localStorage.setItem(THEME_KEY, dark ? "1" : "0");
    } catch (err) {
      /* non-fatal: the choice just won't persist */
    }
    syncThemeLabel();
  });

  // Keep the menu item's label in sync with the actual state.
  function syncThemeLabel() {
    themeBtn.textContent =
      "Dark mode: " + (document.body.classList.contains("dark") ? "on" : "off");
  }
  syncThemeLabel();

  // ------------------------------------------------ hamburger menu

  // Overflow actions (QR, CSV, timezone re-set, raw state, dark mode).
  // Auto-closes after any action, on Escape, or on a click anywhere
  // outside it.
  const menuBtn = document.getElementById("menu-btn");
  const menu = document.getElementById("menu");
  const dropdown = menuBtn.parentElement;

  menuBtn.addEventListener("click", () => {
    menu.hidden = !menu.hidden;
  });
  // Any click on a menu item runs that item's own handler and closes.
  menu.addEventListener("click", (event) => {
    if (event.target.closest("button, a")) menu.hidden = true;
  });
  document.addEventListener("click", (event) => {
    if (!menu.hidden && !dropdown.contains(event.target)) menu.hidden = true;
  });

  // -------------------------------------- set timezone (re-base)

  // Deliberate escape hatch for the first-timezone-wins rule: keep every
  // event's seconds-of-day (the HH:MM column is unchanged) but make the
  // schedule follow this browser's timezone instead.  Absolute times —
  // including any running event — shift by the timezone difference.
  document.getElementById("set-timezone").addEventListener("click", () => {
    const tz = browserTimezone();
    if (!tz) {
      error("This browser cannot report its timezone");
      return;
    }
    if (
      !window.confirm(
        "Make this session follow this browser's timezone (" + tz + ")?\n\n" +
          "The schedule's HH:MM times stay the same, but they will now " +
          "mean this browser's local time; running and upcoming events " +
          "shift by the timezone difference."
      )
    ) {
      return;
    }
    guard(async () => {
      await mutate("/timezone", "POST", {
        timezone: tz,
        force: true,
      });
    });
  });

  // ------------------------------------------------- show raw state

  // The JSON exactly as this client last received it (WebSocket push or
  // mutation response); re-rendered on every open.
  const stateModal = document.getElementById("state-modal");
  const stateJson = document.getElementById("state-json");
  document.getElementById("show-state").addEventListener("click", () => {
    stateJson.textContent = JSON.stringify(state, null, 2);
    stateModal.hidden = false;
  });
  document.getElementById("state-close").addEventListener("click", () => {
    stateModal.hidden = true;
  });
  // Click on the dark backdrop (not the box) also closes.
  stateModal.addEventListener("click", (event) => {
    if (event.target === stateModal) stateModal.hidden = true;
  });

  const qrModal = document.getElementById("qr-modal");
  const qrCodeEl = document.getElementById("qr-code");
  const qrUrlEl = document.getElementById("qr-url");

  function showQr() {
    if (typeof window.qrcode !== "function") {
      error("QR code library failed to load");
      return;
    }
    const url = displayUrl();
    const qr = window.qrcode(0, "M"); // typeNumber 0 = smallest version that fits
    // The vendored library's default byte encoder TRUNCATES non-ASCII
    // chars (255 & charCode), which would silently corrupt the URL for
    // a unicode hostname.  Swap in its proper UTF-8 encoder.
    window.qrcode.stringToBytes = window.qrcode.stringToBytesFuncs["UTF-8"];
    qr.addData(url);
    qr.make();
    qrCodeEl.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
    qrUrlEl.textContent = url;
    qrUrlEl.href = url;
    qrModal.hidden = false;
  }

  document.getElementById("show-qr").addEventListener("click", showQr);
  document.getElementById("qr-close").addEventListener("click", () => {
    qrModal.hidden = true;
  });
  // Click on the dark backdrop (not the box) also closes.
  qrModal.addEventListener("click", (event) => {
    if (event.target === qrModal) qrModal.hidden = true;
  });

  // ---------------------------------------------- data-loss warning

  // The "Events are NOT saved long term" text only applies when the
  // operator has not enabled snapshot persistence (SESSION_TIMER_DATA_DIR);
  // hide the text when they did.  Shown by default, so a failed check
  // fails toward the safe (visible warning) side.  The banner box
  // itself stays up while the sysadmin message (below) is visible.
  const warnBanner = document.getElementById("warn-banner");
  const sysadminMessage = document.getElementById("sysadmin-message");
  const dataLossWarning = document.getElementById("data-loss-warning");

  /** The yellow banner holds two optional texts: hide the whole box
      only when both are absent (otherwise it would render as an empty
      yellow rectangle of just padding). */
  function updateWarnBanner() {
    warnBanner.hidden = sysadminMessage.hidden && dataLossWarning.hidden;
  }

  fetch("/api/persistence")
    .then((r) => r.json())
    .then((cfg) => {
      if (cfg && cfg.persisted) {
        dataLossWarning.hidden = true;
        updateWarnBanner();
      }
    })
    .catch(() => {
      /* keep the warning visible */
    });

  // ---------------------------------------------- sysadmin message

  // SESSION_TIMER_MESSAGE (server environment): shown at the top of
  // the yellow banner, above the data-loss warning.  Static per
  // deployment (the server reads it at startup), so one fetch on load
  // is enough; a failed fetch just leaves the message hidden.
  fetch("/api/banner")
    .then((r) => r.json())
    .then((cfg) => {
      const msg = cfg && typeof cfg.message === "string" ? cfg.message : "";
      if (msg) {
        sysadminMessage.textContent = msg; // never innerHTML: no injection path
        sysadminMessage.hidden = false;
        updateWarnBanner();
      }
    })
    .catch(() => {
      /* no message available; stay hidden */
    });

  // ------------------------------------------------- help popup

  const helpModal = document.getElementById("help-modal");
  document.getElementById("show-help").addEventListener("click", () => {
    helpModal.hidden = false;
  });
  document.getElementById("help-close").addEventListener("click", () => {
    helpModal.hidden = true;
  });
  // Click on the dark backdrop (not the box) also closes.
  helpModal.addEventListener("click", (event) => {
    if (event.target === helpModal) helpModal.hidden = true;
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      qrModal.hidden = true;
      helpModal.hidden = true;
      stateModal.hidden = true;
      const menu = document.getElementById("menu");
      if (menu) menu.hidden = true;
    }
  });

  // ------------------------------------------------------- toolbar

  document.getElementById("add-event").addEventListener("click", () => {
    guard(async () => {
      // Default: chained after the final event (same duration), so a
      // run of events just extends the schedule.  With no events yet,
      // start at the current time of day (rounded down to the minute)
      // for CONFIG.newEventDefaultDuration.  Times are wall-clock
      // seconds of day in the session timezone.  Note: chaining past
      // midnight is rejected by the server (400) — the last event's
      // end must stay within the day for the next start.
      const events = sortedEvents();
      const last = events.length > 0 ? events[events.length - 1] : null;
      const start = last ? last.start_time + last.duration : Math.floor(nowDaySeconds() / 60) * 60;
      const duration = last ? last.duration : CONFIG.newEventDefaultDuration;
      await mutate("/events", "POST", { name: "", start_time: start, duration: duration });
    });
  });

  document.getElementById("event-now").addEventListener("click", () => {
    const minutes = window.prompt("Event duration in minutes:", "10");
    if (minutes === null) return;
    const duration = Math.round(parseFloat(minutes) * 60);
    if (!Number.isFinite(duration) || duration <= 0) {
      error("Duration must be positive");
      return;
    }
    guard(() => mutate("/events/now", "POST", { name: "", duration: duration }));
  });

  document.getElementById("download-csv").addEventListener("click", () => {
    window.location.assign("/api/" + encodeURIComponent(name) + "/events.csv");
  });

  const fileInput = document.getElementById("upload-csv");
  document.getElementById("upload-csv-btn").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    if (
      state.events.length > 0 &&
      !window.confirm(
        "Replace all " + state.events.length + " event(s) with the contents of " + file.name + "?"
      )
    ) {
      return;
    }
    const text = await file.text();
    guard(async () => {
      const resp = await fetch("/api/" + encodeURIComponent(name) + "/events.csv", {
        method: "PUT",
        headers: {
          "Content-Type": "text/csv",
          "X-Generation": String(generationForRequest()),
        },
        body: text,
      });
      if (resp.status === 409) {
        showStale();
        return;
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
        throw new Error(message);
      }
      onState(await resp.json());
    });
  });

  renderTable();
})();
