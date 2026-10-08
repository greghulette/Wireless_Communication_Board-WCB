// What the config tool does with what it reads: framing, markers, panes, fragment reassembly, and a CONFIG that is
// not a config (docs/hil_plan/NAVICORE.md §2, nct.rx.*, nct.term.panes, L1). No board.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');
const { fragSlices } = require('../../lib/navicore/emulator');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const paneCount = (page, id) => page.evaluate((id) => document.getElementById(id).childElementCount, id);

test.describe(() => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.rx_framing_markers lines cut anywhere (a UTF-8 character included) reassemble; [MAE:], [OTA:], [TERM:] and plain text go where they belong; debug lines follow their chip; the pane keeps 3000 lines', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    // A JSON line split mid-token, and a text line split inside 'é' (0xC3 0xA9): the tool's streaming TextDecoder and
    // line buffer must put both back together.
    emu.injectRaw('{"type":"PO');
    emu.injectRaw('NG","version":"v0.2.0_SPLIT"}\r\n\r\n');
    await expect(page.locator('#fw-current-version')).toHaveText('v0.2.0_SPLIT');
    const text = Buffer.from('HILTEXT café ✓\r\n', 'utf8');
    const cut = text.indexOf(0xA9);
    emu.injectRaw(text.subarray(0, cut));
    emu.injectRaw(text.subarray(cut));
    await expect.poll(() => T.termLines(page, /HILTEXT/)).toEqual(['< HILTEXT café ✓']);
    expect(await T.termLines(page, /�/), 'no replacement characters').toEqual([]);

    // A Maestro read answer fills that slot's inline readout (the Maestro Locations panel) and echoes a friendly line.
    await page.locator('#btn-hwsetup').click();
    await page.locator('#cfg-tabs .cfg-tab[data-tab="maestro"]').click();
    await page.locator('[data-mae-read="GET"][data-mae-slot="1"]').click();   // sends ?MAE,GET,1,<ch>: the emulator answers
    await expect(page.locator('#mae-read-1')).toHaveText('ch0 = 6000 ¼µs');
    expect(emu.lines(/^\?MAE,GET,1,0$/)).toHaveLength(1);
    emu.inject('[MAE:2]{"q":"mov","err":"timeout"}');
    await expect(page.locator('#mae-read-2')).toHaveText('no reply');
    expect(await T.termLines(page, /^< M2 mov: no reply$/)).toHaveLength(1);
    expect(await T.termLines(page, /\[MAE:/), 'MAE markers never reach the terminal raw').toEqual([]);
    await page.evaluate(() => closeHwSetup());

    // [OTA:...] markers are shown; clip-list markers are consumed.
    emu.inject('[OTA:ACK,1024]');
    emu.inject('[CLIPLIST:BEGIN]{"n":0}');
    emu.inject('[CLIPLIST:END]');
    await expect.poll(() => T.termLines(page, /OTA:ACK/)).toEqual(['< [OTA:ACK,1024]']);
    expect(await T.termLines(page, /CLIPLIST/)).toEqual([]);

    // A debug stream line is hidden while its chip is off and shown once it is on (DEBUG_CATEGORIES).
    emu.inject('[DISPATCH] Maestro 1 ch0 -> 6000 HILDBG1');
    await page.waitForTimeout(200);
    expect(await T.termLines(page, /HILDBG1/)).toEqual([]);
    await page.locator('#btn-terminal').click();
    await page.locator('#terminal-debug-bar .dbg-chip[data-cat="maestro"]').click();
    emu.inject('[DISPATCH] Maestro 1 ch0 -> 6000 HILDBG2');
    await expect.poll(() => T.termLines(page, /HILDBG2/)).toHaveLength(1);

    // Bridged (the tool's own toggle, as Intellex calls it): [TERM:20] is NaviCore's pane, bare; another source is
    // tagged; plain text is the bridge WCB's pane; the bridge's echo of our own relayed line is hidden.
    await page.evaluate(() => onViaWcbToggle(true));
    emu.inject('[TERM:20]HILTERM20');
    emu.inject('[TERM:5]HILTERM5');
    emu.inject('HILWCBTEXT from the bridge');
    emu.inject('Processing input from Serial0: ;w20,{"sys":1,"type":"GET_WCB_STATUS"}');
    await expect.poll(() => T.termLines(page, /HILTERM/)).toEqual(['HILTERM20', '[#5] HILTERM5']);
    await expect.poll(() => T.termLines(page, /HILWCBTEXT/, 'wcb')).toEqual(['< HILWCBTEXT from the bridge']);
    expect(await T.termLines(page, /Processing input/, 'wcb')).toEqual([]);
    expect(await T.termLines(page, /Processing input/)).toEqual([]);
    await page.evaluate(() => onViaWcbToggle(false));

    // Scrollback is capped at TERM_MAX_LINES (3000) by trimming the head.
    const lines = Array.from({ length: 3050 }, (_, i) => `HILFLOOD ${String(i).padStart(4, '0')}`).join('\r\n') + '\r\n';
    emu.injectRaw(Buffer.from(lines));
    // 20 s, not the default 5: the flood renders in ~200 ms, yet in 2 of 7 full HIL runs on the Mac one poll of the
    // page went unanswered for the whole 5 s ('waiting on the predicate', 20261006-095709 and 20261007-183330), never
    // when run alone (15 of 15). What this checks is the cap below, not the speed; a page that hangs still fails.
    await expect.poll(() => T.termLines(page, /HILFLOOD 3049/), { timeout: 20_000 }).toHaveLength(1);
    expect(await paneCount(page, 'terminal-output')).toBe(3000);
    expect(await T.termLines(page, /HILFLOOD 0000/), 'the oldest lines were trimmed').toEqual([]);
    T.expectNoPageErrors(page);
  });

  test('nctool.rx_fragments a fragmented CONFIG applies once whatever the order, duplicates and noise; broken envelopes are ignored; a session idle 5 s expires and never completes', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    const applied = () => page.evaluate(() => config.tapWindowMs);
    // Parts in reverse, one duplicated, another session's part and bad envelopes interleaved.
    const wrap = (sid, data) => fragSlices(JSON.stringify({ type: 'CONFIG', data })).map((s, i, a) => JSON.stringify({ f: i + 1, of: a.length, sid, s }));
    const parts = wrap(7, { ...BENCH, tapWindowMs: 777 });
    expect(parts.length).toBeGreaterThan(40);
    const other = wrap(8, { ...BENCH, tapWindowMs: 888 });
    const lines = [...parts].reverse();
    lines.splice(5, 0, parts[3]);                                   // a duplicate
    lines.splice(9, 0, other[0]);                                   // another session, never finished
    lines.splice(12, 0, JSON.stringify({ f: 1, of: 600, sid: 9, s: 'x' }), JSON.stringify({ f: 0, of: 2, sid: 9, s: 'x' }),
                 JSON.stringify({ f: 3, of: 2, sid: 9, s: 'x' }));   // of > 512, f < 1, f > of: dropped
    await page.evaluate(() => { window.__applies = 0; const a = applyConfig; applyConfig = (d) => { window.__applies++; return a(d); }; });
    for (const l of lines) emu.inject(l);
    await expect.poll(applied).toBe(777);
    expect(await page.evaluate(() => window.__applies)).toBe(1);
    expect(await page.evaluate(() => [..._fragSessions.keys()])).toEqual([8]);   // only the unfinished one is left
    // Idle for more than FRAG_TIMEOUT_MS: it is dropped at the next fragment's GC, and its last part then starts a new
    // session instead of completing the old one.
    await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000);
    await page.clock.runFor(6000);
    for (const l of other.slice(1)) emu.inject(l);
    await page.waitForTimeout(500);
    expect(await applied(), 'an expired session must not complete').toBe(777);
    // The same sid reused with a different part count replaces the stale session rather than mixing parts.
    const reused = wrap(8, { ...BENCH, tapWindowMs: 999, wifiSsid: 'HIL'.repeat(80) });
    expect(reused.length).not.toBe(other.length);
    for (const l of reused) emu.inject(l);
    await expect.poll(applied).toBe(999);
    T.expectNoPageErrors(page);
  });

  test('nctool.config_error_no_baseline a GET_CONFIG answered with an ERROR (the overflow guard) leaves the tool unloaded, and Save warns before pushing its defaults', async ({ page, emu }) => {
    emu.override.GET_CONFIG = () => '{"type":"ERROR","msg":"GET_CONFIG overflow — config too large, not sent. Reduce mapped actions and retry."}';
    const dialogs = T.answerDialogs(page, [false, true]);
    await T.openTool(page);
    await T.connectUsb(page, emu, { expectConfig: false });
    await expect.poll(() => T.termLines(page, /GET_CONFIG overflow/)).toHaveLength(1);
    expect(await page.evaluate(() => ({ loaded: _configLoaded, base: _configBaseline, sbus: config.sbusOutEnabled })))
      .toEqual({ loaded: false, base: null, sbus: false });     // the tool's defaults, not the board's
    await page.evaluate(() => saveConfigToBoard());              // dismissed
    expect(dialogs[0].message).toContain('No config has been loaded from the board yet');
    expect(emu.requests('SET_CONFIG')).toEqual([]);
    // "Save anyway" pushes the tool's defaults in full (no baseline): the risk the prompt exists for, pinned.
    await page.evaluate(() => saveConfigToBoard());
    const [sent] = await T.waitRequests(emu, 'SET_CONFIG');
    expect(Object.keys(sent.data)).toEqual(expect.arrayContaining(['wcbNetwork', 'mappings', 'thresholds', 'maestros', 'hcrDest']));
    expect(sent.data.mappings, "the tool's empty default mappings").toEqual({});
    expect(sent.data.wcbNetwork.deviceId).toBe(20);
    T.expectNoPageErrors(page);
  });
});
