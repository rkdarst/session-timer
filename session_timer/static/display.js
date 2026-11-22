"use strict";

(function () {
  const timerEl = document.getElementById("timer");
  const barEl = document.getElementById("bar");
  const nextWrap = document.getElementById("next-wrap");
  const nextBar = document.getElementById("next-bar");
  const nextText = document.getElementById("next-text");
  const fullscreenBtn = document.getElementById("fullscreen");
  const fontSmallerBtn = document.getElementById("font-smaller");
  const fontBiggerBtn = document.getElementById("font-bigger");
  const name = ST.sessionName();
  // Flashing after expiry is capped: 5 minutes after an event ends the
  // counter and bar stop flashing (steady red, still counting negative).
  const FLASH_LIMIT = 5 * 60;
  // "Next speaker" bar: if no event starts within 10 minutes of the
  // current event's end (long gap), the bar stays hidden and only
  // fills during the final 10 minutes before the next event.  Shorter
  // gaps fill across the whole gap, starting at expiry.
  const NEXT_WINDOW = 10 * 60;

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

  let events = [];
  // In the admin preview (embed mode) the mirrored font scale comes
  // from the server state: display browsers sync their +/- choice there
  // (see applyFontSize), so the preview shows the chosen relative size.
  let serverFontScale = null;

  ST.connect(name, (data) => {
    // Raw events hold service-day seconds; convert to absolute unixtime
    // once here so all countdown/current-event logic is unchanged.
    events = ST.absoluteEvents(data.events, data.anchor);
    if (embed) serverFontScale = data.font_scale;
  });

  /** Per DESIGN.md (via ST.currentEvent): the event with the greatest
      start_time <= now (overlaps resolve to the most recently started
      one); it stays current, counting up negative, until the next event
      starts — but only FLASH_LIMIT seconds past its end: after that
      (and before anything has started) the display idles. */
  function render() {
    const now = Date.now() / 1000;
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
      if (fractionLeft <= 0.15) colorClass = "red";
      else if (fractionLeft <= 0.25) colorClass = "yellow";
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
  const FONT_SCALES = [
    0.33, 0.4, 0.48, 0.58, 0.69, 0.83, 1, 1.2, 1.45, 1.73, 2.07, 2.49, 2.99, 3.58, 4.3,
  ];
  const FONT_SCALE_KEY = "session-timer:font-scale";

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
