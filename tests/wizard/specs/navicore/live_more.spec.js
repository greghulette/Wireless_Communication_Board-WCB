// The live view beyond the SBUS panel (docs/hil_plan/NAVICORE.md §2, nct.live.rc_telemetry, L1): the matrix readout and
// button chip, the SVG glow of a pressed button or a moving control, the channel grid, the mode tabs following the
// transmitter and a clicked tab pinning until the switch moves (applyLiveMode, config_tool/index.html ~16635), the
// tier flash an rc_trig lights (flashAssignmentTier ~11975), and over the bridge the whole rc_ch / rc_hb / rc_trig /
// rc_mode / rc_err telemetry (handleBoardMessage ~9379-9525) with its stale watchdog. Direct USB frames are the
// emulator's PWM_UPDATE stream; the bridged lines are injected as the WCB prints them.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const chip = (page) => page.locator('#live-btn-name');
const activeTab = (page) => page.locator('.mode-tab[class*="active-"]').getAttribute('data-mode');
const glowing = (page, btn) => page.locator(`.btn-zone.live-active[data-btn="${btn}"]`).count();
const fired = (page, slot) => page.locator(`#assignment-list .tier-row[data-slot="${slot}"].trig-fired`).evaluateAll((els) => els.map((e) => +e.dataset.tier).sort());
// Whether button `btn`'s zone glows at any moment during `action` (a MutationObserver armed first, up to `ms`): over
// Direct USB the next PWM frame's render drops the glow again within 50 ms, which a poll can miss.
async function glowsDuring(page, btn, action, ms = 3000) {
  const armed = page.evaluate(([b, t]) => new Promise((resolve) => {
    const hit = () => !!document.querySelector(`.btn-zone.live-active[data-btn="${b}"]`);
    if (hit()) return resolve(true);
    const obs = new MutationObserver(() => { if (hit()) { obs.disconnect(); resolve(true); } });
    obs.observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class'] });
    setTimeout(() => { obs.disconnect(); resolve(hit()); }, t);
  }), [btn, ms]);
  await action();
  return armed;
}

test.describe('the live view over Direct USB', () => {
  const live = { matrixVal: 992, btn: 0, mode: 1, swing: {} };
  let phase = 0;
  test.use({ emuOptions: { config: BENCH, monitorHz: 20, pwmFrame: (emu) => {
    const f = emu.defaultPwmFrame();
    phase ^= 1;
    for (const [ch, [lo, hi]] of Object.entries(live.swing)) f.sbus.channels[ch - 1] = phase ? hi : lo;
    return Object.assign(f, { matrixVal: live.matrixVal, btn: live.btn, mode: live.mode });
  } } });

  test('nctool.live_view_direct the frames drive the matrix readout, the button chip and the pressed zone\'s glow, the channel grid with the matrix row marked, a moving control\'s glow that fades 600 ms after it stops, the mode tabs following the transmitter with a clicked tab pinned until the switch moves, and an rc_trig lights the tiers it fired (cumulative, or the long press alone)', async ({ page, emu }) => {
    Object.assign(live, { matrixVal: 992, btn: 0, mode: 1, swing: {} });
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await expect(page.locator('#live-ch6')).toHaveText('992');
    await expect(chip(page)).toHaveText('Neutral / Released');
    await expect(page.locator('#channels-grid .ch-row')).toHaveCount(16);
    await expect(page.locator('#channels-grid .ch-row[data-ch="7"]')).toHaveClass(/is-matrix/);
    await expect(page.locator('#channels-grid .ch-row[data-ch="7"] .ch-val')).toHaveText('992');
    // B3 pressed: the readout, the chip, the glow; released: back to neutral.
    live.matrixVal = 1730; live.btn = 3;
    await expect(page.locator('#live-ch6')).toHaveText('1730');
    await expect(chip(page)).toHaveText('Btn 3: B3 (Mode 1)');
    await expect.poll(() => glowing(page, 3)).toBeGreaterThan(0);
    live.matrixVal = 992; live.btn = 0;
    await expect(chip(page)).toHaveText('Neutral / Released');
    await expect.poll(() => glowing(page, 3)).toBe(0);
    // SA (channel 8) moving glows its zone; still, the glow fades.
    live.swing = { 8: [172, 1811] };
    await expect(page.locator('.switch-zone[data-sw="SA"]:visible').first()).toHaveClass(/live-active/);
    await expect(page.locator('#channels-grid .ch-row[data-ch="8"] .ch-val')).toHaveText(/^(172|1811)$/);
    live.swing = {};
    await page.clock.fastForward(800);
    await expect(page.locator('.switch-zone[data-sw="SA"]:visible').first()).not.toHaveClass(/live-active/);
    // The mode tabs follow the switch; a clicked tab pins until the switch moves again.
    expect(await activeTab(page)).toBe('1');
    live.mode = 2;
    await expect(page.locator('.mode-tab[data-mode="2"]')).toHaveClass(/active-2/);
    await page.locator('.mode-tab[data-mode="1"]').click();
    await expect(page.locator('.mode-tab[data-mode="1"]')).toHaveClass(/active-1/);
    await expect(page.locator('.mode-tab[data-mode="1"]')).toHaveClass(/mode-tab-override/);
    await page.clock.fastForward(500);
    expect(await activeTab(page), 'pinned while the switch stays in mode 2').toBe('1');
    live.mode = 3;
    await expect(page.locator('.mode-tab[data-mode="3"]')).toHaveClass(/active-3/);
    await expect(page.locator('.mode-tab-override')).toHaveCount(0);
    live.mode = 1;
    await expect(page.locator('.mode-tab[data-mode="1"]')).toHaveClass(/active-1/);
    // rc_trig on the wire: a double tap on button 10 (cumulative) lights tiers 1 and 2 and glows the zone briefly; a
    // long press on button 7 lights its long-press tier alone.
    expect(await glowsDuring(page, 10, async () => emu.inject('{"type":"rc_trig","mode":1,"btn":10,"tap":2}')),
           'the zone glowed on the trigger').toBe(true);
    await expect.poll(() => fired(page, 10)).toEqual([1, 2]);
    await page.clock.fastForward(300);
    await expect.poll(() => glowing(page, 10)).toBe(0);
    await page.clock.fastForward(2000);
    await expect.poll(() => fired(page, 10)).toEqual([]);
    emu.inject('{"type":"rc_trig","mode":1,"btn":7,"tap":4}');
    await expect.poll(() => fired(page, 7)).toEqual([4]);
    // Another mode's trigger lights nothing in the cards shown.
    emu.inject('{"type":"rc_trig","mode":2,"btn":10,"tap":1}');
    await page.waitForTimeout(200);
    expect(await fired(page, 10)).toEqual([]);
    T.expectNoPageErrors(page);
  });
});

test.describe('the live view through a WCB', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb', relayId: 1 } });

  test('nctool.live_rc_telemetry bridged telemetry drives the view: rc_hb sets the status line, the firmware readouts, the mode and the SBUS panel; rc_ch feeds the grid and the matrix readout; rc_trig flashes the tier and the zone; rc_mode moves the tab; rc_err is shown in the terminal; another board\'s lines are ignored; silence trips the SBUS panel to idle', async ({ page, emu }) => {
    const hb = (extra = '') => `{"sys":1,"type":"rc_hb","id":20,"fw":"vHIL.1","up":9,"mode":2,"model":0,"sbusFps":111,"sbusAge":3,"sbusLost":false,"sbusFail":false${extra}}`;
    const ch = (id, matrix) => `{"sys":1,"type":"rc_ch","id":${id},"ch":[${Array.from({ length: 24 }, (_, i) => (i === 6 ? matrix : 992)).join(',')}]}`;
    await T.openTool(page);
    await T.connectViaWcb(page);
    emu.inject(hb());
    await expect(page.locator('#status-text')).toHaveText('Connected via WCB → RC #20');
    await expect(page.locator('#fw-current-version')).toHaveText('vHIL.1');
    await expect(page.locator('.mode-tab[data-mode="2"]')).toHaveClass(/active-2/);
    await expect(page.locator('#sbus-state')).toHaveText('Receiving');
    await expect(page.locator('#sbus-detail')).toHaveText('111 fps · age 3 ms');
    emu.inject(ch(20, 1730));
    await expect(page.locator('#channels-grid .ch-row')).toHaveCount(24);
    await expect(page.locator('#channels-grid .ch-row[data-ch="7"] .ch-val')).toHaveText('1730');
    await expect(page.locator('#live-ch6')).toHaveText('1730');
    await expect(page.locator('#sbus-state')).toHaveText('Receiving · SBUS-24');
    await expect(page.locator('#sbus-detail')).toHaveText('24ch · 111 fps · age 3 ms');
    // Another NaviCore's channels are not ours.
    emu.inject(ch(21, 500));
    await page.waitForTimeout(200);
    await expect(page.locator('#channels-grid .ch-row[data-ch="7"] .ch-val')).toHaveText('1730');
    // A trigger: the tier and the zone, released 250 ms on; one from another board does nothing.
    emu.inject('{"sys":1,"type":"rc_trig","id":20,"mode":2,"btn":10,"tap":1}');
    await expect.poll(() => fired(page, 10)).toEqual([1]);
    await expect(chip(page)).toHaveText('Btn 10: T5 Right (Mode 2)');
    await expect.poll(() => glowing(page, 10)).toBeGreaterThan(0);
    await page.clock.fastForward(300);
    await expect(chip(page)).toHaveText('Neutral / Released');
    await expect.poll(() => glowing(page, 10)).toBe(0);
    emu.inject('{"sys":1,"type":"rc_trig","id":21,"mode":2,"btn":3,"tap":1}');
    await page.waitForTimeout(200);
    expect(await glowing(page, 3)).toBe(0);
    // The mode, from rc_mode; another board's is ignored.
    emu.inject('{"sys":1,"type":"rc_mode","id":20,"mode":3}');
    await expect(page.locator('.mode-tab[data-mode="3"]')).toHaveClass(/active-3/);
    emu.inject('{"sys":1,"type":"rc_mode","id":21,"mode":1}');
    await page.waitForTimeout(200);
    expect(await activeTab(page)).toBe('3');
    // An error the board reports lands in the terminal.
    emu.inject('{"sys":1,"type":"rc_err","id":20,"code":"E_SIZE","note":"SET_CONFIG too large"}');
    await expect.poll(() => T.termLines(page, /^< \[RC#20 rc_err\] E_SIZE: SET_CONFIG too large$/)).toHaveLength(1);
    // Silence: no rc_ch for over three frames at 5 Hz (1.5 s) trips the panel.
    await page.clock.fastForward(2500);
    await expect(page.locator('#sbus-state')).toHaveText('No data — link idle');
    emu.inject(ch(20, 992));
    await expect(page.locator('#sbus-state')).toHaveText('Receiving · SBUS-24');
    T.expectNoPageErrors(page);
  });
});
