// Regression harness for the admin page's session recovery
// (session_timer/static/admin.js): drives the REAL admin.js (plus the
// real common.js/config.js) through the recovery scenarios with a
// minimal DOM stub and a scripted fetch.  Each scenario boots a FRESH
// page, like a browser loading the admin URL.
//
// Run: node tests/js/test_recovery.mjs
//
// Verifies:
//   S1  real state -> server-empty state: timezone stamp + CSV replace
//       at the fresh generation + "restored" notice,
//   S1b a second empty state during the in-flight restore is ignored,
//   S1c a LATER server loss restores again (latch re-armed),
//   S2  a re-created session (generation rewind) pops the download
//       modal; download is side-effect-free; Continue adopts,
//   S2b after Continue, a later loss restores the adopted state,
//   S3  a fresh URL (never latched) does nothing on an empty state,
//   S4  an Add-event click DURING the restore waits for it and goes
//       out with the post-restore generation (no 409 race).

import { readFileSync } from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const staticDir = path.join(
  path.dirname(fileURLToPath(import.meta.url)), "..", "..", "session_timer", "static");

function makeEl(tag) {
  const el = {
    tag,
    children: [],
    dataset: {},
    style: {},
    _handlers: {},
    _set: new Set(),
    classList: {
      add: (c) => el._set.add(c),
      remove: (c) => el._set.delete(c),
      toggle: (c, force) => {
        if (force === undefined) {
          el._set.has(c) ? el._set.delete(c) : el._set.add(c);
        } else {
          force ? el._set.add(c) : el._set.delete(c);
        }
      },
      contains: (c) => el._set.has(c),
    },
    addEventListener(type, fn) { (el._handlers[type] ||= []).push(fn); },
    appendChild(c) { el.children.push(c); return c; },
    querySelector: () => null,
    remove() {},
    click() { (el._handlers.click || []).forEach((f) => f()); },
    contains: () => false,
    parentElement: null,
    rows: [],
    hidden: false,
    textContent: "",
    title: "",
    value: "",
    className: "",
    type: "",
    maxLength: 0,
    min: "",
    placeholder: "",
    disabled: false,
    href: "",
    download: "",
    src: "",
  };
  return el;
}

// Shared registries across page boots.
const elements = {};
const fetchCalls = [];
let fetchPlan = [];

// Count fetches SINCE a marker: boot-time calls (/state, /api/banner)
// land before scenarios start, so assertions must
// look at the tail of the log, not absolute offsets.
const mark = () => fetchCalls.length;
const since = (m) => fetchCalls.slice(m);

function bootPage() {
  const els = {};
  const getEl = (id) => (els[id] ||= makeEl("div#" + id));
  const tbody = makeEl("tbody");
  let tbodyText = "";
  Object.defineProperty(tbody, "textContent", {
    get: () => tbodyText,
    set(v) { tbodyText = v; tbody.children.length = 0; tbody.rows.length = 0; },
  });

  const sandbox = {
    document: {
      getElementById: (id) => { const e = getEl(id); elements[id] = e; return e; },
      querySelector: (sel) => (sel.includes("tbody") ? tbody : getEl(sel)),
      createElement: (tag) => makeEl(tag),
      createDocumentFragment: () => makeEl("fragment"),
      addEventListener() {},
      body: makeEl("body"),
      activeElement: null,
      title: "",
    },
    window: {
      location: { pathname: "/testsession/", protocol: "http:", host: "localhost" },
      performance: { now: () => Date.now() },
      localStorage: { getItem: () => null, setItem() {} },
      setTimeout() { return 0; },
      addEventListener() {},
      confirm: () => true,
      prompt: () => "10",
    },
    console,
    setInterval() { return 0; },
    clearInterval() {},
    setTimeout() { return 0; },
    Intl, URL, Blob,
    JSON, Math, Date, Number, String, Object, Array, Promise,
    isNaN, parseInt, parseFloat, encodeURIComponent,
    fetch: async () => { throw new Error("unexpected fetch"); },
    globalThis: {},
  };
  sandbox.globalThis = sandbox;
  // Recording fetch: every call is logged; responses come from fetchPlan.
  sandbox.fetch = async (url, opts) => {
    fetchCalls.push({ url, opts: opts || {} });
    const next = fetchPlan.shift();
    if (!next) {
      throw new Error("unexpected fetch: " + ((opts && opts.method) || "GET") + " " + url);
    }
    return next(url, opts);
  };
  const ctx = vm.createContext(sandbox);

  vm.runInContext(readFileSync(path.join(staticDir, "config.js"), "utf8"), ctx);
  vm.runInContext(readFileSync(path.join(staticDir, "common.js"), "utf8"), ctx);
  vm.runInContext(
    'ST.connect = (name, onState, onStatus) => { globalThis.__conn = { name, onState, onStatus }; return { close() {} }; };',
    ctx
  );
  vm.runInContext(readFileSync(path.join(staticDir, "admin.js"), "utf8"), ctx, {
    filename: "admin.js",
  });
  return { onState: sandbox.__conn.onState, el: (id) => elements[id] };
}

const tick = async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
};
const fail = (msg) => { console.error("FAIL:", msg); process.exit(1); };
const ok = (msg) => console.log("ok:", msg);
const plan = (...handlers) => fetchPlan.push(...handlers);

const realState = (gen, events, tz) => ({
  name: "testsession", generation: gen, timezone: tz, utc_offset: 0,
  server_time: 1700000000, dim_level: 0,
  events: events.map((e, i) => ({ id: "e" + i, ...e })),
});
const emptyState = () => ({
  name: "testsession", generation: null, timezone: null, utc_offset: null,
  server_time: 1700000001, dim_level: 0, events: [],
});
const json = (data) => ({ ok: true, json: async () => data });

(async () => {
  const base = mark();

  // ---- S1: page loads on an EXISTING session, then the server restarts.
  const p1 = bootPage();
  p1.onState(realState(3, [{ name: "Talk", start_time: 3600, duration: 600 }], "Europe/Berlin"));
  plan(
    (url, opts) => {
      if (opts.method !== "POST" || !url.includes("/timezone")) throw new Error("expected tz POST");
      return json(realState(4, [], "Europe/Berlin"));
    },
    (url, opts) => {
      if (opts.method !== "PUT" || !url.includes("/events.csv")) throw new Error("expected CSV PUT");
      if (opts.headers["X-Generation"] !== "4") throw new Error("CSV gen = " + opts.headers["X-Generation"]);
      if (!opts.body.includes("1:00,600,Talk")) throw new Error("CSV body wrong: " + opts.body);
      return json(realState(5, [{ name: "Talk", start_time: 3600, duration: 600 }], "Europe/Berlin"));
    },
  );
  p1.onState(emptyState());
  await tick();
  const s1 = since(base).filter(
    (c) => c.url.includes("/api/") && !c.url.includes("/state") && !c.url.includes("/banner")
  );
  if (s1.length !== 2) fail("S1: expected tz POST + CSV PUT, got " + s1.length);
  if (!elements["error-banner"].textContent.includes("restored 1 event(s)")) {
    fail("S1: banner missing: " + elements["error-banner"].textContent);
  }
  ok("S1: empty state after restart -> tz stamp (gen 4) + CSV PUT (gen 4) + banner");

  // ---- S1b: an empty state arriving WHILE the restore is in flight
  // must not start a second one (the restoring guard).
  const b1 = mark();
  plan(
    () => json(realState(6, [], "Europe/Berlin")),
    (url, opts) => {
      if (opts.headers["X-Generation"] !== "6") throw new Error("gen " + opts.headers["X-Generation"]);
      return json(realState(7, [{ name: "Talk", start_time: 3600, duration: 600 }], "Europe/Berlin"));
    },
  );
  p1.onState(emptyState());
  p1.onState(emptyState());
  await tick();
  const s1b = since(b1).filter(
    (c) => c.url.includes("/api/") && !c.url.includes("/state") && !c.url.includes("/banner")
  );
  if (s1b.length !== 2) fail("S1b: in-flight double restore! calls: " + s1b.length);
  ok("S1b: empty state during an in-flight restore does not double-restore");

  // ---- S1c: after a successful restore the latch is re-armed, so a
  // LATER server loss restores again (second restart also recovers).
  const b1c = mark();
  plan(
    () => json(realState(8, [], "Europe/Berlin")),
    (url, opts) => {
      if (opts.headers["X-Generation"] !== "8") throw new Error("gen " + opts.headers["X-Generation"]);
      return json(realState(9, [{ name: "Talk", start_time: 3600, duration: 600 }], "Europe/Berlin"));
    },
  );
  p1.onState(emptyState());
  await tick();
  const s1c = since(b1c).filter(
    (c) => c.url.includes("/api/") && !c.url.includes("/state") && !c.url.includes("/banner")
  );
  if (s1c.length !== 2) fail("S1c: re-restore after re-arm failed: " + s1c.length);
  ok("S1c: latch re-armed after a successful restore (second restart also recovers)");

  // ---- S2: NEW EPOCH (gen rewound, tz equal): popup, no auto-restore.
  const p2 = bootPage();
  const base2 = mark();
  p2.onState(realState(40, [{ name: "Old", start_time: 3600, duration: 600 }], "Europe/Berlin"));
  p2.onState(realState(2, [{ name: "Other", start_time: 7200, duration: 60 }], "Europe/Berlin"));
  await tick();
  const s2 = since(base2).filter(
    (c) => c.url.includes("/api/") && !c.url.includes("/state") && !c.url.includes("/banner")
  );
  if (elements["new-session-modal"].hidden !== false) fail("S2: new-epoch popup not shown");
  if (s2.length !== 0) fail("S2: popup must not fetch (saw " + s2.length + ")");
  if (!elements["new-session-text"].textContent.includes("previous version")) {
    fail("S2: popup text wrong");
  }
  ok("S2: rewound generation -> new-epoch popup with download offer");
  elements["new-session-download"].click(); // repeatable, keeps latch
  if (since(base2).length !== s2.length) fail("S2: download click must not fetch");
  ok("S2: download button works without side effects");
  elements["new-session-continue"].click(); // adopt

  // ---- S2b: after adopting, a LATER server loss must re-arm recovery
  // (epochHandled reset) — restore should put the adopted events back.
  plan(
    () => json(realState(3, [], "Europe/Berlin")),
    (url, opts) => {
      if (opts.headers["X-Generation"] !== "3") throw new Error("gen " + opts.headers["X-Generation"]);
      if (!opts.body.includes("Other")) throw new Error("CSV body wrong: " + opts.body);
      return json(realState(4, [{ name: "Other", start_time: 7200, duration: 60 }], "Europe/Berlin"));
    },
  );
  p2.onState(emptyState());
  await tick();
  const s2b = since(base2).filter(
    (c) => c.url.includes("/api/") && !c.url.includes("/state") && !c.url.includes("/banner")
  );
  if (s2b.length !== 2) fail("S2b: adopt did not re-arm recovery (epochHandled stuck)");
  ok("S2b: after Continue, a later restart re-restores the adopted state");

  // ---- S3: fresh URL never edited -> empty state -> NOTHING happens.
  const p3 = bootPage();
  const base3 = mark();
  p3.onState(emptyState());
  await tick();
  const s3 = since(base3).filter((c) => c.url.includes("/api/"));
  if (s3.length !== 0) fail("S3: fresh URL must not restore (saw " + s3.length + " calls)");
  ok("S3: fresh URL (no latch) -> no restore, no crash");

  // ---- S4: an Add-event click DURING the restore must wait for it and
  // go out with the POST-restore generation (the old code raced the
  // CSV PUT with the pre-restore generation and 409'd — the reported
  // "State changed by someone else" dead-end).
  const p4 = bootPage();
  const base4 = mark();
  p4.onState(realState(3, [{ name: "Talk", start_time: 3600, duration: 600 }], "Europe/Berlin"));
  plan(
    () => json(realState(4, [], "Europe/Berlin")),
    (url, opts) => {
      if (opts.headers["X-Generation"] !== "4") throw new Error("CSV gen " + opts.headers["X-Generation"]);
      return json(realState(5, [{ name: "Talk", start_time: 3600, duration: 600 }], "Europe/Berlin"));
    },
    (url, opts) => {
      if (opts.method !== "POST" || !url.includes("/events")) throw new Error("expected events POST");
      if (opts.headers["X-Generation"] !== "5") {
        throw new Error("add-event raced the restore: gen " + opts.headers["X-Generation"]);
      }
      return json(realState(6, [{ name: "Talk", start_time: 3600, duration: 600 }], "Europe/Berlin"));
    },
  );
  p4.onState(emptyState()); // restore starts (silent until it finishes)
  elements["add-event"].click(); // user clicks while the restore runs
  await tick();
  const s4calls = since(base4).filter(
    (c) => c.url.includes("/api/") && !c.url.includes("/state") && !c.url.includes("/banner")
  );
  if (s4calls.length !== 3) fail("S4: expected tz + CSV + events POST, got " + s4calls.length);
  const kinds = s4calls.map((c) => c.opts.method + " " + c.url);
  if (!kinds[0].includes("/timezone")) fail("S4: first call should be the tz stamp: " + kinds[0]);
  if (!kinds[1].includes("/events.csv")) fail("S4: second call should be the CSV PUT: " + kinds[1]);
  if (!kinds[2].includes("/events") || kinds[2].includes("events.csv")) fail("S4: third call should be the events POST: " + kinds[2]);
  if (s4calls[2].opts.headers["X-Generation"] !== "5") {
    fail("S4: add-event used pre-restore generation " + s4calls[2].opts.headers["X-Generation"]);
  }
  ok("S4: add-event during a restore waits for it and uses the fresh generation");

  console.log("ALL SCENARIOS PASS");
})().catch((e) => { console.error("HARNESS ERROR:", e); process.exit(1); });