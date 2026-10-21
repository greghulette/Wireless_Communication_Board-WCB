// Needs no board: the decisions Wizard/flasher.js makes before it writes a byte (docs/hil_plan/WCB.md WCB-WP41 row 8) -
// which chip family it flashes, what it refuses, which S3 bootloader it picks for the flash size, when an app-only
// update becomes a full one, and which branch's firmware it pulls. flashFirmware runs for real in the page against a
// fake esptool-js (lib/fake_esptool_wcb.mjs, served in place of the vendored bundle) and a fake GitHub listing; nothing
// is flashed and no port is opened. flasher.js is not exported for node, which is why this is a browser spec.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { install } = require('../lib/fake');

const FAKE_ESPTOOL = fs.readFileSync(path.join(__dirname, '..', 'lib', 'fake_esptool_wcb.mjs'), 'utf8');
// Each published file's first byte says which it is, so a write list reads as the files chosen.
const FIRST = { ESP32: 0xA1, ESP32_part: 0xB1, ESP32_boot: 0xC1, ESP32S3: 0xA3, ESP32S3_part: 0xB3,
                ESP32S3_boot_16MB: 0xD6, ESP32S3_boot_8MB: 0xD8, ESP32S3_boot: 0xDA };
const NAME = (k) => `WCB_9.9.9_010101RJAN2030_${k}.bin`;
const image = (k) => Buffer.from([FIRST[k], ...Array.from({ length: 31 }, (_, i) => (i * 7) & 0xFF)]);

async function setup(page) {
  const listings = [];
  await page.route('**/vendor/esptool-js/**', (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: FAKE_ESPTOOL }));
  await page.route('https://api.github.com/**', (route) => {
    listings.push(route.request().url());
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(
      Object.keys(FIRST).map((k) => ({ type: 'file', name: NAME(k), download_url: `https://fake.invalid/${NAME(k)}` }))) });
  });
  await page.route('https://fake.invalid/**', (route) => {
    const k = /_(ESP32[^.]*)\.bin$/.exec(route.request().url())[1];
    return route.fulfill({ status: 200, contentType: 'application/octet-stream', body: image(k) });
  });
  await openWizard(page);
  await install(page);
  return listings;
}

// flashFirmware(port, hw, opts) with the fake loader configured as `esp`; what it wrote and logged, or why it stopped.
function flash(page, esp, hw, opts = {}) {
  return page.evaluate(async ({ esp, hw, opts }) => {
    const flash = {};
    for (const [a, bytes] of Object.entries(esp.flash || {})) flash[a] = Uint8Array.from(bytes);
    globalThis.__wcbEsp = { ...esp, flash, calls: [] };
    const logs = [];
    let error = null;
    try {
      await flashFirmware({ fake: true }, hw, { onProgress() {}, onLog: (m) => logs.push(m), onStatus() {}, ...opts });
    } catch (e) { error = e.message; }
    const writes = globalThis.__wcbEsp.calls.filter((c) => c.op === 'write').flatMap((c) => c.files)
      .map((f) => [f.address, f.first]);
    return { error, logs, writes };
  }, { esp, hw, opts });
}
const hex = (w) => w.map(([a, f]) => `0x${a.toString(16)}:${f.toString(16).toUpperCase()}`);
const PART = (k) => [...image(k)];

test('wizard.flasher_fake_helpers the flasher: a detected chip overrides the HW selection, an ESP32-S2 and an unidentified chip with no HW version are refused, the S3 bootloader follows the flash size (and an unknown size writes none), a changed partition table turns an app-only update into a full one, a factory reset erases NVS, and the firmware branch comes from the /dev/ path or a valid override', async ({ page }) => {
  const listings = await setup(page);

  // Refusals: nothing is written.
  let r = await flash(page, { chip: 'ESP32-S2' }, 24);
  expect([r.error, r.writes]).toEqual([expect.stringContaining('not a supported WCB chip'), []]);
  r = await flash(page, { chip: '' }, 0);
  expect([r.error, r.writes]).toEqual([expect.stringContaining('Could not identify the chip'), []]);

  // An S3 on a board whose card says HW 2.4: re-fetched as S3, and a blank board gets the 8 MB bootloader it reports.
  const n0 = listings.length;
  r = await flash(page, { chip: 'ESP32-S3', flashKB: 8192 }, 24);
  expect(r.error).toBe(null);
  expect(r.logs.join('\n')).toContain('auto-detection overrides the manual selection');
  expect(listings.length - n0, 'the listing fetched again for the right family').toBe(2);
  expect(hex(r.writes)).toEqual(['0x0:D8', '0x8000:B3', '0xe000:FF', '0x10000:A3']);
  // The flash ID's size byte when getFlashSize is missing: 0x18 is 16 MB, 0x17 8 MB.
  r = await flash(page, { chip: 'ESP32-S3', flashId: 0x184016 }, 32);
  expect(hex(r.writes)[0]).toBe('0x0:D6');
  r = await flash(page, { chip: 'ESP32-S3', flashId: 0x174016 }, 32);
  expect(hex(r.writes)[0]).toBe('0x0:D8');
  // No size at all: a board that needs a bootloader is refused rather than given one declaring the wrong size.
  r = await flash(page, { chip: 'ESP32-S3' }, 32);
  expect([r.error, r.writes]).toEqual([expect.stringContaining('Cannot determine flash size'), []]);

  // Classic ESP32, Update FW (app only): the same partition table keeps it app-only; a different one escalates once.
  const programmed = { 0x1000: [0xE9, 0, 0, 0] };
  r = await flash(page, { chip: 'ESP32-D0WD-V3', flash: { ...programmed, 0x8000: PART('ESP32_part') } }, 1, { appOnly: true });
  expect(hex(r.writes)).toEqual(['0xe000:FF', '0x10000:A1']);
  r = await flash(page, { chip: 'ESP32-D0WD-V3', flash: { ...programmed, 0x8000: [1, 2, 3] } }, 1, { appOnly: true });
  expect(r.logs.join('\n')).toContain('Partition table mismatch');
  expect(hex(r.writes)).toEqual(['0x1000:C1', '0x8000:B1', '0xe000:FF', '0x10000:A1']);
  // Factory reset: the full image, and NVS erased.
  r = await flash(page, { chip: 'ESP32-D0WD-V3' }, 1, { eraseNvs: true });
  expect(hex(r.writes)).toEqual(['0x1000:C1', '0x8000:B1', '0x9000:FF', '0xe000:FF', '0x10000:A1']);
  // A chip it cannot name, with a HW version picked: the selection stands (here a programmed board, same table).
  r = await flash(page, { chip: 'mystery', flash: { ...programmed, 0x8000: PART('ESP32_part') } }, 24);
  expect(r.logs.join('\n')).toContain('falling back to the HW version selection');
  expect(hex(r.writes)).toEqual(['0xe000:FF', '0x10000:A1']);

  // The branch: the /dev/<branch>/Wizard path first, then a valid localStorage override, else main.
  const branches = await page.evaluate(() => {
    const run = (pathname, stored) => new Function('location', 'localStorage', `return (${getFirmwareBranch.toString()})();`)(
      { pathname }, { getItem: () => stored });
    return [
      run('/dev/feature/x/Wizard/index.html', 'main'), run('/dev/WIFI/Wizard/', ''), run('/dev/a b/Wizard/', ''),
      run('/Wizard/index.html', 'feature/x'), run('/Wizard/index.html', 'main?x=1'), run('/Wizard/index.html', 'x#y'),
      run('/Wizard/index.html', '  dev  '), run('/Wizard/index.html', 'x'.repeat(101)), run('/Wizard/index.html', null),
      ['feature/x', 'a&b', 'a=b', '', null, '../x'].map((b) => isValidFwBranch(b)),
    ];
  });
  // '../x' passes the whitelist (flasher.js:61), although the comment above it says '/../' is kept out. The value is
  // only ever a ?ref= query value (getBinaryData, fetchLatestFirmwareVersion), where it is harmless; pinned as it is.
  expect(branches).toEqual(['feature/x', 'WIFI', 'main', 'feature/x', 'main', 'main', 'dev', 'main', 'main',
                            [true, false, false, false, false, true]]);
  expect(page.wizErrors).toEqual([]);
});

// The fake loader's calls on the last flash, in order: main() with its mode and why it failed, the DTR/RTS changes,
// the post-flash reset and the disconnect.
const espCalls = (page) => page.evaluate(() => globalThis.__wcbEsp.calls
  .filter((c) => ['main', 'dtr', 'rts', 'afterFlash', 'disconnect'].includes(c.op))
  .map((c) => (c.op === 'main' ? `main:${c.mode ?? ''}${c.failed ? `!${c.failed}` : ''}`
    : c.op === 'dtr' || c.op === 'rts' ? `${c.op}:${c.v}` : c.op)));

test('wizard.flasher_fake_reset_tap an automatic reset that never syncs becomes a request to tap RESET: the flasher holds GPIO0 low (DTR asserted, RTS released), retries with no reset of its own until the chip answers, releases GPIO0 before anything resets the board, and flashes; a window with no tap, a lost port, or a failure that is not a missed sync stops it with advice for that board', async ({ page }) => {
  await setup(page);

  // Missed by the automatic reset, answered on the third try after the request.
  let r = await flash(page, { chip: 'ESP32-D0WD-V3', noSync: 3 }, 24);
  expect(r.error).toBe(null);
  expect(await espCalls(page)).toEqual(['main:!nosync', 'rts:false', 'dtr:true', 'main:no_reset!nosync',
                                        'main:no_reset!nosync', 'main:no_reset', 'dtr:false', 'afterFlash', 'disconnect']);
  const log = r.logs.join('\n');
  expect(log).toContain('The automatic reset did not reach bootloader mode');
  expect(log).toContain('► Tap the RESET button on the board now');
  expect(log).toContain('Bootloader mode entered after the RESET tap');
  expect(hex(r.writes)).toEqual(['0x1000:C1', '0x8000:B1', '0xe000:FF', '0x10000:A1']);

  // No tap inside the window: nothing written, GPIO0 released, and the advice fits the board.
  await page.evaluate(() => { BOOTLOADER_ASSIST_MS = 700; });
  r = await flash(page, { chip: 'ESP32-D0WD-V3', noSync: 999 }, 24);
  expect([r.error, r.writes]).toEqual([expect.stringContaining('Bootloader connection failed: no RESET tap was seen'), []]);
  expect(r.error).toContain('RESET is its only button');
  const c = await espCalls(page);
  expect([c.slice(0, 3), c.slice(-2)]).toEqual([['main:!nosync', 'rts:false', 'dtr:true'], ['dtr:false', 'disconnect']]);
  r = await flash(page, { chip: 'ESP32-S3', noSync: 999 }, 32);
  expect(r.error).toContain('holding BOOT while you tap RESET');

  // The board drops off USB while it waits.
  r = await flash(page, { chip: 'ESP32-D0WD-V3', noSync: 999, dropOnDtr: true }, 24);
  expect([r.error, r.writes]).toEqual([expect.stringContaining('the board dropped off USB'), []]);
  await page.evaluate(() => { BOOTLOADER_ASSIST_MS = 60000; });

  // A port that would not open is not a missed sync: no request, the lines untouched.
  r = await flash(page, { chip: 'ESP32-D0WD-V3', mainError: "Failed to execute 'open' on 'SerialPort': Failed to open serial port." }, 24);
  expect(r.error).toContain("Bootloader connection failed: Failed to execute 'open'");
  expect((await espCalls(page)).filter((x) => /^(dtr|rts):/.test(x))).toEqual([]);
  expect(page.wizErrors).toEqual([]);
});
