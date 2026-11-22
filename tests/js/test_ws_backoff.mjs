// Regression test for the WebSocket reconnect backoff in
// session_timer/static/common.js (ST.connect).
//
// Run: node tests/js/test_ws_backoff.mjs
//
// Verifies, with stubbed timers and a WebSocket class that first always
// fails and then succeeds:
//   1. the first retry waits CONFIG.reconnectBaseMs,
//   2. every further failure doubles the wait, capped at
//      CONFIG.reconnectMaxMs,
//   3. a successful connection resets the ladder to the base,
//   4. the 5 s /state polling fallback runs while disconnected and is
//      never duplicated.
// No real sockets or real time: setTimeout/setInterval are captured
// stubs, so the whole ladder is walked instantly.

import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert";
import path from "node:path";
import { fileURLToPath } from "node:url";

const staticDir = path.join(
  path.dirname(fileURLToPath(import.meta.url)), "..", "..", "session_timer", "static");

// config.js must load before common.js (classic scripts, shared scope).
const src =
  readFileSync(path.join(staticDir, "config.js"), "utf8") + "\n" +
  readFileSync(path.join(staticDir, "common.js"), "utf8") + "\n" +
  "globalThis.__ST = ST; globalThis.__CONFIG = CONFIG;";

const pending = [];  // captured reconnect timers { fn, ms, cancelled }
const polls = [];    // captured setInterval(fn, ms) calls
let failConnections = true;
const sockets = [];  // successfully constructed WebSocket instances

class FakeWS {
  constructor() {
    if (failConnections) throw new Error("unreachable");
    sockets.push(this);
  }
  close() {}
}

const sandbox = {
  console,
  Date,
  performance: { now: () => 0 },
  setTimeout(fn, ms) { pending.push({ fn, ms, cancelled: false }); return pending.length; },
  clearTimeout(id) { if (pending[id - 1]) pending[id - 1].cancelled = true; },
  setInterval(fn, ms) { polls.push({ fn, ms }); return polls.length; },
  clearInterval() {},
  WebSocket: FakeWS,
};
sandbox.window = sandbox; // code reads window.location.protocol/.host
sandbox.window.location = { protocol: "http:", host: "test:8000" };

vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const ST = sandbox.__ST;
const CONFIG = sandbox.__CONFIG;

// Fire the oldest pending (non-cancelled) reconnect timer; return the
// delay it had been scheduled with.
function fireNext() {
  const timer = pending.find((t) => !t.cancelled);
  assert.ok(timer, "expected a pending reconnect timer");
  timer.cancelled = true;
  timer.fn();
  return timer.ms;
}

const cfgMax = CONFIG.reconnectMaxMs;
const ladder = [];
for (let d = CONFIG.reconnectBaseMs; d < cfgMax; d *= 2) ladder.push(d);
ladder.push(cfgMax, cfgMax); // 256 s then capped on the next two steps

// 1) Initial connect: open() fails -> 5 s polling + timer at the base.
const handle = ST.connect("sess", () => {});
const pollCount = () => polls.filter((p) => p.ms === 5000).length;
assert.ok(polls.some((p) => p.ms === 3600000),
  "hourly /state resync interval must be armed");
assert.strictEqual(pollCount(), 1,
  "5 s polling fallback must start on the first failure");
assert.strictEqual(pending[0].ms, CONFIG.reconnectBaseMs,
  "first retry must wait reconnectBaseMs");

// 2) Walk the ladder: base doubling to the 5 min cap.
const observed = [];
for (let i = 0; i < ladder.length; i++) {
  observed.push(fireNext());
  assert.strictEqual(pollCount(), 1,
    "poll fallback must stay a single 5 s interval");
}
assert.deepStrictEqual(observed, ladder,
  `delays must be ${CONFIG.reconnectBaseMs} ms doubling, capped at ${cfgMax} ms`);

// 3) Recovery: let a connection succeed; onopen resets the ladder.
failConnections = false;
assert.strictEqual(fireNext(), cfgMax, "last pending timer used the cap");
assert.ok(!pending.some((t) => !t.cancelled),
  "a successful open must not schedule a reconnect");
const sock = sockets[sockets.length - 1];
sock.onopen(); // stopPolling + backoff reset
sock.onclose(); // connection drops again
assert.strictEqual(pending[pending.length - 1].ms, CONFIG.reconnectBaseMs,
  "after a successful connection, the first retry is the base delay");
assert.strictEqual(pollCount(), 2,
  "polling restarts after the drop");

// 4) The ladder climbs from the base again after the reset.
failConnections = true;
assert.strictEqual(fireNext(), CONFIG.reconnectBaseMs);
assert.strictEqual(fireNext(), CONFIG.reconnectBaseMs * 2,
  "ladder climbs from the base again after the reset");

handle.close();
console.log("ok: reconnect backoff ladder, cap, reset-on-open, polling fallback");
