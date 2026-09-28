// The live monitor (docs/hil_plan/NAVICORE.md §2, nct.live.pwm_update and nct.live.stale_resubscribe, L1): the SBUS
// panel from scripted PWM_UPDATE frames, and the watchdog that re-subscribes a silent board (_sbusStaleTick,
// config_tool/index.html:10973-11021) without ever re-sending after a disconnect. The emulator streams frames at
// monitorHz while monitoring (sendPWMUpdate, NaviCore.ino:3119-3160).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));

test.describe('a streaming board', () => {
  let sbus = {};                                   // what the next frame's sbus block overrides
  test.use({ emuOptions: { config: BENCH, monitorHz: 20,
                           pwmFrame: (emu) => { const f = emu.defaultPwmFrame(); Object.assign(f.sbus, sbus); return f; } } });

  test('nctool.live_monitor the SBUS panel follows the frames (receiving, failsafe, lost); when frames stop for 4 s the panel says so and START_MONITOR is re-sent, which brings them back; after a disconnect nothing is re-sent', async ({ page, emu }) => {
    sbus = {};
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await expect(page.locator('#sbus-state')).toHaveText('Receiving · SBUS-16');
    await expect(page.locator('#sbus-detail')).toHaveText('16ch (25-byte frame) · 111 fps · age 4 ms');
    sbus = { failsafe: true };
    await expect(page.locator('#sbus-state')).toHaveText('FAILSAFE');
    sbus = { lost: true };
    await expect(page.locator('#sbus-state')).toHaveText('No signal');
    sbus = {};
    await expect(page.locator('#sbus-state')).toHaveText('Receiving · SBUS-16');

    // The board stops streaming without a word (a reboot clears its subscription): 4 s later the panel goes stale and
    // the tool subscribes again, once.
    const starts = emu.requests('START_MONITOR').length;
    emu._stopMonitor();
    await page.clock.fastForward(4600);
    await expect(page.locator('#sbus-state')).toHaveText(/^(No data — link idle|Receiving · SBUS-16)$/);
    await T.waitRequests(emu, 'START_MONITOR', starts + 1);
    await expect(page.locator('#sbus-state')).toHaveText('Receiving · SBUS-16');
    expect(await T.termLines(page, /\[monitor\] panel stale .* transport=direct · port=own .*resubscribe sent via own port/)).toHaveLength(1);
    expect(emu.requests('START_MONITOR').length).toBe(starts + 1);

    // Disconnect stops the stream and the watchdog with it: however long the silence, nothing more is sent.
    await page.locator('#btn-connect').click();
    await T.waitRequests(emu, 'STOP_MONITOR', 1);
    await expect.poll(() => T.state(page).then((s) => s.connected)).toBe(false);
    const after = emu.rx.length;
    await page.clock.fastForward(10_000);
    await page.clock.fastForward(10_000);
    expect(emu.rx.slice(after).map((r) => r.line)).toEqual([]);
    await expect(page.locator('#sbus-state')).toHaveText('No data — link idle');
    T.expectNoPageErrors(page);
  });
});

// The WCB status panel over the bridge (nct.wcb.status, nct.wcb.forget_peer): the 3 s GET_WCB_STATUS poll in both
// reply shapes (buildWcbStatus's dense arrays and its sparse rows, rc_telemetry.h:1879-1985), the names fetched apart
// in a fragmented WCB_META (:1986-2030) and asked for at most 8 times (index.html:5139-5165), and the forget flow.
test.describe('the WCB status panel through a relay', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb', relayId: 1, sparseStatus: true,
                           roster: { known: [1, 1, 1, 1, 0, 1], online: [1, 0, 1, 1, 0, 1],
                                     aliases: ['relay', 'body', '', 'legs', '', 'newbie'] } } });

  test('nctool.wcb_status_panel chips follow the polled roster in both reply shapes; names come from WCB_META, asked at most 8 times while a board stays nameless; a learned peer is forgotten and stays gone', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, [true]);
    const chips = () => page.locator('#wcb-status-list .wcb-chip').evaluateAll((els) => els.map((e) => [
      e.querySelector('.wcb-chip-name').textContent.replace(/\s+/g, ' ').trim(), e.querySelector('.wcb-chip-state').textContent]));
    await T.openTool(page);
    await T.connectViaWcb(page);
    await page.clock.fastForward(3100);
    // The bench's quantity is 1, so WCB 2-6 are learned peers ("· auto", forgettable); WCB 1 is the relay itself.
    await expect.poll(chips).toEqual([
      ['WCB 1 · relay 📡', 'Online'], ['WCB 2 · body · auto', 'Unknown'], ['WCB 3 · auto', 'Online'], ['WCB 4 · legs · auto', 'Online'],
      ['WCB 6 · newbie · auto', 'Online'],
    ]);
    expect(emu.requests('GET_WCB_STATUS').length).toBeGreaterThan(0);
    // WCB 3 has no name to give: the tool keeps asking, throttled, and gives up after 8.
    for (let i = 0; i < 14; i++) await page.clock.fastForward(6000);
    const metas = emu.requests('GET_WCB_META').length;
    expect(metas, 'GET_WCB_META requests while WCB 3 stays nameless').toBeGreaterThan(1);
    expect(metas).toBeLessThanOrEqual(8);
    // The dense shape draws the same roster; a board that drops shows Offline, not Unknown.
    emu.sparseStatus = false;
    emu.roster.online = [1, 0, 0, 1, 0, 1];
    await page.clock.fastForward(3100);
    await expect.poll(chips).toEqual([
      ['WCB 1 · relay 📡', 'Online'], ['WCB 2 · body · auto', 'Unknown'], ['WCB 3 · auto', 'Offline'], ['WCB 4 · legs · auto', 'Online'],
      ['WCB 6 · newbie · auto', 'Online'],
    ]);
    // Forget the learned peer: asked first, FORGET_PEER sent, the chip goes at once and the next polls keep it gone.
    await page.locator('#wcb-status-list .wcb-chip', { hasText: 'WCB 6' }).locator('button[title="Forget this learned peer"]').click();
    expect(dialogs[0].message.split('\n')[0]).toBe('Forget learned peer WCB 6 · newbie?');
    await T.waitRequests(emu, 'FORGET_PEER', 1);
    expect(emu.requests('FORGET_PEER')).toEqual([{ sys: 1, type: 'FORGET_PEER', id: 6 }]);
    await page.clock.fastForward(700);
    await page.clock.fastForward(3100);
    await expect.poll(chips).toEqual([
      ['WCB 1 · relay 📡', 'Online'], ['WCB 2 · body · auto', 'Unknown'], ['WCB 3 · auto', 'Offline'], ['WCB 4 · legs · auto', 'Online'],
    ]);
    expect(emu.roster.known[5]).toBe(0);
    T.expectNoPageErrors(page);
  });
});
