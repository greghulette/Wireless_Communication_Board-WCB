// Needs no board: the board grid and the cards' identity, serial-port and WiFi fields (Wizard/app.js) - medium-risk
// gaps the 2026-10-12 coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in
// docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each test's comment). Fake boards in the real page
// (lib/fake.js): boardPull and boardGo run as on the bench, the handlers are reached through the inputs' own events,
// and only the wire is recorded. Runs standalone, in CI, and under the harness by id (s30_wizard.py). Verify pulls are
// held back where a spec pushes more than once (lib/rescan.js holdVerifyPulls).
//
// Nothing here is a real credential: every SSID and passphrase is a dummy.
const { test, expect } = require('../lib/fixtures');
const { board, pullFake, push, edit } = require('../lib/fake');
const { setup, holdVerifyPulls, toastMark, toastsSince, shown, sent } = require('../lib/rescan');
const sections = (page) => page.evaluate(() => [...document.querySelectorAll('#boards-container [id^="section-board-"]')]
  .map((el) => +el.id.replace('section-board-', '')));

test('wizard.grid_fake_reconcile the board grid is the quantity\'s floor plus the boards the mesh reported plus live connections, in ascending order on the page; shrinking the floor removes an idle section with its config but never a connected board\'s, nor one that is mid-flash with its port closed', async ({ page }) => {
  // desiredBoardNumbers and reconcileBoardGrid (app.js:481-521), renderBoards (524-527), addDiscoveredBoards (643-650).
  // Row grid.reconcile.
  await setup(page);
  await pullFake(page, 5, board([], { wcb: 5, wcbq: 5 }));
  expect(await sections(page)).toEqual([1, 2, 3, 4, 5]);
  // Discovered out of order: placed in order.
  await page.evaluate(() => { addDiscoveredBoards([12]); addDiscoveredBoards([7]); });
  expect(await sections(page)).toEqual([1, 2, 3, 4, 5, 7, 12]);
  expect(await page.evaluate(() => [...document.querySelectorAll('.wcb-nav-btn')].map((b) => b.id.replace('wcb-nav-btn-', '')).map(Number)))
    .toEqual([1, 2, 3, 4, 5, 7, 12]);

  // The floor drops to 2: 3 and 4 go, with their configs; 5 is connected and stays, with its config and baseline.
  await page.evaluate(() => renderBoards(2));
  expect(await sections(page)).toEqual([1, 2, 5, 7, 12]);
  expect(await page.evaluate(() => [3 in boardConfigs, 4 in boardConfigs, boardConfigs[5].wcbNumber, !!boardBaselines[5]]))
    .toEqual([false, false, 5, true]);

  // Mid-flash: the port is esptool's, so the connection reads closed - the section and config stay all the same.
  await page.evaluate(() => { boardConnections[5]._connected = false; _boardFlashing[5] = true; renderBoards(1); });
  expect(await sections(page)).toEqual([1, 5, 7, 12]);
  expect(await page.evaluate(() => [!!boardConnections[5], boardConfigs[5]?.wcbNumber])).toEqual([true, 5]);
  // The flash over and the port still closed: now it goes, connection and config with it.
  await page.evaluate(() => { _boardFlashing[5] = false; renderBoards(1); });
  expect(await sections(page)).toEqual([1, 7, 12]);
  expect(await page.evaluate(() => [5 in boardConnections, 5 in boardConfigs])).toEqual([false, false]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.identity_fake_hw_version a board pulled with HW 0 (unset) pushes no ?HW line - a label edit sends the label alone - nor does picking "Select" on a set board; the LED pin group shows for 3.1/3.2 only, and only a user\'s pick marks the board unsaved', async ({ page }) => {
  // onHWVersionChange (app.js:2318-2342); buildCommandString sends HW only for a known version (parser.js:1309-1314).
  // Row identity.hw_version.
  await setup(page);
  const m = await toastMark(page);
  await pullFake(page, 1, board(['HW,0']));
  await holdVerifyPulls(page);
  expect(await page.evaluate(() => [document.getElementById('b1-hw-version').value, boardConfigs[1].hwVersion])).toEqual(['0', 0]);
  expect(await shown(page, 'b1-unsaved-badge'), 'loaded, not edited').toBe(false);
  expect(await toastsSince(page, m), 'no "Changes pending" for the board on a load').not.toContain('push to WCB 1');
  await edit(page, '#b1-s5-label', 'HIL5');
  expect(await sent(page, 1)).toEqual(['1:?LABEL,S5,HIL5']);

  await setup(page);
  await pullFake(page, 1, board());                                  // HW 2.4
  await holdVerifyPulls(page);
  expect(await shown(page, 'b1-led-pin-group')).toBe(false);
  await edit(page, '#b1-hw-version', '31');
  expect([await shown(page, 'b1-led-pin-group'), await shown(page, 'b1-unsaved-badge')]).toEqual([true, true]);
  expect(await sent(page, 1)).toEqual(['1:?HW,31']);
  await edit(page, '#b1-hw-version', '0');
  expect(await shown(page, 'b1-led-pin-group')).toBe(false);
  expect(await page.evaluate(() => boardConfigs[1].hwVersion)).toBe(0);
  expect((await push(page, 1)).outcome.reason, '"Select" never clears the board\'s version').toBe('no changes to push');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.serial_fake_baud_options the serial table offers S1-S2 every baud and S3-S5 up to 57600, plus the baud a port is configured for marked (!) - a board pulled with S4 at 115200 shows it, and pushed untouched sends nothing; labels are cut at 30', async ({ page }) => {
  // renderSerialTable (app.js:802-830), populateUIFromConfig's serial rows (3375-3385), syncSerialUIToConfig
  // (3310-3330). A pulled baud the table was not built with matched no option, so the select showed blank (the push
  // still fell back to the config's value): fixed 2026-10-12, populateUIFromConfig rebuilds a port's options for its
  // baud (_serialBaudOptions, 790-800). Row serial.table_baud_options.
  await setup(page);
  const opts = (p) => page.evaluate((p) => [...document.getElementById(`b1-s${p}-baud`).options].map((o) => o.textContent), p);
  expect(await opts(2)).toEqual(['110', '300', '600', '1,200', '2,400', '9,600', '14,400', '19,200', '38,400', '57,600', '115,200', '128,000', '256,000']);
  expect(await opts(4)).toEqual(['110', '300', '600', '1,200', '2,400', '9,600', '14,400', '19,200', '38,400', '57,600']);

  await pullFake(page, 1, board(['BAUD,S4,115200', 'BAUD,S2,57600', 'LABEL,S4,Fast']));
  await holdVerifyPulls(page);
  expect(await page.evaluate(() => [document.getElementById('b1-s4-baud').value, document.getElementById('b1-s2-baud').value]))
    .toEqual(['115200', '57600']);
  expect(await opts(4)).toEqual(['110', '300', '600', '1,200', '2,400', '9,600', '14,400', '19,200', '38,400', '57,600', '115,200 (!)']);
  expect(await page.evaluate(() => document.getElementById('b1-s4-baud').selectedOptions[0].textContent)).toBe('115,200 (!)');
  expect((await push(page, 1)).outcome.reason).toBe('no changes to push');
  // A label edit on another port leaves S4 alone.
  await edit(page, '#b1-s3-label', 'HIL3');
  expect(await sent(page, 1)).toEqual(['1:?LABEL,S3,HIL3']);
  // Moved back down, the (!) option stays on offer for the push it came from; the push sends the new rate.
  await edit(page, '#b1-s4-baud', '57600');
  expect(await sent(page, 1)).toEqual(['1:?BAUD,S4,57600', '1:?LABEL,S3,HIL3']);
  // Every port: baud, both broadcast boxes, and a label field the browser cuts at 30.
  expect(await page.evaluate(() => [1, 2, 3, 4, 5].map((p) => ['baud', 'bcin', 'bcout', 'label'].every((f) => document.getElementById(`b1-s${p}-${f}`))
    && document.getElementById(`b1-s${p}-label`).maxLength === 30))).toEqual([true, true, true, true, true]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wifi_fake_ui the WiFi radios: Host AP shows the AP fields and a note naming the mesh channel, Join the join fields and its note, Off hides both; the mode and credentials go into the config, the board is marked unsaved, and the push sends one ?WIFI line for it', async ({ page }) => {
  // updateWifiVisibility, onWifiModeChange, onWifiFieldChange (app.js:936-980); the ?WIFI line (parser.js:1484-1502).
  // A mesh-channel change left an open note naming the old channel: fixed 2026-10-12, onMeshChannelChange refreshes
  // every board's note (1323-1334). Row wifi.ui.
  await setup(page);
  await pullFake(page, 1, board());                   // WIFI,OFF
  await holdVerifyPulls(page);
  const vis = () => Promise.all(['b1-wifi-ap', 'b1-wifi-join', 'b1-wifi-note'].map((id) => shown(page, id)));
  const note = () => page.evaluate(() => document.getElementById('b1-wifi-note').textContent);
  expect(await vis()).toEqual([false, false, false]);

  await edit(page, 'input[name="b1-wifi"][value="ap"]', true);
  expect(await vis()).toEqual([true, false, true]);
  expect(await note()).toContain('Runs on channel 1 — the mesh channel');
  expect(await shown(page, 'b1-unsaved-badge')).toBe(true);
  await edit(page, '#b1-wifi-ap-ssid', 'HIL-AP');
  await edit(page, '#b1-wifi-ap-pass', 'dummy_ap_pass_1');
  expect(await page.evaluate(() => boardConfigs[1].wifi)).toEqual(expect.objectContaining({ mode: 'ap', apSsid: 'HIL-AP', apPass: 'dummy_ap_pass_1' }));
  let r = await push(page, 1, { skipReboot: true });
  expect([r.sent, r.returned], 'WiFi applies on a reboot').toEqual([['1:?WIFI,AP,HIL-AP,dummy_ap_pass_1'], true]);
  // The note follows the mesh channel.
  await edit(page, '#g-meshch', '6');
  expect(await note()).toContain('Runs on channel 6 — the mesh channel');

  await edit(page, 'input[name="b1-wifi"][value="join"]', true);
  expect(await vis()).toEqual([false, true, true]);
  expect(await note()).toContain('must be on channel 6');
  await edit(page, '#b1-wifi-join-ssid', 'HIL-Net');
  await edit(page, '#b1-wifi-join-pass', 'dummy_join_pass_1');
  r = await push(page, 1, { skipReboot: true });
  expect(r.sent.filter((s) => s.includes('WIFI'))).toEqual(['1:?WIFI,JOIN,HIL-Net,dummy_join_pass_1']);

  // A board pulled in AP mode, switched Off: the fields and the note go, and the push sends ?WIFI,OFF alone.
  await setup(page);
  await pullFake(page, 1, board(['WIFI,AP,HIL-AP,dummy_ap_pass_1']));
  await holdVerifyPulls(page);
  expect(await vis()).toEqual([true, false, true]);
  await edit(page, 'input[name="b1-wifi"][value="off"]', true);
  expect(await vis()).toEqual([false, false, false]);
  expect(await note()).toBe('');
  expect(await page.evaluate(() => boardConfigs[1].wifi.mode)).toBe('off');
  expect(await sent(page, 1)).toEqual(['1:?WIFI,OFF']);
  expect(page.wizErrors).toEqual([]);
});
