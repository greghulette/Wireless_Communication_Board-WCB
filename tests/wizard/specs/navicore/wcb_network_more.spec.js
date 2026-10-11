// The WCB Network tab beyond its profiles and the bridged strip (docs/hil_plan/NAVICORE.md §2, nct.edit.wcb_network,
// nct.mesh.stats, L1; nctool.wcb_network_profiles and nctool.wcb_status_panel cover the rest): the identity fields
// (onWcbHexInput, config_tool/index.html ~11042), Reboot Board with its dirty warning (rebootBoard ~11047), the WCB Send
// box (WCB_SEND ~18909), a profile deleted and one edited in place (deleteWcbProfile ~10985,
// _snapshotLiveIntoSelectedProfile ~10916), the new-peer event editor (renderPeerEventEditor ~11006); and the Mesh
// Stats: the paged MESH_STATS reply merged with a duplicate ignored and a gap dropped (_meshStatsMergePage ~3954), the
// per-chip lines and the glance (_meshStatsChipLine ~4164), the modal's table, Refresh, auto-poll and Reset
// (openMeshStats ~4020). The emulator's meshStats hook serves the pages.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const wcbTab = async (page) => {
  await page.locator('#btn-hwsetup').click();
  await page.locator('#cfg-tabs .cfg-tab[data-tab="wcb"]').click();
};
const closeConfig = (page) => page.evaluate(() => closeHwSetup());
const reboot = (page) => page.locator('button', { hasText: 'Reboot Board' });
const net = (page) => page.evaluate(() => JSON.parse(JSON.stringify(config.wcbNetwork)));
const chipLine = (page, n) => page.locator('#wcb-status-list .wcb-chip', { hasText: `WCB ${n}` }).locator('xpath=following-sibling::div[1]');

test.describe('the WCB Network tab', () => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.wcb_network_tools unconnected, Reboot and Send say so; the MAC octets parse as hex, password, ID and channel edit the config and mark it dirty; Reboot while dirty warns about NVS (declined sends nothing), clean it asks plainly and sends REBOOT; Send needs a command and a WCB id 0-20 and sends WCB_SEND; a profile is deleted behind a confirm and a loaded one is edited in place, both carried by Save; the new-peer alert and actions (one typed, one from the Command Builder) save as peerEvent', async ({ page, emu }) => {
    const answers = [];
    const dialogs = T.answerDialogs(page, answers);
    const messages = () => dialogs.map((d) => d.message);
    await T.openTool(page);
    await wcbTab(page);
    // Unconnected, both are gated (index.html ~5019 enables them on connect), so their "not connected" alerts stay
    // unreachable from the UI.
    await expect(reboot(page)).toBeDisabled();
    await expect(page.locator('#btn-qs-send')).toBeDisabled();
    await closeConfig(page);
    await T.connectUsb(page, emu);
    await wcbTab(page);
    await expect(reboot(page)).toBeEnabled();
    await expect(page.locator('#btn-qs-send')).toBeEnabled();
    // Clean: Reboot asks plainly and sends REBOOT.
    answers.push(true);
    await reboot(page).click();
    await expect.poll(messages).toContainEqual('Reboot the board?');
    await expect.poll(() => emu.requests('REBOOT')).toEqual([{ sys: 1, type: 'REBOOT' }]);
    // The identity fields: the badge means "differs from what the board is running", so it stays after a Save, as
    // REBOOT PENDING, until the board reboots on the new credentials.
    await expect(page.locator('#wcb-network-dirty')).toBeHidden();
    await page.locator('#wcb-oct2').fill('A5');
    await page.locator('#wcb-oct3').fill('ff');
    await page.locator('#wcb-password').fill('hil-mesh-pass');
    await page.locator('#wcb-deviceid').fill('19');
    await page.locator('#wcb-channel').fill('6');
    await expect(page.locator('#wcb-network-dirty')).toBeVisible();
    await expect(page.locator('#wcb-network-dirty')).toHaveText('REBOOT PENDING');
    expect(await net(page)).toMatchObject({ macOct2: 0xA5, macOct3: 0xFF, password: 'hil-mesh-pass', deviceId: 19, channel: 6, quantity: BENCH.wcbNetwork.quantity });
    // Reboot while dirty: the NVS warning; declined, nothing goes.
    answers.push(false);
    await reboot(page).click();
    await expect.poll(messages).toContainEqual(expect.stringMatching(/^Reboot the board\?\n\nThe running WCB client will reload from NVS/));
    await page.waitForTimeout(200);
    expect(emu.requests('REBOOT')).toHaveLength(1);
    // Save carries the branch; the badge stays until a reboot.
    await page.locator('#btn-hwsetup-save').click();
    const [saved] = await T.waitRequests(emu, 'SET_CONFIG');
    expect(saved.data.wcbNetwork).toMatchObject({ macOct2: 0xA5, macOct3: 0xFF, password: 'hil-mesh-pass', deviceId: 19, channel: 6 });
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    expect(emu.config.toJSON().wcbNetwork).toMatchObject({ macOct2: 0xA5, macOct3: 0xFF, deviceId: 19, channel: 6 });
    await expect(page.locator('#wcb-network-dirty')).toBeVisible();
    // WCB Send.
    await page.locator('#qs-cmd').fill('');
    await page.locator('#btn-qs-send').click();
    await expect.poll(messages).toContainEqual('Enter a command.');
    await page.locator('#qs-cmd').fill(':BL55,1');
    await page.locator('#qs-target').fill('21');
    await page.locator('#btn-qs-send').click();
    await expect.poll(messages).toContainEqual('WCB ID must be 0 (broadcast) or 1-20.');
    expect(emu.requests('WCB_SEND')).toEqual([]);
    await page.locator('#qs-target').fill('0');
    await page.locator('#btn-qs-send').click();
    await page.locator('#qs-target').fill('2');
    await page.locator('#qs-cmd').fill(';S3HILQS');
    await page.locator('#btn-qs-send').click();
    await expect.poll(() => emu.requests('WCB_SEND')).toEqual([{ sys: 1, type: 'WCB_SEND', target: 0, cmd: ':BL55,1' }, { sys: 1, type: 'WCB_SEND', target: 2, cmd: ';S3HILQS' }]);
    // Profiles: delete the second (declined first), then edit the first in place through the fields.
    expect(BENCH.wcbProfiles.map((p) => p.name)).toEqual(['HILdev', 'HILdroid']);
    const del = (i) => page.locator('#wcb-profile-list button[title="Delete this profile"]').nth(i);
    answers.push(false);
    await del(1).click();
    await expect.poll(messages).toContainEqual(expect.stringMatching(/^Delete WCB profile "HILdroid"\?/));
    expect(await page.evaluate(() => config.wcbProfiles.map((p) => p.name))).toEqual(['HILdev', 'HILdroid']);
    answers.push(true);
    await del(1).click();
    await expect.poll(() => page.evaluate(() => config.wcbProfiles.map((p) => p.name))).toEqual(['HILdev']);
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Deleted WCB profile'));
    await page.locator('#wcb-profile-list input[type=radio]').nth(0).click();
    await expect(page.locator('#wcb-channel')).toHaveValue(String(BENCH.wcbProfiles[0].channel));
    await page.locator('#wcb-channel').fill('9');
    await page.locator('#btn-hwsetup-save').click();
    const [, saved2] = await T.waitRequests(emu, 'SET_CONFIG', 2);
    expect(Object.keys(saved2.data).sort()).toEqual(['wcbNetwork', 'wcbProfiles']);
    expect(saved2.data.wcbProfiles).toEqual([{ ...BENCH.wcbProfiles[0], channel: 9 }]);
    expect(saved2.data.wcbNetwork.channel).toBe(9);
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    expect(emu.config.toJSON().wcbProfiles.map((p) => [p.name, p.channel])).toEqual([['HILdev', 9]]);
    // The new-peer event: alert off, one action typed, one built.
    expect(BENCH.peerEvent).toEqual({ alert: true, actions: [] });
    await expect(page.locator('#peer-alert')).toBeChecked();
    await page.locator('#peer-alert').uncheck();
    await page.locator('#fields-peer-0-cmd').fill(';S2HILPEER');
    await page.locator('#tier-card-peer .btn-add-action').click();
    await page.locator('#fields-peer-1 button', { hasText: 'Command Builder' }).click();
    await page.locator('#cmdlib-search').fill('Lights on');
    await page.locator('.cmdlib-item[data-board="wcb-wled"][data-cmd="wled.on"]').click();
    await page.locator('#cmdlib-use').click();
    await expect(page.locator('#cmdlib-modal')).not.toHaveClass(/open/);
    await page.locator('#btn-hwsetup-save').click();
    const [, , saved3] = await T.waitRequests(emu, 'SET_CONFIG', 3);
    expect(saved3.data.peerEvent).toEqual({ alert: false, actions: [{ type: 'wcb_unicast', target: '1', cmd: ';S2HILPEER' }, { type: 'wled', cmd: ';L1,ON' }] });
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    expect(emu.config.toJSON().peerEvent).toEqual(saved3.data.peerEvent);
    T.expectNoPageErrors(page);
  });
});

test.describe('the mesh stats', () => {
  test.use({ emuOptions: { config: BENCH } });

  // Two pages: the aggregate and WCB 1 on page 0, WCB 2 on page 1 (last).
  const PAGE0 = (sent = 50, ackd = 48) => `{"type":"MESH_STATS","pg":0,"self":20,"upMs":600000,"agg":{"sent":${sent},"ackd":${ackd},"rty":3,"fail":1,"ung":1,"bcast":4,"recv":25},"peers":[[1,40,38,3,1,1,20]],"last":0}`;
  const PAGE1 = '{"type":"MESH_STATS","pg":1,"self":20,"peers":[[2,10,10,0,0,0,5]],"last":1}';

  test('nctool.mesh_stats_modal with the stats report on, connecting pulls MESH_STATS and its pages merge into a line under each chip and an aggregate glance; a duplicate page is ignored and a page after a gap waits for a fresh cycle; the stats checkbox hides the lines and stops the sub-poll; the 📊 glance opens the modal with this board\'s table, Refresh asks again, auto-poll asks every 5 s until the modal closes, Reset asks first (declined, nothing) then sends RESET_MESH_STATS and re-reads; unconnected the modal says to connect', async ({ page, emu }) => {
    const answers = [];
    const dialogs = T.answerDialogs(page, answers);
    emu.meshStats = () => [PAGE0(), PAGE1];
    const stats = () => emu.requests('GET_MESH_STATS').length;
    await T.openTool(page);
    await page.evaluate(() => openMeshStats());
    await expect(page.locator('#meshstats-modal')).toHaveClass(/open/);
    await expect(page.locator('#mesh-stats-body')).toHaveText('Connect the board, then press Refresh.');
    await page.locator('#meshstats-close').click();
    await expect(page.locator('#meshstats-modal')).not.toHaveClass(/open/);
    expect(BENCH.statsReport).toMatchObject({ enabled: true });
    await T.connectUsb(page, emu);
    await expect(page.locator('#wcb-show-stats')).toBeChecked();
    await expect.poll(stats).toBeGreaterThanOrEqual(1);
    await expect(chipLine(page, 1)).toHaveText('40 sent · 95.0% ack · 3↻ · 2✗');
    await expect(page.locator('#wcb-status-list')).toContainText('All links: 50 sent · 96.0% ack · 2 failed');
    expect(await page.evaluate(() => _meshStats.peers)).toEqual([[1, 40, 38, 3, 1, 1, 20], [2, 10, 10, 0, 0, 0, 5]]);
    // A duplicate of the last page (an ETM retry) changes nothing; a page after a gap is dropped until a fresh cycle.
    emu.inject(PAGE1);
    emu.inject('{"type":"MESH_STATS","pg":2,"self":20,"peers":[[3,1,1,0,0,0,0]],"last":1}');
    await page.waitForTimeout(200);
    expect(await page.evaluate(() => _meshStats.peers.map((p) => p[0]))).toEqual([1, 2]);
    emu.inject(PAGE0(60, 60).replace('"last":0', '"last":1'));
    await expect(page.locator('#wcb-status-list')).toContainText('All links: 60 sent · 100.0% ack');
    expect(await page.evaluate(() => _meshStats.peers.map((p) => p[0]))).toEqual([1]);
    // Off: the lines go and the sub-poll stops; on: fetched at once.
    await page.locator('#wcb-show-stats').uncheck();
    await expect(chipLine(page, 1)).toHaveCount(0);
    await expect(page.locator('#wcb-status-list')).not.toContainText('All links');
    const n0 = stats();
    for (let i = 0; i < 6; i++) await page.clock.fastForward(3000);
    expect(stats(), 'no stats poll while the lines are off').toBe(n0);
    await page.locator('#wcb-show-stats').check();
    await expect.poll(stats).toBe(n0 + 1);
    await expect(chipLine(page, 1)).toHaveText('40 sent · 95.0% ack · 3↻ · 2✗');
    await page.locator('#wcb-show-stats').uncheck();
    // The modal from the glance: the table; Refresh; auto-poll every 5 s until the modal closes.
    await page.locator('#wcb-show-stats').check();
    await page.locator('#wcb-status-list button[title="Open the full per-board table"]').click();
    await expect(page.locator('#meshstats-modal')).toHaveClass(/open/);
    // The sidebar's checkbox sits under the modal's overlay now: turn the sub-poll off the way its handler does, so
    // the counts below are the modal's requests alone.
    await page.evaluate(() => { document.getElementById('wcb-show-stats').checked = false; setShowMeshStats(false); });
    const body = page.locator('#mesh-stats-body');
    await expect(body).toContainText('This board (WCB 20) · up 10 min');
    const row = (n) => body.locator('tr', { has: page.locator('td', { hasText: new RegExp(`^WCB ${n}( ·|$)`) }) });
    await expect(row(1)).toContainText('95.0%');
    await expect(row(2)).toContainText('100.0%');
    const n1 = stats();
    await page.locator('#meshstats-modal button', { hasText: 'Refresh' }).click();
    await expect.poll(stats).toBe(n1 + 1);
    await page.locator('#stats-autopoll').check();
    await expect.poll(stats).toBe(n1 + 2);
    await page.clock.fastForward(5100);
    await expect.poll(stats).toBe(n1 + 3);
    await page.clock.fastForward(5100);
    await expect.poll(stats).toBe(n1 + 4);
    // Reset: declined, nothing; accepted, RESET_MESH_STATS then a re-read.
    answers.push(false);
    await page.locator('#meshstats-modal button[title^="Zero every ESP-NOW counter"]').click();
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual(expect.stringMatching(/^Zero every ESP-NOW counter on the NaviCore\?/));
    expect(emu.requests('RESET_MESH_STATS')).toEqual([]);
    answers.push(true);
    const n2 = stats();
    await page.locator('#meshstats-modal button[title^="Zero every ESP-NOW counter"]').click();
    await expect.poll(() => emu.requests('RESET_MESH_STATS')).toEqual([{ sys: 1, type: 'RESET_MESH_STATS' }]);
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Mesh stats cleared'));
    await page.clock.fastForward(400);
    await expect.poll(stats).toBeGreaterThanOrEqual(n2 + 1);
    await page.locator('#meshstats-close').click();
    await expect(page.locator('#meshstats-modal')).not.toHaveClass(/open/);
    await expect(page.locator('#stats-autopoll')).not.toBeChecked();
    const n3 = stats();
    await page.clock.fastForward(5100);
    await page.clock.fastForward(5100);
    expect(stats(), 'auto-poll stopped with the modal').toBe(n3);
    T.expectNoPageErrors(page);
  });
});
