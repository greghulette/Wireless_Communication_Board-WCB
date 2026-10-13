// Needs no board: the flasher's choices and its write (Wizard/flasher.js) - which images an update, a flash and a
// factory reset write, what it does when the flash or the listing cannot be read, and the write's retry, progress and
// reset. Medium-risk gaps the 2026-10-12 coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in
// docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each test's comment). flashFirmware runs for real
// in the page against a fake esptool-js (lib/fake_esptool_rescan.mjs, served in place of the vendored bundle) and a
// fake GitHub listing whose files the spec chooses; nothing is flashed and no port is opened (as
// specs/flasher_fake.spec.js). Runs standalone, in CI, and under the harness by id (s30_wizard.py).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { install } = require('../lib/fake');

const FAKE_ESPTOOL = fs.readFileSync(path.join(__dirname, '..', 'lib', 'fake_esptool_rescan.mjs'), 'utf8');
// Each published file's first byte says which it is, so a write list reads as the files chosen.
const FIRST = { ESP32: 0xA1, ESP32_part: 0xB1, ESP32_boot: 0xC1, ESP32S3: 0xA3, ESP32S3_part: 0xB3,
                ESP32S3_boot_16MB: 0xD6, ESP32S3_boot_8MB: 0xD8, ESP32S3_boot: 0xDA };
const NAME = (k) => `WCB_9.9.9_010101RJAN2030_${k}.bin`;
const OLD_OTA0 = 0x140000;   // the legacy layout's app slot (flasher.js)

// The listing the fake GitHub serves (`files`: which of FIRST it lists) and the app's size.
async function setup(page) {
  const served = { files: Object.keys(FIRST), appBytes: 32 };
  await page.route('**/vendor/esptool-js/**', (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: FAKE_ESPTOOL }));
  await page.route('https://api.github.com/**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(
    served.files.map((k) => ({ type: 'file', name: NAME(k), download_url: `https://fake.invalid/${NAME(k)}` }))) }));
  await page.route('https://fake.invalid/**', (route) => {
    const k = /_(ESP32[^.]*)\.bin$/.exec(route.request().url())[1];
    const len = (k === 'ESP32' || k === 'ESP32S3') ? served.appBytes : 32;
    const body = Buffer.alloc(len, 0x5A);
    body[0] = FIRST[k];
    return route.fulfill({ status: 200, contentType: 'application/octet-stream', body });
  });
  await openWizard(page);
  await install(page);
  return served;
}

// flashFirmware(port, hw, opts) with the fake loader configured as `esp`: what it wrote (in order), logged, reported as
// progress, toasted and called on the transport, or why it stopped.
function flash(page, esp, hw, opts = {}) {
  return page.evaluate(async ({ esp, hw, opts }) => {
    const flash = {};
    for (const [a, bytes] of Object.entries(esp.flash || {})) flash[a] = Uint8Array.from(bytes);
    globalThis.__wcbEsp = { ...esp, flash, calls: [] };
    const logs = [], progress = [];
    const t0 = __fake.toasts.length;
    let error = null;
    try {
      await flashFirmware({ fake: true }, hw, { onProgress: (w, t) => progress.push([w, t]), onLog: (m) => logs.push(m), onStatus() {}, ...opts });
    } catch (e) { error = e.message; }
    const calls = globalThis.__wcbEsp.calls;
    const writes = calls.filter((c) => c.op === 'write').map((c) => c.files.map((f) => [f.address, f.first, f.length]));
    return { error, logs: logs.join('\n'), writes, progress, toasts: __fake.toasts.slice(t0).map((t) => t.message).join('\n'),
             tail: calls.filter((c) => ['afterFlash', 'dtr', 'rts', 'disconnect'].includes(c.op)).map((c) => c.op + (c.v !== undefined ? `=${c.v}` : (c.mode ? `=${c.mode}` : ''))) };
  }, { esp, hw, opts });
}
const hex = (files) => (files || []).map(([a, f]) => `0x${a.toString(16)}:${f.toString(16).toUpperCase()}`);
const PART = (k) => [FIRST[k], ...Array(31).fill(0x5A)];
const PROGRAMMED = { 0x1000: [0xE9, 0, 0, 0] };

test('wizard.flasher_fake_image_choice what the flasher writes: an update with an unreadable layout stays app-only - unless the app outgrew the legacy slot, then it is refused; a plain flash with an unreadable flash, a blank (0xFF) or a corrupted bootloader writes the full set; a full flash with no bootloader files is refused; the regions go out in ascending address order', async ({ page }) => {
  // flashFirmware's Step 3b and its guards (flasher.js:463-616), comparePartitionTable (246-260). Row flasher.image_choice.
  const served = await setup(page);
  // Update FW on a loader that cannot read flash: the layout cannot be checked, so app-only - while the app still fits.
  let r = await flash(page, { chip: 'ESP32-D0WD-V3', noReadFlash: true }, 1, { appOnly: true });
  expect(r.error).toBe(null);
  expect(r.logs).toContain('Could not read on-board partition table — migration check skipped, flashing app only');
  expect(hex(r.writes[0])).toEqual(['0xe000:FF', '0x10000:A1']);
  // ...the same with a read that fails.
  r = await flash(page, { chip: 'ESP32-D0WD-V3', readFlashThrows: true }, 1, { appOnly: true });
  expect(hex(r.writes[0])).toEqual(['0xe000:FF', '0x10000:A1']);
  // An app bigger than the legacy layout's slot: refused, nothing written.
  served.appBytes = OLD_OTA0 + 1;
  r = await flash(page, { chip: 'ESP32-D0WD-V3', noReadFlash: true }, 1, { appOnly: true });
  expect(r.error).toBe('Cannot verify partition layout and the firmware no longer fits the legacy layout — use the full Flash path');
  expect(r.writes).toEqual([]);
  expect(r.toasts).toContain('Cannot verify partition layout');
  // ...but a readable, matching layout lets that app go app-only.
  r = await flash(page, { chip: 'ESP32-D0WD-V3', flash: { ...PROGRAMMED, 0x8000: PART('ESP32_part') } }, 1, { appOnly: true });
  expect(hex(r.writes[0])).toEqual(['0xe000:FF', '0x10000:A1']);
  served.appBytes = 32;

  // A plain flash: unreadable flash, blank, corrupted - the full set, ascending (boot, partitions, otadata, app).
  const FULL = ['0x1000:C1', '0x8000:B1', '0xe000:FF', '0x10000:A1'];
  r = await flash(page, { chip: 'ESP32-D0WD-V3', noReadFlash: true }, 1);
  expect([r.logs.includes('Cannot read flash — forcing a safe full flash (NVS preserved)'), hex(r.writes[0])]).toEqual([true, FULL]);
  r = await flash(page, { chip: 'ESP32-D0WD-V3', readFlashThrows: true }, 1);
  expect([r.logs.includes('Flash read check failed — forcing a safe full flash (NVS preserved)'), hex(r.writes[0])]).toEqual([true, FULL]);
  r = await flash(page, { chip: 'ESP32-D0WD-V3' }, 1);                              // 0xFF at 0x1000: blank
  expect([r.logs.includes('Blank board detected'), hex(r.writes[0])]).toEqual([true, FULL]);
  r = await flash(page, { chip: 'ESP32-D0WD-V3', flash: { 0x1000: [0xAA, 0, 0, 0] } }, 1);
  expect([r.logs.includes('Corrupted bootloader detected (0xaa) — forcing full flash to recover'), hex(r.writes[0])]).toEqual([true, FULL]);
  // A programmed board: the same table, app only; a table that cannot be read back, full - safe rather than fast.
  r = await flash(page, { chip: 'ESP32-D0WD-V3', flash: { ...PROGRAMMED, 0x8000: PART('ESP32_part') } }, 1);
  expect(hex(r.writes[0]), 'same table: app only').toEqual(['0xe000:FF', '0x10000:A1']);
  r = await flash(page, { chip: 'ESP32-D0WD-V3', flash: PROGRAMMED, readFlashThrowsAt: 0x8000 }, 1);
  expect([r.logs.includes('Could not read partition table — forcing a safe full flash (NVS preserved)'), hex(r.writes[0])]).toEqual([true, FULL]);
  // S3, factory: boot at 0x0 first, NVS and otadata in address order.
  r = await flash(page, { chip: 'ESP32-S3', flashKB: 16384 }, 32, { eraseNvs: true });
  expect(hex(r.writes[0])).toEqual(['0x0:D6', '0x8000:B3', '0x9000:FF', '0xe000:FF', '0x10000:A3']);

  // No bootloader/partition files this run: a board that needs a full flash is refused; one that does not, app-only.
  served.files = ['ESP32', 'ESP32S3'];
  r = await flash(page, { chip: 'ESP32-D0WD-V3' }, 1);                              // blank
  expect(r.error).toContain('A full flash is required (blank / changed / corrupted board) but the bootloader + partition files could not be fetched');
  expect(r.writes).toEqual([]);
  r = await flash(page, { chip: 'ESP32-D0WD-V3', flash: PROGRAMMED }, 1);
  expect([r.error, hex(r.writes[0])]).toEqual([null, ['0xe000:FF', '0x10000:A1']]);
  r = await flash(page, { chip: 'ESP32-D0WD-V3' }, 1, { eraseNvs: true });
  expect(r.error, 'a factory reset needs the full set too').toContain('A full flash is required');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.flasher_fake_fetch getBinaryData takes each file by its suffix from the listing, falls back to the legacy unsized S3 bootloader for 16 MB, says when the 8 MB one is missing (and a full flash of an 8 MB board is then refused), and when the bootloader or partition file cannot be fetched warns loudly and returns app only', async ({ page }) => {
  // getBinaryData (flasher.js:115-191). Row flasher.fetch.
  const served = await setup(page);
  const fetchBins = (type) => page.evaluate(async (type) => {
    const logs = [];
    const t0 = __fake.toasts.length;
    try {
      const fw = await getBinaryData(type, (m) => logs.push(m));
      const first = (b) => (b ? new Uint8Array(b)[0].toString(16).toUpperCase() : null);
      return { images: fw.images.map((i) => [i.address, first(i.buf)]), boot: Object.fromEntries(Object.entries(fw.boot).map(([k, b]) => [k, first(b)])),
               hasBootPart: fw.hasBootPart, logs: logs.join('\n'), toasts: __fake.toasts.slice(t0).map((t) => t.message).join('\n') };
    } catch (e) { return { error: e.message, logs: logs.join('\n'), toasts: __fake.toasts.slice(t0).map((t) => t.message).join('\n') }; }
  }, type);

  let r = await fetchBins('ESP32S3');
  expect([r.images, r.boot, r.hasBootPart]).toEqual([[[0x8000, 'B3'], [0x10000, 'A3']], { '8MB': 'D8', '16MB': 'D6' }, true]);
  // No sized 16 MB bootloader: the legacy unsized one is the 16 MB build.
  served.files = ['ESP32S3', 'ESP32S3_part', 'ESP32S3_boot', 'ESP32S3_boot_8MB'];
  r = await fetchBins('ESP32S3');
  expect(r.boot).toEqual({ '8MB': 'D8', '16MB': 'DA' });
  let f = await flash(page, { chip: 'ESP32-S3', flashKB: 16384 }, 32);
  expect(hex(f.writes[0])[0]).toBe('0x0:DA');
  // No 8 MB bootloader: said, and an 8 MB board that needs a bootloader is refused - an update still goes app-only.
  served.files = ['ESP32S3', 'ESP32S3_part', 'ESP32S3_boot_16MB'];
  r = await fetchBins('ESP32S3');
  expect([r.boot, r.hasBootPart]).toEqual([{ '8MB': null, '16MB': 'D6' }, true]);
  expect(r.logs).toContain('⚠️ No 8 MB ESP32-S3 bootloader available');
  f = await flash(page, { chip: 'ESP32-S3', flashKB: 8192 }, 32);
  expect(f.error).toBe('No 8 MB ESP32-S3 bootloader is available / no matching bootloader — refusing to write bootloader region');
  f = await flash(page, { chip: 'ESP32-S3', flashKB: 8192, flash: { 0x0: [0xE9], 0x8000: PART('ESP32S3_part') } }, 32, { appOnly: true });
  expect([f.error, hex(f.writes[0])]).toEqual([null, ['0xe000:FF', '0x10000:A3']]);
  // No partition file: app only, said loudly (log and toast).
  served.files = ['ESP32', 'ESP32_boot'];
  r = await fetchBins('ESP32');
  expect([r.images, r.boot, r.hasBootPart]).toEqual([[[0x10000, 'A1']], { single: null }, false]);
  expect(r.logs).toContain('⚠️ Could not fetch bootloader/partition files (No file ending with "_ESP32_part.bin" found in Code/bin/) — flashing app only;');
  expect(r.toasts).toContain('partition-table migration check SKIPPED this run');
  expect(r.logs).toContain('Loaded 0 KB (app only)');
  // No app: the fetch fails, said.
  served.files = ['ESP32_part', 'ESP32_boot'];
  r = await fetchBins('ESP32');
  expect(r.error).toBe('GitHub fetch failed: No file ending with "_ESP32.bin" found in Code/bin/');
  expect(r.toasts).toContain('Could not fetch firmware from GitHub');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.flasher_fake_write_retry_reset a write that fails once is retried and succeeds; one that fails twice stops with the recovery steps when the bootloader or partitions were being written (none for an app-only write) and releases the port; progress is weighted by each file\'s size and ends at the total; the board is reset with afterFlash, or a DTR/RTS pulse without it, and the port released', async ({ page }) => {
  // flashFirmware's Steps 4 and 5 (flasher.js:655-765). Row flasher.write_retry_reset.
  await setup(page);
  let r = await flash(page, { chip: 'ESP32-D0WD-V3', writeFails: 1 }, 1);
  expect(r.error).toBe(null);
  expect(r.writes.length).toBe(2);
  expect(r.logs).toContain('Flash failed once (write timeout (fake)) — retrying automatically…');
  expect(r.tail).toEqual(['afterFlash=hard_reset', 'disconnect']);

  r = await flash(page, { chip: 'ESP32-D0WD-V3', writeFails: 2 }, 1);
  expect(r.writes.length).toBe(2);
  expect(r.error).toContain('Flash write failed after retry: write timeout (fake)');
  expect(r.error).toContain('The board may be in an unbootable state. To recover:');
  expect(r.tail, 'the port released, no reset').toEqual(['disconnect']);
  r = await flash(page, { chip: 'ESP32-D0WD-V3', writeFails: 2, flash: { ...PROGRAMMED, 0x8000: PART('ESP32_part') } }, 1);
  expect(hex(r.writes[1])).toEqual(['0xe000:FF', '0x10000:A1']);
  expect(r.error).toBe('Flash write failed after retry: write timeout (fake)');

  // No afterFlash on the loader: the reset is a manual pulse - DTR released, RTS asserted then released.
  r = await flash(page, { chip: 'ESP32-D0WD-V3', noAfterFlash: true }, 1);
  expect(r.error).toBe(null);
  expect(r.tail).toEqual(['dtr=false', 'rts=true', 'rts=false', 'disconnect']);

  // Progress, reported per file in compressed counts: weighted by each file's own size, never past the total, and
  // at the total when done. Files (ascending): boot 32, part 32, otadata 8192, app 32 - total 8288.
  r = await flash(page, { chip: 'ESP32-D0WD-V3', progressSteps: 2 }, 1);
  const total = r.progress[0][1];
  expect(total).toBe(32 + 32 + 0x2000 + 32);
  const done = r.progress.map(([w]) => w);
  // Each half-step adds half of that file's own (uncompressed) size: boot, partitions, otadata (8192), app - then the
  // final report at the total. Halfway through the otadata file: 32 + 32 + 4096.
  expect(done).toEqual([0, 16, 32, 48, 64, 64 + 4096, 64 + 8192, 8256 + 16, 8288, 8288]);
  expect(done.at(-1)).toBe(total);
  expect(page.wizErrors).toEqual([]);
});
