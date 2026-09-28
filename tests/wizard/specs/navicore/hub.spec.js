// Two tool tabs on one tethered WCB through the shared-port hub (docs/hil_plan/NAVICORE.md §2, nct.hub.*, L1;
// config_tool/serial-hub.js): one tab owns the port and the other follows over BroadcastChannel, elected by a Web Lock.
// Both tabs are pages of one context, so they share an origin, a lock manager and the fake port, as two browser tabs do.
// FakeSerial tags every open and write with the page that made it (pg 1 = the first tab).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));

async function secondTab(context) {
  const p = await context.newPage();
  p.setDefaultTimeout(15_000);
  p.ncErrors = [];
  p.on('pageerror', (e) => p.ncErrors.push(e.message));
  return p;
}

test.describe('two tabs on one WCB', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb', relayId: 1 } });

  test('nctool.shared_hub_two_pages the first tab owns the port and the second follows it: the follower handshakes through the owner, both read what the board says, DTR is never asserted, and when the owner closes the follower takes the port over and carries on', async ({ context, page, emu, serial }) => {
    await T.openTool(page);
    await T.connectViaWcb(page);
    const b = await secondTab(context);
    await T.openTool(b);
    const pings = emu.requests('PING').length;
    await T.connectViaWcb(b);
    expect(serial.events.filter((e) => e.op === 'open').map((e) => e.pg), 'who opened the port').toEqual([1]);
    expect(serial.events.filter((e) => e.op === 'write' && e.pg !== 1), 'writes not made by the owner').toEqual([]);
    expect(emu.requests('PING').length, 'the follower\'s handshake reached the board').toBeGreaterThan(pings);
    expect(await b.evaluate(() => [sharedActive, viaWcbActive, _configLoaded])).toEqual([true, true, true]);

    emu.inject('[TERM:20]hello from the droid');
    await expect.poll(() => T.termLines(page, /hello from the droid/)).toHaveLength(1);
    await expect.poll(() => T.termLines(b, /hello from the droid/)).toHaveLength(1);

    // The owner closes: the follower is elected, opens the port it was granted, and its next request goes out itself.
    await page.close();
    await expect.poll(() => serial.events.filter((e) => e.op === 'open' && e.pg === 2).length, { timeout: 15_000 }).toBe(1);
    await expect.poll(() => b.evaluate(() => !!(sharedHub && sharedHub.portOpen)), { timeout: 15_000 }).toBe(true);
    const gets = emu.requests('GET_CONFIG').length;
    const writes = serial.events.filter((e) => e.op === 'write').length;
    await b.locator('#btn-refresh').click();
    await T.waitRequests(emu, 'GET_CONFIG', gets + 1);
    expect(serial.events.filter((e) => e.op === 'write').slice(writes).map((e) => e.pg)).toContain(2);
    // The shared port is a WCB, whose RC differentiator resets it on DTR (serial-hub.js DEFAULTS.assertDTR).
    expect(serial.events.filter((e) => e.op === 'signals' && e.signals.dataTerminalReady), 'DTR asserted').toEqual([]);
    expect(b.ncErrors).toEqual([]);
    await b.close();
  });

  test('nctool.multi_tab_save (should) with two tabs on one WCB, one tab\'s save ACK does not confirm the other tab\'s save (D-NC35)', async ({ context, page, emu }) => {
    test.fail(true, 'known defect D-NC35: every tab numbers its saves from 1 (_saveSeq, index.html:17006, :16948) and the ACK handler matches the ' +
                    'saveId alone (:9598, :9616), so a tab takes another tab\'s ACK for its own save; its edit is then in its baseline, ' +
                    'not on the board, and the next Save does not send it. (Both tabs also number fragment sessions from 1, :5508)');
    await T.openTool(page);
    await T.connectViaWcb(page);
    const b = await secondTab(context);
    await T.openTool(b);
    await T.connectViaWcb(b);
    emu.hold.add('SET_CONFIG');
    // Tab A saves one field: it lands on the board and its ACK waits.
    await page.evaluate(() => { config.maeGateMs = 300; saveConfigToBoard(); });
    await expect.poll(() => emu.config.toJSON().maeGateMs).toBe(300);
    // Tab B saves its own value for it, which the relay loses on the way. (maeGateMs, because Save re-reads the
    // General tab's inputs into config first, index.html:16846-16872, and this field is not one of them.)
    emu.mode = 'silent';
    await b.evaluate(() => { config.maeGateMs = 450; saveConfigToBoard(); });
    await expect.poll(() => emu.rx.filter((r) => r.line.includes('"maeGateMs":450')).length).toBe(1);
    emu.mode = 'via-wcb';
    expect(await b.evaluate(() => [_pendingSaveId, !!_pendingSaveBaseline])).toEqual([1, true]);
    // A's ACK arrives, and both tabs read it (a marker line after it proves B has).
    emu.held.shift().fn();
    emu.inject('[TERM:20]after-the-ack');
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    await expect.poll(() => T.termLines(b, /after-the-ack/)).toHaveLength(1);
    expect(emu.config.toJSON().maeGateMs, 'the board holds tab A\'s value, not tab B\'s').toBe(300);
    expect(await b.evaluate(() => !!_pendingSaveBaseline), 'tab B still waiting for its own ACK').toBe(true);
    await b.close();
  });
});
