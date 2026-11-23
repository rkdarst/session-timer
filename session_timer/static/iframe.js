/* Embeddable progress-only view (/{name}/iframe): the display page's
   progress bar as a full-viewport horizontal fill and the "next
   speaker" bar as a top-20%-band fill — no countdown text, no
   controls.  Intended for embedding in other pages.  The bar render
   policy below mirrors display.js (color thresholds, expiry flash,
   next-bar fill windows) — keep the two in sync; the shared
   primitives (ST.currentEvent/nextEvent/dayNow, CONFIG.*) are common
   so only the render policy is duplicated. */
"use strict";

(function () {
  const name = ST.sessionName();
  const barEl = document.getElementById("bar");
  const nextWrap = document.getElementById("next-wrap");
  const nextBar = document.getElementById("next-bar");
  const dimOverlay = document.getElementById("dim-overlay");

  if (!ST.validName(name)) return;

  const FLASH_LIMIT = CONFIG.flashLimitSeconds;
  const NEXT_WINDOW = CONFIG.nextWindowSeconds;

  let events = [];
  let utcOffset = null;

  ST.connect(name, (data) => {
    // Raw events hold wall-clock seconds of day in the session's
    // timezone; rendering derives "now of day" from server time +
    // utc_offset on every tick, so no conversion is needed here.
    events = data.events;
    utcOffset = data.utc_offset;
    if (typeof data.dim_level === "number") {
      // Session-synced room dimming, as on the display page.
      dimOverlay.style.opacity = String(data.dim_level);
    }
  });

  function render() {
    const now = ST.dayNow(utcOffset);
    const current = ST.currentEvent(events, now);
    const remaining =
      current === null ? null : current.start_time + current.duration - now;

    if (remaining === null || remaining <= -FLASH_LIMIT) {
      // Idle: before the first event starts, or 5 min after the last
      // one ended — empty track (the "next speaker" bar may still show
      // during its window).
      barEl.className = "";
      barEl.style.width = "0%";
      updateNextBar(now, current, remaining);
      return;
    }

    let colorClass = "green";
    let widthPct = 0;

    if (remaining <= 0) {
      // Bar stays full-width red; it flashes (CSS, 0.5 Hz) during the
      // first FLASH_LIMIT seconds after expiry.
      colorClass = "red";
      widthPct = 100;
    } else {
      widthPct = Math.min(100, (100 * (now - current.start_time)) / current.duration);
      const fractionLeft = remaining / current.duration;
      if (fractionLeft <= CONFIG.warnFractionRed) colorClass = "red";
      else if (fractionLeft <= CONFIG.warnFractionYellow) colorClass = "yellow";
    }

    barEl.className = colorClass + (remaining <= 0 ? " expired" : "");
    barEl.style.width = widthPct + "%";
    updateNextBar(now, current, remaining);
  }

  /** Same policy as display.js's updateNextBar, minus the text
      overlay: hidden while the current event is still running; a
      short gap fills from expiry; a long gap — or nothing running at
      all — fills only during the final NEXT_WINDOW before the next
      event. */
  function updateNextBar(now, current, remaining) {
    const next = ST.nextEvent(events, now);
    if (next === null || (current !== null && remaining > 0)) {
      nextWrap.hidden = true;
      return;
    }
    if (current !== null) {
      const currentEnd = current.start_time + current.duration;
      const gap = next.start_time - currentEnd;
      if (gap <= NEXT_WINDOW) {
        showNextBar(gap > 0 ? (100 * (now - currentEnd)) / gap : 100);
        return;
      }
    }
    const windowStart = next.start_time - NEXT_WINDOW;
    if (now < windowStart) {
      nextWrap.hidden = true;
      return;
    }
    showNextBar((100 * (now - windowStart)) / NEXT_WINDOW);
  }

  function showNextBar(fraction) {
    nextBar.style.width = Math.min(100, Math.max(0, fraction)) + "%";
    nextWrap.hidden = false;
  }

  setInterval(render, 250);
  render();
})();