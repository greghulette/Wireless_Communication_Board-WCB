// Intellex over WiFi, through NaviCore's access point (INTELLEX.md IX-WP9). Each test is started by the harness test of
// the same id (tests/hil/suites/s35_intellex_wifi.py), which puts the PC's spare WiFi adapter on NaviCore's access point
// for the test (hil/wlan.py pc_on_ap), stages a leashed host allowed no COM port at all, and attaches it the way
// Intellex's chooser attaches a droid it identified: {kind: ws, host: 192.168.4.1, role: navicore}. The pages connect on
// their own (intellex_shim.js autoConnect, autoConnectWcb). Afterwards the harness checks NaviCore never restarted (its
// uptime over USB, W1's view of the mesh), puts NaviCore's monitor and debug flags back, and stops every WCB's remote
// terminal when the Wizard ran.
//
// No network name ever reaches a spec: /_api/status is read here only for attached, kind and role, and what the harness
// hands back from its hooks has the names taken out. D-NC5: nothing reads #terminal-output or quotes a config value.
const { test, expect, skipUnlessHost, hostPost } = require('../lib/fixtures');
const B = require('../lib/board');

skipUnlessHost(test);
test.beforeEach(() => {
  test.skip(!B.hil.present, 'board tests run under the HIL harness: python tests/hil/run.py "intellex.wifi_*"');
});

const args = async () => (await B.hil.context()).args || {};

// intellex_shim.js NOT_USB_MSG (wireNativeFlash) and OTA_WIFI_TITLE (relabelOtaButton); host.py _claim_port_for_flash.
const NOT_USB_MSG = 'Flashing needs a direct USB connection. This session is over WiFi, which has no DTR/RTS lines to '
                  + 'enter the bootloader. Attach the board over USB, or use OTA.';
const OTA_WIFI_TITLE = 'OTA update over this WiFi connection (no bootloader mode). Streams the new firmware to the '
                     + 'inactive OTA slot, verifies it (SHA), then reboots. Brick-safe; keeps your config.';
const HOST_NOT_USB = 'Flashing needs a direct USB serial connection.';

const button = (page, id) => page.evaluate((i) => {
  const b = document.getElementById(i);
  return b ? { disabled: b.disabled, title: b.title, text: (b.textContent || '').trim(), wired: !!b.__intellexWired }
           : null;
}, id);

// What the host is attached to, from Node: never its lastError (a held move names both networks there).
async function linkKind() {
  const st = (await B.hostGet('/_api/status')).json || {};
  return { attached: st.attached, kind: st.kind, role: st.role };
}

test('intellex.wifi_nc_tool the config tool through Intellex on NaviCore\'s access point: WiFi label, OTA over WiFi, no USB flash',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  const link = B.watchLink(page);
  await page.goto('/', { waitUntil: 'load' });
  const s = await B.ncHandshake(page, { timeout: 60_000 });
  expect(s.status, 'the status line').toBe('Connected ✓');
  expect([s.viaWcbActive, s.pongEpoch], 'on NaviCore\'s own access point the link is direct: the direct PING answered, ' +
    'no Via WCB').toEqual([false, 1]);
  expect(s.fw, 'the firmware version from the PONG over WiFi').toBe(a.version);
  expect(await linkKind(), 'what the host is attached to').toEqual({ attached: true, kind: 'ws', role: 'navicore' });
  await expect.poll(async () => (await B.ncState(page)).transport, { timeout: 8_000,
    message: 'the transport label beside the status' }).toBe(`· WiFi ${a.host} ▾`);
  await expect.poll(() => button(page, 'btn-fw-ota'), { timeout: 10_000, message: 'the OTA button, over WiFi' })
    .toEqual(expect.objectContaining({ title: OTA_WIFI_TITLE }));
  const ota = await button(page, 'btn-fw-ota');
  expect(ota.text, 'the OTA button').toContain('Update over WiFi (OTA)');
  expect(ota.text, 'the OTA button names USB on a WiFi link').not.toContain('USB');
  for (const id of ['btn-fw-flash', 'btn-fw-wipe']) {
    await expect.poll(() => button(page, id), { timeout: 10_000, message: `${id}: disabled over WiFi, saying why` })
      .toEqual(expect.objectContaining({ disabled: true, title: NOT_USB_MSG }));
  }
  // The host refuses a flash over WiFi before anything starts: it has no port to hand esptool.
  for (const route of ['/_api/flash', '/_api/flash-wcb']) {
    const r = await hostPost(route, {});
    expect(r.status, `${route} with the host attached over WiFi`).toBe(409);
    expect(String(r.json && r.json.error), `${route}'s refusal`).toContain(HOST_NOT_USB);
  }
  expect(B.unaskedWrites(link), 'JSON the tool sent over WiFi that is not a read').toEqual([]);
  B.expectClean(rec, 'the config tool');
});

// The Wizard through NaviCore as its doorway: NaviCore's backup carries ?RELAY,1 and ?WCB,<its id> (WCB_Client
// WCB_Mgmt.h printBackup), so the Wizard files it as a relay card at its own id (app.js applyRelayRole), and the shim's
// routeMeshThroughRelay has relayRouteAll arm and pull every WCB the card lists (intellex_shim.js). -> times and results.
async function throughNaviCore(page, a) {
  await B.openWizard(page);
  await B.spyRemotePulls(page);
  const t0 = Date.now();
  await expect.poll(() => page.evaluate((r) => !!document.getElementById(`relay-card-${r}`), a.relay),
    { timeout: 90_000, message: `no relay card for NaviCore (WCB${a.relay}): its backup did not file it as a relay` })
    .toBe(true);
  const card = Date.now() - t0;
  const pulled = () => page.evaluate((bs) => bs.every((b) => !!boardBaselines[b] && !_pullingBoards.has(b)), a.boards);
  await expect.poll(pulled, { timeout: 300_000,
    message: `WCB${a.boards.join(', ')} were not all pulled through NaviCore` }).toBe(true);
  const res = {};
  for (const b of a.boards) res[b] = await B.pullResult(page, b);
  for (const b of a.boards) {
    const r = res[b];
    expect(r.baseline && r.wcbNumber, `WCB${b}: a baseline naming its own number`).toBe(b);
    expect(String(r.relayFor), `WCB${b} is managed through NaviCore's slot`).toBe(String(a.relay));
    expect(r.stillPulling, `WCB${b} left _pullingBoards`).toBe(false);
    expect(r.rawInTerminal, `WCB${b}: no raw pull text in a terminal`).toBe(false);
  }
  expect(await page.evaluate((r) => !!document.getElementById(`section-board-${r}`), a.relay),
    `NaviCore (WCB${a.relay}) is shown as a numbered board beside its relay card`).toBe(false);
  const calls = res[a.boards[0]].calls;
  const stray = calls.filter((c) => !a.boards.includes(c.target)).map((c) => c.target);
  expect(stray, 'pulls of anything but the bench\'s WCBs (NaviCore itself, a client)').toEqual([]);
  return { card, all: Date.now() - t0, res, calls };
}

test('intellex.wifi_wizard_via_navicore the Wizard through Intellex on NaviCore\'s access point pulls every WCB through NaviCore',
  async ({ page, context, rec }) => {
  test.setTimeout(480_000);                         // the harness's watchdog is 540 s (s35)
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  const r = await throughNaviCore(page, a);
  const each = a.boards.map((b) => {
    const done = r.res[b].done.filter((d) => d.target === b).map((d) => `${d.ok ? 'ok' : 'failed'} in ${d.ms} ms`);
    return `WCB${b}: ${r.calls.filter((c) => c.target === b).length} pull(s), ${done.join(', ') || 'none settled'}`;
  });
  await B.hil.note(`intellex.wifi_wizard_via_navicore: the relay card for WCB${a.relay} ${Math.round(r.card / 1000)} s ` +
    `after the page opened, every board pulled at ${Math.round(r.all / 1000)} s; ${each.join('; ')}`);
  B.expectClean(rec, 'the Wizard');
});

test('intellex.wifi_rterm_rate the Wizard through NaviCore re-arms no WCB\'s remote terminal more than 6 times a minute once settled',
  async ({ page, context, rec }) => {
  test.setTimeout(600_000);                         // the harness's watchdog is 660 s (s35)
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  const link = B.watchLink(page);
  const r = await throughNaviCore(page, a);
  await page.waitForTimeout(10_000);                // the last pull's own re-arm (three sends a call) goes out first
  const n0 = link.sent.length;
  await B.hil.hook('rterm_mark');
  await page.waitForTimeout(a.window * 1000);
  const c = await B.hil.hook('rterm_count');
  const mine = {};
  for (const x of link.sent.slice(n0)) if (x.inner === 'RTERM,START') mine[x.frag] = (mine[x.frag] || 0) + 1;
  await B.hil.note(`intellex.wifi_rterm_rate: in ${c.seconds} s from ${Math.round(r.all / 1000) + 10} s after the page ` +
    `opened: ?RTERM,START,${a.relay} run per WCB ${JSON.stringify(c.counts)}; written by the page per WCB ` +
    `${JSON.stringify(mine)}`);
  for (const [b, k] of Object.entries(c.counts)) {
    expect(k, `WCB${b} ran ?RTERM,START,${a.relay} ${k} times in ${c.seconds} s (the page wrote ${mine[b] || 0}): ` +
      `over ${a.bound} (INTELLEX.md DX10)`).toBeLessThanOrEqual(a.bound);
  }
  B.expectClean(rec, 'the Wizard');
});

// Attended (intellex_wifi_join). The harness moves the spare adapter when asked (hooks hop, hop_back) and waits for the
// host to have attached again with the role it re-identified (host.py _reidentify_if_moved); the tool must connect
// through a WCB doorway in Via WCB (CLAUDE.md rule 10) and through NaviCore's own access point directly.
test('intellex.wifi_ap_hop_reidentify moved from NaviCore\'s access point to W1\'s and back, Intellex re-identifies the board first',
  async ({ page, context, rec }) => {
  test.setTimeout(900_000);                         // two joins with their lease waits; the harness's watchdog is 960 s
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  const link = B.watchLink(page);
  await page.goto('/', { waitUntil: 'load' });
  let s = await B.ncHandshake(page, { timeout: 60_000 });
  expect([s.viaWcbActive, s.pongEpoch], 'on NaviCore\'s access point: direct').toEqual([false, 1]);
  const hop = await B.hil.hook('hop');
  if (hop.skip) test.skip(true, hop.skip);
  expect(hop.status, 'after the hop to W1\'s access point, /_api/status').toMatchObject(
    { attached: true, kind: 'ws', role: 'wcb', relayId: a.w1 });
  // Without a reload the shim's watchLink reconnects the page and autoConnect forces Via WCB for a doorway.
  const alone = await page.waitForFunction(() => typeof viaWcbActive !== 'undefined' && viaWcbActive === true && !!port,
    null, { timeout: 45_000 }).then(() => true, () => false);
  await B.pressF5(page);
  s = await B.ncHandshake(page, { config: false, timeout: 90_000 });
  expect(s.viaWcbActive, 'the tool connected through W1\'s access point is in Via WCB (rule 10)').toBe(true);
  await expect.poll(async () => (await B.ncState(page)).transport, { timeout: 8_000,
    message: 'the transport label through the doorway' }).toBe(`· WCB ${a.host} → mesh ▾`);
  const back = await B.hil.hook('hop_back');
  expect(back.status, 'moved back to NaviCore\'s access point, /_api/status').toMatchObject(
    { attached: true, kind: 'ws', role: 'navicore' });
  expect(back.status.relayId ?? null, 'no relay id for a NaviCore').toBe(null);
  const aloneBack = await page.waitForFunction(() => viaWcbActive === false && !!port && isMonitoring === true, null,
    { timeout: 45_000 }).then(() => true, () => false);
  await B.pressF5(page);
  s = await B.ncHandshake(page, { timeout: 90_000 });
  expect([s.viaWcbActive, s.pongEpoch], 'back on NaviCore\'s access point: direct again').toEqual([false, 1]);
  await B.hil.note(`intellex.wifi_ap_hop_reidentify: the host re-identified in ${hop.seconds} s after the hop and ` +
    `${back.seconds} s after the hop back; without a reload the tool went Via WCB: ${alone}, and direct again: ${aloneBack}`);
  expect(B.unaskedWrites(link), 'JSON the tool sent that is not a read').toEqual([]);
  B.expectClean(rec, 'the config tool');
});
