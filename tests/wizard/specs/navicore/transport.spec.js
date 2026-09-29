// The config tool's transports: direct USB, Via a WCB (the WcbSerialHub), the auto-detect between them, teardown,
// link loss and the bridge keep-alive (docs/hil_plan/NAVICORE.md §2 config-tool rows nct.conn.*, L1). No board.
//
// (should) specs assert the behaviour the tool ought to have and are marked test.fail(): Playwright counts them as
// expected failures (green standalone and in CI, red the day the fix lands so the marker is removed), and the HIL
// harness reports them FAIL until then, as it does every (should) test (docs/HIL_TESTING.md §6).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const wrapped = (emu, re) => emu.rx.filter((r) => re.test(r.line)).map((r) => r.line);

test.describe('direct USB', () => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.pong_epoch_slow_direct (should) a direct board whose PONG comes 3.5 s late is still taken for a direct link, not "switched to Via WCB"', async ({ page, emu }) => {
    test.fail(true, 'known tool defect: the PONG handler stamps _pongSeenEpoch with the epoch CURRENT when the reply lands ' +
                    '(index.html:9515-9517), so a late direct PONG satisfies the Via-WCB probe; a direct PONG carries no sys/id and could be told apart');
    emu.delay.PING = 3500;
    await T.openTool(page);
    await T.connectUsb(page, emu, { handshake: false });        // ends when the tool itself has finished its handshake
    const s = await T.state(page);
    expect(s.viaWcbActive, 'transport after a slow direct board answered').toBe(false);
    expect(await T.toasts(page)).not.toContainEqual(expect.stringContaining('switched to Via WCB'));
    expect(wrapped(emu, /^;w20,\{"sys":1,"type":"GET_CONFIG"/), 'GET_CONFIG went out wrapped for a WCB').toEqual([]);
    T.expectNoPageErrors(page);
  });

  test('nctool.transport_flag_reset every connect and disconnect puts the firmware buttons on the live transport (Via WCB -> Disconnect -> USB)', async ({ page, emu }) => {
    await T.openTool(page);
    expect(await T.fwButtons(page)).toEqual({ 'btn-fw-flash': true, 'btn-fw-wipe': true, 'btn-fw-ota': true, 'btn-fw-ota-wcb': false });
    emu.mode = 'via-wcb';
    await T.connectViaWcb(page);
    expect(await T.fwButtons(page), 'bridged: only relay OTA').toEqual({ 'btn-fw-flash': false, 'btn-fw-wipe': false, 'btn-fw-ota': false, 'btn-fw-ota-wcb': true });
    await page.locator('#btn-connect').click();                // Disconnect
    await page.waitForFunction(() => !sharedActive && !viaWcbActive);
    expect(await T.fwButtons(page), 'disconnected: back to Direct-USB').toEqual({ 'btn-fw-flash': true, 'btn-fw-wipe': true, 'btn-fw-ota': true, 'btn-fw-ota-wcb': false });
    emu.mode = 'direct';
    await T.connectUsb(page, emu);
    expect(await T.state(page)).toMatchObject({ viaWcbActive: false, connected: true });
    expect(await T.fwButtons(page), 'USB after a WCB session').toEqual({ 'btn-fw-flash': true, 'btn-fw-wipe': true, 'btn-fw-ota': true, 'btn-fw-ota-wcb': false });
    T.expectNoPageErrors(page);
  });

  test('nctool.disconnect_teardown Disconnect stops the monitor, releases and closes the port, drops the baseline and says a pending save was not confirmed', async ({ page, emu, serial }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    emu.hold.add('SET_CONFIG');                                 // the board never answers the save
    await page.evaluate(() => { document.getElementById('tapwin-input').value = '600'; return saveConfigToBoard(); });
    await T.waitRequests(emu, 'SET_CONFIG');
    expect((await T.state(page)).pendingSave).toBe(true);
    await page.locator('#btn-connect').click();
    await page.waitForFunction(() => !port && document.getElementById('status-text').textContent === 'Disconnected');
    expect(emu.requests('STOP_MONITOR').length).toBe(1);
    const log = await page.evaluate(() => window.__hilSerial.log.map((e) => ({ op: e.op, result: e.result })));
    expect(log.map((e) => e.op)).toContain('rxcancel');
    expect(log.filter((e) => e.op === 'close').pop().result, 'the close succeeds: reader cancelled and writer released first').toBe('closed');
    const st = await page.evaluate(() => ({ loaded: _configLoaded, base: _configBaseline, pending: _pendingSaveBaseline,
                                            poll: wcbStatusTimer, keep: _wcbKeepaliveTimer, label: document.getElementById('btn-connect').textContent }));
    expect(st).toEqual({ loaded: false, base: null, pending: null, poll: null, keep: null, label: 'Connect' });
    expect(await T.toasts(page)).toContainEqual(expect.stringContaining('Disconnected before the save was confirmed'));
    // The held ACK now arrives at a closed port (dropped), and the 12 s save watchdog must not fire on the dead session.
    emu.release('SET_CONFIG');
    await page.clock.runFor(13_000);
    expect(await T.toasts(page)).not.toContainEqual(expect.stringContaining('No response from NaviCore'));
    expect(serial.owner).toBe(null);
    T.expectNoPageErrors(page);
  });

  test('nctool.link_loss_reconnect a lost device (NetworkError) tears down and auto-reconnects to the one granted port; a BreakError keeps the session; pagehide drops DTR/RTS and closes', async ({ page, emu, serial }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    // A recoverable read error: the reader is re-acquired, nothing closes, and the next line is still read.
    await serial.fault('BreakError');
    emu.inject('{"type":"PONG","version":"v0.2.0_AFTERBREAK"}');
    await expect(page.locator('#fw-current-version')).toHaveText('v0.2.0_AFTERBREAK');
    expect(serial.events.filter((e) => e.op === 'close')).toEqual([]);
    // A fatal one (the USB device vanished): the terminal says so, the session is torn down, and with exactly one
    // granted port the tool reopens it and handshakes again on its own.
    const opens = emu.opens;
    await serial.fault('NetworkError');
    await expect.poll(() => T.termLines(page, /serial link lost/)).toHaveLength(1);
    await expect.poll(() => emu.opens, { timeout: 10_000 }).toBe(opens + 1);
    await page.clock.fastForward(4000);                         // openPortAndStart's settle, as on the first connect
    await page.waitForFunction(() => _configLoaded === true && !_wantAutoReconnect, null, { timeout: 20_000 });
    expect(await T.state(page)).toMatchObject({ connected: true, status: 'Connected ✓' });
    // Leaving the page: the 'pagehide' handler (_releaseSerialOnUnload) deasserts DTR/RTS, then closes. The streams
    // are still locked, so the close itself is refused: best-effort by design (index.html:4492-4502). The event is
    // dispatched in the live page, because a real unload tears the page down before its calls reach Node.
    T.expectNoPageErrors(page);
    const mark = await page.evaluate(() => window.__hilSerial.log.length);
    await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
    const after = await page.evaluate((m) => window.__hilSerial.log.slice(m), mark);
    const sig = after.findIndex((e) => e.op === 'signals'), cls = after.findIndex((e) => e.op === 'close');
    expect(sig, 'setSignals on pagehide').toBeGreaterThan(-1);
    expect(after[sig].signals).toEqual({ dataTerminalReady: false, requestToSend: false });
    expect(cls, 'close on pagehide').toBeGreaterThan(sig);
    await expect.poll(() => serial.events.filter((e) => e.op === 'closeAttempt').length).toBeGreaterThan(0);
    // `try { p.close(); } catch (_) {}` (index.html:4501) cannot catch the promise close() returns, so the locked-stream
    // refusal is an unhandled rejection. Harmless while the page unloads; it is the only error this may leave.
    await expect.poll(() => page.ncErrors).toEqual(['Cannot cancel a locked stream']);
  });

});

test.describe('direct USB, a second port granted to the origin', () => {
  test.use({ emuOptions: { config: BENCH }, serialOptions: { dummies: 1 } });

  test('nctool.link_loss_ambiguous_ports with two granted ports a lost link is torn down and NOT reopened, since the wrong device could answer', async ({ page, emu, serial }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    expect(await page.evaluate(async () => (await navigator.serial.getPorts()).length)).toBe(2);
    const opens = emu.opens;
    await serial.fault('NetworkError');
    await expect.poll(() => T.termLines(page, /serial link lost/)).toHaveLength(1);
    await page.waitForFunction(() => !port);
    await page.waitForTimeout(1500);                            // time enough for tryAutoReconnect to have acted
    expect(emu.opens, 'tryAutoReconnect acts only on exactly one granted port (index.html:4900-4915)').toBe(opens);
    expect(await T.state(page)).toMatchObject({ connected: false, status: 'Connection lost — click Connect to resume' });
    T.expectNoPageErrors(page);
  });
});

test.describe('via a WCB', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb', relayId: 19, relayName: 'HILrelay', roster: { known: [1, 1], online: [1, 1] } } });

  test('nctool.via_wcb_hub a cancelled picker says "No port selected"; picking the WCB elects this tab the hub leader, handshakes with ;w20,{"sys":1,"type":"PING"} and shows the relay chip; DTR is never asserted', async ({ page, emu, serial }) => {
    await T.openTool(page);
    await page.evaluate(() => window.__hilSerial.setRequestMode('cancel'));
    await page.locator('#btn-connect').click();
    await page.locator('#connect-modal .connect-opt').nth(1).click();
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('No port selected'));
    expect(await T.state(page)).toMatchObject({ sharedActive: false, viaWcbActive: false, connected: false });
    expect(emu.opens).toBe(0);

    await page.evaluate(() => window.__hilSerial.setRequestMode('grant'));
    await T.connectViaWcb(page);
    // The status poll starts with setConnected(true) and goes first; then the probe PING, byte for byte, then CONFIG.
    const lines = emu.rx.filter((r) => /^;w20,/.test(r.line)).map((r) => r.line);
    const ping = lines.findIndex((l) => /"type":"PING"/.test(l));
    expect(lines[ping]).toBe(';w20,{"sys":1,"type":"PING"}');
    expect(lines.findIndex((l) => /"type":"GET_CONFIG"/.test(l))).toBeGreaterThan(ping);
    expect(await page.evaluate(() => sharedHub.role)).toBe('leader');
    await expect(page.locator('#status-text')).toHaveText('Connected via WCB → RC #20');
    // updateTerminalSplit: the bridge column's chip appears only while bridged, and the column follows the chip
    // (unchecked by default, index.html:2368-2369).
    const split = () => page.evaluate(() => [document.getElementById('term-vis-chip-wcb').style.display,
                                             document.getElementById('terminal-col-wcb').style.display]);
    expect(await split()).toEqual(['', 'none']);
    await page.evaluate(() => { const cb = document.getElementById('term-vis-cb-wcb'); cb.checked = true; cb.dispatchEvent(new Event('change')); });
    expect(await split()).toEqual(['', '']);
    expect(await page.evaluate(() => config.tapWindowMs)).toBe(BENCH.tapWindowMs);   // the fragmented CONFIG arrived whole
    expect(emu.fragSends.filter((f) => /"type":"CONFIG"/.test(f.wrapped)).length).toBe(1);
    await expect(page.locator('#wcb-status-list')).toContainText('relay 📡', { timeout: 10_000 });
    await expect(page.locator('#wcb-status-list')).toContainText('HILrelay');
    expect(serial.events.filter((e) => e.op === 'signals' && e.signals.dataTerminalReady)).toEqual([]);
    for (const r of emu.rx.filter((x) => x.via && x.json)) expect(r.json.sys, r.json.type).toBe(1);
    T.expectNoPageErrors(page);
  });

  // NAVICORE.md D-NC71, found writing NC-WP13 (nctool.board_via_wcb's dry run). A WCB runs any console line that starts
  // with neither '?' nor ';' as a broadcast out of its ports and onto the mesh (handleSingleCommand, WCB.ino:6153-6164),
  // so on a Via-WCB link every line must be ;w20,-wrapped.
  test('nctool.via_wcb_nothing_bare (should) connecting Via a WCB writes only ;w20,-wrapped lines to the WCB; today the first status poll goes out bare, before the tool has set its Via-WCB flag (D-NC71)', async ({ page, emu }) => {
    test.fail(true, "known tool defect D-NC71: connectSharedPort sets sharedActive (index.html:4715) and waits for the hub's port " +
                    '(:4717), whose state event runs onSharedState -> setConnected(true) (:4672-4674) -> startWcbStatusPoll, which sends ' +
                    'GET_WCB_STATUS at once (:5091) while viaWcbActive is still false (set at :4743), so sendJSON writes it unwrapped (:5618-5620)');
    await T.openTool(page);
    await T.connectViaWcb(page);
    const bare = emu.rx.filter((r) => !/^;w\d+,/i.test(r.line)).map((r) => (/"type"\s*:\s*"(\w+)"/.exec(r.line) || [, r.line[0]])[1]);
    expect(bare, 'lines written to the WCB console without the ;w20, wrapper').toEqual([]);
    T.expectNoPageErrors(page);
  });

  test('nctool.keepalive_via_wcb bridged, a silent PING goes out every 10 s so the RC keeps its rc_ch subscription', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectViaWcb(page);
    await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000);
    const pingsBefore = wrapped(emu, /^;w20,\{"sys":1,"type":"PING"\}$/).length;
    const echoesBefore = (await T.termLines(page, /"type":"PING"/)).length;
    await page.clock.runFor(31_000);
    await expect.poll(() => wrapped(emu, /^;w20,\{"sys":1,"type":"PING"\}$/).length - pingsBefore).toBe(3);
    expect((await T.termLines(page, /"type":"PING"/)).length, 'the keep-alive is silent in the terminal').toBe(echoesBefore);
    await page.locator('#btn-connect').click();
    await page.waitForFunction(() => !sharedActive);
    const after = wrapped(emu, /"type":"PING"/).length;
    await page.clock.runFor(30_000);
    expect(wrapped(emu, /"type":"PING"/).length, 'the keep-alive stops with the session').toBe(after);
    T.expectNoPageErrors(page);
  });
});

test.describe('a WCB doorway', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'doorway' } });

  test('nctool.doorway_pong_misdetect (should) a PONG relayed through a WCB ({"sys":1,...,"id":20}) is not taken for a direct link: Via WCB, and USB OTA disabled (D-NC30)', async ({ page, emu }) => {
    test.fail(true, 'known tool defect D-NC30: any PONG satisfies the direct probe (index.html:9515-9517); Intellex works ' +
                    'around it by calling onViaWcbToggle(true) itself (intellex_shim.js:411-437)');
    await T.openTool(page);
    await T.connectUsb(page, emu, { handshake: false });
    expect((await T.state(page)).viaWcbActive, 'transport through a doorway').toBe(true);
    expect(await T.fwButtons(page)).toMatchObject({ 'btn-fw-ota': false, 'btn-fw-ota-wcb': true });
    T.expectNoPageErrors(page);
  });
});
