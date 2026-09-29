// Board specs: a tool page through a staged Intellex host attached to a bench board - its COM port (INTELLEX.md IX-WP7,
// IX-WP8, the flashes of IX-WP10) or NaviCore's access point over the PC's spare WiFi adapter (IX-WP9). The harness test
// of the same id (tests/hil/suites/s34_intellex_tools.py, s35_intellex_wifi.py, s36_intellex_flash.py) releases that one
// port to the host or joins the access point, keeps every other board and probe, and reaches the bench for the spec
// through its bridge (tests/hil/hil/bridge.py: /context, /note, /wire/*, and /hook for a step the spec sequences). The
// pages connect on their own: Intellex's shim replaces navigator.serial and auto-connects (intellex_shim.js autoConnect,
// autoConnectWcb), so there is no port picker and no Chrome profile.
//
// Credentials: through NaviCore the link carries GET_CONFIG, through a WCB its ?backup and every pull - the mesh
// password and the WiFi passphrase. Nothing here keeps a line's text: watchLink keeps the JSON type (and a SET_CONFIG's
// branch names) of each line a page wrote, a command's first word, and first-seen times of the markers a spec asks
// about; the tools' own state is read by name, one field at a time. Never read the NaviCore tool's #terminal-output
// (D-NC5): it holds the CONFIG echo. A spec is never handed a network name: over WiFi the harness's hooks scrub them.
const { expect } = require('@playwright/test');
const { hostGuard } = require('./fixtures');

const BRIDGE = process.env.HIL_BRIDGE || '';

async function call(path, body = {}) {
  const r = await fetch(BRIDGE + path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                         body: JSON.stringify(body) });
  const reply = await r.json();
  if (reply.skip) throw new Error(`harness has no such wire: ${reply.skip}`);
  if (!r.ok) throw new Error(`harness ${path}: ${reply.error}`);
  return reply;
}

// A bench action the harness test offers this spec (hil/intellex.py run_intellex_test hooks=, the bridge's /hook route):
// its reply, or {skip} when the harness raised Skip. The harness's own test thread is parked while Playwright runs, so a
// step the spec must sequence - moving the PC's WiFi adapter, counting a console's lines, judging the host's flash log -
// is asked for here, by name.
async function hook(name, body = {}) {
  const r = await fetch(BRIDGE + '/hook', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                            body: JSON.stringify(Object.assign({}, body, { name })) });
  const reply = await r.json();
  if (r.status === 424) return { skip: reply.skip };
  if (!r.ok) throw new Error(`harness hook ${name}: ${reply.error}`);
  return reply;
}

// The harness bridge: what it decided for this run, a line in session.log, the probe wires, and the test's hooks.
const hil = {
  present: !!BRIDGE,
  context: () => call('/context'),
  note: (text) => call('/note', { text }),
  wire: (wcb, port) => ({
    mark: async () => (await call('/wire/mark', { wcb, port })).mark,
    received: async (since) => Buffer.from((await call('/wire/received', { wcb, port, since })).hex, 'hex'),
    expect: (text, since, timeout = 3) => call('/wire/expect', { wcb, port, text, since, timeout }),
  }),
  hook,
};

// GET from the host from Node (no Origin: a native client) -> {status, json}. Never read /_api/status's lastError into a
// message from a WiFi test: a host held off an access-point move names both networks there (the harness's hooks hand
// it over scrubbed instead).
async function hostGet(path) {
  const r = await fetch(process.env.INTELLEX_URL + path);
  return { status: r.status, json: await r.json().catch(() => null) };
}

// /_api/signals through to the host, each call recorded with its answer: {body, status, ok}.
function passSignals(signals) {
  return async (route, req) => {
    let body = null;
    try { body = req.postDataJSON(); } catch (_) { body = null; }
    const resp = await route.fetch();
    let ok = null;
    try { ok = (await resp.json()).ok; } catch (_) { ok = null; }
    signals.push({ body, status: resp.status(), ok });
    return route.fulfill({ response: resp });
  };
}

// hostGuard for a board run: every route that reaches past the host is still answered here (a COM-port probe, the
// droid's access point, GitHub, esptool, an attach) except /_api/signals, which a tool's DTR/RTS must really reach -
// what the page does to the control lines is part of what these tests are about. Each signals call is recorded with the
// host's answer: {body, status, ok}.
async function boardGuard(context, rec) {
  const signals = [];
  const calls = await hostGuard(context, rec, { signals: passSignals(signals) });
  return { calls, signals };
}

// boardGuard for a flash run (INTELLEX.md IX-WP10): /_api/flash-wcb and /_api/flash reach the host too - a flash is what
// the test is about - each call recorded with the host's answer: {name, body, status, ok, error}. Everything else that
// reaches past the host is still answered here. The harness seeds the only firmware the host can find, for one product.
async function flashGuard(context, rec) {
  const signals = [];
  const flashes = [];
  const pass = (name) => async (route, req) => {
    let body = null;
    try { body = req.postDataJSON(); } catch (_) { body = null; }
    const resp = await route.fetch();
    let reply = null;
    try { reply = await resp.json(); } catch (_) { reply = null; }
    flashes.push({ name, body, status: resp.status(), ok: reply && reply.ok, error: reply && reply.error });
    return route.fulfill({ response: resp });
  };
  const calls = await hostGuard(context, rec, { signals: passSignals(signals), 'flash-wcb': pass('flash-wcb'),
                                                flash: pass('flash') });
  return { calls, signals, flashes };
}

// Follow the host's flash on /_api/flash-status until it has run and ended -> {final, percents, sawRunning}: every
// percent read while it ran (INTELLEX.md finding 13: under esptool 5 it reads 0 until the end). during(st) runs once, at
// the first reading that shows it running. stopIf(sawRunning) -> a reason to give up (the page failed before the flash
// started), checked each round.
async function followFlash({ startTimeout = 90_000, timeout = 420_000, during = null, stopIf = null } = {}) {
  const t0 = Date.now();
  const percents = [];
  let saw = false, ran = false, st = null;
  for (;;) {
    st = (await hostGet('/_api/flash-status')).json || {};
    if (st.running) {
      saw = true;
      percents.push(st.percent || 0);
      if (during && !ran) { ran = true; await during(st); }
    } else if (saw) {
      break;
    }
    if (stopIf) {
      const why = await stopIf(saw);
      if (why) throw new Error(why);
    }
    if (!saw && Date.now() - t0 > startTimeout) throw new Error(`no flash started within ${startTimeout / 1000} s`);
    if (Date.now() - t0 > timeout) throw new Error(`the host's flash still ran after ${timeout / 1000} s`);
    await new Promise((r) => setTimeout(r, 500));
  }
  return { final: st, percents, sawRunning: saw };
}

// Record every toast the Wizard shows (showToast is a top-level function, so reassigning it on window reaches its own
// calls): window.__hilToasts [{message, type}]. Toasts are how boardGo reports a flash's outcome.
async function recordToasts(page) {
  await page.evaluate(() => {
    if (window.__hilToasts) return;
    const toasts = window.__hilToasts = [];
    const real = window.showToast;
    window.showToast = (message, type, duration) => {
      toasts.push({ message: String(message), type: type || 'info' });
      return real(message, type, duration);
    };
  });
}

// Count the Wizard's config pulls of one slot (boardPull, a top-level function): window.__hilBoardPulls
// [{n, at, ok}] - ok is what the pull's promise settled to, or 'error'. After a flash the Wizard pulls the board again.
async function spyBoardPulls(page) {
  await page.evaluate(() => {
    if (window.__hilBoardPulls) return;
    const rec = window.__hilBoardPulls = [];
    const real = window.boardPull;
    window.boardPull = function (n, ...rest) {
      const e = { n: Number(n), at: Date.now(), ok: null };
      rec.push(e);
      const p = real.call(this, n, ...rest);
      Promise.resolve(p).then((v) => { e.ok = v === undefined ? true : !!v; }, () => { e.ok = 'error'; });
      return p;
    };
  });
}

// One line a page wrote, summarised: its JSON type after an optional ;w<n>, prefix (wrapped: the tool's Via-WCB form),
// with a SET_CONFIG's branch names and never a value; or a console command's first word (up to a comma or a space) -
// and for a ?MGMT,FRAG, the board it goes to and the inner command's first two words ('RTERM,START'), never its values.
function summarise(line) {
  const t = String(line).trim();
  const m = /^;w(\d+),([\s\S]*)$/i.exec(t);
  const body = m ? m[2] : t;
  const out = { at: Date.now(), wrapped: !!m, to: m ? Number(m[1]) : null };
  if (body.startsWith('{')) {
    try {
      const o = JSON.parse(body);
      out.type = String(o.type || '?');
      if (o.data && typeof o.data === 'object' && !Array.isArray(o.data)) out.keys = Object.keys(o.data);
    } catch (_) {
      out.type = 'unparsed-json';
    }
  } else {
    out.cmd = body.split(/[,\s]/)[0].slice(0, 24);
    const f = /^.MGMT,FRAG,(\d+),[0-9A-Fa-f]+,\d+,\d+,.([A-Za-z]+)(?:,([A-Za-z]+))?/.exec(body);
    if (f) { out.frag = Number(f[1]); out.inner = f[2].toUpperCase() + (f[3] ? `,${f[3].toUpperCase()}` : ''); }
  }
  return out;
}

// What the pages of this Playwright page wrote to and read from their /_link sockets, as summaries (summarise) and
// marker times: {sockets, sent: [...], rx: bytes read, pongs: [times], seen: {marker: first time}}. Lines read are
// rebuilt per socket across frames and dropped once looked at. Frames of every frame of the page are seen (the shell's
// two tools share one list; each entry of `sent` says nothing about which frame wrote it).
function watchLink(page, markers = []) {
  const w = { sockets: 0, sent: [], rx: 0, pongs: [], seen: {} };
  const text = (p) => (typeof p === 'string' ? p : Buffer.from(p).toString('utf8'));
  page.on('websocket', (ws) => {
    let path = '';
    try { path = new URL(ws.url()).pathname; } catch (_) { return; }
    if (path !== '/_link') return;
    w.sockets++;
    let buf = '';
    ws.on('framesent', (f) => { w.sent.push(summarise(text(f.payload))); });
    ws.on('framereceived', (f) => {
      const s = text(f.payload);
      w.rx += s.length;
      buf += s;
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i);
        buf = buf.slice(i + 1);
        if (line.includes('"type":"PONG"')) w.pongs.push(Date.now());
        for (const mk of markers) if (!(mk in w.seen) && line.includes(mk)) w.seen[mk] = Date.now();
      }
      if (buf.length > 65536) buf = buf.slice(-4096);   // a line that never ends must not grow without bound
    });
  });
  return w;
}

// The JSON types the NaviCore tool sends on its own - its connect handshake (index.html openPortAndStart :4511-4605),
// its polls and its keep-alive - none of which writes NaviCore's flash. The command library is only compared on connect
// (GET_CMDLIB_META, :14487-14496), and a different one is only reported (the CMDLIB_META handler, :9139-9155): the pull
// (GET_CMDLIB) is the "Load from NaviCore" button (:14500-14505) and SET_CMDLIB only its Save (:14693-14733).
// SET_DEBUG_FLAGS and START/STOP_MONITOR set RAM (NaviCore.ino:489-494). Anything else a spec did not ask for is a write
// nobody made.
const NC_READ_ONLY = new Set(['PING', 'GET_CONFIG', 'GET_CMDLIB_META', 'GET_CMDLIB', 'START_MONITOR', 'STOP_MONITOR',
  'SET_DEBUG_FLAGS', 'GET_WCB_STATUS', 'GET_WCB_META', 'GET_MESH_STATS', 'GET_WCB_SEQ', 'GET_WCB_SEQVAL']);

function unaskedWrites(w, allow = []) {
  return w.sent.filter((s) => s.type && !NC_READ_ONLY.has(s.type) && !allow.includes(s.type)).map((s) => s.type);
}

// F5, which the shim turns into location.reload() (intellex_shim.js: an app window has no browser chrome), on a page or
// a frame. Resolves once the new document has loaded.
async function pressF5(page) {
  const nav = page.waitForEvent('framenavigated', { predicate: (f) => f === page.mainFrame(), timeout: 15_000 });
  await page.keyboard.press('F5');
  await nav;
  await page.waitForLoadState('load');
}

// Uncaught page errors (any frame), and failed requests other than the host's GitHub proxy answering 502 offline with
// no firmware cache - the designed con-floor answer the tools must survive (ui_tools_load.spec.js).
function expectClean(rec, what = 'the page') {
  expect(rec.errors, `${what}: uncaught page errors`).toEqual([]);
  expect(rec.bad.filter((b) => !/^502 .*\/_api\/gh\//.test(b)), `${what}: failed requests`).toEqual([]);
}

// ── The Wizard (Wizard/app.js: top-level lets, read by bare name) ─────────────────────────────────────────────────
async function openWizard(page, path = '/wcb/Wizard/') {
  await page.goto(path, { waitUntil: 'load' });
  await page.waitForFunction(() => typeof _modalDoConnect === 'function' && typeof boardBaselines !== 'undefined');
}

// Slot `n` pulled, with no click: the shim's autoConnectWcb connects slot 1 and the Wizard pulls it 3 s later
// (_modalDoConnect). -> the pulled board's WCB number.
async function waitPulled(target, n, timeout = 45_000) {
  await target.waitForFunction((n) => !!boardBaselines[n] && !_boardPullInFlight.has(n), n, { timeout });
  return target.evaluate((n) => boardBaselines[n].wcbNumber, n);
}

// Wait until every board in `boards` has a baseline and no pull of it is running, or `ms` passed -> the boards still
// missing (reported by the caller). The shim pulls each mesh board it routes through the doorway (routeMeshThroughBoard,
// or relayRouteAll through a relay card); a flash or a push started meanwhile would share the doorway's stream with it.
async function meshSettled(page, boards, ms = 60_000) {
  const until = Date.now() + ms;
  for (;;) {
    const left = await page.evaluate((bs) => bs.filter((b) => !boardBaselines[b] || _pullingBoards.has(b)), boards);
    if (!left.length || Date.now() > until) return left;
    await page.waitForTimeout(1000);
  }
}

// The Wizard's slots whose connection is up.
function liveSlots(target) {
  return target.evaluate(() => Object.entries(boardConnections)
    .filter(([, c]) => c && c.isConnected && c.isConnected()).map(([k]) => Number(k)));
}

// Set a field the way a user would when it is on screen; otherwise set it and fire the handlers the Wizard listens on
// (tests/wizard/lib/wizard.js setField).
async function setField(page, selector, value) {
  const el = page.locator(selector);
  if (await el.isVisible()) {
    await el.fill(value);
    await el.press('Tab');
    return;
  }
  await el.evaluate((node, value) => {
    node.value = value;
    node.dispatchEvent(new Event('input', { bubbles: true }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
  }, value);
}

// Click Push Config and return boardGo()'s recorded outcome (tests/wizard/lib/wizard.js pushConfig: waits on boardGo's
// own promise, since polling the outcome races its provisional one).
async function pushConfig(page, n) {
  await page.evaluate(() => {
    if (!window.__hilBoardGo) {
      window.__hilBoardGo = boardGo;
      window.boardGo = (...a) => (window.__hilPush = window.__hilBoardGo(...a));
    }
    window.__hilPush = null;
  });
  await page.locator(`#b${n}-btn-go`).click();
  await page.waitForFunction(() => window.__hilPush, null, { timeout: 10_000 });
  return page.evaluate(async (n) => { await window.__hilPush; return boardPushOutcome[n]; }, n);
}

// Wrap remoteBoardPull, which Intellex's shim calls by name for each mesh board it routes through a plain WCB
// (intellex_shim.js routeMeshThroughBoard) and the Wizard itself calls on an ETM came-ONLINE edge. Each call is kept
// with its caller (byShim: /_intellex.js is on the stack), its relay argument as given and its type, and the one
// onComplete it gets. The first call through a relay also puts tests/wizard/specs/remote_pull.spec.js's spy on that
// relay's connection: the tag, board, lengths and part numbers of each [MGMT:...] line, never its text, and every
// ?MGMT,PULL it sends. Install it right after the page loads: the shim's first pull comes at least 2.4 s later.
async function spyRemotePulls(target) {
  await target.evaluate(() => {
    if (window.__hilPulls) return;
    const rec = window.__hilPulls = { calls: [], done: [], seen: [], sent: [] };
    const spy = (line) => {
      const m = /^\[MGMT:(CONFIG|CFGPART|CFGERR),(\d+)\]([\s\S]*)$/.exec(line);
      if (!m) return;
      const r = { tag: m[1], n: Number(m[2]), len: m[3].length };
      if (m[1] === 'CFGPART') {
        const p = /^P([0-9A-F]{4}),(\d+),(\d+):/.exec(m[3]);
        if (p) {
          Object.assign(r, { id: p[1], k: Number(p[2]), K: Number(p[3]),
                             dataLen: m[3].length - p[0].length - 1, framed: m[3].endsWith('~') });
        }
      }
      if (m[1] === 'CFGERR') {
        const code = m[3].split(',')[0].trim();          // shaped like the firmware's, or not kept (remote_pull.spec)
        r.code = /^[A-Z]{2,12}$/.test(code) ? code : 'MALFORMED';
      }
      rec.seen.push(r);
    };
    const orig = window.remoteBoardPull;
    window.remoteBoardPull = function (relayN, targetN, attempt, maxAttempts, onComplete) {
      const c = { relay: String(relayN), relayType: typeof relayN, target: Number(targetN), at: Date.now(),
                  byShim: /_intellex\.js/.test(String(new Error().stack || '')) };
      rec.calls.push(c);
      const conn = boardConnections[relayN];
      if (conn && !conn.__hilSpied) {
        conn.__hilSpied = true;
        conn._dataCallbacks.push(spy);
        const origSend = conn.send;
        conn.send = function (data, ...rest) {
          const s = String(data);
          if (/MGMT,PULL/.test(s)) rec.sent.push(s.trim());
          return origSend.call(this, data, ...rest);
        };
      }
      const done = (ok) => {
        rec.done.push({ target: c.target, ok, byShim: c.byShim, ms: Date.now() - c.at });
        if (typeof onComplete === 'function') onComplete(ok);
      };
      return orig.call(this, relayN, targetN, attempt, maxAttempts, done);
    };
  });
}

// What the spy holds for `target`, and the Wizard's state for it. seqs: the HILPnn throwaway sequences' value lengths;
// rawInTerminal: a raw ?SEQ,SAVE,HILPnn line reached any terminal pane (read in the page, only the answer leaves it).
function pullResult(target, n) {
  return target.evaluate((n) => {
    const r = window.__hilPulls || { calls: [], done: [], seen: [], sent: [] };
    const cfg = boardConfigs[n] || {};
    const seqs = {};
    for (const s of cfg.sequences || []) if (/^HILP\d\d$/.test(s.key)) seqs[s.key] = String(s.value).length;
    const panes = [...document.querySelectorAll('[id^="term-pane-output-"]')].map((el) => el.textContent).join('\n');
    return {
      calls: r.calls, done: r.done, seen: r.seen, sent: r.sent, seqs,
      wcbNumber: cfg.wcbNumber ?? null, fwVersion: cfg.fwVersion ?? null, type: cfg.type ?? null,
      baseline: !!boardBaselines[n], stillPulling: _pullingBoards.has(n),
      relayFor: remoteRelayForBoard[n] === undefined ? null : remoteRelayForBoard[n],
      relayForType: typeof remoteRelayForBoard[n],
      client: typeof _meshClients !== 'undefined' && _meshClients.has(n),
      rawInTerminal: /\?SEQ,SAVE,HILP\d\d,/.test(panes),
    };
  }, n);
}

// ── The NaviCore config tool (config_tool/index.html: top-level lets, read by bare name) ─────────────────────────
// Its state, and the handshake's PONG epoch: 1 when the direct PING was answered, 2 when only the Via-WCB retry was
// (openPortAndStart's pingForPong and its fallback, index.html:4568-4595).
function ncState(target) {
  return target.evaluate(() => ({
    viaWcbActive, configLoaded: _configLoaded, connected: !!port, isMonitoring, pongEpoch: _pongEpoch,
    status: document.getElementById('status-text').textContent,
    transport: ((document.getElementById('intellex-transport') || {}).textContent || '').trim(),
    fw: ((document.getElementById('fw-current-version') || {}).textContent || '').trim(),
    ota: (() => {
      const b = document.getElementById('btn-fw-ota');
      return b ? { text: (b.textContent || '').trim(), disabled: b.disabled } : null;
    })(),
  }));
}

// The end of the tool's own connect: START_MONITOR has gone out (isMonitoring) and, with config, CONFIG was applied.
// openPortAndStart waits 4 s, then up to 3 s per PING phase, then GET_CONFIG.
async function ncHandshake(target, { config = true, timeout = 45_000 } = {}) {
  await target.waitForFunction((config) => typeof isMonitoring !== 'undefined' && isMonitoring === true && !!port
                                           && (!config || _configLoaded === true), config, { timeout });
  return ncState(target);
}

// The NaviCore tool's visible toasts (#toast-host).
function ncToasts(target) {
  return target.evaluate(() => [...(document.getElementById('toast-host')?.children || [])].map((t) => t.textContent));
}

module.exports = {
  hil, hostGet, boardGuard, flashGuard, followFlash, summarise, watchLink, NC_READ_ONLY, unaskedWrites, pressF5,
  expectClean, openWizard, waitPulled, meshSettled, liveSlots, setField, pushConfig, spyRemotePulls, pullResult,
  recordToasts, spyBoardPulls, ncState, ncHandshake, ncToasts,
};
