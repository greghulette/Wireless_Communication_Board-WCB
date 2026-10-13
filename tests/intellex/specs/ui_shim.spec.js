// The shim's own machinery in the NaviCore config tool, with nothing attached (the IX re-scan of 2026-10-12, rows
// shim.line_framing and shim.start_native_flash). Nothing reaches a port, a droid or esptool: the host is leashed, and
// the flash routes are fulfilled here.
//
// intellex.shim_line_framing: Intellex src/intellex_shim.js:152-221. The fake port's writable sends ONE WebSocket
// message per line - split on \r, \n or \r\n (a CRLF inside one chunk counts once), terminator kept, an unterminated
// tail buffered across write() calls through one streaming TextDecoder, the tail flushed by close(). The frames the page
// puts on /_link are read off Playwright's websocket events; the host answers each with an ERROR line (nothing attached),
// which is not looked at.
//
// intellex.shim_native_flash: intellex_shim.js:670-738 startNativeFlash, reached through the shim's capture-phase click
// handler on the tool's own buttons (wireNativeFlash, :574-607). Full Wipe asks first and a cancel sends nothing; the
// flash, OTA and connect buttons are locked while /_api/flash-status says running; new log lines are appended once
// each; the run ends 'complete ✓ — board is running <v>' or '<label> failed: <error>'; a 404/405 from /_api/flash is a
// host older than the page (STALE_HOST_MSG).
const { test, expect, skipUnlessHost, hostGuard } = require('../lib/fixtures');

skipUnlessHost(test);

// Every /_link socket the page opens, with the text of each frame it sent, in order.
function linkFrames(page) {
  const sockets = [];
  page.on('websocket', (ws) => {
    let path = '';
    try { path = new URL(ws.url()).pathname; } catch (_) { return; }
    if (path !== '/_link') return;
    const s = { sent: [] };
    sockets.push(s);
    ws.on('framesent', (f) => s.sent.push(typeof f.payload === 'string' ? f.payload : Buffer.from(f.payload).toString('utf8')));
  });
  return sockets;
}

test('intellex.shim_line_framing the shim sends one WebSocket message per line', async ({ page, rec }) => {
  const sockets = linkFrames(page);
  await page.goto('/', { waitUntil: 'load' });
  await expect.poll(() => rec.intellex.some((l) => l.includes('host has no transport attached')), { timeout: 15_000 })
    .toBe(true);
  // A 1500-byte line in the tool's 512-byte write chunks (sendLine's USB_CHUNK), with an 'é' whose two UTF-8 bytes
  // straddle the first cut: bytes 511 and 512. The ~1391-byte OTA DATA line is the case this framing exists for.
  const MARK = 'HILFRAME-' + Date.now().toString(36);
  const head = 'LONG,' + MARK + ',';
  const long = head + 'a'.repeat(511 - head.length) + 'é' + 'b'.repeat(1500 - 513 - 1) + '\n';
  const want = [MARK + '\n', 'abc\r\n', 'one\r', 'two\n', 'three\r\n', 'no-end-yet,then-the-end\r', long, 'tail'];
  const got = await page.evaluate(async ({ MARK, long }) => {
    const enc = new TextEncoder();
    const port = await navigator.serial.requestPort();
    await port.open({ baudRate: 115200 });
    const w = port.writable.getWriter();
    const out = { cuts: [] };
    await w.write(enc.encode(MARK + '\n'));
    await w.write(enc.encode('abc\r\n'));                          // CRLF in one chunk: one terminator
    await w.write(enc.encode('one\rtwo\nthree\r\n'));              // CR, LF and CRLF lines in one chunk
    await w.write(enc.encode('no-end-yet,'));                      // a tail with no terminator...
    await w.write(enc.encode('then-the-end\r'));                   // ...held until its terminator arrives
    const bytes = enc.encode(long);
    out.longBytes = bytes.length;
    out.splitAt = [bytes[511], bytes[512]];
    for (let i = 0; i < bytes.length; i += 512) {
      out.cuts.push(Math.min(512, bytes.length - i));
      await w.write(bytes.slice(i, i + 512));
    }
    await w.write(enc.encode('tail'));                             // never terminated: close() must flush it
    await w.close();
    return out;
  }, { MARK, long });
  expect(got.longBytes, 'the long line in bytes').toBe(1500);
  expect(got.splitAt, "the first 512-byte cut splits the 'é' (0xC3 | 0xA9)").toEqual([0xC3, 0xA9]);
  expect(got.cuts, 'the chunks written').toEqual([512, 512, 476]);
  // The page's own /_link socket is the one that carried our marker; the frames from it on are ours.
  await expect.poll(() => {
    const s = sockets.find((x) => x.sent.includes(MARK + '\n'));
    return s ? s.sent.slice(s.sent.indexOf(MARK + '\n')).length : 0;
  }, { timeout: 5_000, message: 'the frames did not all arrive' }).toBeGreaterThanOrEqual(want.length);
  const s = sockets.find((x) => x.sent.includes(MARK + '\n'));
  const mine = s.sent.slice(s.sent.indexOf(MARK + '\n'));
  const show = (a) => a.map((x) => (x.length > 40 ? `${JSON.stringify(x.slice(0, 20))}…(${x.length})` : JSON.stringify(x)));
  expect(show(mine), 'one frame per line, terminators kept').toEqual(show(want));
  expect(mine[6] === long, 'the 1500-byte line arrived as ONE frame equal to the line, the split é intact').toBe(true);
  expect(rec.errors, 'page errors').toEqual([]);
});

const STALE = 'This page is newer than the running Intellex host — quit and reopen Intellex to enable flashing.';
const LOCKED = ['btn-fw-flash', 'btn-fw-wipe', 'btn-fw-ota', 'btn-fw-ota-wcb', 'btn-connect'];
const IDLE = { running: false, ok: null, error: '', version: '', percent: 0, log: [] };

test('intellex.shim_native_flash the shim runs the config tool\'s flash buttons through the host', async ({ page, context, rec }) => {
  test.setTimeout(120_000);
  // /_api/flash answered per case; /_api/flash-status follows `flow`, a list of replies served in turn once a flash
  // has been POSTed (the last one repeats), IDLE before.
  let flashReply = { status: 200, json: { ok: true, started: true, target: 'serial COMFAKE', eraseNvs: false } };
  let flow = null;
  let step = 0;
  const calls = await hostGuard(context, rec, {
    flash: (route) => {
      step = 0;
      return route.fulfill({ status: flashReply.status, json: flashReply.json });
    },
  });
  await context.route(/\/_api\/flash-status(\?|$)/, (route) => {
    const posted = calls.some((c) => c.name === 'flash');
    const reply = posted && flow ? flow[Math.min(step++, flow.length - 1)] : IDLE;
    return route.fulfill({ json: reply });
  });
  const dialogs = [];
  let answer = 'dismiss';
  page.on('dialog', async (d) => {
    dialogs.push(d.message());
    await (answer === 'accept' ? d.accept() : d.dismiss());
  });
  const flashCalls = () => calls.filter((c) => c.name === 'flash').map((c) => c.body);
  const status = () => page.locator('#fw-status').textContent();
  const buttons = () => page.evaluate((ids) => Object.fromEntries(ids.map((i) => {
    const b = document.getElementById(i);
    return [i, b ? b.disabled : 'absent'];
  })), LOCKED);
  const ready = async () => {
    await expect.poll(() => page.evaluate(() => ['btn-fw-flash', 'btn-fw-wipe'].every((i) => {
      const b = document.getElementById(i);
      return b && b.__intellexWired && !b.disabled;
    })), { timeout: 15_000, message: 'the flash buttons are not wired to the host and enabled' }).toBe(true);
  };
  const click = (id) => page.locator('#' + id).dispatchEvent('click');

  await page.goto('/', { waitUntil: 'load' });
  await ready();

  // ── Full Wipe, cancelled: the confirm is the only thing that happens ──────────────────────────────────────────────
  const before = await status();
  answer = 'dismiss';
  await click('btn-fw-wipe');
  await expect.poll(() => dialogs.length, { timeout: 5_000, message: 'Full Wipe asked nothing' }).toBe(1);
  expect(dialogs[0], 'the Full Wipe confirm').toContain('ERASE all saved settings on the board (NVS)');
  await page.waitForTimeout(1_500);
  expect(flashCalls(), 'a cancelled Full Wipe POSTed /_api/flash').toEqual([]);
  expect(await status(), 'a cancelled Full Wipe changed the status line').toBe(before);
  expect((await buttons())['btn-fw-wipe'], 'a cancelled Full Wipe left its button locked').toBe(false);

  // ── Full Wipe, accepted, and failing: locked while it runs, each log line once, the error in the verdict ─────────
  flow = [
    { running: true, ok: null, error: '', version: '', percent: 40, log: ['HIL line 1', 'HIL line 2'] },
    { running: true, ok: null, error: '', version: '', percent: 70, log: ['HIL line 1', 'HIL line 2', 'HIL line 3'] },
  ];
  answer = 'accept';
  await click('btn-fw-wipe');
  await expect.poll(flashCalls, { timeout: 5_000 }).toEqual([{ eraseNvs: true }]);
  expect(dialogs.length, 'the accepted Full Wipe asked once').toBe(2);
  await expect(page.locator('#fw-status')).toHaveText('Full Wipe & Flash: 70%', { timeout: 10_000 });
  const locked = await buttons();
  for (const id of LOCKED) {
    if (locked[id] !== 'absent') expect(locked[id], `${id} while the flash runs`).toBe(true);
  }
  expect(Object.values(locked).filter((v) => v !== 'absent').length, 'locked buttons found').toBeGreaterThanOrEqual(4);
  const progress = await page.evaluate(() => ({
    log: document.getElementById('fw-log').textContent,
    bar: document.getElementById('fw-progress-bar').style.width,
    pct: document.getElementById('fw-progress-pct').textContent,
    wrap: document.getElementById('fw-progress-wrap').style.display,
  }));
  expect(progress, 'the progress while running').toEqual({ log: 'HIL line 1\nHIL line 2\nHIL line 3\n', bar: '70%',
    pct: '70%', wrap: 'block' });
  flow = [{ running: false, ok: false, error: 'E', version: '', percent: 70,
            log: ['HIL line 1', 'HIL line 2', 'HIL line 3', 'FAILED: E'] }];
  step = 0;
  await expect(page.locator('#fw-status')).toHaveText('Full Wipe & Flash failed: E', { timeout: 10_000 });
  expect(await page.locator('#fw-log').textContent(), 'the log after the failure')
    .toBe('HIL line 1\nHIL line 2\nHIL line 3\nFAILED: E\n');
  const after = await buttons();
  expect([after['btn-fw-flash'], after['btn-fw-wipe']], 'the flash buttons after the run').toEqual([false, false]);

  // ── Update Firmware, succeeding: no confirm, eraseNvs false, the version in the verdict, a fresh log ─────────────
  await ready();
  flow = [{ running: true, ok: null, error: '', version: '', percent: 50, log: ['HIL update 1'] },
          { running: false, ok: true, error: '', version: 'v9.9.9_hil', percent: 100,
            log: ['HIL update 1', 'Done - board is running v9.9.9_hil'] }];
  await click('btn-fw-flash');
  await expect.poll(flashCalls, { timeout: 5_000 }).toEqual([{ eraseNvs: true }, { eraseNvs: false }]);
  await expect(page.locator('#fw-status')).toHaveText('Update Firmware complete ✓ — board is running v9.9.9_hil',
    { timeout: 10_000 });
  expect(dialogs.length, 'Update Firmware asked for confirmation').toBe(2);
  expect(await page.locator('#fw-log').textContent(), 'the log of the second run (the first one\'s cleared)')
    .toBe('HIL update 1\nDone - board is running v9.9.9_hil\n');

  // ── The host refuses: its own words; then a host too old to have the route (405, 404): STALE_HOST_MSG ───────────
  for (const [code, json, want] of [
    [409, { ok: false, error: 'a flash is already running' }, 'Update Firmware failed: a flash is already running'],
    [405, { message: 'Method Not Allowed' }, `Update Firmware failed: ${STALE}`],
    [404, { message: 'Not Found' }, `Update Firmware failed: ${STALE}`],
  ]) {
    await ready();
    flashReply = { status: code, json };
    flow = null;
    const n = flashCalls().length;
    await click('btn-fw-flash');
    await expect.poll(() => flashCalls().length, { timeout: 5_000 }).toBe(n + 1);
    await expect(page.locator('#fw-status'), `/_api/flash answering ${code}`).toHaveText(want, { timeout: 5_000 });
  }
  expect(rec.errors, 'page errors').toEqual([]);
});

// ── IX re-scan shim.native_flash_buttons (intellex_shim.js:514-607): a host too old to have /_api/flash-status ───────
const NATIVE_FLASH = "Update via the host's native esptool. Saved configuration (NVS) is preserved.";
const NATIVE_WIPE = "Full wipe via the host's native esptool. ERASES saved configuration (NVS).";

test('intellex.shim_stale_host_buttons a host without /_api/flash-status disables the flash buttons', async ({ page, context, rec }) => {
  test.setTimeout(90_000);
  const calls = await hostGuard(context, rec);
  let stale = true;
  await context.route(/\/_api\/flash-status(\?|$)/, (route) => {
    if (!stale) return route.fulfill({ json: IDLE });
    rec.fulfilled.add(route.request().url());
    return route.fulfill({ status: 404, contentType: 'text/plain', body: '404: Not Found' });
  });
  const btns = () => page.evaluate(() => ['btn-fw-flash', 'btn-fw-wipe'].map((i) => {
    const b = document.getElementById(i);
    return b ? { disabled: b.disabled, title: b.title, wired: !!b.__intellexWired } : null;
  }));
  const STALE_BTN = { disabled: true, title: STALE, wired: false };
  await page.goto('/', { waitUntil: 'load' });
  await expect.poll(btns, { timeout: 10_000, message: 'the flash buttons on a stale host' })
    .toEqual([STALE_BTN, STALE_BTN]);
  // The tool rewrites these on every transport change, so the shim re-asserts every 3 s: still stale after two.
  await page.waitForTimeout(6_500);
  expect(await btns(), 'after two re-asserts').toEqual([STALE_BTN, STALE_BTN]);
  expect(calls.filter((c) => c.name === 'flash'), 'a stale host was asked to flash').toEqual([]);
  // The host answers again (restarted): the next re-assert wires them with the native titles.
  stale = false;
  await expect.poll(btns, { timeout: 8_000, message: 'the buttons once the host answers /_api/flash-status' })
    .toEqual([{ disabled: false, title: NATIVE_FLASH, wired: true }, { disabled: false, title: NATIVE_WIPE, wired: true }]);
  // Only a positive answer is kept: a later miss does not disable buttons the host has shown it can drive.
  stale = true;
  await page.waitForTimeout(4_000);
  expect((await btns()).map((b) => b.disabled), 'one failed probe after a good one').toEqual([false, false]);
  expect(rec.errors, 'page errors').toEqual([]);
});

// ── IX re-scan shim.wizard_port_sharing_off (intellex_shim.js:784-794, :1625-1641) ──────────────────────────────────
test('intellex.shim_wizard_no_share the Wizard\'s establishConnection always gets allowShare false', async ({ page, rec, guarded }) => {
  // Before any page script: a DOMContentLoaded listener on document, registered ahead of the shim's, puts a recorder in
  // place of the Wizard's establishConnection, so the shim wraps the recorder; one on window (bubbling, after the
  // shim's on document) sees whether the wrap is already there at DOMContentLoaded.
  await page.addInitScript(() => {
    window.__hilShare = [];
    document.addEventListener('DOMContentLoaded', () => {
      window.__hilOrigType = typeof window.establishConnection;
      window.establishConnection = function (n, port, used, allowShare) {
        window.__hilShare.push({ n, port: port && port.tag, used: used && used.tag, allowShare, args: arguments.length,
                                 self: this === window });
        return 'hil-result';
      };
    }, { once: true });
    window.addEventListener('DOMContentLoaded', () => {
      const f = window.establishConnection;
      window.__hilWrappedAtDCL = !!(f && f.__intellexNoShare);
    }, { once: true });
  });
  await page.goto('/wcb/Wizard/', { waitUntil: 'load' });
  await expect.poll(() => rec.intellex.some((l) => l.includes('cross-tab port sharing disabled')), { timeout: 15_000 })
    .toBe(true);
  await page.waitForTimeout(1_000);              // past load+400 ms, where the shim wraps again as a backstop
  const got = await page.evaluate(() => {
    window.__hilShare.length = 0;
    const f = window.establishConnection;
    const out = [f.call(window, 3, { tag: 'P' }, { tag: 'U' }, true), f(4, { tag: 'Q' }, { tag: 'V' })];
    return { out, seen: window.__hilShare, marked: f.__intellexNoShare === true, origType: window.__hilOrigType,
             atDCL: window.__hilWrappedAtDCL };
  });
  expect(got.origType, 'the Wizard defines establishConnection by DOMContentLoaded').toBe('function');
  expect(got.atDCL, 'establishConnection was not wrapped yet at DOMContentLoaded').toBe(true);
  expect(got.marked, 'window.establishConnection is the shim\'s wrapper').toBe(true);
  expect(got.out, 'the wrapper returns what the original returns').toEqual(['hil-result', 'hil-result']);
  expect(got.seen, 'what the original was called with (once per call: wrapped once, not twice)').toEqual([
    { n: 3, port: 'P', used: 'U', allowShare: false, args: 4, self: true },
    { n: 4, port: 'Q', used: 'V', allowShare: false, args: 4, self: true },
  ]);
  expect(rec.errors, 'page errors').toEqual([]);
});

// ── IX re-scan shim.wizard_flash_replacement (intellex_shim.js:1327-1417) ────────────────────────────────────────────
const NOT_USB = 'Flashing needs a direct USB connection. This session is over WiFi, which has no DTR/RTS lines to enter '
              + 'the bootloader. Attach the board over USB, or use OTA.';

test('intellex.shim_wizard_flash_refusals the Wizard\'s flashFirmware refuses and fails the way boardGo needs', async ({ page, context, rec }) => {
  test.setTimeout(150_000);
  let kind = '';                                          // '' = the host's own /_api/status (nothing attached)
  await context.route(/\/_api\/status(\?|$)/, (route) => (kind ? route.fulfill({ json: { attached: false, target: '',
    lastError: '', reconnecting: false, wantsLink: true, kind, role: '', relayId: null } }) : route.continue()));
  let flashStatus = 'stale';
  let flow = [];
  let step = 0;
  await context.route(/\/_api\/flash-status(\?|$)/, (route) => {
    if (flashStatus === 'stale') {
      rec.fulfilled.add(route.request().url());
      return route.fulfill({ status: 404, contentType: 'text/plain', body: '404: Not Found' });
    }
    return route.fulfill({ json: flow.length ? flow[Math.min(step++, flow.length - 1)] : IDLE });
  });
  let wcbReply = { status: 200, json: { ok: true, started: true, target: 'serial COMFAKE' } };
  const calls = await hostGuard(context, rec, {
    'flash-wcb': (route) => { step = 0; return route.fulfill({ status: wcbReply.status, json: wcbReply.json }); },
  });
  const wcbCalls = () => calls.filter((c) => c.name === 'flash-wcb').map((c) => c.body);
  await page.goto('/wcb/Wizard/', { waitUntil: 'load' });
  await expect.poll(() => rec.intellex.some((l) => l.includes('flashFirmware() now runs on the host')),
    { timeout: 15_000 }).toBe(true);
  // flashFirmware as boardGo calls it, every callback recorded.
  const run = (opts) => page.evaluate(async (o) => {
    const ev = { log: [], status: [], progress: [] };
    try {
      await window.flashFirmware({ fake: true }, 'HIL-HW', Object.assign({}, o, {
        onLog: (m) => ev.log.push(m), onStatus: (m) => ev.status.push(m), onProgress: (a, b) => ev.progress.push([a, b]),
      }));
      ev.result = 'resolved';
    } catch (e) {
      ev.result = 'rejected';
      ev.error = e && e.message;
    }
    return ev;
  }, opts);
  const NOTHING = { log: [], status: [], progress: [] };

  // ── a host older than the page (no /_api/flash-status): refused before anything is POSTed or reported ──────────
  expect(await run({ appOnly: true }), 'a stale host').toEqual({ ...NOTHING, result: 'rejected', error: STALE });
  // ── a WiFi link: refused with the USB message, before anything is POSTed or reported ─────────────────────────────
  flashStatus = 'ok';
  kind = 'ws';
  expect(await run({ appOnly: true }), 'a WiFi link').toEqual({ ...NOTHING, result: 'rejected', error: NOT_USB });
  expect(wcbCalls(), 'flash-wcb POSTed although refused').toEqual([]);

  // ── USB: the host runs it and fails - the error is THROWN (boardGo's only failure signal), the log fed once ──────
  kind = 'serial';
  flow = [
    { running: true, ok: null, error: '', version: '', percent: 40, log: ['HIL w1'] },
    { running: true, ok: null, error: '', version: '', percent: 70, log: ['HIL w1', 'HIL w2'] },
    { running: false, ok: false, error: 'E', version: '', percent: 70, log: ['HIL w1', 'HIL w2', 'FAILED: E'] },
  ];
  let ev = await run({ eraseNvs: true });
  expect(wcbCalls(), 'Factory Reset POSTs eraseNvs').toEqual([{ appOnly: false, eraseNvs: true }]);
  expect(ev.result + ': ' + ev.error, 'a failed host flash').toBe('rejected: E');
  expect(ev.log, 'the flash log fed to the Wizard').toEqual(['Intellex: flashing through the host (esptool), not the browser.',
    'Mode: Factory Reset — NVS will be erased.', 'HIL w1', 'HIL w2', 'FAILED: E']);
  expect(ev.status, 'the status fed to the Wizard').toEqual(['Starting native flash…', 'Flashing… 40%', 'Flashing… 70%']);
  expect(ev.progress, 'the progress fed to the Wizard (percent of 100)').toEqual([[40, 100], [70, 100], [70, 100]]);
  flow = [{ running: false, ok: false, error: '', version: '', percent: 0, log: [] }];
  ev = await run({ appOnly: true });
  expect(ev.result + ': ' + ev.error, 'a failure with no words').toBe('rejected: flash failed — see the log');

  // ── the host refuses the start: its words; a route it does not have (404/405): the stale-host message ───────────
  for (const [status, json, want] of [[409, { ok: false, error: 'a flash is already running' }, 'a flash is already running'],
                                      [500, {}, 'the host refused (HTTP 500)'],
                                      [405, { message: 'Method Not Allowed' }, STALE], [404, { message: 'Not Found' }, STALE]]) {
    wcbReply = { status, json };
    ev = await run({ appOnly: true });
    expect(ev.result + ': ' + ev.error, `flash-wcb answering ${status}`).toBe('rejected: ' + want);
  }

  // ── boardGo's Update FW with a fake board connection: the failure becomes 'Flash failed', no config is pushed ─────
  wcbReply = { status: 200, json: { ok: true, started: true } };
  flow = [{ running: false, ok: false, error: 'E', version: '', percent: 10, log: ['FAILED: E'] }];
  const goUpdate = () => page.evaluate(async () => {
    const seen = [];
    const fake = {
      _shared: false, _hub: null, port: { close: async () => seen.push('port.close') },
      isConnected: () => true,
      closeForReconnect: async () => { seen.push('closeForReconnect'); },
      reconnect: async () => { seen.push('reconnect'); return true; },
      send: () => seen.push('send'), sendAndCollect: async () => { seen.push('sendAndCollect'); return ''; },
    };
    /* global boardConnections, boardGo */
    const keep = boardConnections[1];
    boardConnections[1] = fake;
    const before = document.querySelectorAll('#toast-container .toast.error').length;
    try { await boardGo(1, { mode: 'update' }); } catch (e) { seen.push('threw: ' + (e && e.message)); }
    const toasts = [...document.querySelectorAll('#toast-container .toast.error .toast-msg')].slice(before)
      .map((t) => t.textContent);
    boardConnections[1] = keep;
    return { seen, toasts };
  });
  let go = await goUpdate();
  expect(go.toasts, 'boardGo on a failed host flash').toEqual(['Flash failed: E']);
  expect(go.seen.filter((s) => s.startsWith('send') || s.startsWith('threw')), 'boardGo pushed config (or threw) after '
    + 'a failed flash').toEqual([]);
  // Over WiFi. PINNED AS IT IS TODAY: boardGo closes the board's connection (closeForReconnect) BEFORE it calls
  // flashFirmware, so the shim's refusal - whose comment says it comes "BEFORE boardGo() tears the board's connection
  // down" - comes after the teardown; boardGo's failure path then reconnects. Nothing is POSTed.
  kind = 'ws';
  const n0 = wcbCalls().length;
  go = await goUpdate();
  expect(go.toasts, 'boardGo over WiFi').toEqual(['Flash failed: ' + NOT_USB]);
  expect(wcbCalls().length, 'flash-wcb POSTed over WiFi').toBe(n0);
  expect(go.seen, 'boardGo over WiFi: the teardown before the refusal, then the reconnect (pinned)')
    .toEqual(['closeForReconnect', 'reconnect']);
  expect(rec.errors, 'page errors').toEqual([]);
});

// ── IX re-scan shim.close_intent (intellex_shim.js:48-54, :224-246, :1595-1605) ──────────────────────────────────────
// The page's /_link is routed to a stand-in NaviCore here (page.routeWebSocket): it answers the tool's ?REC and PING
// while `answering`, and the spec can drop it. /_api/status says a serial link is attached throughout. Nothing reaches a
// port or a board.
test('intellex.shim_close_intent a Disconnect is honoured, a teardown after a loss is not', async ({ page, context, rec }) => {
  test.setTimeout(180_000);
  await hostGuard(context, rec);
  await context.route(/\/_api\/status(\?|$)/, (r) => r.fulfill({ json: { attached: true, target: 'serial COMFAKE',
    lastError: '', reconnecting: false, wantsLink: true, kind: 'serial', role: 'navicore', relayId: null } }));
  const link = { sockets: [], opened: 0, answering: true };
  await page.routeWebSocket(/\/_link$/, (ws) => {
    link.opened += 1;
    link.sockets.push(ws);
    ws.onMessage((msg) => {
      const t = typeof msg === 'string' ? msg : Buffer.from(msg).toString('utf8');
      if (!link.answering) return;
      if (t.startsWith('?REC')) ws.send('[REC] state=idle\n');
      else if (t.includes('"PING"')) ws.send('{"type":"PONG","version":"v0.2.0_hilclose"}\n');
    });
  });
  const reconnecting = () => rec.intellex.filter((l) => l.includes('host is attached but the page is not')).length;
  /* global isMonitoring */
  const monitoring = () => page.evaluate(() => (typeof isMonitoring === 'undefined' ? null : isMonitoring));
  const connectBtn = () => page.locator('#btn-connect').textContent();

  await page.goto('/', { waitUntil: 'load' });
  await expect.poll(monitoring, { timeout: 30_000, message: 'the tool did not connect through the stand-in' }).toBe(true);
  expect(await connectBtn()).toBe('Disconnect');

  // ── the user's Disconnect, with no loss before it: honoured - nothing reconnects ──────────────────────────────────
  const opened0 = link.opened;
  const lines0 = reconnecting();
  await page.click('#btn-connect');
  await expect.poll(connectBtn, { timeout: 10_000 }).toBe('Connect');
  await page.waitForTimeout(8_000);               // four of watchLink's 2 s ticks, past its 5 s backoff
  expect(await monitoring(), 'isMonitoring after Disconnect').toBe(false);
  expect(reconnecting(), 'the shim reconnected after the user\'s Disconnect').toBe(lines0);
  expect(link.opened, '/_link sockets opened after the Disconnect').toBe(opened0);
  expect(await connectBtn()).toBe('Connect');

  // ── a loss: the far end drops, the tool's one-shot reconnect finds nobody and closes the port again - within 60 s
  //    of the loss that close is teardown, so the shim keeps trying, and gets the page back ─────────────────────────
  await page.reload({ waitUntil: 'load' });
  await expect.poll(monitoring, { timeout: 30_000, message: 'the reloaded tool did not connect' }).toBe(true);
  const lines1 = reconnecting();
  const opened1 = link.opened;
  link.answering = false;
  for (const ws of link.sockets) ws.close();
  await expect.poll(reconnecting, { timeout: 60_000, message: 'the shim never tried to get the page back after a '
    + 'loss: a close within LOSS_GRACE_MS of it was taken for the user\'s Disconnect' }).toBeGreaterThan(lines1);
  link.answering = true;
  await expect.poll(monitoring, { timeout: 45_000, message: 'the page did not come back' }).toBe(true);
  expect(link.opened, '/_link sockets after the loss (the tool\'s own try, then the shim\'s)')
    .toBeGreaterThanOrEqual(opened1 + 2);
  expect(rec.errors, 'page errors').toEqual([]);
});
