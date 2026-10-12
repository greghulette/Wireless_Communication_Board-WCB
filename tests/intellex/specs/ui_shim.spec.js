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
