// Needs no board: what the Wizard (Wizard/app.js) does around a flash and an OTA - the General fields a post-flash push
// restores, a flash that failed, the Factory Reset modal, the OTA over USB and through a relay, and the ETM edges an
// OTA held back. Medium-risk gaps the 2026-10-12 coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in
// docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each test's comment). Fake boards in the real page
// (lib/fake.js); window.flashFirmware, getBinaryData, waitForBoardReady and the remote pull are recorders where a spec
// stops at that seam (the flasher itself is specs/flasher_*fake.spec.js, the wait specs/connect_rescan_fake.spec.js).
// Runs standalone, in CI, and under the harness by id (s30_wizard.py).
//
// Nothing here is a real credential: the mesh passwords are dummies.
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { EPASS, board, chain, install, pullFake, push, relayFake } = require('../lib/fake');
const { setup, holdVerifyPulls, toastMark, toastsSince, sent } = require('../lib/rescan');

test('wizard.push_fake_post_flash_general the push that follows a flash or erase puts the General fields back as they were before it - password, MAC octets, quantity, characters - and sends those, once; the next push reads General as it is', async ({ page }) => {
  // boardGo's restore (app.js:8165-8168), captureGeneralDOMSnapshot / restoreGeneralDOMSnapshot (9313-9346).
  // Row push.post_flash_general_restore.
  await setup(page);
  await pullFake(page, 1, board());
  await holdVerifyPulls(page);
  await page.evaluate(() => {
    const snap = captureGeneralDOMSnapshot();          // as boardGo takes it before a flash
    Object.assign(snap, { password: 'dummy_snapshot_password', mac3: '4C', wcbq: '3' });
    postFlashGeneralSnapshot[1] = snap;
    document.getElementById('g-password').value = 'dummy_drifted_password';   // a pull during the flash moved it
  });
  expect(await sent(page, 1)).toEqual(['1:?WCBQ,3', '1:?MAC,3,4C', '1:?EPASS,dummy_snapshot_password']);
  expect(await page.evaluate(() => [document.getElementById('g-password').value, document.getElementById('g-mac3').value,
                                    document.getElementById('g-wcbq').value, 1 in postFlashGeneralSnapshot,
                                    systemConfig.general.espnowPassword, boardConfigs[1].espnowPassword]))
    .toEqual(['dummy_snapshot_password', '4C', '3', false, 'dummy_snapshot_password', 'dummy_snapshot_password']);
  // Used once: the next push takes General as it is now.
  await page.evaluate(() => { document.getElementById('g-password').value = 'dummy_typed_password'; });
  expect(await sent(page, 1)).toEqual(['1:?WCBQ,3', '1:?MAC,3,4C', '1:?EPASS,dummy_typed_password']);
  expect(page.wizErrors).toEqual([]);
});

// Every boardGo call from here on, with its mode and whether the board's baseline was null (a full push).
const recordGo = (page) => page.evaluate(() => {
  window.__realBoardGo = window.__realBoardGo || boardGo;
  window.__goCalls = [];
  window.boardGo = (n, opts = {}) => { __goCalls.push({ n, mode: opts.mode ?? null, baselineNull: boardBaselines[n] === null }); return window.__realBoardGo(n, opts); };
});

test('wizard.flash_fake_failure_recovery a flash that failed reopens the same port; in the setup wizard a board already on the target firmware is pushed its wizard config whole with the pre-flash General, one on another version is told to retry; on Windows the port is closed (retried) before the reopen', async ({ page }) => {
  // boardGo's failed-flash branch (app.js:7981-8052). The same-port reopen and the factory "incomplete" toast are
  // wizard.flash_fake_entry_snapshot's. Row flash.failure_recovery.
  const run = async ({ boardVer, wizard = true, win = false } = {}) => {
    await openWizard(page);
    await install(page);
    await pullFake(page, 1, board(['LABEL,S1,Keep']));
    await recordGo(page);
    await page.evaluate(({ boardVer, wizard }) => {
      const c = boardConnections[1];
      const log = (op, s = '') => __fake.log.push({ slot: 1, op, s, at: Date.now() });
      c.port = { closes: 0, async close() { log('portclose'); if (++this.closes <= 2) throw new Error('busy'); }, getInfo: () => ({}) };
      c.reconnect = async (...a) => { log('reconnect', JSON.stringify(a)); c._connected = true; return true; };
      window.waitForBoardReady = async (n) => { log('ready'); return true; };
      window.flashFirmware = async () => {
        log('flash');
        document.getElementById('g-password').value = 'dummy_drifted_password';   // a pull landed meanwhile
        throw new Error('Bootloader connection failed (hil)');
      };
      latestFirmwareVersion = 'v6.2.3';
      boardConfigs[1].fwVersion = boardVer;
      _wizardOpen = wizard;
      updateConnectionUI(1, true);
      __fake.log.length = 0;
      __goCalls.length = 0;
    }, { boardVer, wizard });
    const m = await toastMark(page);
    await page.evaluate(() => boardGo(1, { mode: 'flash' }));
    await page.waitForTimeout(300);
    return { log: await page.evaluate(() => __fake.log.filter((e) => e.slot === 1).map((e) => ({ op: e.op, s: e.s, at: e.at }))),
             go: await page.evaluate(() => __goCalls.map(({ mode, baselineNull }) => ({ mode, baselineNull }))),
             toasts: await toastsSince(page, m) };
  };

  // Already on the target version: the wizard's config pushed whole, General as it was before the flash.
  let r = await run({ boardVer: '6.2.3' });
  expect(r.toasts).toContain('WCB 1: already on firmware 6.2.3 — pushing config…');
  expect(r.go).toEqual([{ mode: 'flash', baselineNull: false }, { mode: 'configure', baselineNull: true }]);
  const ops = r.log.map((e) => e.op);
  expect(ops.slice(0, 4)).toEqual(['close', 'flash', 'reconnect', 'ready']);
  expect(r.log.find((e) => e.op === 'reconnect').s).toBe('[5,1500,{"skipFreshPortSearch":true}]');
  const pushed = r.log.filter((e) => e.op === 'await').map((e) => e.s);
  expect(pushed).toEqual(expect.arrayContaining(['?WCB,1', '?HW,24', '?LABEL,S1,Keep', `?EPASS,${EPASS}`]));
  expect(pushed.filter((s) => s.includes('drifted'))).toEqual([]);
  expect(ops.filter((o) => o === 'portclose'), 'off Windows nothing is closed before the reopen').toEqual([]);

  // On another version: told to retry, nothing pushed, no wait.
  r = await run({ boardVer: '6.1.0' });
  expect(r.toasts).toContain('WCB 1: needs firmware 6.2.3 — retry flash');
  expect(r.go).toEqual([{ mode: 'flash', baselineNull: false }]);
  expect(r.log.filter((e) => ['await', 'ready'].includes(e.op))).toEqual([]);
  // Outside the setup wizard: reopened, and left there.
  r = await run({ boardVer: '6.2.3', wizard: false });
  expect(r.go).toEqual([{ mode: 'flash', baselineNull: false }]);
  expect(r.toasts).not.toContain('already on firmware');

  // Windows: 1.5 s after the failure the port is closed, retried until it goes (here the third try), then reopened.
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'userAgentData', { configurable: true, get: () => ({ platform: 'Windows' }) });
  });
  r = await run({ boardVer: '6.1.0', wizard: false });
  expect(await page.evaluate(() => _isWindows)).toBe(true);
  const flashAt = r.log.find((e) => e.op === 'flash').at;
  const closes = r.log.filter((e) => e.op === 'portclose');
  expect(r.log.map((e) => e.op).slice(0, 6)).toEqual(['close', 'flash', 'portclose', 'portclose', 'portclose', 'reconnect']);
  expect(closes[0].at - flashAt).toBeGreaterThanOrEqual(1450);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.flash_fake_factory_modal Factory Reset opens its modal for a board on USB - refused for a relayed board and for one not connected - and Erase NVS + Load Fresh Firmware runs boardGo in factory mode with the push-config box as ticked; Cancel runs nothing', async ({ page }) => {
  // boardFactoryReset, closeFactoryResetModal, doFactoryResetWithFw (app.js:10334-10361). Erase NVS Only is
  // wizard.flash_fake_erase_path's. Row flash.factory_modal.
  await setup(page);
  await pullFake(page, 1, board());
  await page.evaluate(() => {
    updateConnectionUI(1, true);
    window.__go = [];
    window.boardGo = (n, opts) => { __go.push({ n, ...opts }); };
  });
  const modalOpen = () => page.evaluate(() => [document.getElementById('factory-reset-modal').classList.contains('open'), _factoryResetSlot]);
  await page.locator('#b1-btn-erase').click();
  expect(await modalOpen()).toEqual([true, 1]);
  expect(await page.locator('#factory-reset-modal-title').textContent()).toBe('🏭 Factory Reset WCB 1');
  expect(await page.locator('#factory-reset-push-config').isChecked(), 'push-config starts unticked').toBe(false);
  await page.locator('#factory-reset-modal button.modal-option', { hasText: 'Erase NVS + Load Fresh Firmware' }).click();
  expect(await modalOpen()).toEqual([false, null]);
  await page.locator('#b1-btn-erase').click();
  await page.locator('#factory-reset-push-config').check();
  await page.locator('#factory-reset-modal button.modal-option', { hasText: 'Erase NVS + Load Fresh Firmware' }).click();
  await page.locator('#b1-btn-erase').click();
  await page.locator('#factory-reset-modal .modal-footer button', { hasText: 'Cancel' }).click();
  expect(await modalOpen()).toEqual([false, null]);
  expect(await page.evaluate(() => __go)).toEqual([{ n: 1, mode: 'factory', pushConfig: false }, { n: 1, mode: 'factory', pushConfig: true }]);

  // Not connected: refused, no modal.
  let m = await toastMark(page);
  await page.evaluate(() => { boardConnections[1]._connected = false; boardFactoryReset(1); });
  expect(await toastsSince(page, m)).toContain('Board not connected');
  expect(await modalOpen()).toEqual([false, null]);
  // Relayed: refused, no modal.
  await openWizard(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  m = await toastMark(page);
  await page.locator('#b2-btn-erase').click();
  expect(await toastsSince(page, m)).toContain('Connect via USB to factory reset — not available over wireless relay');
  expect(await modalOpen()).toEqual([false, null]);
  expect(page.wizErrors).toEqual([]);
});

// ── OTA ──────────────────────────────────────────────────────────────────────────────────────────────────────────

// A board answering ?OTALOCAL in slot 1 (lib/fake.js's fake, its sendAndCollect replaced): `script(sub, rest, b)` may
// return a reply to override the default for a command. Every command is in __ota.log as `SUB,args @baud`.
async function otaBoard(page, { bytes = 3000, shared = false, raise = false, script = null } = {}) {
  await page.evaluate(({ bytes, shared, raise, script }) => {
    window.getBinaryData = async () => ({ images: [{ address: 0x10000, buf: new Uint8Array(bytes).fill(0xa5).buffer }] });
    const user = script ? (0, eval)(`(${script})`) : null;
    const b = { baud: 115200, host: 115200, active: false, size: 0, written: 0, dataSeen: 0 };
    const log = [];
    window.__ota = { b, log };
    const c = __fake.conn(1, { shared });
    if (!shared) c.port = raise ? { reconfigure() {}, getInfo: () => ({ usbVendorId: 0x1a86, usbProductId: 0x55d4 }) } : { getInfo: () => ({}) };
    c.setBaud = async (r) => { b.host = r; log.push(`host ${r}`); };
    const send = c.send;
    c.send = async (s) => { log.push(`send ${String(s).trim()}`); return send(s); };
    c.sendAndCollect = async (s) => {
      const m = s.match(/OTALOCAL,(\w+)(?:,(.*))?$/);
      if (!m) return '';
      const [, sub, rest] = m;
      log.push(sub === 'DATA' ? `DATA,${rest.split(',')[0]} @${b.host}` : `${sub}${rest ? ',' + rest : ''} @${b.host}`);
      const own = user ? user(sub, rest, b) : undefined;
      if (own !== undefined) return own;
      if (sub === 'STATUS') return `Chip:        ESP32-D0WD (family 0)\r\nSession:     ${b.active ? 'ACTIVE id=1' : 'idle'}`;
      if (sub === 'BEGIN') { Object.assign(b, { active: true, size: parseInt(rest, 10), written: 0 }); return '[OTA:BEGIN,OK,0]'; }
      if (sub === 'BAUD') { const r = parseInt(rest, 10); setTimeout(() => { b.baud = r; }, 0); return `[OTA:BAUD,OK,${r}]`; }
      if (sub === 'DATA') {
        const [off, b64] = rest.split(',');
        b.dataSeen++;
        if (parseInt(off, 10) !== b.written) return `[OTA:NAK,${b.written}]`;
        b.written = parseInt(off, 10) + atob(b64).length;
        return `[OTA:ACK,${b.written}]`;
      }
      if (sub === 'END') return b.written === b.size ? '[OTA:END,OK]' : '[OTA:END,ERR]';
      return '';
    };
    window.__otaCalls = [];
    c.reconnect = async (...a) => { __otaCalls.push(`reconnect ${JSON.stringify(a)}`); c._connected = true; return true; };
    const close = c.closeForReconnect;
    c.closeForReconnect = async () => { __otaCalls.push('close'); return close(); };
    window.waitForBoardReady = async (n) => { __otaCalls.push(`ready ${n}`); return true; };
    window.boardPull = async (n) => { __otaCalls.push(`pull ${n}`); };
  }, { bytes, shared, raise, script: script ? script.toString() : null });
}
const otaRun = (page) => page.evaluate(async () => {
  const m = __fake.toasts.length;
  await boardOtaSerial(1);
  await new Promise((r) => setTimeout(r, 200));
  return { log: __ota.log, calls: __otaCalls, toasts: __fake.toasts.slice(m).map((t) => t.message).join('\n'), inProgress: _otaInProgress.has(1) };
});

test('wizard.ota_fake_serial_stream an OTA over USB follows the board\'s cursor - a NAK rewinds it and the chunk is sent again - verifies with END and then reconnects (direct) or waits on the shared port, and pulls; END failing, or the board\'s cursor collapsing to 0, sends ABORT and puts a raised baud back to 115200', async ({ page }) => {
  // boardOtaSerial (app.js:1786-2022). The raised-baud proof is wizard.app_fake_ota_baud_fallback's; the 60-stall cap
  // (minutes of back-off) is left out. Row ota.serial_stream.
  await setup(page);
  // The board misses the chunk at 2048 once: it NAKs with its own cursor (1024 already written... then 2048), and the
  // Wizard resends from there.
  await otaBoard(page, { script: (sub, rest, b) => {
    if (sub === 'DATA' && rest.startsWith('2048,') && !b.missed) { b.missed = true; return `[OTA:NAK,${b.written}]`; }
  } });
  let r = await otaRun(page);
  const offsets = r.log.filter((l) => l.startsWith('DATA,')).map((l) => +l.split(',')[1].split(' ')[0]);
  expect(offsets).toEqual([0, 1024, 2048, 2048]);
  expect(r.log.filter((l) => /^(STATUS|BEGIN|END)/.test(l))).toEqual(['STATUS @115200', 'BEGIN,3000,0 @115200', 'END @115200']);
  expect(r.calls).toEqual(['close', 'reconnect [12,2000]', 'ready 1', 'pull 1']);
  expect(r.toasts).toContain('WCB 1: OTA complete in');
  expect(r.inProgress).toBe(false);

  // A board that answers with an earlier cursor (a gap): the Wizard rewinds to it.
  await otaBoard(page, { bytes: 4000, script: (sub, rest, b) => {
    if (sub === 'DATA' && rest.startsWith('3072,') && !b.rewound) { b.rewound = true; b.written = 1024; return '[OTA:NAK,1024]'; }
  } });
  r = await otaRun(page);
  expect(r.log.filter((l) => l.startsWith('DATA,')).map((l) => +l.split(',')[1].split(' ')[0])).toEqual([0, 1024, 2048, 3072, 1024, 2048, 3072]);
  expect(r.toasts).not.toContain('OTA failed');

  // END fails after a raised baud: ABORT, then the host back at 115200; nothing reconnected or pulled.
  await otaBoard(page, { raise: true, script: (sub) => { if (sub === 'END') return '[OTA] SHA mismatch\r\n[OTA:END,ERR]'; } });
  r = await otaRun(page);
  expect(r.toasts).toContain('OTA failed: OTA verify/finalize failed — [OTA] SHA mismatch');
  const abort = r.log.indexOf('send ?OTALOCAL,ABORT');
  expect(abort).toBeGreaterThan(r.log.indexOf('END @921600'));
  expect(r.log.slice(abort + 1)).toEqual(['host 115200']);
  expect(r.calls).toEqual([]);

  // The cursor collapses to 0 after real progress (the board's session timed out): stopped at once, ABORT sent.
  await otaBoard(page, { script: (sub, rest, b) => { if (sub === 'DATA' && rest.startsWith('2048,')) { b.written = 0; return '[OTA:ACK,0]'; } } });
  r = await otaRun(page);
  expect(r.toasts).toContain('OTA failed: board lost the OTA session at 2048 bytes');
  expect(r.log.filter((l) => l.startsWith('DATA,')).length).toBe(3);
  expect(r.log).toContain('send ?OTALOCAL,ABORT');
  expect(r.log.some((l) => l.startsWith('END'))).toBe(false);

  // On the shared port: no close or reopen - the board reboots on the hub's live port - then the wait and the pull.
  await otaBoard(page, { shared: true });
  r = await otaRun(page);
  expect(r.toasts).toContain('WCB 1: OTA complete in');
  expect(r.calls).toEqual(['ready 1', 'pull 1']);
  expect(await page.evaluate(() => boardConnections[1]._rebootManaged)).toBe(false);
  expect(page.wizErrors).toEqual([]);
});

// A relay in slot 1 for WCB2 whose target answers ?OTA,<verb>,<target>,<session>,... with [OTA:ACK,...] lines; the
// spec's `script(verb, args, t)` may return its own reply. Commands are in __rota.log.
async function otaRelay(page, { bytes = 1000, script = null } = {}) {
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })), 3: chain(board([], { wcb: 3, wcbq: 4 })), 4: chain(board([], { wcb: 4, wcbq: 4 })) });
  await page.evaluate(({ bytes, script }) => {
    window.getBinaryData = async () => ({ images: [{ address: 0x10000, buf: new Uint8Array(bytes).fill(0x5a).buffer }] });
    const user = script ? (0, eval)(`(${script})`) : null;
    const t = { written: 0, total: 0 };
    const log = [];
    window.__rota = { t, log };
    const relay = boardConnections[1];
    const send = relay.send;
    relay.send = async (s) => { log.push(`send ${String(s).trim()}`); return send(s); };
    relay.sendAndCollect = async (s) => {
      const m = s.match(/^\?OTA,(\w+),(\d+),(\d+)(?:,(.*))?$/);
      if (!m) return '';
      const [, verb, target, session, rest] = m;
      log.push({ verb, target: +target, session: +session, rest: rest ?? '' });
      const own = user ? user(verb, rest ?? '', t) : undefined;
      const ack = (off, st = 0) => `[OTA:ACK,${target},${session},${off},${st}]`;
      if (own !== undefined) return own === null ? '' : ack(own.offset, own.status);
      if (verb === 'BEGIN') { t.total = parseInt(rest, 10); t.written = 0; return ack(0); }
      if (verb === 'DATA') {
        const [off, b64] = rest.split(',');
        if (parseInt(off, 10) === t.written) t.written += atob(b64).length;
        return ack(t.written);
      }
      if (verb === 'END') return ack(t.written, t.written === t.total ? 0 : 1);
      return '';
    };
    window.__pulls = [];
    window.remoteBoardPull = async (...a) => { __pulls.push(a); };
  }, { bytes, script: script ? script.toString() : null });
}

test('wizard.ota_fake_relay a wireless OTA through a relay sends ?OTA,BEGIN / DATA in 192-byte frames following the target\'s cursor / END, each tagged with the target and one session, then re-arms the relay link and pulls the board with 12 attempts 8 s later; a non-zero status or a cursor collapsing to 0 stops it at once with ABORT', async ({ page }) => {
  // boardOtaRelay and _otaRelayAwaitAck (app.js:2027-2196). Row ota.relay.
  await setup(page);
  await otaRelay(page);
  const t0 = await page.evaluate(() => Date.now());
  let r = await page.evaluate(async () => {
    const m = __fake.toasts.length;
    await boardOtaRelay(2);
    return { log: __rota.log, toasts: __fake.toasts.slice(m).map((t) => t.message).join('\n'), managed: remoteRelayForBoard[2], ota: _otaInProgress.size };
  });
  const frames = r.log.filter((e) => typeof e === 'object');
  const session = frames[0].session;
  expect(session).toBeGreaterThanOrEqual(1);
  expect(frames.every((f) => f.target === 2 && f.session === session)).toBe(true);
  expect(frames[0]).toEqual({ verb: 'BEGIN', target: 2, session, rest: '1000,0' });
  const data = frames.filter((f) => f.verb === 'DATA').map((f) => [+f.rest.split(',')[0], atob(f.rest.split(',')[1]).length]);
  expect(data).toEqual([[0, 192], [192, 192], [384, 192], [576, 192], [768, 192], [960, 40]]);
  expect(frames.at(-1)).toEqual({ verb: 'END', target: 2, session, rest: '' });
  expect(r.toasts).toContain('WCB 2: wireless OTA complete in');
  expect([r.managed, r.ota]).toEqual([1, 0]);
  await page.waitForFunction(() => __pulls.length > 0, null, { timeout: 12_000 });
  expect(await page.evaluate(() => __pulls)).toEqual([[1, 2, 1, 12]]);
  expect(Date.now() - t0).toBeGreaterThanOrEqual(7900);

  // The target reports an error status for a frame: stopped there, ABORT for that session.
  await openWizard(page);
  await install(page);
  await otaRelay(page, { script: (verb, rest) => (verb === 'DATA' && rest.startsWith('384,') ? { offset: 384, status: 1 } : undefined) });
  r = await page.evaluate(async () => {
    const m = __fake.toasts.length;
    await boardOtaRelay(2);
    return { log: __rota.log, toasts: __fake.toasts.slice(m).map((t) => t.message).join('\n') };
  });
  expect(r.toasts).toContain('Wireless OTA failed: target rejected OTA data near 384 bytes');
  const s2 = r.log.find((e) => typeof e === 'object').session;
  expect(r.log.filter((e) => typeof e === 'object' && e.verb === 'DATA').length).toBe(3);
  expect(r.log.at(-1)).toBe(`send ?OTA,ABORT,2,${s2}`);
  // The cursor collapses to 0 after progress: the session is gone, stopped at once.
  await openWizard(page);
  await install(page);
  await otaRelay(page, { script: (verb, rest) => (verb === 'DATA' && rest.startsWith('576,') ? { offset: 0, status: 0 } : undefined) });
  r = await page.evaluate(async () => {
    const m = __fake.toasts.length;
    await boardOtaRelay(2);
    return { toasts: __fake.toasts.slice(m).map((t) => t.message).join('\n'), last: __rota.log.at(-1), pulls: __pulls.length };
  });
  expect(r.toasts).toContain('Wireless OTA failed: target lost the OTA session at 576 bytes');
  expect(r.last).toMatch(/^send \?OTA,ABORT,2,\d+$/);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.ota_fake_edges_replay once the last OTA ends, the ETM edges held during it are replayed one board at a time - a pull, then its terminal re-armed - skipping the OTA\'s own target and a board whose relay is gone; while another OTA runs they are kept', async ({ page }) => {
  // boardOtaRelay's finally (app.js:2166-2195); the edges are recorded by installEtmListener (wizard.app_fake_etm_listener).
  // Row ota.suppressed_edges_replay.
  await setup(page);
  await otaRelay(page, { script: (verb) => (verb === 'BEGIN' ? { offset: 0, status: 1 } : undefined) });   // rejected at once
  const r = await page.evaluate(async () => {
    const seq = [];
    window.remoteBoardPull = async (relay, n) => {
      seq.push(`pull ${n} start`);
      await new Promise((res) => setTimeout(res, 200));
      seq.push(`pull ${n} end`);
    };
    window.startRemoteTermSession = async (relay, n) => { seq.push(`rterm ${n} via ${relay}`); };
    // Another OTA still running: nothing replayed yet.
    _otaInProgress.add(9);
    for (const n of [2, 3, 4, 5]) _suppressedEtmEdges.add(n);
    await boardOtaRelay(2);
    await new Promise((res) => setTimeout(res, 300));
    const held = [...seq, [..._suppressedEtmEdges].sort()];
    // The last OTA ends: replayed.
    _otaInProgress.delete(9);
    await boardOtaRelay(2);
    await new Promise((res) => setTimeout(res, 800));
    return { held, seq, left: [..._suppressedEtmEdges] };
  });
  expect(r.held).toEqual([[2, 3, 4, 5]]);
  // WCB5 has no relay (never managed): skipped. WCB2 is the OTA's own target: skipped.
  expect(r.seq).toEqual(['pull 3 start', 'pull 3 end', 'rterm 3 via 1', 'pull 4 start', 'pull 4 end', 'rterm 4 via 1']);
  expect(r.left).toEqual([]);
  expect(page.wizErrors).toEqual([]);
});
