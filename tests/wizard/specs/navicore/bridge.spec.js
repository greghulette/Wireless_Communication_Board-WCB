// How a save leaves the tool: the Via-WCB fragmenter and its push-budget readout, and Direct USB's paced 512-byte
// writes (docs/hil_plan/NAVICORE.md §2, nct.send.*, nct.save.push_budget, L1). No board: the emulator reassembles the
// envelopes as rc_telemetry.h does and stores the result as rcConfigFromJSON does, so byte-exactness is checked end to
// end, not only on the wire.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const fragLines = (emu, from = 0) => emu.rx.slice(from).filter((r) => /^;w20,\{"f":/.test(r.line));

// Rewrite the command of every action of mappings 101-1<n> in the page's config (what an editor's Apply leaves
// behind), so a Save has a large, known diff. Commands stay under the firmware's 95-byte field.
function bigEdit(page, n, cmd) {
  return page.evaluate(({ n, cmd }) => {
    for (let b = 1; b <= n; b++) {
      const key = String(100 * (1 + Math.floor((b - 1) / 36)) + ((b - 1) % 36) + 1);   // 101..136, then 201..
      config.mappings[key] = { exclusive: false,
        t1: Array.from({ length: 5 }, (_, i) => ({ type: 'wcb_unicast', target: '1', cmd: `${cmd}${b}.${i}` })) };
    }
  }, { n, cmd });
}

test.describe('via a WCB', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb' } });

  test('nctool.push_budget_prediction the Config footer predicts the bridged Save: a single packet for a small edit, and exactly as many fragments as the Save then sends', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectViaWcb(page);
    await page.locator('#btn-hwsetup').click();
    await expect(page.locator('#push-budget')).toHaveText('No changes to save');
    // A small edit fits one packet: no fragment envelope at all.
    await page.locator('#cfg-tabs .cfg-tab[data-tab="general"]').click();
    await page.locator('#sbus-out-toggle').click();
    await expect(page.locator('#push-budget')).toHaveText('Mesh push — single packet');
    const mark = emu.rx.length;
    await page.locator('#btn-hwsetup-save').click();
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    const sent = emu.rx.slice(mark).filter((r) => /"type":"SET_CONFIG"/.test(r.line));
    expect(sent).toHaveLength(1);
    expect(sent[0].line.startsWith(';w20,{"sys":1,"type":"SET_CONFIG"')).toBe(true);
    expect(fragLines(emu, mark)).toEqual([]);
    expect(emu.config.toJSON().sbusOutEnabled).toBe(!BENCH.sbusOutEnabled);
    // A large one: the readout's N is the Save's fragment count, envelope for envelope.
    await bigEdit(page, 8, ';S2HILBUDGET');
    await expect(page.locator('#push-budget')).toHaveText(/^Mesh push \d+\/192/);   // the readout polls every 750 ms
    const label = await page.locator('#push-budget').textContent();
    const m = /^Mesh push (\d+)\/192 \((\d+)%\)/.exec(label);
    expect(m, `readout "${label}"`).not.toBeNull();
    const mark2 = emu.rx.length;
    await page.locator('#btn-hwsetup-save').click();
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline), { timeout: 30_000 }).toBe(false);
    const frags = fragLines(emu, mark2).map((r) => JSON.parse(r.line.slice(5)));
    expect(frags.length, 'fragments sent').toBe(+m[1]);
    expect(new Set(frags.map((f) => f.sid)).size).toBe(1);
    expect(frags.map((f) => f.f)).toEqual(Array.from({ length: frags.length }, (_, i) => i + 1));
    expect(emu.config.toJSON().mappings['108'].t1[4].cmd).toBe(';S2HILBUDGET8.4');
    T.expectNoPageErrors(page);
  });

  test('nctool.sendjson_fragments_exact a bridged Save of hostile text (quotes, backslashes, CJK, emoji, tabs) goes out in <= 187-byte envelopes, paced >= 100 ms with no poll between parts, and lands byte-exact', async ({ page, emu, serial }) => {
    await T.openTool(page);
    await T.connectViaWcb(page);
    const cmd = '"q"\\b\\ 机器人 🤖\t;S2';                       // 3-byte and 4-byte UTF-8, escapes, a control char
    await bigEdit(page, 12, cmd);
    const mark = emu.rx.length, ev = serial.events.length;
    await page.evaluate(() => saveConfigToBoard());
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline), { timeout: 30_000 }).toBe(false);
    const lines = emu.rx.slice(mark).filter((r) => /^;w20,/.test(r.line));
    const first = lines.findIndex((r) => /^;w20,\{"f":/.test(r.line));
    const frags = lines.filter((r) => /^;w20,\{"f":/.test(r.line));
    expect(frags.length, 'enough parts to span a 3 s status poll').toBeGreaterThan(35);
    // Nothing else between the first and last envelope: the pollers stand down for the whole send (_bridgeUploadInFlight).
    expect(lines.slice(first, first + frags.length).map((r) => r.line)).toEqual(frags.map((r) => r.line));
    for (const r of frags) expect(Buffer.byteLength(r.line.slice(5)), 'envelope bytes').toBeLessThanOrEqual(187);
    const parsed = frags.map((r) => JSON.parse(r.line.slice(5)));
    const joined = parsed.map((p) => p.s).join('');
    const msg = JSON.parse(joined);
    expect(msg.type).toBe('SET_CONFIG');
    expect(msg.data.mappings['112'].t1[4].cmd).toBe(`${cmd}12.4`);
    expect(emu.reassembled.at(-1), 'the emulator joined the same text').toBe(joined);
    expect(emu.config.toJSON().mappings['112'].t1[4].cmd, 'stored byte-exact').toBe(`${cmd}12.4`);
    // Pacing: at least FRAG_PACE_FLOOR_MS (100 ms) between envelopes, by the page's own clock.
    const t = serial.events.slice(ev).filter((e) => e.op === 'write' && e.head.startsWith(';w20,{"f":')).map((w) => w.pageT);
    expect(t).toHaveLength(frags.length);
    const gaps = t.slice(1).map((x, i) => x - t[i]);
    expect(Math.min(...gaps)).toBeGreaterThanOrEqual(95);
    T.expectNoPageErrors(page);
  });

  test('nctool.push_refused_not_pending (should) a bridged Save over the 192-fragment cap is refused before a byte is sent, and the tool says so instead of waiting 12 s for an ACK that cannot come', async ({ page, emu }) => {
    test.fail(true, 'known tool defect: sendJSON returns normally after its "Send aborted" (index.html:5663-5668), so ' +
                    'saveConfigToBoard has already latched _pendingSaveBaseline and shows "Saving to NaviCore…" until the 12 s watchdog');
    await T.openTool(page);
    await T.connectViaWcb(page);
    await bigEdit(page, 50, ';S2HILTOOLARGE' + 'x'.repeat(60));   // well past 192 fragments
    await page.locator('#btn-hwsetup').click();
    await expect(page.locator('#push-budget')).toHaveText(/too large/);
    await expect(page.locator('#push-budget')).toHaveClass(/over/);
    const mark = emu.rx.length;
    await page.locator('#btn-hwsetup-save').click();
    await expect.poll(() => T.termLines(page, /Send aborted — payload needs \d+ fragments \(max 192\)/)).toHaveLength(1);
    expect(fragLines(emu, mark), 'nothing was sent').toEqual([]);
    expect(await page.evaluate(() => !!_pendingSaveBaseline), 'no save is pending after a refusal').toBe(false);
    expect(await T.toasts(page)).not.toContainEqual(expect.stringContaining('Saving to NaviCore'));
    T.expectNoPageErrors(page);
  });
});

test.describe('direct USB', () => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.sendline_chunking a SET_CONFIG over 512 bytes is written in 512-byte pieces >= 4 ms apart, and a send made meanwhile waits for the whole line', async ({ page, emu, serial }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await bigEdit(page, 20, ';S2HILCHUNK' + 'y'.repeat(40));
    const ev = serial.events.length, pings = emu.requests('PING').length;
    await page.evaluate(() => { window.__save = saveConfigToBoard(); sendJSON({ type: 'PING' }); return window.__save; });
    await T.waitRequests(emu, 'SET_CONFIG');
    await T.waitRequests(emu, 'PING', pings + 1);
    const writes = serial.events.slice(ev).filter((e) => e.op === 'write');
    const setCfg = emu.rx.find((r) => r.json && r.json.type === 'SET_CONFIG');
    const lineBytes = Buffer.byteLength(setCfg.line) + 1;
    expect(lineBytes).toBeGreaterThan(8 * 512);
    const start = writes.findIndex((w) => w.head.startsWith('{"sys":1,"type":"SET_CONFIG"'));
    const pieces = writes.slice(start, start + Math.ceil(lineBytes / 512));
    expect(pieces.map((w) => w.len).reduce((a, b) => a + b, 0)).toBe(lineBytes);
    for (const w of pieces) expect(w.len).toBeLessThanOrEqual(512);
    const gaps = pieces.slice(1).map((w, i) => w.pageT - pieces[i].pageT);
    expect(Math.min(...gaps)).toBeGreaterThanOrEqual(3.5);
    // The PING queued behind it (sendLine's _sendChain) arrived after, as its own intact line.
    const order = emu.rx.filter((r) => r.json).map((r) => r.json.type);
    expect(order.lastIndexOf('PING')).toBeGreaterThan(order.indexOf('SET_CONFIG'));
    expect(emu.config.toJSON().mappings['120'].t1[4].cmd).toBe(';S2HILCHUNK' + 'y'.repeat(40) + '20.4');
    T.expectNoPageErrors(page);
  });
});
