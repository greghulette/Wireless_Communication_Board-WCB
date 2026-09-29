// L2 through a WCB: the config tool on W1's port (piped, like the NaviCore specs in board.spec.js, but the device is
// the bench's W1), reaching the real NaviCore over the mesh (docs/hil_plan/NAVICORE.md §5.2, NC-WP13). Only under
// the harness, which runs each inside nc_guard: python tests/hil/run.py nctool.board_via_wcb. Standalone and in CI
// these skip.
//
// NaviCore's CONFIG crosses W1's USB as fragment envelopes and W1 is not one of the devices session.log redacts, so
// the harness's PipeLog (hil/wizard.py) keeps the envelopes' contents out of the log. A spec here, as everywhere,
// reads counts, booleans and key names, never a config value.
const { test, expect, hil } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

test.beforeEach(({ hilCtx }) => {
  test.skip(!hilCtx || !hilCtx.pipe || hilCtx.kind !== 'wcb',
            'L2 through a WCB runs under the HIL harness with W1 piped: python tests/hil/run.py nctool.board_via_wcb');
});

async function openConfigTab(page, tab) {
  if (!(await page.locator('#hwsetup-modal.open').count())) await page.locator('#btn-hwsetup').click();
  await page.locator(`#cfg-tabs .cfg-tab[data-tab="${tab}"]`).click();
}

// nct.conn.via_wcb_hub and nct.save.diff over the bridge. "Via a WCB" (connectViaWcbOpt -> connectSharedPort,
// index.html:4651-4756) makes this tab the WcbSerialHub leader on W1's port, forces the Via-WCB transport and
// handshakes with ;w20,-wrapped JSON only. The harness picked 24-character HIL labels for every port-label key the board
// has not set: together they make a SET_CONFIG over the 187-byte single-packet limit (sendJSON, :5635-5652), so the
// Save goes out as fragment envelopes, which NaviCore reassembles and ACKs over the mesh.
test('nctool.board_via_wcb through W1\'s port the tool connects Via a WCB to the real NaviCore: the bridged PONG and fragmented CONFIG apply (every mapping, nothing to save), W1 shows as the relay, and a port-label Save goes out in fragment envelopes and is ACKed', async ({ page, device, hilCtx }) => {
  test.setTimeout(180_000);
  const a = hilCtx.args;
  await T.openTool(page);
  await T.connectViaWcb(page, { expectConfig: false });
  // ~100 envelopes at NaviCore's 150 ms pacing (FRAG_PACING_MS, rc_telemetry.h:529).
  await page.waitForFunction(() => _configLoaded === true, null, { timeout: 90_000 });
  expect(await T.state(page)).toMatchObject({ viaWcbActive: true, sharedActive: true, status: `Connected via WCB → RC #20` });
  await expect(page.locator('#fw-current-version')).toHaveText(a.version);
  const facts = await page.evaluate(() => ({
    deviceId: config.wcbNetwork.deviceId, mappings: Object.keys(config.mappings).sort(),
    diff: Object.keys(_diffConfigBranches(config, _configBaseline)),
  }));
  expect(facts.deviceId).toBe(20);
  expect(facts.mappings, 'the mapping keys of the bridged CONFIG').toEqual([...a.mappings].sort());
  expect(facts.diff, 'branches a Save right after connecting would send').toEqual([]);
  // The relay chip (renderWcbStatus, index.html:5227-5280): WCB <relay> carries the 📡 mark, in the roster or apart.
  await expect.poll(() => page.locator('#wcb-status-list .wcb-chip-name').evaluateAll((els, relay) => els
    .map((e) => e.textContent.replace(/\s+/g, ' ').trim())
    .filter((t) => new RegExp(`^WCB ${relay}( |$)`).test(t) && t.includes('📡')).length, a.relay), { timeout: 20_000 }).toBe(1);

  await openConfigTab(page, 'serial');
  for (const [key, label] of Object.entries(a.labels)) await page.locator(`#slabel-${key}`).fill(label);
  expect(await page.evaluate(() => Object.keys(_diffConfigBranches(config, _configBaseline)))).toEqual(['serialLabels']);
  const from = device.sentFrags.length;
  await page.locator('#btn-hwsetup-save').click();
  await expect.poll(() => T.toasts(page), { timeout: 30_000 }).toContainEqual(expect.stringContaining('Config saved to NaviCore'));
  expect(await page.evaluate(() => [!!_pendingSaveBaseline, _configUnsaved()]), '[save pending, unsaved edits]').toEqual([false, false]);
  const frags = device.sentFrags.slice(from);
  expect(frags.length, 'fragment envelopes the Save wrote').toBeGreaterThanOrEqual(2);
  expect(new Set(frags.map((f) => f.sid)).size, 'fragment sessions').toBe(1);
  expect(frags.map((f) => `${f.f}/${f.of}`)).toEqual(Array.from({ length: frags[0].of }, (_, i) => `${i + 1}/${frags[0].of}`));
  expect(Math.max(...frags.map((f) => f.bytes)), 'largest envelope (FRAG_MAX_ENV_BYTES 187)').toBeLessThanOrEqual(187);

  await page.evaluate(() => closeHwSetup());
  await page.locator('#btn-connect').click();                       // Disconnect: STOP_MONITOR through the hub, leave
  await page.waitForFunction(() => !sharedActive);
  await device.flush();
  // Everything goes out ;w20,-wrapped but the first status poll, which leaves bare before the transport flag is set:
  // D-NC71, pinned by nctool.via_wcb_nothing_bare (L1). Noted here; any other bare line fails.
  const bare = device.sentLog.filter((l) => l.how === 'bare');
  await hil.note(`board_via_wcb: ${bare.length} bare JSON line(s) typed on W1's console (${bare.map((l) => l.type).join(', ')})`);
  expect(bare.filter((l) => l.type !== 'GET_WCB_STATUS'), 'bare JSON typed on the WCB console').toEqual([]);
  expect(device.sentTypes.filter((t) => t === 'RESET_DEFAULTS' || t === 'SET_CMDLIB')).toEqual([]);
  expect(device.errors, 'bridge errors').toEqual([]);
  T.expectNoPageErrors(page);
});

// NAVICORE.md D-NC70. "Connect via USB" on a port that turns out to be a tethered WCB: openPortAndStart probes the
// direct link first with up to six bare {"sys":1,"type":"PING"} lines 500 ms apart (index.html:4568-4578) and only then
// falls back to Via WCB (:4579-4593). A WCB runs a console line that starts with neither its function nor its command
// character as a broadcast (handleSingleCommand, WCB.ino:6153-6164): processBroadcastCommand writes it out of every port with
// broadcast output on that no Maestro, MP3 Trigger, DFPlayer or HCR is configured on, and onto the mesh
// (:8494-8545, :8560-8563). So every probe PING reaches whatever serial device sits on those ports. The harness bound
// the W1 ports that its own bare marker line reached, so the spec only has to look.
test('nctool.board_usb_probe_no_broadcast (should) Connect via USB on a tethered WCB puts nothing out of that WCB\'s serial ports while it probes for a direct NaviCore; today the probe\'s bare JSON PINGs are broadcast (D-NC70)', async ({ page, device, hilCtx }) => {
  test.fail(true, 'known tool behaviour D-NC70: the direct probe writes bare JSON PINGs (openPortAndStart, index.html:4568-4578), and ' +
                  'a WCB broadcasts an unprefixed console line to its serial ports and the mesh (WCB.ino:6153-6164, :8494-8563)');
  test.setTimeout(120_000);
  const wires = hilCtx.args.wires.map((w) => ({ ...w, h: hil.wire(w.wcb, w.port) }));
  await T.openTool(page);
  for (const w of wires) w.since = await w.h.mark();
  await page.locator('#btn-connect').click();
  await page.locator('#connect-modal .connect-opt').first().click();   // Connect via USB (connectDirect, :4639-4645)
  await page.waitForFunction(() => window.__hilSerial.isOpen());
  await page.clock.fastForward(4000);                                 // the settle before the first PING
  // The direct phase has ended once the tool switches itself to Via WCB (onViaWcbToggle(true), :4581) - or once the
  // handshake finished direct (isMonitoring, :4600): a PONG W1 printed from NaviCore over the mesh, inside the 20 s
  // relay window a ;w20, line opens, is taken for a direct link (D-NC30), and board_via_wcb, run before this, opens it
  // (run 20260929-172549 waited 30 s for a fallback that never came). The PINGs this test is about went out bare
  // either way.
  await page.waitForFunction(() => viaWcbActive === true || isMonitoring === true, null, { timeout: 30_000 });
  const path = await page.evaluate(() => (viaWcbActive ? 'fell back to Via WCB' : 'took a mesh PONG for a direct link'));
  await device.flush();
  await page.waitForTimeout(1000);                                    // the last broadcast's bytes reach the probes
  const bare = device.sentLog.filter((l) => l.how === 'bare' && l.type === 'PING').length;
  await hil.note(`board_usb_probe_no_broadcast: ${bare} bare PING line(s) written to W1's console; the tool ${path}`);
  const hit = [];
  for (const w of wires) {
    const got = await w.h.received(w.since);
    if (got.includes(Buffer.from('"type":"PING"'))) hit.push(`W${w.wcb}${w.port}`);
  }
  expect(hit, 'W1 ports that put the tool\'s probe PING out to their device').toEqual([]);
});
