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
