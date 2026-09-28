// The NaviCore config tool loads and connects (docs/hil_plan/NAVICORE.md §5.4, L1). No board: the fake
// navigator.serial (lib/navicore/shim.js) talks to the emulator (lib/navicore/emulator.js). Runs standalone
// (`npx playwright test specs/navicore`) and under the harness by id (tests/hil/suites/s49_navicore_tool.py).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const { openTool, connectUsb, state, waitRequests, expectNoPageErrors } = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));

test.describe(() => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.load_smoke GET /NaviCore/ lands on the tool, which loads its command library (wcb-native hidden) and opens its modals with no page error', async ({ page }) => {
    await openTool(page, { path: '/NaviCore/' });
    expect(new URL(page.url()).pathname).toBe('/NaviCore/config_tool/');   // the repo-root redirect (NaviCore/index.html)
    const lib = await page.evaluate(() => ({
      ids: (window.NC_COMMAND_LIBRARY?.boards || []).map((b) => b.id),
      seedOnly: window.NC_COMMAND_LIBRARY === undefined,
    }));
    expect(lib.seedOnly).toBe(false);                            // the vendored manifests loaded (cmdlib/droidnet, cmdlib/navicore)
    expect(lib.ids.length).toBeGreaterThan(10);
    expect(lib.ids).not.toContain('wcb-native');                 // NC_CMDLIB_HIDDEN
    expect(lib.ids).not.toContain('maestro');                    // superseded by nc-maestro-wcb (NC_CMDLIB_SUPERSEDE)
    expect(lib.ids[0]).toBe('wcb-sequences');                    // NC_CMDLIB_ORDER_TOP pins the WCB group first
    expect(lib.ids).toContain('navicore');

    await page.locator('#btn-connect').click();                  // the transport chooser
    await expect(page.locator('#connect-modal')).toHaveClass(/open/);
    await page.locator('#connect-modal .btn-ghost').click();
    await expect(page.locator('#connect-modal')).not.toHaveClass(/open/);

    await page.locator('#btn-hwsetup').click();                  // the Config window, pre-connect
    await expect(page.locator('#hwsetup-modal')).toHaveClass(/open/);
    for (const tab of ['general', 'channels', 'logical', 'transmitter', 'wcb', 'audio', 'maestro', 'wled', 'smoothing', 'serial']) {
      await page.locator(`#cfg-tabs .cfg-tab[data-tab="${tab}"]`).click();
      await expect(page.locator(`#cfg-pane-${tab}`)).toHaveClass(/active/);
    }
    await page.evaluate(() => closeHwSetup());
    await expect(page.locator('#hwsetup-modal')).not.toHaveClass(/open/);

    await page.locator('#btn-clips').click();
    await expect(page.locator('#clips-modal')).toHaveClass(/open/);
    await page.evaluate(() => closeClipsModal());

    await page.clock.runFor(2000);                               // deferred init
    expectNoPageErrors(page);
  });

  test('nctool.connect_direct_sequence Connect via USB opens at 115200, drops DTR/RTS before any write, and handshakes PING, GET_CONFIG, GET_CMDLIB_META, START_MONITOR, SET_DEBUG_FLAGS (all sys:1); the debug chips drive the flags', async ({ page, emu, serial }) => {
    await openTool(page);
    await connectUsb(page, emu);

    // The fake port's own log: open, then the DTR/RTS release, before the first write.
    const log = await page.evaluate(() => window.__hilSerial.log.map((e) => ({ op: e.op, opts: e.opts, signals: e.signals })));
    const open = log.findIndex((e) => e.op === 'open');
    expect(log[open].opts).toEqual({ baudRate: 115200 });
    expect(log[open + 1]).toEqual({ op: 'signals', signals: { dataTerminalReady: false, requestToSend: false } });
    const firstWrite = serial.events.findIndex((e) => e.op === 'write');
    const signalsAt = serial.events.findIndex((e) => e.op === 'signals');
    expect(signalsAt).toBeGreaterThan(-1);
    expect(signalsAt).toBeLessThan(firstWrite);

    // The handshake, in order (openPortAndStart): PINGs until a PONG, then the rest once each. The pollers run beside
    // it (GET_WCB_STATUS every 3 s; GET_MESH_STATS because this config has statsReport on: applyConfig seeds it).
    const BACKGROUND = new Set(['GET_WCB_STATUS', 'GET_MESH_STATS', 'GET_WCB_META']);
    const types = emu.rx.filter((r) => r.json && !BACKGROUND.has(r.json.type)).map((r) => r.json.type);
    const firstOther = types.findIndex((t) => t !== 'PING');
    expect(firstOther).toBeGreaterThan(0);
    expect(types.slice(firstOther, firstOther + 4)).toEqual(['GET_CONFIG', 'GET_CMDLIB_META', 'START_MONITOR', 'SET_DEBUG_FLAGS']);
    for (const r of emu.rx.filter((x) => x.json)) expect(r.json.sys, `${r.json.type} carries sys:1`).toBe(1);
    expect(emu.requests('SET_DEBUG_FLAGS')[0].flags).toBe(0);    // every chip starts off

    const s = await state(page);
    expect(s).toMatchObject({ viaWcbActive: false, configLoaded: true, connected: true, isMonitoring: true,
                              status: 'Connected ✓', connectLabel: 'Disconnect' });
    await expect(page.locator('#fw-current-version')).toHaveText(emu.version);   // from the PONG
    expect(await page.evaluate(() => config.tapWindowMs)).toBe(BENCH.tapWindowMs);

    // A chip is a firmware bit (DEBUG_CATEGORIES): Maestro = bit 0, HCR = bit 3; turning one off clears only it.
    await page.locator('#btn-terminal').click();
    await page.locator('#terminal-debug-bar .dbg-chip[data-cat="maestro"]').click();
    await page.locator('#terminal-debug-bar .dbg-chip[data-cat="hcr"]').click();
    await page.locator('#terminal-debug-bar .dbg-chip[data-cat="maestro"]').click();
    const flags = (await waitRequests(emu, 'SET_DEBUG_FLAGS', 4)).slice(1).map((m) => m.flags);
    expect(flags).toEqual([1, 9, 8]);
    expectNoPageErrors(page);
  });
});
