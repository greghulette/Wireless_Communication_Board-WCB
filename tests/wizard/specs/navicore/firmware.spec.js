// The Firmware tab (docs/hil_plan/NAVICORE.md §2, nct.fw.*, L1): the esptool-js flash and the full wipe, the latest-
// version check, and both OTA state machines, with no board and nothing leaving 127.0.0.1. GitHub's firmware/ listing,
// its raw downloads, CryptoJS and esptool-js are served by page.route (lib/navicore/firmware.js); the esptool-js
// stand-in records what the tool hands it. The emulator speaks ?OTALOCAL on a direct link and plays the tethered
// WCB's ?OTA relay plus NaviCore behind it in via-wcb mode (lib/navicore/ota.js), faults included, and restarts the
// way the board does after a verified image: off the bus, deaf while it boots, back on the other slot.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');
const F = require('../../lib/navicore/firmware');
const { otaOps } = require('../../lib/navicore/ota');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const SET = F.firmwareSet();                       // v0.3.0_011200QOCT26: 20,000 B app, bootloader, partition table
const CONFIG_FS = [0x3D0000, 0x3F0000];            // partitions.csv:19 — /config.json's LittleFS (rc_config.h:2018-2030)

async function openFirmwareTab(page) {
  await page.locator('#btn-hwsetup').click();
  await page.locator('#cfg-tabs .cfg-tab[data-tab="firmware"]').click();
  await expect(page.locator('#cfg-pane-firmware')).toHaveClass(/active/);
}

// The regions one writeFlash call was handed, and the calls after index `from`.
async function flashCalls(page, from = 0) { return (await F.esptool(page)).calls.slice(from); }
const regions = (call) => call.files.map((f) => ({ address: f.address, len: f.len, allFF: f.allFF }));

// After a flash from a live session the tool reopens the same port 3 s later (reopenAfterFlash, index.html:17294);
// the wait is on the page clock, so skip it.
async function skipReconnectWait(page) {
  await expect(page.locator('#fw-log')).toContainText('Reconnecting to the board…', { timeout: 20_000 });
  await page.clock.fastForward(3000);
}

// The OTA sender parked on _otaAwaitMarker with nothing buffered, and still so 150 ms later.
async function otaParked(page) {
  const parked = () => page.evaluate(() => _otaMarkerResolver !== null && _otaPendingMarkers.length === 0);
  for (let i = 0; i < 200; i++) {
    if (await parked()) {
      await new Promise((r) => setTimeout(r, 150));
      if (await parked()) return;
    }
    await new Promise((r) => setTimeout(r, 50));
  }
  throw new Error('the OTA sender never waited on a marker');
}

test.describe('esptool-js flash (GitHub and the CDNs mocked)', () => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.fw_flash_mocked Update Firmware writes the bootloader, partition table and app GitHub lists (never a decoy) with otadata reset, hands esptool the port with no stray writes, and resumes the session on the same port; the Latest check reads the same listing', async ({ page, emu, serial }) => {
    const gh = await F.mockFirmware(page, SET);
    const dialogs = T.answerDialogs(page);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openFirmwareTab(page);
    // "Latest on GitHub" is the app image's name in the SAME listing the flasher writes from (flasher.js:66-79).
    await expect(page.locator('#fw-latest-version')).toHaveText(SET.version);
    await expect(page.locator('#fw-current-version')).toHaveText(emu.version);
    await expect(page.locator('#fw-update-status')).toHaveText(`⬆ Update available — running ${emu.version}, latest ${SET.version}`);
    expect(gh.listings).toEqual(['/repos/greghulette/NaviCore/contents/firmware?ref=main']);

    gh.throttle = 1;                                // GitHub throttles the flasher's listing once: it waits Retry-After
    const ev0 = serial.events.length, rx0 = emu.rx.length;
    await page.locator('#btn-fw-flash').click();
    await expect.poll(async () => (await flashCalls(page)).some((c) => c.op === 'afterFlash'), { timeout: 20_000 }).toBe(true);
    emu.version = SET.version;                      // the board now runs the image it was given
    await skipReconnectWait(page);
    await expect(page.locator('#fw-status')).toHaveText('Flash complete — reconnected.', { timeout: 20_000 });

    // What esptool-js was handed (flasher.js:333-410): otadata erased as 0xFF, then boot, partitions and app, byte-exact.
    const calls = await flashCalls(page);
    const [wf] = calls.filter((c) => c.op === 'writeFlash');
    expect(regions(wf)).toEqual([
      { address: 0xE000, len: 0x2000, allFF: true },
      { address: 0x0, len: SET.boot.length, allFF: false },
      { address: 0x8000, len: SET.part.length, allFF: false },
      { address: 0x10000, len: SET.app.length, allFF: false },
    ]);
    expect(wf.files.slice(1).map((f) => f.fnv)).toEqual([F.fnv(SET.boot), F.fnv(SET.part), F.fnv(SET.app)]);
    for (const f of wf.files) expect(f.md5, `calculateMD5Hash over the region at 0x${f.address.toString(16)}`).toBe(`md5/${f.len}/${f.fnv}`);
    expect(wf).toMatchObject({ flashSize: 'keep', flashMode: 'keep', flashFreq: 'keep', eraseAll: false, compress: true });
    expect(calls.filter((c) => c.op === 'afterFlash')).toEqual([{ op: 'afterFlash', mode: 'hard_reset' }]);
    const win = await page.evaluate(() => /Win/i.test(navigator.platform || ''));
    expect(calls.find((c) => c.op === 'loader')).toMatchObject({ baudrate: win ? 115200 : 460800, romBaudrate: 115200 });
    // Downloads: the app, then the custom bootloader, then the table of the app's own version — no decoy.
    expect(gh.fetched).toEqual([SET.names.app, SET.names.boot, SET.names.part]);
    expect(gh.throttled).toBe(1);
    await expect(page.locator('#fw-log')).toContainText('GitHub throttled firmware list (HTTP 429) — retrying in 1s… (1/3)');
    for (const t of ['Reusing the connected port for flashing', 'Update mode — NVS preserved.', 'OTA boot selector (0xE000, 8 KB) reset to ota_0',
                     '[esptool] Chip is ESP32-S3', 'Update complete. Your saved config was preserved.', 'Reconnected — config session resumed.']) {
      await expect(page.locator('#fw-log')).toContainText(t);
    }
    await expect(page.locator('#fw-progress-pct')).toHaveText('100%');

    // The handover: the session's STOP_MONITOR, its close, a warm-up open/close, esptool's own open/close, then the
    // reconnect — and not one byte written to the port in between.
    const ev = serial.events.slice(ev0);
    const firstClose = ev.findIndex((e) => e.op === 'close');
    const opens = ev.map((e, i) => (e.op === 'open' ? i : -1)).filter((i) => i >= 0);
    expect(opens.length).toBeGreaterThanOrEqual(3);
    const between = ev.slice(firstClose, opens[2]);
    expect(between.filter((e) => e.op === 'write'), 'writes while esptool-js owned the port').toEqual([]);
    expect(between.map((e) => e.op).filter((o) => o === 'open' || o === 'close')).toEqual(['close', 'open', 'close', 'open', 'close']);
    expect(opens.slice(0, 3).map((i) => ev[i].opts.baudRate)).toEqual([115200, 115200, 115200]);
    const sent = emu.rx.slice(rx0).map((r) => (r.json ? r.json.type : r.line)).filter((t) => !/^(GET_MESH_STATS|GET_WCB_STATUS)$/.test(t));
    expect(sent.slice(0, 3)).toEqual(['STOP_MONITOR', 'PING', 'GET_CONFIG']);

    // The session is back, on the new version, with nothing to save and no close prompt (_postFlashReload).
    const s = await T.state(page);
    expect([s.connected, s.configLoaded, s.isMonitoring, s.pendingSave]).toEqual([true, true, true, false]);
    await expect(page.locator('#fw-current-version')).toHaveText(SET.version);
    await expect(page.locator('#fw-update-status')).toHaveText('✓ Up to date');
    await expect(page.locator('#btn-fw-flash')).toHaveText('⬆ Update Firmware');
    for (const id of ['#btn-fw-flash', '#btn-fw-wipe', '#btn-connect']) await expect(page.locator(id)).toBeEnabled();
    expect(await page.evaluate(() => _configUnsaved())).toBe(false);
    await page.evaluate(() => closeHwSetup());
    expect(dialogs).toEqual([]);
    T.expectNoPageErrors(page);
  });

  test('nctool.fw_flash_partial_refused a firmware set with only one of the bootloader / partition-table pair is refused before the bootloader is touched (CI\'s stock _boot.bin never stands in); with neither, the flash is app-only', async ({ page, emu, serial }) => {
    const gh = await F.mockFirmware(page, SET, { omit: ['part'] });
    await T.openTool(page);
    await page.evaluate(() => window.__hilSerial.grant(true));      // a port this origin was granted earlier
    await openFirmwareTab(page);
    const flash = async () => {
      const n = (await flashCalls(page)).length;
      await page.locator('#btn-fw-flash').click();
      await expect(page.locator('#fw-status')).toHaveText(/^Flash (failed|complete)\.$/, { timeout: 20_000 });
      await expect(page.locator('#btn-fw-flash')).toHaveText('⬆ Update Firmware');
      return (await flashCalls(page)).slice(n);
    };
    // The table of the app's own version is missing (another build's is listed): refused.
    let calls = await flash();
    await expect(page.locator('#fw-status')).toHaveText('Flash failed.');
    await expect(page.locator('#fw-log')).toContainText(`✖ Firmware download failed: Incomplete firmware on GitHub: the partition table (${SET.names.part}) is missing while its pair is present.`);
    expect(calls.map((c) => c.op), 'esptool-js calls').toEqual([]);
    // The custom bootloader is missing (CI's stock _boot.bin is listed): refused, and the stock one is never fetched.
    gh.omit = ['boot'];
    calls = await flash();
    await expect(page.locator('#fw-status')).toHaveText('Flash failed.');
    await expect(page.locator('#fw-log')).toContainText(`the custom bootloader (${F.BOOT_NAME}) is missing while its pair is present`);
    expect(calls.map((c) => c.op)).toEqual([]);
    expect(gh.fetched.filter((n) => /_boot\.bin$|RC-Controller|v0\.1\.0/.test(n)), 'decoys downloaded').toEqual([]);
    // Neither: an app-only flash, otadata still reset.
    gh.omit = ['boot', 'part'];
    calls = await flash();
    await expect(page.locator('#fw-status')).toHaveText('Flash complete.');
    await expect(page.locator('#fw-log')).toContainText('Note: bootloader/partition files not on GitHub — flashing app only.');
    const [wf] = calls.filter((c) => c.op === 'writeFlash');
    expect(regions(wf)).toEqual([{ address: 0xE000, len: 0x2000, allFF: true }, { address: 0x10000, len: SET.app.length, allFF: false }]);
    expect(await page.evaluate(() => window.__hilSerial.log.filter((e) => e.op === 'requestPort').length), 'port pickers shown').toBe(0);
    expect(serial.events.filter((e) => e.op === 'write'), 'bytes written to the board outside esptool-js').toEqual([]);
    T.expectNoPageErrors(page);
  });

  test('nctool.fw_refused_flash_keeps_session (should) an Update refused before anything is written (here: an incomplete firmware set on GitHub) leaves the live session connected', async ({ page, emu }) => {
    await F.mockFirmware(page, SET, { omit: ['part'] });
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openFirmwareTab(page);
    await page.locator('#btn-fw-flash').click();
    await expect(page.locator('#fw-status')).toHaveText('Flash failed.', { timeout: 20_000 });
    expect((await flashCalls(page)).filter((c) => c.op === 'writeFlash')).toEqual([]);
    await expect.poll(async () => (await T.state(page)).connected, { timeout: 10_000, message: 'connected again after the refusal' }).toBe(true);
    T.expectNoPageErrors(page);
  });
});

test.describe('Full Wipe & Flash (not connected; the port was granted earlier)', () => {
  test.use({ emuOptions: { config: BENCH }, serialOptions: { granted: true } });

  test('nctool.fw_wipe_regions Full Wipe asks first (Cancel writes nothing), then erases NVS and otadata as 0xFF regions ahead of boot, partitions and app on the granted port without a picker, and writes nothing at or above app1 — the config LittleFS and the clips are untouched', async ({ page, serial }) => {
    await F.mockFirmware(page, SET);
    const dialogs = T.answerDialogs(page, [false, true]);
    await T.openTool(page);
    await openFirmwareTab(page);
    await page.locator('#btn-fw-wipe').click();
    expect(dialogs.map((d) => d.type)).toEqual(['confirm']);
    expect(await page.evaluate(() => !!window.__fakeEsptool), 'esptool-js loaded after Cancel').toBe(false);
    expect(serial.events.filter((e) => e.op === 'open'), 'port opened after Cancel').toEqual([]);

    await page.locator('#btn-fw-wipe').click();
    await expect(page.locator('#fw-status')).toHaveText('Flash complete.', { timeout: 20_000 });
    const [wf] = (await flashCalls(page)).filter((c) => c.op === 'writeFlash');
    expect(regions(wf)).toEqual([
      { address: 0x9000, len: 0x5000, allFF: true },
      { address: 0xE000, len: 0x2000, allFF: true },
      { address: 0x0, len: SET.boot.length, allFF: false },
      { address: 0x8000, len: SET.part.length, allFF: false },
      { address: 0x10000, len: SET.app.length, allFF: false },
    ]);
    const top = Math.max(...wf.files.map((f) => f.address + f.len));
    expect(top, 'highest byte written').toBeLessThan(0x1F0000);   // app1, the config FS (0x3D0000), clips (0x400000)
    expect(wf.files.filter((f) => f.address < CONFIG_FS[1] && f.address + f.len > CONFIG_FS[0])).toEqual([]);
    await expect(page.locator('#fw-log')).toContainText('Using the previously-authorized USB port…');
    await expect(page.locator('#fw-log')).toContainText('NVS (0x9000, 20 KB) and OTA data (0xE000, 8 KB) will be erased.');
    await expect(page.locator('#fw-log')).toContainText('Click Connect on the top bar to start a config session.');
    expect(await page.evaluate(() => window.__hilSerial.log.filter((e) => e.op === 'requestPort').length)).toBe(0);
    await expect(page.locator('#btn-fw-wipe')).toHaveText('⚠ Full Wipe & Flash');
    await expect(page.locator('#btn-fw-wipe')).toBeEnabled();
    expect(serial.events.filter((e) => e.op === 'write')).toEqual([]);
    T.expectNoPageErrors(page);
  });

  test('nctool.fw_wipe_text (should) nothing the Full Wipe shows the user promises the saved configuration is erased, since the flasher never writes the config LittleFS (D-NC34)', async ({ page }) => {
    await F.mockFirmware(page, SET);
    const dialogs = T.answerDialogs(page, [true]);
    await T.openTool(page);
    await openFirmwareTab(page);
    const shown = [['button title', await page.locator('#btn-fw-wipe').getAttribute('title')]];
    for (const t of await page.locator('#cfg-pane-firmware .noconn').allTextContents()) shown.push(['Firmware tab', t]);
    await page.locator('#btn-fw-wipe').click();
    await expect(page.locator('#fw-status')).toHaveText('Flash complete.', { timeout: 20_000 });
    shown.push(['confirm', dialogs[0].message], ['flash log', await page.locator('#fw-log').textContent()]);
    // If a future flasher erased the config FS, saying so would be true; it does not today.
    const [wf] = (await flashCalls(page)).filter((c) => c.op === 'writeFlash');
    const configErased = wf.files.some((f) => f.address < CONFIG_FS[1] && f.address + f.len > CONFIG_FS[0]);
    const ERASE = /\b(eras\w*|wip\w*|lose|lost|delet\w*|factory-fresh|clear\w*)\b/i;
    const CONFIG = /\b(config\w*|settings)\b/i;
    const NEGATED = /\b(not|never|no|kept|keeps?|preserv\w*|surviv\w*|intact|untouched)\b/i;
    const claims = [];
    for (const [where, text] of shown) {
      for (const s of String(text).replace(/\s+/g, ' ').split(/(?<=[.!?])\s+/)) {
        if (ERASE.test(s) && CONFIG.test(s) && !NEGATED.test(s)) claims.push(`${where}: ${s.trim()}`);
      }
    }
    expect(configErased).toBe(false);
    expect(claims, 'texts that promise the saved configuration is erased').toEqual([]);
  });
});

test.describe('OTA over USB (?OTALOCAL)', () => {
  test.use({ emuOptions: { config: BENCH, otaNewVersion: SET.version, otaReboot: { afterMs: 2000, goneMs: 3000, bootMs: 800 } } });

  test('nctool.ota_usb_state_machine the windowed sender survives coalesced and split markers and a lost last ACK (stall, rewind, the resend\'s NAK names the end), skips late cursor markers ahead of END,OK, locks the port while it streams, writes the image byte-exact, and reconnects across the restart', async ({ page, emu }) => {
    const gh = await F.mockFirmware(page, SET);
    const dialogs = T.answerDialogs(page);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openFirmwareTab(page);
    await expect(page.locator('#fw-latest-version')).toHaveText(SET.version);
    const size = SET.app.length, K = 1024;
    emu.otaFault = {
      coalesce: 3,                                   // three markers to a read
      split: true,                                   // the first ACK arrives cut mid-marker
      holdFrom: 14 * K,                              // the board's answers from 14 KB on wait (mid-stream checks below)
      lostAck: new Set([size]),                      // the last ACK never arrives
      stale: [`[OTA:NAK,${size}]`, `[OTA:ACK,${size}]`],   // late cursor markers land after END, before its verdict
    };
    await page.locator('#btn-fw-ota').click();
    await expect.poll(() => (emu._otaHeld || []).length, { timeout: 20_000 }).toBeGreaterThan(0);
    // Mid-stream: the OTA owns the port (_otaLockPortOwnerButtons, index.html:17388-17392).
    await expect(page.locator('#btn-fw-ota')).toHaveText('⚡ Updating over USB…');
    for (const id of ['#btn-fw-ota', '#btn-fw-flash', '#btn-fw-wipe', '#btn-connect']) await expect(page.locator(id)).toBeDisabled();
    await expect(page.locator('#fw-status')).toHaveText('OTA: streaming…');
    emu.otaRelease();
    // The last ACK is lost: the sender waits out its 10 s marker timeout (skipped here), rewinds to its cursor and resends;
    // the board NAKs the resend with the end of the image, which completes the stream.
    await expect.poll(() => otaOps(emu, 'lostAck').length, { timeout: 20_000 }).toBe(1);
    await otaParked(page);
    await page.clock.fastForward(10_000);
    await expect(page.locator('#fw-status')).toHaveText('OTA complete — reconnected. ✓', { timeout: 30_000 });

    expect(emu.otaImage && emu.otaImage.equals(SET.app), 'the image the board verified is the one GitHub served').toBe(true);
    expect(emu.lines(/^\?OTALOCAL,BEGIN,/)).toEqual([`?OTALOCAL,BEGIN,${size},1`]);
    expect(emu.lines(/^\?OTALOCAL,END$/)).toHaveLength(1);
    expect(emu.lines(/^\?OTALOCAL,ABORT/)).toEqual([]);
    const data = otaOps(emu, 'data');
    expect(data.every((d) => d.offset % K === 0 && d.len <= K)).toBe(true);
    expect(Math.max(...data.map((d) => d.offset - d.cursor)), 'furthest ahead of the board\'s cursor').toBeLessThanOrEqual(7 * K);
    expect(data.length, 'DATA lines for a 20-chunk image').toBeLessThanOrEqual(Math.ceil(size / K) + 8);
    expect(otaOps(emu, 'nak').map((n) => n.cursor), 'the resend after the stall').toContain(size);
    // Markers reach the terminal whole, even the one that arrived in two reads.
    expect(await T.termLines(page, /^< \[OTA:ACK,1024\]$/)).toHaveLength(1);
    expect(await T.termLines(page, /^< \[OTA:AC$/)).toEqual([]);
    // Across the restart: the first reopen finds no device, a later one the new image.
    expect((await T.termLines(page, /\[reconnect\] reconnect attempt \d+: open\(\) failed/)).length).toBeGreaterThan(0);
    expect(emu.reboots).toBe(1);
    expect(emu.otaSlot.running).toBe('app1');
    await expect(page.locator('#fw-current-version')).toHaveText(SET.version);
    await expect(page.locator('#fw-update-status')).toHaveText('✓ Up to date');
    await expect(page.locator('#fw-log')).toContainText('Reconnected to the updated board.');
    await expect(page.locator('#btn-fw-ota')).toHaveText('⚡ Update over USB (OTA)');
    for (const id of ['#btn-fw-ota', '#btn-fw-flash', '#btn-fw-wipe', '#btn-connect']) await expect(page.locator(id)).toBeEnabled();
    const s = await T.state(page);
    expect([s.connected, s.configLoaded, s.pendingSave]).toEqual([true, true, false]);
    expect(await page.evaluate(() => _configUnsaved())).toBe(false);
    await page.evaluate(() => closeHwSetup());
    expect(dialogs).toEqual([]);
    expect(gh.fetched).toEqual([SET.names.app, SET.names.boot, SET.names.part]);
    T.expectNoPageErrors(page);
  });

  test('nctool.ota_usb_failures a rejected BEGIN and a failed verify are reported as failures and ABORTed, and the board keeps running its image on the live session', async ({ page, emu }) => {
    await F.mockFirmware(page, SET);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openFirmwareTab(page);
    const version = emu.version;
    emu.otaFault = { beginErr: true };
    await page.locator('#btn-fw-ota').click();
    await expect(page.locator('#fw-status')).toHaveText('OTA failed: board rejected BEGIN — [OTA:BEGIN,ERR,0]', { timeout: 20_000 });
    await expect.poll(() => emu.lines(/^\?OTALOCAL,ABORT$/).length).toBe(1);
    expect(emu.lines(/^\?OTALOCAL,DATA,/)).toEqual([]);
    await expect(page.locator('#btn-fw-ota')).toBeEnabled();

    emu.otaFault = { verify: false };
    await page.locator('#btn-fw-ota').click();
    await expect(page.locator('#fw-status')).toHaveText('OTA failed: verify/finalize failed — [OTA:END,ERR]', { timeout: 30_000 });
    await expect.poll(() => emu.lines(/^\?OTALOCAL,ABORT$/).length).toBe(2);
    expect(emu.lines(/^\?OTALOCAL,END$/)).toHaveLength(1);
    await expect(page.locator('#fw-log')).toContainText('OTA failed: verify/finalize failed');
    expect([emu.reboots, emu.version, emu.otaSlot.running]).toEqual([0, version, 'app0']);
    const s = await T.state(page);
    expect([s.connected, s.isMonitoring]).toEqual([true, true]);
    for (const id of ['#btn-fw-ota', '#btn-fw-flash', '#btn-connect']) await expect(page.locator(id)).toBeEnabled();
    T.expectNoPageErrors(page);
  });

  test('nctool.ota_usb_lost_chunk (should) a lost DATA line is resent once the chunks behind it are NAKed, not after the 10 s stall timeout', async ({ page, emu }) => {
    await F.mockFirmware(page, SET);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openFirmwareTab(page);
    emu.otaFault = { lostData: new Set([3 * 1024]) };
    await page.locator('#btn-fw-ota').click();
    await expect.poll(() => otaOps(emu, 'lost').length, { timeout: 20_000 }).toBe(1);
    const lostAt = Date.now();
    await expect.poll(() => otaOps(emu, 'data').some((d) => d.offset === 3 * 1024), { timeout: 20_000 }).toBe(true);
    expect(Date.now() - lostAt, 'ms from the lost line to its resend').toBeLessThan(5000);
  });
});

test.describe('OTA over WCB (?OTA through the tethered relay)', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb', relayId: 1, otaNewVersion: SET.version, otaReboot: { goneMs: 800, bootMs: 400 } } });

  test('nctool.ota_wcb_state_machine 192-B CRC-suffixed chunks, eight in flight, one session id; a line the relay\'s CRC drops is rewound and resent, other targets\' and sessions\' ACKs are ignored, and only the offset-0 END answer counts; the relay stays connected', async ({ page, emu, serial }) => {
    await F.mockFirmware(page, SET);
    await T.openTool(page);
    await T.connectViaWcb(page);
    await openFirmwareTab(page);
    expect(await T.fwButtons(page)).toMatchObject({ 'btn-fw-ota': false, 'btn-fw-ota-wcb': true, 'btn-fw-flash': false, 'btn-fw-wipe': false });
    const size = SET.app.length, C = 192;
    emu.otaFault = {
      mangle: new Set([5 * C]),                      // a serial overrun eats 4 base64 characters: the relay's CRC drops it
      lostAck: new Set([12 * C]),                    // one mesh ACK lost mid-stream
      foreign: (s) => [`[OTA:ACK,7,${s},${40 * C},0]`, `[OTA:ACK,20,${(s % 0x7FFF) + 1},${60 * C},0]`],
      stale: (s) => [`[OTA:ACK,20,${s},${size},0]`],   // a cursor ACK of this session, status 0, landing on END's wait
    };
    const ev0 = serial.events.length;
    await page.locator('#btn-fw-ota-wcb').click();
    await expect(page.locator('#fw-status')).toHaveText('OTA over WCB complete ✓ — target rebooting.', { timeout: 60_000 });

    expect(emu.otaImage && emu.otaImage.equals(SET.app), 'the image NaviCore verified is the one GitHub served').toBe(true);
    const lines = emu.lines(/^\?OTA,/);
    const m = /^\?OTA,BEGIN,20,(\d+),(\d+),1$/.exec(lines[0]);
    expect(m, lines[0]).not.toBeNull();
    const session = +m[1];
    expect(+m[2]).toBe(size);
    expect(lines.filter((l) => !l.startsWith(`?OTA,DATA,20,${session},`) && !/^\?OTA,(BEGIN|END),20,/.test(l))).toEqual([]);
    expect(lines.filter((l) => /^\?OTA,END,/.test(l))).toEqual([`?OTA,END,20,${session}`]);
    expect(lines.filter((l) => /^\?OTA,DATA,/.test(l)).every((l) => /^\?OTA,DATA,20,\d+,\d+:[0-9A-F]{8},[A-Za-z0-9+/=]+$/.test(l))).toBe(true);
    const fwd = otaOps(emu, 'forward').filter((f) => f.type === 'DATA');
    expect(fwd.every((f) => f.offset % C === 0 && f.len <= C)).toBe(true);
    expect(otaOps(emu, 'crcDrop').map((d) => d.offset)).toEqual([5 * C]);
    expect(fwd.filter((f) => f.offset === 5 * C), 'the dropped chunk, forwarded once resent').toHaveLength(1);
    const data = otaOps(emu, 'data').filter((d) => d.inSession);
    expect(Math.max(...data.map((d) => d.offset - d.cursor)), 'furthest ahead of the target\'s cursor').toBeLessThanOrEqual(7 * C);
    await expect(page.locator('#fw-log')).toContainText(`Verified. RC #20 is rebooting into the new firmware`);
    expect(otaOps(emu, 'abort')).toEqual([]);
    // The relay never rebooted and never left: the Via-WCB session is still up; NaviCore restarted behind it.
    expect(serial.events.slice(ev0).filter((e) => e.op === 'close' || e.op === 'open')).toEqual([]);
    const st = await T.state(page);
    expect([st.sharedActive, st.viaWcbActive]).toEqual([true, true]);
    await expect.poll(() => emu.reboots, { timeout: 5_000 }).toBe(1);
    await expect(page.locator('#btn-fw-ota-wcb')).toHaveText('📡 Update over WCB (OTA)');
    await expect(page.locator('#btn-fw-ota-wcb')).toBeEnabled();
    T.expectNoPageErrors(page);
  });

  test('nctool.ota_wcb_failures a BEGIN the target rejects and an END it cannot verify are reported as failures and ABORTed on the session; the target keeps its image', async ({ page, emu }) => {
    await F.mockFirmware(page, SET);
    await T.openTool(page);
    await T.connectViaWcb(page);
    await openFirmwareTab(page);
    const version = emu.version;
    emu.otaFault = { beginErr: true };
    await page.locator('#btn-fw-ota-wcb').click();
    await expect(page.locator('#fw-status')).toHaveText('OTA over WCB failed: target rejected BEGIN (chip family / no OTA slot / offline / unreachable?)', { timeout: 20_000 });
    await expect.poll(() => emu.lines(/^\?OTA,ABORT,20,\d+$/).length).toBe(1);
    const s1 = /^\?OTA,BEGIN,20,(\d+),/.exec(emu.lines(/^\?OTA,BEGIN,/)[0])[1];
    expect(emu.lines(/^\?OTA,ABORT,/)).toEqual([`?OTA,ABORT,20,${s1}`]);
    expect(emu.lines(/^\?OTA,DATA,/)).toEqual([]);

    emu.otaFault = { verify: false };
    await page.locator('#btn-fw-ota-wcb').click();
    await expect(page.locator('#fw-status')).toHaveText('OTA over WCB failed: target verify/finalize failed', { timeout: 60_000 });
    await expect.poll(() => emu.lines(/^\?OTA,ABORT,20,\d+$/).length).toBe(2);
    expect([emu.reboots, emu.version, emu.otaSlot.running]).toEqual([0, version, 'app0']);
    await expect(page.locator('#btn-fw-ota-wcb')).toBeEnabled();
    T.expectNoPageErrors(page);
  });
});
