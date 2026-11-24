"use strict";

(function () {
  const timerEl = document.getElementById("timer");
  const barEl = document.getElementById("bar");
  const nextWrap = document.getElementById("next-wrap");
  const nextBar = document.getElementById("next-bar");
  const nextText = document.getElementById("next-text");
  const dimOverlay = document.getElementById("dim-overlay");
  const dimDarkerBtn = document.getElementById("dim-darker");
  const dimLighterBtn = document.getElementById("dim-lighter");
  const fullscreenBtn = document.getElementById("fullscreen");
  const fontSmallerBtn = document.getElementById("font-smaller");
  const fontBiggerBtn = document.getElementById("font-bigger");
  const clockEl = document.getElementById("clock");
  const clockToggleBtn = document.getElementById("clock-toggle");
  const wakeLockBtn = document.getElementById("wake-lock");
  const wakeStatusBtn = document.getElementById("wake-status");
  const name = ST.sessionName();
  // Flashing after expiry is capped: FLASH_LIMIT after an event ends the
  // counter and bar stop flashing (steady red, still counting negative).
  // Tunables live in config.js (shared with the admin page); bound to
  // short local names here for readability.
  const FLASH_LIMIT = CONFIG.flashLimitSeconds;
  // "Next speaker" bar: if no event starts within NEXT_WINDOW of the
  // current event's end (long gap), the bar stays hidden and only
  // fills during the final NEXT_WINDOW before the next event.  Shorter
  // gaps fill across the whole gap, starting at expiry.
  const NEXT_WINDOW = CONFIG.nextWindowSeconds;

  // Embedded in the admin page's live preview (iframe with ?embed=1):
  // the fullscreen and text-size controls make no sense there, and the
  // preview must not touch the persisted per-browser font size (that
  // belongs to the real display's browser).  Everything else renders
  // exactly like a standalone display.
  const embed = new URLSearchParams(window.location.search).has("embed");
  if (embed) {
    document.getElementById("display-controls").hidden = true;
  }

  if (!ST.validName(name)) {
    timerEl.textContent = "Invalid session URL";
    return;
  }
  ST.setPageTitle(name);

  let events = [];
  // The session's current UTC offset (seconds) from the server: turns
  // server time into the session's wall-clock seconds of day (ST.dayNow).
  // Null until the server sends it; dayNow falls back to local time.
  let utcOffset = null;
  // In the admin preview (embed mode) the mirrored font scale comes
  // from the server state: display browsers sync their +/- choice there
  // (see applyFontSize), so the preview shows the chosen relative size.
  let serverFontScale = null;

  // Room-dimming overlay: a session-synced black fade for dark rooms.
  // All panels (standalone displays and embedded previews) share one
  // level; the display's ☾/☀ buttons and the admin page's Darker/
  // Lighter buttons both sync through the server (PUT /dim —
  // presentation-only, no generation change).  The overlay sits below
  // the control buttons so a human standing at a dimmed display can
  // always reach "lighter".
  const DIM_STEPS = CONFIG.dimSteps;
  let dimLevel = 0;

  function nearestDimIndex() {
    let best = 0;
    for (let i = 1; i < DIM_STEPS.length; i++) {
      if (Math.abs(DIM_STEPS[i] - dimLevel) < Math.abs(DIM_STEPS[best] - dimLevel)) {
        best = i;
      }
    }
    return best;
  }

  function applyDim() {
    dimOverlay.style.opacity = String(dimLevel);
    dimDarkerBtn.disabled = nearestDimIndex() >= DIM_STEPS.length - 1;
    dimLighterBtn.disabled = nearestDimIndex() <= 0;
  }

  function changeDim(delta) {
    const idx = Math.min(
      DIM_STEPS.length - 1,
      Math.max(0, nearestDimIndex() + delta)
    );
    dimLevel = DIM_STEPS[idx];
    applyDim();
    // Fire-and-forget; the state broadcast confirms it everywhere.
    fetch(ST.apiUrl(name, "/dim"), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level: dimLevel }),
    }).catch(() => {});
  }

  dimDarkerBtn.addEventListener("click", () => changeDim(1));
  dimLighterBtn.addEventListener("click", () => changeDim(-1));

  ST.connect(name, (data) => {
    // Raw events hold wall-clock seconds of day in the session's
    // timezone; rendering derives "now of day" from server time +
    // utc_offset on every tick, so no conversion is needed here.
    events = data.events;
    utcOffset = data.utc_offset;
    if (embed) serverFontScale = data.font_scale;
    if (typeof data.dim_level === "number") {
      dimLevel = data.dim_level;
      applyDim();
    }
  });

  /** Per DESIGN.md (via ST.currentEvent): the event with the greatest
      start_time <= now (overlaps resolve to the most recently started
      one); it stays current, counting up negative, until the next event
      starts — but only FLASH_LIMIT seconds past its end: after that
      (and before anything has started) the display idles. */
  function render() {
    // Now of day in the session's timezone: server time + utc_offset,
    // wrapped at midnight (serverNow tracks the server clock via
    // noteServerTime, so a wrong display clock self-corrects).
    const now = ST.dayNow(utcOffset);
    if (clockVisible) clockEl.textContent = ST.fmtClock(now);
    const current = ST.currentEvent(events, now);
    const remaining =
      current === null ? null : current.start_time + current.duration - now;

    if (remaining === null || remaining <= -FLASH_LIMIT) {
      // No current countdown: before the first event starts, or 5
      // minutes after the last one ended.  Instead of a fully blank
      // screen, show a greyed-out 00:00 (no progress bar) so the text
      // size can be judged and adjusted in advance.  The "next
      // speaker" bar still shows during its 10-minute window.
      timerEl.textContent = "00:00";
      timerEl.className = "idle";
      barEl.className = "";
      barEl.style.width = "0%";
      applyTimerSize();
      updateNextBar(now, current, remaining);
      return;
    }

    let colorClass = "green";
    let widthPct = 0;

    if (remaining <= 0) {
      // Bar stays full-width red; counter and bar flash (CSS, 0.5 Hz)
      // during the first FLASH_LIMIT seconds after expiry.
      colorClass = "red";
      widthPct = 100;
    } else {
      widthPct = Math.min(100, (100 * (now - current.start_time)) / current.duration);
      const fractionLeft = remaining / current.duration;
      if (fractionLeft <= CONFIG.warnFractionRed) colorClass = "red";
      else if (fractionLeft <= CONFIG.warnFractionYellow) colorClass = "yellow";
    }

    timerEl.textContent = ST.fmtDuration(remaining);
    timerEl.className = colorClass + (remaining <= 0 ? " expired" : "");
    barEl.className = colorClass + (remaining <= 0 ? " expired" : "");
    barEl.style.width = widthPct + "%";

    applyTimerSize();
    updateNextBar(now, current, remaining);
  }

  /** White "next speaker" bar + countdown.  Hidden while the current
      event is still running.  For a short gap (the next event starts
      within NEXT_WINDOW of the current event's end) it fills from
      expiry to the next start.  For a long gap — or when nothing is
      running at all (before the first event, or after the idle
      cutoff) — it only appears NEXT_WINDOW before the next event and
      fills over that window. */
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
        // Short gap: fill across the actual gap from expiry.
        showNextBar(
          gap > 0 ? (100 * (now - currentEnd)) / gap : 100,
          next.start_time - now,
          next.name
        );
        return;
      }
    }
    // Long gap / nothing running: fill during the final NEXT_WINDOW
    // before the next event.
    const windowStart = next.start_time - NEXT_WINDOW;
    if (now < windowStart) {
      nextWrap.hidden = true;
      return;
    }
    showNextBar(
      (100 * (now - windowStart)) / NEXT_WINDOW,
      next.start_time - now,
      next.name
    );
  }

  function showNextBar(fraction, untilNext, name) {
    nextBar.style.width = Math.min(100, Math.max(0, fraction)) + "%";
    nextText.textContent =
      "Next: " + ST.fmtDuration(untilNext) + (name ? " (" + name + ")" : "");
    nextWrap.hidden = false;
  }

  // Countdown text size: discrete steps around the default (1 = the
  // CSS default min(28vw, 40vh)), persisted per browser in localStorage
  // so a phone and a laptop can each keep their own setting.  Whatever
  // the step, the size is capped at exactly filling the screen: the
  // current text is measured on a canvas and the font size clamped to
  // fit the viewport width and the timer box height, so the largest
  // step reaches zero black border.
  const FONT_SCALES = CONFIG.fontScales;
  const FONT_SCALE_KEY = "session-timer:font-scale";
  const CLOCK_KEY = "session-timer:clock";

  const measureCtx = document.createElement("canvas").getContext("2d");

  function barHeightPx() {
    return 0.2 * window.innerHeight; // --bar-height
  }

  function baseFontSizePx() {
    return Math.min(0.28 * window.innerWidth, 0.4 * window.innerHeight);
  }

  /** Max px size at which `text` exactly fills the screen: width to
      100vw (digits replaced with "0" to match the tabular-nums advance
      width) and height to the timer box (viewport minus the bar band),
      using tight glyph bounds when the browser provides them. */
  function fitFontSizePx(text) {
    const sample = String(text);
    if (!sample || !measureCtx) return Number.POSITIVE_INFINITY;
    measureCtx.font = "100px " + window.getComputedStyle(timerEl).fontFamily;
    const m = measureCtx.measureText(sample.replace(/\d/g, "0"));
    const glyphHeight =
      typeof m.actualBoundingBoxAscent === "number" &&
      typeof m.actualBoundingBoxDescent === "number"
        ? m.actualBoundingBoxAscent + m.actualBoundingBoxDescent
        : 100;
    const fitWidth = (100 * window.innerWidth) / m.width;
    const fitHeight = (100 * (window.innerHeight - barHeightPx())) / glyphHeight;
    return Math.max(1, Math.floor(Math.min(fitWidth, fitHeight)));
  }

  /** Largest step index that still grows the rendered size: the first
      step reaching the fit-to-screen cap (any later step would render
      identically at the cap, making the + button a no-op). */
  function maxFontIndex() {
    const maxScale = fitFontSizePx(timerEl.textContent || "MM:SS") / baseFontSizePx();
    if (!Number.isFinite(maxScale) || maxScale <= 0) return FONT_SCALES.length - 1;
    for (let i = 0; i < FONT_SCALES.length; i++) {
      if (FONT_SCALES[i] >= maxScale) return i;
    }
    return FONT_SCALES.length - 1;
  }

  /** Nearest FONT_SCALES step to `scale` (the default step for junk:
      null, non-numbers, or non-positive values).  Used both to restore
      a saved size and to mirror a display machine's synced choice. */
  function nearestFontIndex(scale) {
    if (typeof scale !== "number" || !Number.isFinite(scale) || scale <= 0) {
      return FONT_SCALES.indexOf(1);
    }
    let best = 0;
    for (let i = 1; i < FONT_SCALES.length; i++) {
      if (Math.abs(FONT_SCALES[i] - scale) < Math.abs(FONT_SCALES[best] - scale)) {
        best = i;
      }
    }
    return best;
  }

  function loadFontIndex() {
    if (embed) return FONT_SCALES.indexOf(1); // preview: server scale instead
    try {
      const saved = parseFloat(window.localStorage.getItem(FONT_SCALE_KEY));
      if (Number.isFinite(saved) && saved > 0) {
        // Nearest step, so values saved with an older step grid still
        // restore a sensible size.
        return nearestFontIndex(saved);
      }
    } catch (err) {
      /* localStorage unavailable (privacy mode etc.); use default */
    }
    return FONT_SCALES.indexOf(1);
  }

  let fontIndex = loadFontIndex();

  /** Clock visibility: per-display-browser, persisted like the font
      scale.  Defaults to on.  The admin-page preview (embed) always
      shows the clock in its default look — a localStorage setting
      cannot be mirrored via the server the way the font scale is. */
  function loadClockVisible() {
    if (embed) return true;
    try {
      return window.localStorage.getItem(CLOCK_KEY) !== "0";
    } catch (err) {
      return true; // localStorage unavailable (privacy mode etc.): on
    }
  }
  let clockVisible = loadClockVisible();

  function applyClock() {
    clockEl.hidden = !clockVisible;
  }

  clockToggleBtn.addEventListener("click", () => {
    clockVisible = !clockVisible;
    applyClock();
    if (embed) return; // the preview never persists (its toggle is hidden)
    try {
      window.localStorage.setItem(CLOCK_KEY, clockVisible ? "1" : "0");
    } catch (err) {
      /* non-fatal: visibility just won't persist */
    }
  });
  applyClock();

  // ------------------------------------ wake lock (keep the screen on)
  // Opt-in Screen Wake Lock for a phone/tablet propped up at the talk
  // (or left on a desk): the screen does not auto-lock while the timer
  // is shown.  Deliberately simple and OS-deferring: the lock exists
  // exactly from an explicit tap until it is lost — tapping the bulb
  // (or the badge) requests it, tapping again releases it, and ANY
  // release — the tab being hidden (screen off, app switch), the OS
  // taking it back, or a policy denial — only updates the UI.  Nothing
  // re-acquires behind the user's back: if the page is left and
  // returned to, the lock is gone (the "screen stays on" badge, shown
  // only while the lock is held, is the visible tell) and one tap
  // re-engages it.  Unsupported (old browsers): the button hides
  // entirely; the admin preview (embed) skips all of it — its iframe
  // must not keep the admin's own screen awake.  No server
  // involvement.
  let wakeSentinel = null;

  function applyWakeUi() {
    const on = wakeSentinel !== null && !wakeSentinel.released;
    wakeLockBtn.classList.toggle("on", on);
    wakeLockBtn.setAttribute("aria-pressed", String(on));
    wakeStatusBtn.hidden = !on;
    wakeStatusBtn.setAttribute("aria-pressed", String(on));
  }

  async function toggleWakeLock() {
    if (wakeSentinel) {
      try {
        await wakeSentinel.release();
      } catch (err) {
        /* already released */
      }
      wakeSentinel = null;
    } else if (!embed && "wakeLock" in navigator) {
      try {
        wakeSentinel = await navigator.wakeLock.request("screen");
        wakeSentinel.addEventListener("release", () => {
          // Fires for every loss: the tab being hidden (screen off,
          // app switch), the OS taking the lock back, or release().
          // UI-only — the lock is deliberately not re-acquired (see
          // the comment above).
          wakeSentinel = null;
          applyWakeUi();
        });
      } catch (err) {
        wakeSentinel = null; // denied/throttled: the UI shows the truth
      }
    }
    applyWakeUi();
  }

  wakeLockBtn.addEventListener("click", toggleWakeLock);
  wakeStatusBtn.addEventListener("click", toggleWakeLock);

  if (embed || !("wakeLock" in navigator)) {
    // The admin preview or an old browser: no wake-lock controls.
    wakeLockBtn.hidden = true;
  }
  applyWakeUi();

  /** Set the rendered size and button states from fontIndex; called by
      render() on every tick so the fit cap tracks the current text. */
  function applyTimerSize() {
    const fitPx = fitFontSizePx(timerEl.textContent || "MM:SS");
    if (embed) {
      // Mirror the display machine's chosen scale (synced via the
      // server, see applyFontSize); the fit cap still applies to this
      // (smaller) viewport.  The +/- buttons are hidden here.
      const idx = nearestFontIndex(serverFontScale);
      timerEl.style.fontSize =
        Math.min(baseFontSizePx() * FONT_SCALES[idx], fitPx) + "px";
      return;
    }
    fontIndex = Math.min(fontIndex, maxFontIndex());
    timerEl.style.fontSize =
      Math.min(baseFontSizePx() * FONT_SCALES[fontIndex], fitPx) + "px";
    fontSmallerBtn.disabled = fontIndex === 0;
    fontBiggerBtn.disabled = fontIndex >= maxFontIndex();
  }

  function applyFontSize() {
    applyTimerSize();
    if (embed) return; // the preview never persists a font size
    try {
      window.localStorage.setItem(FONT_SCALE_KEY, String(FONT_SCALES[fontIndex]));
    } catch (err) {
      /* non-fatal: size just won't persist */
    }
    // Sync the choice to the session so embedded previews (the admin
    // page) mirror it.  Fire-and-forget: a failure just leaves the
    // preview on its previous scale.
    fetch(ST.apiUrl(name, "/font-scale"), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scale: FONT_SCALES[fontIndex] }),
    }).catch(() => {});
  }

  fontSmallerBtn.addEventListener("click", () => {
    if (fontIndex > 0) {
      fontIndex -= 1;
      applyFontSize();
    }
  });

  fontBiggerBtn.addEventListener("click", () => {
    if (fontIndex < maxFontIndex()) {
      fontIndex += 1;
      applyFontSize();
    }
  });

  // Re-fit when the viewport changes (rotate a phone, resize a window).
  window.addEventListener("resize", applyTimerSize);

  setInterval(render, 250);
  render();

  // Fullscreen toggle; requestFullscreen can reject (e.g. without user
  // gesture or unsupported), so swallow the rejection and stay put.
  fullscreenBtn.addEventListener("click", () => {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      document.documentElement.requestFullscreen().catch(() => {});
    }
  });
})();
