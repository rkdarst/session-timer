"use strict";

(function () {
  const name = ST.sessionName();
  const tbody = document.querySelector("#events-table tbody");
  const staleBanner = document.getElementById("stale-banner");
  const errorBanner = document.getElementById("error-banner");
  const cascadeBox = document.getElementById("cascade");

  let state = { name: name, generation: null, anchor: null, events: [] };
  let stale = false;
  let anchorEnsured = false;

  if (!ST.validName(name)) {
    errorBanner.textContent = "Invalid session URL";
    errorBanner.hidden = false;
    return;
  }

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
    if (data.generation !== null && data.generation !== undefined) {
      stale = false;
      staleBanner.hidden = true;
    }
    if (!anchorEnsured) {
      anchorEnsured = true;
      ensureAnchor();
    }
    renderTable();
  }

  // ------------------------------------------------ service-day anchor

  /** GTFS-style service times are measured from "noon minus 12h" of the
      session's day; only the browser knows the intended local day, so
      the admin page fixes the anchor when it loads (first one wins —
      the server ignores later calls).  On normal days this is local
      midnight; on DST-change days it stays unambiguous. */
  function serviceAnchorForToday() {
    const now = new Date();
    const noon = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 12, 0, 0);
    return Math.round(noon.getTime() / 1000) - 12 * 3600;
  }

  function ensureAnchor() {
    if (state.generation !== null && state.anchor != null) return;
    fetch("/api/" + encodeURIComponent(name) + "/anchor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ anchor: serviceAnchorForToday() }),
    })
      .then((resp) => (resp.ok ? resp.json() : null))
      .then((data) => {
        if (data) onState(data);
      })
      .catch(() => {
        /* WS/polling will refresh; client-side editing works without
           the anchor, only server-side "event now" needs it. */
      });
  }

  /** Now as service-day seconds (anchor is set on page load). */
  function nowDaySeconds() {
    return Math.floor(Date.now() / 1000 - state.anchor);
  }

  /** Events converted to absolute unixtime for countdown / current-event
      comparisons (raw state.events hold service-day seconds). */
  function absEvents() {
    return ST.absoluteEvents(state.events, state.anchor);
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
    for (const ev of events) {
      const tr = document.createElement("tr");
      tr.dataset.eid = ev.id;
      if (overlaps.has(ev.id)) tr.classList.add("overlap");

      tr.appendChild(makeInput(ev, "name", ev.name, "text"));
      tr.appendChild(makeInput(ev, "start_time", ST.fmtServiceTime(ev.start_time), "text"));
      tr.appendChild(makeStepButtons(ev, "start_time", 60));
      tr.appendChild(makeInput(ev, "duration", String(ev.duration), "number"));
      tr.appendChild(makeStepButtons(ev, "duration", 60));
      tr.appendChild(makeReadonlyCell(ST.fmtServiceTime(ev.start_time + ev.duration)));
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
      } else if (field === "start_time") {
        const daySecs = ST.parseServiceTime(input.value);
        if (daySecs === null) {
          error("Invalid start time — use HH:MM (24-hour)");
          renderTable();
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
        await mutate("/events/" + eid, "PATCH", { duration: duration });
      }
    });
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
    const now = Date.now() / 1000;
    const current = ST.currentEvent(absEvents(), now);
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

  // ------------------------------------------- display link + QR popup

  function displayUrl() {
    return window.location.origin + "/" + encodeURIComponent(name) + "/display";
  }

  document.getElementById("display-link").href = displayUrl();

  // Tiny always-on live preview in the toolbar: an iframe of the real
  // display page (?embed=1) — identical rendering by construction
  // (same WebSocket push, same display.js render loop), including the
  // font size a display browser last chose.
  document.getElementById("preview-frame").src = displayUrl() + "?embed=1";

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
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") qrModal.hidden = true;
  });

  // ------------------------------------------------------- toolbar

  document.getElementById("add-event").addEventListener("click", () => {
    guard(async () => {
      if (state.anchor == null) {
        error("Session is not initialized yet — try again in a moment");
        return;
      }
      // Default: chained after the final event (same duration), so a
      // run of events just extends the schedule.  With no events yet,
      // start at the current time (rounded down to the minute) for
      // 10 minutes.  Times are service-day seconds.
      const events = sortedEvents();
      const last = events.length > 0 ? events[events.length - 1] : null;
      const start = last ? last.start_time + last.duration : Math.floor(nowDaySeconds() / 60) * 60;
      const duration = last ? last.duration : 600;
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
