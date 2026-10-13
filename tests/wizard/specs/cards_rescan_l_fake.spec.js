// Needs no board: the board grid and the cards (Wizard/app.js) - identity, serial ports, Kyber, HCR, WLED and mapping
// panels - the low-risk gaps the 2026-10-12 coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in
// docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each test's comment). Fake boards in the real page
// (lib/fake.js): boardPull and boardGo run as on the bench, the handlers are reached through the inputs' own events,
// and only the wire is recorded. Runs standalone, in CI, and under the harness by id (s30_wizard.py, _FAKE_RESCAN_L).
// Verify pulls are held back where a spec pushes (lib/rescan.js holdVerifyPulls).
//
// Nothing here is a real credential: the mesh password is lib/fake.js's dummy.
const { test, expect } = require('../lib/fixtures');
const { board, chain, pullFake, push, edit, relayFake } = require('../lib/fake');
const { setup, holdVerifyPulls, toastMark, toastsSince, options, shown, sent } = require('../lib/rescan');

const navCaptions = (page) => page.evaluate(() => [...document.querySelectorAll('#wcb-nav .wcb-nav-btn')].map((b) => b.textContent));
const text = (page, id) => page.evaluate((id) => document.getElementById(id).textContent, id);
const value = (page, id) => page.evaluate((id) => document.getElementById(id).value, id);
const sentOn = (page, slot) => page.evaluate((slot) => __fake.sent(slot), slot);
const frag = (s) => s.replace(/^(\?MGMT,FRAG,\d+,)[0-9A-F]{4}(,)/, '$1SID$2');
// A local Kyber on S2 with its Marcuino port on S3, as a board backs it up.
const KYBER = ['BAUD,S2,115200', 'LABEL,S2,Kyber Maestro', 'LABEL,S3,Kyber Marcuino', 'BCAST,OUT,S2,OFF', 'BCAST,IN,S2,OFF',
               'KYBER,LOCAL,S2'];

test('wizard.grid_fake_jump_nav the WCB jump nav has one button per rendered board, captioned with its alias and kept through a grid rebuild; a button scrolls its board into view, and the board most in view is the highlighted one', async ({ page }) => {
  // updateWCBNav, _updateActiveNavBtn and the scroll listener (app.js:422-476), updateBoardAliasUI (2250-2275).
  // Row grid.jump_nav.
  await setup(page);
  await pullFake(page, 1, board(['ALIAS,Body'], { wcbq: 3 }));
  await edit(page, '#b2-alias', 'Dome');
  expect(await page.evaluate(() => document.getElementById('wcb-nav').style.display)).toBe('flex');
  expect(await navCaptions(page)).toEqual(['WCB 1 (Body)', 'WCB 2 (Dome)', 'WCB 3']);
  // A rebuild (a board discovered on the mesh) keeps every caption.
  await page.evaluate(() => addDiscoveredBoards([5]));
  expect(await navCaptions(page)).toEqual(['WCB 1 (Body)', 'WCB 2 (Dome)', 'WCB 3', 'WCB 5']);

  const active = () => page.evaluate(() => [...document.querySelectorAll('#wcb-nav .wcb-nav-btn.active')].map((b) => b.id));
  const top = (n) => page.evaluate((n) => Math.round(document.getElementById(`section-board-${n}`).getBoundingClientRect().top), n);
  await page.locator('#wcb-nav-btn-3').click();
  await expect.poll(() => top(3), { message: 'WCB 3 scrolled up under the nav' }).toBeLessThan(250);
  await expect.poll(() => active()).toEqual(['wcb-nav-btn-3']);
  await page.locator('#wcb-nav-btn-1').click();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeLessThan(await page.evaluate(() =>
    document.getElementById('section-board-3').offsetTop));
  await expect.poll(() => active()).toEqual(['wcb-nav-btn-1']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.grid_fake_add_section a section built for a slot that already holds a config shows that config (a board loaded or migrated before its section existed), a section for a slot already connected comes up Connected, and one built in Advanced mode shows its advanced parts', async ({ page }) => {
  // addBoardSection (app.js:652-691) through addDiscoveredBoards / reconcileBoardGrid. Row grid.add_section.
  await setup(page);
  await page.evaluate(() => {
    const cfg = WCBParser.parseBackupString(['?WCB,7', '?ALIAS,Periscope', '?LABEL,S1,Lifter', '?BAUD,S2,38400'].join('^'));
    boardConfigs[7] = cfg;
    addDiscoveredBoards([7]);
  });
  expect(await page.evaluate(() => [document.getElementById('b7-s1-label').value, document.getElementById('b7-s2-baud').value,
                                    document.getElementById('b7-alias').value, document.getElementById('b7-alias-label').textContent,
                                    boardConfigs[7].serialPorts[0].label]))
    .toEqual(['Lifter', '38400', 'Periscope', '(Periscope)', 'Lifter']);
  expect([await text(page, 'b7-btn-connect'), await text(page, 'b7-status-badge')]).toEqual(['Connect', 'Not Connected']);

  // A connection already live in slot 9 when its section is built: the card says so and its buttons work.
  await page.evaluate(() => { __fake.conn(9, { backup: '' }); boardConfigs[9] = WCBParser.parseBackupString('?WCB,9^?LABEL,S4,Held'); });
  await page.evaluate(() => addDiscoveredBoards([9]));
  expect(await page.evaluate(() => [document.getElementById('b9-btn-connect').textContent, document.getElementById('b9-status-badge').textContent,
                                    document.getElementById('b9-btn-pull').disabled, document.getElementById('b9-btn-go').disabled,
                                    document.getElementById('b9-s4-label').value]))
    .toEqual(['Disconnect', '● Connected', false, false, 'Held']);

  // Advanced mode: a section added now shows its advanced-only parts (the WiFi block).
  await page.evaluate(() => setMode('advanced'));
  await page.evaluate(() => addDiscoveredBoards([11]));
  expect(await page.evaluate(() => document.querySelectorAll('#section-board-11 .advanced-only.hidden').length)).toBe(0);
  expect(await page.evaluate(() => document.querySelectorAll('#section-board-11 .advanced-only').length)).toBeGreaterThan(0);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.grid_fake_slot_type the slot-type radio flips a pulled card to WCB_Client - the WCB body hidden, the client pane and its alias (cut at 24) shown, the header and nav reading Client - and back, with the pulled WCB config whole, so a push then has nothing to send', async ({ page }) => {
  // onSlotTypeChange, updateSlotTypeUI, onClientAliasChange (app.js:2277-2316). The export's ?CLIENT is
  // wizard.app_fake_system_file's; that a client slot is never pushed, wizard.app_fake_identity's. Row grid.slot_type.
  await setup(page);
  await pullFake(page, 1, board(['ALIAS,Body', 'LABEL,S1,Keep', 'BAUD,S2,38400']));
  await holdVerifyPulls(page);
  const view = () => page.evaluate(() => ({
    type: boardConfigs[1].type,
    body: document.getElementById('b1-section-body').style.display !== 'none',
    pane: document.getElementById('b1-client-pane').style.display !== 'none',
    header: document.getElementById('b1-alias-label').textContent,
    nav: document.getElementById('wcb-nav-btn-1').textContent,
  }));
  await edit(page, 'input[name="b1-slot-type"][value="client"]', true);
  expect(await view()).toEqual({ type: 'client', body: false, pane: true, header: '· Client', nav: 'Client 1' });
  await edit(page, '#b1-client-alias', 'Dome Sensor with a long name');
  expect(await page.evaluate(() => boardConfigs[1].clientAlias)).toBe('Dome Sensor with a long ');
  expect(await view()).toMatchObject({ header: '· Client (Dome Sensor with a long)', nav: 'Client 1 (Dome Sensor with a long)' });

  await edit(page, 'input[name="b1-slot-type"][value="wcb"]', true);
  expect(await view()).toEqual({ type: 'wcb', body: true, pane: false, header: '(Body)', nav: 'WCB 1 (Body)' });
  expect(await page.evaluate(() => [boardConfigs[1].alias, boardConfigs[1].serialPorts[0].label, boardConfigs[1].serialPorts[1].baud,
                                    document.getElementById('b1-s1-label').value]))
    .toEqual(['Body', 'Keep', 38400, 'Keep']);
  expect((await push(page, 1, { skipReboot: true })).outcome.reason, 'the type is the Wizard\'s own: nothing for the board')
    .toBe('no changes to push');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.identity_fake_alias the alias swaps the live delimiter for _ too - a | delimiter typed in General turns a|b into a_b - along with ^ , ; ? and line breaks, and the card header and the jump nav follow the alias and the WCB number', async ({ page }) => {
  // onAliasChange (app.js:2218-2243), updateBoardAliasUI (2250-2275), onWCBNumberChange (2205-2214). The cut at 24 and
  // the push are wizard.app_fake_identity's. Row identity.alias.
  await setup(page);
  await pullFake(page, 1, board([], { wcbq: 3 }));
  await holdVerifyPulls(page);
  await edit(page, '#g-delimiter', '|');
  expect(await value(page, 'g-delimiter')).toBe('|');
  await edit(page, '#b1-alias', 'a|b');
  expect(await page.evaluate(() => [document.getElementById('b1-alias').value, boardConfigs[1].alias])).toEqual(['a_b', 'a_b']);
  expect(await text(page, 'b1-alias-label')).toBe('(a_b)');
  expect(await text(page, 'wcb-nav-btn-1')).toBe('WCB 1 (a_b)');
  // ^ is no longer the delimiter but is still replaced (it would corrupt a chained backup), as is every other one.
  await edit(page, '#b1-alias', 'x^y,z;w?v');
  expect(await page.evaluate(() => boardConfigs[1].alias)).toBe('x_y_z_w_v');
  // A new WCB number: the nav caption follows it.
  await edit(page, '#b1-wcb-number', '3');
  expect(await text(page, 'wcb-nav-btn-1')).toBe('WCB 3 (x_y_z_w_v)');
  // Cleared: the header suffix goes.
  await edit(page, '#b1-alias', '');
  expect([await text(page, 'b1-alias-label'), await text(page, 'wcb-nav-btn-1')]).toEqual(['', 'WCB 3']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.identity_fake_sw_version after a pull the Wizard asks the board for its version in its own function identifier (<fc>version, collected to "End of Version") and shows it; a "Software Version:" line in a boot banner sets it too; a reply without one leaves it', async ({ page }) => {
  // fetchBoardVersion (app.js:7651-7665), called at the end of boardPull (7612-7619); the boot-banner sniff in
  // _handleLine (5940-5963); updateBoardSwVersionDisplay (248-300). The comparison states are wizard.app_fake_fw_check's.
  // Row identity.sw_version.
  await page.route('https://api.github.com/**', (route) => route.fulfill({ status: 404, body: '' }));
  await setup(page);
  await page.evaluate(() => { latestFirmwareVersion = null; });
  const collects = () => page.evaluate(() => window.__collects);
  // A board on function identifier '!': the version request uses it.
  await pullFake(page, 1, board(['FUNCCHAR,!']), { lfi: '!',
    replies: { '!version': 'Software Version: 6.2.1_290236RSEP2026\r\nBuild: hil\r\nEnd of Version' } });
  await page.evaluate(() => {
    const c = boardConnections[1];
    const real = c.sendAndCollect;
    window.__collects = [];
    c.sendAndCollect = (cmd, timeout, sentinel) => { window.__collects.push([cmd, timeout, sentinel ?? null]); return real(cmd, timeout, sentinel); };
  });
  // The pull's own fetchBoardVersion ran before the recorder: run the pull again, now recorded.
  await page.evaluate(() => boardPull(1));
  await expect.poll(() => text(page, 'b1-sw-version')).toBe('v6.2.1_290236RSEP2026 ✓');
  expect((await collects()).find((c) => c[0].endsWith('version'))).toEqual(['!version', 3000, 'End of Version']);
  expect(await page.evaluate(() => boardConfigs[1].fwVersion)).toBe('6.2.1_290236RSEP2026');

  // A boot banner's version line.
  await page.evaluate(() => boardConnections[1]._handleLine('Software Version: 6.3.0_010101RJAN2027'));
  expect(await text(page, 'b1-sw-version')).toBe('v6.3.0_010101RJAN2027 ✓');
  expect(await page.evaluate(() => boardConfigs[1].fwVersion)).toBe('6.3.0_010101RJAN2027');
  // A ?version that answers without the line changes nothing.
  await page.evaluate(() => { boardConnections[1].__replies['!version'] = 'Unknown command\r\nEnd of Version'; return fetchBoardVersion(1); });
  expect(await page.evaluate(() => boardConfigs[1].fwVersion)).toBe('6.3.0_010101RJAN2027');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.identity_fake_identify Identify sends <fc>IDENTIFY straight to a USB board, and to a relayed one as a one-chunk MGMT FRAG through its relay, each with a toast; a board or relay that is not connected is refused', async ({ page }) => {
  // boardIdentify (app.js:10297-10330). Row identity.identify.
  await setup(page);
  await pullFake(page, 1, board());
  await page.evaluate(() => { updateConnectionUI(1, true); __fake.log.length = 0; });
  let m = await toastMark(page);
  await page.locator('#b1-btn-identify').click();
  await expect.poll(() => sentOn(page, 1)).toEqual(['?IDENTIFY']);
  expect(await toastsSince(page, m)).toContain('WCB 1 identifying — watch the LED');
  await page.evaluate(() => { boardConnections[1]._connected = false; __fake.log.length = 0; });
  m = await toastMark(page);
  await page.evaluate(() => boardIdentify(1));
  expect(await sentOn(page, 1)).toEqual([]);
  expect(await toastsSince(page, m)).toContain('Board not connected');

  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await page.evaluate(() => { __fake.log.length = 0; });
  m = await toastMark(page);
  expect(await page.evaluate(() => document.getElementById('b2-btn-identify').disabled)).toBe(false);
  await page.locator('#b2-btn-identify').click();
  await expect.poll(async () => (await sentOn(page, 1)).map(frag)).toEqual(['?MGMT,FRAG,2,SID,0,1,?IDENTIFY']);
  expect(await toastsSince(page, m)).toContain('WCB 2 identifying — watch the LED');
  await page.evaluate(() => { boardConnections[1]._connected = false; __fake.log.length = 0; });
  m = await toastMark(page);
  await page.evaluate(() => boardIdentify(2));
  expect(await sentOn(page, 1)).toEqual([]);
  expect(await toastsSince(page, m)).toContain('WCB 1 (relay) not connected');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.serial_fake_claim_ui each claim locks its serial row with a note saying whose it is - Kyber, its Marcduino port (baud locked, broadcasts left free), Maestro, PWM (label still editable), MP3, HCR, DFPlayer, WLED - while a serial map and Maestro REMOTE\'s S1 are soft claims: noted, with every control left editable', async ({ page }) => {
  // updatePortClaimUI (app.js:832-919), evaluatePortClaims (parser.js:1060-1162). The MP3 row's cleared boxes are
  // wizard.app_fake_serial_claims'. Row serial.claim_ui.
  await setup(page);
  const rows = (n) => page.evaluate((n) => [1, 2, 3, 4, 5].map((p) => {
    const g = (k) => document.getElementById(`b${n}-s${p}-${k}`);
    return { note: g('claim').textContent, baud: !g('baud').disabled, bcin: !g('bcin').disabled, bcout: !g('bcout').disabled,
             label: !g('label').disabled };
  }), n);
  const LOCKED = { baud: false, bcin: false, bcout: false, label: false };
  const FREE = { baud: true, bcin: true, bcout: true, label: true };
  // WCB1: Maestro REMOTE (S1 reserved), a serial map S2 -> W2 S1, a Maestro on S3, a raw serial map S4 -> S2, PWM in on S5.
  await pullFake(page, 1, board(['MAESTRO,REMOTE', 'MAP,SERIAL,S2,W2S1', 'BAUD,S3,57600', 'MAESTRO,M2:W1S3:57600',
                                 'MAP,SERIAL,S4,R,S2', 'MAP,PWM,S5,W2S4'], { wcbq: 3 }));
  expect(await rows(1)).toEqual([
    { note: 'Maestro remote — S1 kept for the Maestro (no HCR/MP3/DFP/WLED/PWM)', ...FREE },
    { note: 'Serial map → W2 S1', ...FREE },
    { note: 'Managed by Maestro ID 2', ...LOCKED },
    { note: 'Raw serial map → S2', ...FREE },
    { note: 'Managed by PWM', ...LOCKED, label: true },
  ]);
  // A soft claim's boxes show the board's own values; a hard claim's are cleared.
  expect(await page.evaluate(() => [document.getElementById('b1-s2-bcin').checked, document.getElementById('b1-s3-bcin').checked]))
    .toEqual([true, false]);

  // WCB2: HCR on S1, a local Kyber on S2 with its Marcduino port on S3, MP3 on S4, WLED on S5.
  await pullFake(page, 2, board(['BAUD,S1,9600', 'HCR,PORT,S1:9600', 'HCR,POLL,10', ...KYBER, 'MP3,S4:9600:V3',
                                 'BAUD,S5,57600', 'WLED,1:W2S5:57600'], { wcb: 2, wcbq: 3 }));
  expect(await rows(2)).toEqual([
    { note: 'Managed by HCR Vocalizer', ...LOCKED },
    { note: 'Managed by Kyber (Maestro port)', ...LOCKED },
    { note: 'Kyber Marcduino port — baud locked at 9,600', baud: false, bcin: true, bcout: true, label: false },
    { note: 'Managed by MP3 Trigger', ...LOCKED },
    { note: 'Managed by WLED', ...LOCKED },
  ]);
  // WCB3: a DFPlayer on S3; the rest unclaimed.
  await pullFake(page, 3, board(['DFP,S3:9600:V20'], { wcb: 3, wcbq: 3 }));
  const r3 = await rows(3);
  expect(r3[2]).toEqual({ note: 'Managed by DFPlayer', ...LOCKED });
  expect(r3[0]).toEqual({ note: '', ...FREE });
  expect(await page.evaluate(() => document.getElementById('b3-s3-claim').closest('tr').classList.contains('claimed'))).toBe(true);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.serial_fake_label_combobox a port label offers the known device names as you type (case-insensitive, by substring); ArrowDown and Enter pick one, which goes into the config like a typed label and is pushed; Escape closes it; a locked (claimed) label never opens it', async ({ page }) => {
  // initDeviceCombobox, _devRender, _devSetActive, _devCommit, _devHide (app.js:693-787); device-labels.js.
  // Row serial.label_combobox.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S3,57600', 'MAESTRO,M2:W1S3:57600']));
  await holdVerifyPulls(page);
  const pop = () => page.evaluate(() => {
    const p = document.getElementById('dev-combo-pop');
    return { open: p.style.display !== 'none', items: [...p.children].map((c) => c.textContent) };
  });
  const names = await page.evaluate(() => WCB_DEVICE_LABELS);
  const label = page.locator('#b1-s1-label');
  await label.click();
  expect((await pop()).items, 'focus: the whole list').toEqual(names);
  await label.pressSequentially('mae');
  const want = names.filter((t) => t.toLowerCase().includes('mae'));
  expect(want[0]).toBe('Maestro');
  expect(await pop()).toEqual({ open: true, items: want });
  await label.press('ArrowDown');
  await label.press('Enter');
  expect(await pop()).toMatchObject({ open: false });
  expect(await label.inputValue()).toBe('Maestro');
  expect(await page.evaluate(() => boardConfigs[1].serialPorts[0].label)).toBe('Maestro');
  expect(await shown(page, 'b1-unsaved-badge'), 'a change, as a typed label is').toBe(true);
  expect(await sent(page, 1)).toEqual(['1:?LABEL,S1,Maestro']);

  // Escape closes it, the typed text stays.
  const s2 = page.locator('#b1-s2-label');
  await s2.click();
  await s2.pressSequentially('dro');
  expect((await pop()).open).toBe(true);
  await s2.press('Escape');
  expect((await pop()).open).toBe(false);
  expect(await s2.inputValue()).toBe('dro');
  // Nothing matches: no list.
  await s2.fill('');
  await s2.pressSequentially('zzzq');
  expect((await pop()).open).toBe(false);
  // The Maestro's own port: its label is locked, and focusing it opens nothing.
  await s2.press('Tab');
  await page.evaluate(() => document.getElementById('b1-s3-label').focus());
  expect(await page.evaluate(() => document.getElementById('b1-s3-label').disabled)).toBe(true);
  expect((await pop()).open).toBe(false);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.kyber_fake_marc_port moving the Kyber\'s Marcduino port sets the new port to 9600 with both broadcasts on and relabels it (no ?KYBER line); the Marcduino picker offers every port but the Kyber\'s own, and follows the Kyber port', async ({ page }) => {
  // onKyberMarcPortChange, updateKyberMarcPortDropdown (app.js:1154-1209). The relabel alone is
  // wizard.push_fake_card_edits'. Row kyber.marc_port.
  await setup(page);
  await pullFake(page, 1, board([...KYBER, 'BAUD,S4,57600', 'BCAST,OUT,S4,OFF', 'BCAST,IN,S4,OFF']));
  await holdVerifyPulls(page);
  expect(await options(page, 'b1-kyber-marc-port')).toEqual(['0', '1', '3', '4', '5']);
  expect(await value(page, 'b1-kyber-marc-port')).toBe('3');
  await edit(page, '#b1-kyber-marc-port', '4');
  expect(await page.evaluate(() => {
    const sp = boardConfigs[1].serialPorts[3];
    return [sp.baud, sp.broadcastIn, sp.broadcastOut, sp.label, document.getElementById('b1-s4-baud').value,
            document.getElementById('b1-s4-bcin').checked, document.getElementById('b1-s4-bcout').checked];
  })).toEqual([9600, true, true, 'Kyber Marcuino', '9600', true, true]);
  const r = await sent(page, 1);
  expect(r).toEqual(expect.arrayContaining(['1:?BAUD,S4,9600', '1:?BCAST,OUT,S4,ON', '1:?BCAST,IN,S4,ON', '1:?LABEL,S4,Kyber Marcuino',
                                            '1:?LABEL,CLEAR,S3']));
  expect(r.filter((s) => /KYBER/.test(s))).toEqual([]);
  // The Kyber moves to S1: the Marcduino picker now leaves out S1 and offers S2.
  await edit(page, '#b1-kyber-port', '1');
  expect(await options(page, 'b1-kyber-marc-port')).toEqual(['0', '2', '3', '4', '5']);
  expect(await value(page, 'b1-kyber-marc-port')).toBe('4');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.kyber_fake_targets_info the Kyber Targets panel on the local-Kyber board lists every board\'s Maestros - id, WCB, port, baud - its own and those of a board pulled after it, or says none are configured', async ({ page }) => {
  // populateKyberTargetsFromConfig (app.js:10185-10201), called by populateUIFromConfig (3424-3434) and onMaestroChange
  // (3812-3825). The panel was filled only when the Kyber board itself was loaded, so a board pulled after it never
  // showed there: fixed 2026-10-13 (populateUIFromConfig refreshes the Kyber board's panel). Row kyber.targets_info.
  const info = () => page.evaluate(() => document.getElementById('b1-kyber-targets-info').textContent.replace(/\s+/g, ' ').trim());
  await setup(page);
  await pullFake(page, 1, board(KYBER));
  expect(await info()).toBe('No Maestros configured yet — add Maestros to each board first, then push.');
  await setup(page);
  await pullFake(page, 1, board([...KYBER, 'BAUD,S1,57600', 'MAESTRO,M1:W1S1:57600']));
  expect(await info()).toBe('Will target: M1 · WCB1 S1 · 57,600 baud');
  // WCB2 pulled afterwards, with two Maestros: the Kyber board's panel names them too.
  await pullFake(page, 2, board(['BAUD,S1,115200', 'BAUD,S3,57600', 'MAESTRO,M2:W2S1:115200,M3:W2S3:57600'], { wcb: 2 }));
  expect(await info()).toBe('Will target: M1 · WCB1 S1 · 57,600 baud | M2 · WCB2 S1 · 115,200 baud | M3 · WCB2 S3 · 57,600 baud');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.kyber_fake_repush_warning a Maestro edit on another board reminds the user to re-push the local-Kyber board, once per 30 s; an edit on the Kyber board itself does not', async ({ page }) => {
  // showKyberRepushWarning (app.js:10164-10175), onMaestroChange (3807-3826). Row kyber.repush_warning.
  await page.clock.install();
  await setup(page);
  await pullFake(page, 1, board(KYBER));
  await pullFake(page, 2, board([], { wcb: 2 }));
  const warned = (m) => toastsSince(page, m).then((t) => t.split('\n').filter((l) => l.includes('has Kyber Local')));
  const addMaestro = (n, port) => page.evaluate(({ n, port }) => {
    addMaestroRow(n);
    const sel = [...document.querySelectorAll(`#b${n}-maestro-tbody tr [id$="-port"]`)].at(-1);
    sel.value = String(port);
    sel.dispatchEvent(new Event('change', { bubbles: true }));
  }, { n, port });
  let m = await toastMark(page);
  await addMaestro(2, 1);
  expect(await warned(m)).toEqual(['WCB 1 has Kyber Local — re-push WCB 1 after finishing all board configurations']);
  m = await toastMark(page);
  await addMaestro(2, 3);
  expect(await warned(m), 'again within 30 s: no second reminder').toEqual([]);
  await page.clock.fastForward(30_500);
  m = await toastMark(page);
  await page.evaluate(() => onMaestroChange(2));
  expect((await warned(m)).length, '30 s on: reminded again').toBe(1);
  await page.clock.fastForward(30_500);
  m = await toastMark(page);
  await addMaestro(1, 1);
  expect(await warned(m), 'an edit on the Kyber board itself').toEqual([]);
  expect(page.wizErrors).toEqual([]);
});
