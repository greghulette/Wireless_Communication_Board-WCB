// Needs no board: the audio devices on a board card - MP3 Trigger, HCR Vocalizer, DFPlayer (Wizard/app.js) - the
// medium-risk gaps the 2026-10-12 coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in
// docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each test's comment): enabling a device and its
// Local / Remote mode, the port pickers, the clamps and the remote host picker. Fake boards in the real page
// (lib/fake.js): boardPull and boardGo run as on the bench, the handlers are reached through the inputs' own events,
// and only the wire is recorded. Runs standalone, in CI, and under the harness by id (s30_wizard.py). Verify pulls are
// held back where a spec pushes more than once (lib/rescan.js holdVerifyPulls).
const { test, expect } = require('../lib/fixtures');
const { board, pullFake, push, edit } = require('../lib/fake');
const { setup, holdVerifyPulls, options, shown, sent } = require('../lib/rescan');

// Set an input's value without firing anything, as a value typed and not yet left.
const typeOnly = (page, id, value) => page.evaluate(({ id, value }) => { document.getElementById(id).value = value; }, { id, value });
const visibleOptions = (page, id) => page.evaluate((id) => [...document.getElementById(id).options].filter((o) => !o.hidden).map((o) => o.value), id);

test('wizard.audio_fake_toggle_and_mode an audio device\'s enable box shows its block and starts it Local; a Local port is claimed and pushed (?HCR,PORT,S4:9600 and ?HCR,POLL); unchecked it pushes ?HCR,CLEAR; Remote picks a host and pushes ?HCR,REMOTE,W<n>, and leaving Remote pushes ?HCR,REMOTE,OFF before the clear', async ({ page }) => {
  // _audioMode, onAudioToggle (app.js:2485-2502), onHCRChange (2894-2940), onHCRPortChange (2942-2973), onMP3Change
  // (2505-2550); the HCR/MP3 lines (parser.js:1505-1538). Row audio.toggle_and_mode.
  await setup(page);
  await pullFake(page, 1, board());
  await holdVerifyPulls(page);
  expect(await shown(page, 'b1-hcr-block')).toBe(false);
  await edit(page, '#b1-hcr-enable', true);
  expect(await shown(page, 'b1-hcr-block')).toBe(true);
  expect(await page.evaluate(() => [document.querySelector('input[name="b1-hcr"]:checked').value,
                                    ...['port', 'baud', 'poll', 'remote'].map((w) => document.getElementById(`b1-hcr-${w}-wrap`).style.display !== 'none')]))
    .toEqual(['local', true, true, true, false]);
  await edit(page, '#b1-hcr-port', '4');
  expect(await page.evaluate(() => [boardConfigs[1].hcr.enabled, boardConfigs[1].hcr.port, boardConfigs[1].serialPorts[3].claimedBy?.type,
                                    document.getElementById('b1-s4-claim').textContent])).toEqual([true, 4, 'hcr', 'Managed by HCR Vocalizer']);
  expect(await shown(page, 'b1-unsaved-badge')).toBe(true);
  expect(await sent(page, 1)).toEqual(['1:?HCR,PORT,S4:9600', '1:?HCR,POLL,10']);
  // Unchecked again: the block hides, the port is free, and against a board with no HCR nothing is left to push.
  await edit(page, '#b1-hcr-enable', false);
  expect(await shown(page, 'b1-hcr-block')).toBe(false);
  expect(await page.evaluate(() => [boardConfigs[1].hcr.enabled, boardConfigs[1].hcr.port, boardConfigs[1].serialPorts[3].claimedBy])).toEqual([false, null, null]);
  expect((await push(page, 1)).outcome.reason).toBe('no changes to push');

  // A board pulled with an HCR on S4: unchecked, the push clears it.
  await setup(page);
  await pullFake(page, 1, board(['HCR,PORT,S4:9600', 'HCR,POLL,10']));
  await holdVerifyPulls(page);
  expect(await page.evaluate(() => [document.getElementById('b1-hcr-enable').checked, document.getElementById('b1-hcr-port').value])).toEqual([true, '4']);
  await edit(page, '#b1-hcr-enable', false);
  expect(await sent(page, 1)).toEqual(['1:?HCR,CLEAR']);
  // Checked again and switched to Remote: a host is picked (never this board), and the push routes there.
  await edit(page, '#b1-hcr-enable', true);
  await edit(page, 'input[name="b1-hcr"][value="remote"]', true);
  expect(await page.evaluate(() => ['port', 'remote'].map((w) => document.getElementById(`b1-hcr-${w}-wrap`).style.display !== 'none'))).toEqual([false, true]);
  expect(await page.evaluate(() => [document.getElementById('b1-hcr-remote-wcb').value, boardConfigs[1].hcr.remoteWCB, boardConfigs[1].hcr.port]))
    .toEqual(['2', 2, null]);
  expect(await sent(page, 1)).toEqual(['1:?HCR,REMOTE,W2']);

  // A board pulled routing ;H to WCB2: unchecked, the route is dropped and the device cleared.
  await setup(page);
  await pullFake(page, 1, board(['HCR,REMOTE,W2']));
  await holdVerifyPulls(page);
  expect(await page.evaluate(() => [document.querySelector('input[name="b1-hcr"]:checked').value, document.getElementById('b1-hcr-remote-wcb').value]))
    .toEqual(['remote', '2']);
  await edit(page, '#b1-hcr-enable', false);
  expect(await sent(page, 1)).toEqual(['1:?HCR,REMOTE,OFF', '1:?HCR,CLEAR']);

  // The MP3 Trigger the same way: enabled, it starts Local on the first free port.
  await setup(page);
  await pullFake(page, 1, board());
  await holdVerifyPulls(page);
  await edit(page, '#b1-mp3-enable', true);
  expect(await page.evaluate(() => [document.querySelector('input[name="b1-mp3"]:checked').value, boardConfigs[1].mp3.enabled, boardConfigs[1].mp3.port]))
    .toEqual(['local', true, 1]);
  expect(await sent(page, 1)).toEqual(['1:?MP3,S1:9600:V0']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.audio_fake_port_dropdowns a device\'s port picker offers only free ports and its own, a port change refreshes every other picker, and an HCR moved onto S3-S5 has its bauds above 57600 hidden and its 115200 clamped to 57600 - in the HCR picker, the config and the port\'s own serial row', async ({ page }) => {
  // updateMP3PortDropdown (app.js:2624-2643), updateHCRPortDropdown (2853-2869), updateDFPPortDropdown (2777-2796),
  // onHCRPortChange (2942-2973), _hcrApplyBaudCap (2828-2851), onMP3PortChange (2552-2598). Enabling a device (or
  // switching it Local / Remote) refreshed only its own picker, so the MP3 taking S3 left the HCR picker offering S3:
  // fixed 2026-10-12, onMP3Change / onHCRChange / onDFPChange refresh every picker (_refreshDevicePortPickers,
  // 2474-2480). Row audio.port_dropdowns.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S1,57600', 'MAESTRO,M1:W1S1:57600', 'BAUD,S2,115200', 'HCR,PORT,S2:115200', 'HCR,POLL,10']));
  await holdVerifyPulls(page);
  expect(await options(page, 'b1-hcr-port'), 'its own S2 kept, S1 a Maestro\'s').toEqual(['2', '3', '4', '5']);
  // Enabled, the MP3 is offered the free ports and takes the first; the HCR picker stops offering it.
  await edit(page, '#b1-mp3-enable', true);
  expect([await options(page, 'b1-mp3-port'), await page.evaluate(() => boardConfigs[1].mp3.port)]).toEqual([['3', '4', '5'], 3]);
  expect(await options(page, 'b1-hcr-port')).toEqual(['2', '4', '5']);
  await edit(page, '#b1-dfp-enable', true);
  expect([await options(page, 'b1-dfp-port'), await page.evaluate(() => boardConfigs[1].dfp.port)]).toEqual([['4', '5'], 4]);
  expect(await options(page, 'b1-hcr-port')).toEqual(['2', '5']);
  expect(await options(page, 'b1-mp3-port')).toEqual(['3', '5']);
  expect(await visibleOptions(page, 'b1-hcr-baud')).toEqual(['9600', '19200', '38400', '57600', '115200']);

  // The HCR moved to S5 at 115200: clamped to 57600 in its picker, its config and S5's own row; S2 offered again.
  await edit(page, '#b1-hcr-port', '5');
  expect(await page.evaluate(() => [boardConfigs[1].hcr.port, boardConfigs[1].hcr.baud, document.getElementById('b1-hcr-baud').value,
                                    boardConfigs[1].serialPorts[4].baud, document.getElementById('b1-s5-baud').value]))
    .toEqual([5, 57600, '57600', 57600, '57600']);
  expect(await visibleOptions(page, 'b1-hcr-baud')).toEqual(['9600', '19200', '38400', '57600']);
  expect(await options(page, 'b1-mp3-port'), 'S2 freed for the others, S5 taken').toEqual(['2', '3']);
  expect(await options(page, 'b1-dfp-port')).toEqual(['2', '4']);
  const r = await sent(page, 1);
  expect(r).toEqual(expect.arrayContaining(['1:?HCR,PORT,S5:57600', '1:?MP3,S3:9600:V0', '1:?DFP,S4:9600:V15']));
  expect(r.filter((s) => /BAUD,S5,115200|S5:115200/.test(s)), 'no 115200 anywhere on S5').toEqual([]);
  // Back on a hardware port, every baud is offered again.
  await edit(page, '#b1-hcr-port', '2');
  expect(await visibleOptions(page, 'b1-hcr-baud')).toEqual(['9600', '19200', '38400', '57600', '115200']);

  // An MP3 enabled on a board whose S1 hosts a WLED: offered S2-S5, and the WLED row no longer offers the MP3's port.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S1,115200', 'WLED,1:W1S1:115200']));
  await edit(page, '#b1-mp3-enable', true);
  expect(await options(page, 'b1-mp3-port')).toEqual(['2', '3', '4', '5']);
  await edit(page, '#b1-mp3-port', '3');
  expect(await page.evaluate(() => [...document.querySelector('#b1-wled-tbody tr [id$="-port"]').options].map((o) => o.value)))
    .not.toContain('3');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.audio_fake_clamps the MP3 volume clamps to 0-64, the DFPlayer volume to 0-30 and the HCR poll to 0 or 3-3600 (shown in the field) when edited, and again at push time for a value typed and not yet left', async ({ page }) => {
  // onMP3VolChange (app.js:2607-2615), onDFPVolChange (2760-2768), onHCRPollChange (2982-2993) on edit; syncMP3ToConfig
  // (2645-2671), syncDFPToConfig (2798-2822), syncHCRToConfig (2995-3017) at push time. Row audio.clamps.
  await setup(page);
  await pullFake(page, 1, board(['MP3,S2:9600:V10', 'HCR,PORT,S4:9600', 'HCR,POLL,10', 'DFP,S3:9600:V20']));
  await holdVerifyPulls(page);
  await edit(page, '#b1-mp3-vol', '99');
  await edit(page, '#b1-hcr-poll', '1');
  await edit(page, '#b1-dfp-vol', '45');
  expect(await page.evaluate(() => [boardConfigs[1].mp3.volume, boardConfigs[1].hcr.poll, boardConfigs[1].dfp.volume,
                                    document.getElementById('b1-hcr-poll').value])).toEqual([64, 3, 30, '3']);
  expect(await sent(page, 1)).toEqual(['1:?MP3,S2:9600:V64', '1:?HCR,PORT,S4:9600', '1:?HCR,POLL,3', '1:?DFP,S3:9600:V30']);
  await edit(page, '#b1-hcr-poll', '9999');
  expect(await page.evaluate(() => [boardConfigs[1].hcr.poll, document.getElementById('b1-hcr-poll').value])).toEqual([3600, '3600']);
  await edit(page, '#b1-hcr-poll', '-4');
  expect(await page.evaluate(() => [boardConfigs[1].hcr.poll, document.getElementById('b1-hcr-poll').value]), 'off').toEqual([0, '0']);
  await edit(page, '#b1-mp3-vol', '-3');
  expect(await page.evaluate(() => boardConfigs[1].mp3.volume)).toBe(0);

  // Typed and not left (no change event): the push clamps them itself.
  await setup(page);
  await pullFake(page, 1, board(['MP3,S2:9600:V10', 'HCR,PORT,S4:9600', 'HCR,POLL,10', 'DFP,S3:9600:V20']));
  await holdVerifyPulls(page);
  await typeOnly(page, 'b1-mp3-vol', '200');
  await typeOnly(page, 'b1-hcr-poll', '2');
  await typeOnly(page, 'b1-dfp-vol', '31');
  expect(await sent(page, 1)).toEqual(['1:?MP3,S2:9600:V64', '1:?HCR,PORT,S4:9600', '1:?HCR,POLL,3', '1:?DFP,S3:9600:V30']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.audio_fake_route_host the Remote host picker lists the WCB numbers up to the quantity without this board\'s own, never selects this board, and keeps a pulled host; mesh-reported boards above the quantity are not offered (today)', async ({ page }) => {
  // _populateRouteHostDropdown (app.js:2873-2892), reached from onMP3Change / onHCRChange / onDFPChange and
  // populateUIFromConfig. Whether a board the mesh reported above the quantity belongs in the list (mapping
  // destinations include those, appendMappingDestination) is left as it is. Row audio.route_host_dropdown.
  await setup(page);
  await pullFake(page, 2, board([], { wcb: 2, wcbq: 3 }));
  await holdVerifyPulls(page);
  await edit(page, '#b2-mp3-enable', true);
  await edit(page, 'input[name="b2-mp3"][value="remote"]', true);
  expect(await options(page, 'b2-mp3-remote-wcb')).toEqual(['1', '3']);
  expect(await page.evaluate(() => [document.getElementById('b2-mp3-remote-wcb').value, boardConfigs[2].mp3.remoteWCB])).toEqual(['1', 1]);
  expect(await sent(page, 2)).toEqual(['2:?MP3,REMOTE,W1']);
  await edit(page, '#b2-mp3-remote-wcb', '3');
  expect(await sent(page, 2)).toEqual(['2:?MP3,REMOTE,W3']);

  // WCB1 with the quantity at 1: the list is never empty (at least 2) and never WCB1.
  await setup(page);
  await pullFake(page, 1, board([], { wcbq: 1 }));
  await edit(page, '#b1-dfp-enable', true);
  await edit(page, 'input[name="b1-dfp"][value="remote"]', true);
  expect([await options(page, 'b1-dfp-remote-wcb'), await page.evaluate(() => boardConfigs[1].dfp.remoteWCB)]).toEqual([['2'], 2]);

  // A pulled route to WCB5, above the quantity of 3: kept and selected. A board the mesh reported above the quantity
  // (WCB9) is not offered - the list is the quantity's floor, the board's own number and the current host.
  await setup(page);
  await pullFake(page, 2, board(['HCR,REMOTE,W5'], { wcb: 2, wcbq: 3 }));
  await page.evaluate(() => { addDiscoveredBoards([9]); });
  expect(await options(page, 'b2-hcr-remote-wcb')).toEqual(['1', '3', '4', '5']);
  expect(await page.evaluate(() => document.getElementById('b2-hcr-remote-wcb').value)).toBe('5');
  await edit(page, 'input[name="b2-hcr"][value="local"]', true);
  await edit(page, 'input[name="b2-hcr"][value="remote"]', true);
  expect(await options(page, 'b2-hcr-remote-wcb')).not.toContain('9');
  expect(page.wizErrors).toEqual([]);
});
