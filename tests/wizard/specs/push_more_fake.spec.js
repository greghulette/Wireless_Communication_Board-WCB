// Needs no board: the rest of the Wizard's PUSH side (docs/hil_plan/WCB.md WCB-WP20) - the cards push_fake.spec.js
// leaves out, the relay push's reboot and baseline, Push All's stages, the USB pull guards and the General fields shared
// by every board. Fake boards in the real page (lib/fake.js): boardPull, boardGo, boardGoRemote and boardGoAll run as
// on the bench, and only the wire is recorded. Runs standalone, in CI, and under the harness by id (s30_wizard.py).
//
// (should) tests assert what the Wizard ought to do and fail until it does (test.fail, so CI stays green and turns red
// the day the fix lands; the harness reports them FAIL). Each names its W-row in docs/hil_plan/WCB.md §3.
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { board, backup, chain, install, pullFake, push, edit, relayFake } = require('../lib/fake');

// The fixtures push_fake.spec.js uses for these cards, restated here so a change there cannot move these tests.
const KYBER = board(['BAUD,S2,115200', 'LABEL,S2,Kyber Maestro', 'LABEL,S3,Kyber Marcuino', 'BCAST,OUT,S2,OFF',
                     'BCAST,IN,S2,OFF', 'KYBER,LOCAL,S2,M1:W2S1:57600,M2:W2S2:57600']);
const MAPS = board(['MAP,PWM,OUT,S5', 'MAP,SERIAL,S2,R,S3,W2S4', 'MAP,PWM,S1,S4']);
const SEQS = board(['SEQ,SAVE,hello,;S1hi^;t500^;S2there', 'SEQ,SAVE,gate,IF,lights=1^;t200^;M11']);
const NO_CHANGES = { ok: true, aborted: false, reason: 'no changes to push' };

test('wizard.push_fake_card_edits one edit on each card push_fake leaves out sends only that edit: DFPlayer volume, the Kyber Marcuino port, a sequence row, the alias; a mapping edit re-sends every mapping (left as found)', async ({ page }) => {
  const cases = [
    // [what, fixture, edits, the commands the push must send (skipReboot: the fake has no port to reopen)]
    ['DFPlayer volume', board(['DFP,S3:9600:V20']), [['#b1-dfp-vol', '25']], ['?DFP,S3:9600:V25']],
    // The Marcuino port is a label, not a Kyber field: the firmware keeps no Marcuino setting (parser.js infers it from
    // 'Kyber Marcuino'), so moving it relabels two ports and sends no ?KYBER line.
    ['Kyber Marcuino S3 -> S4', KYBER, [['#b1-kyber-marc-port', '4']], ['?LABEL,CLEAR,S3', '?LABEL,S4,Kyber Marcuino']],
    ['alias', board(['ALIAS,Dome']), [['#b1-alias', 'Dome2']], ['?ALIAS,Dome2']],
    ['alias cleared', board(['ALIAS,Dome']), [['#b1-alias', '']], ['?ALIAS,CLEAR']],
    // One mapping edited (Raw off): the builder re-sends the whole table (parser.js mappingsChanged), and the PWM input
    // among it asks for a reboot. docs/hil_plan/WCB.md §3 records this as left as found after the W-5 fix.
    ['serial mapping Raw off', MAPS, [['#b1-mappings-container [id$="-raw"]', false]],
      ['?MAP,SERIAL,S2,S3,W2S4', '?MAP,PWM,S1,S4']],
  ];
  for (const [what, tokens, edits, want] of cases) {
    await openWizard(page);
    await pullFake(page, 1, tokens);
    for (const [selector, value] of edits) await edit(page, selector, value);
    const r = await push(page, 1, { skipReboot: true });
    expect.soft(r.sent.map((s) => s.slice(2)), what).toEqual(want);
  }

  // A sequence row edited in its textarea: that key alone, joined with the board's delimiter.
  await openWizard(page);
  await pullFake(page, 1, SEQS);
  await page.evaluate(() => {
    const ta = document.querySelectorAll('#b1-seq-tbody .seq-val-textarea')[0];
    ta.value = ';S1bye\n;S2now';
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
  expect.soft((await push(page, 1)).sent, 'sequence row edited').toEqual(['1:?SEQ,SAVE,hello,;S1bye^;S2now']);

  // The mapping edit above asks for a reboot; without skipReboot a fake-port reboot is the reconnect path, which this
  // spec does not follow. What it returned says so.
  await openWizard(page);
  await pullFake(page, 1, MAPS);
  await edit(page, '#b1-mappings-container [id$="-raw"]', false);
  expect((await push(page, 1, { skipReboot: true })).returned, 'the PWM input in the re-sent table needs a reboot').toBe(true);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_kyber_own_maestro (should) a local-Kyber board with a Maestro of its own and one on another board, pulled and pushed with no edit, sends nothing (W-20)', async ({ page }) => {
  await openWizard(page);
  // In collectConfigCommands' order: KYBER,CLEAR early (board()), the Maestro table, then KYBER,LOCAL with its targets.
  await pullFake(page, 1, board(['BAUD,S1,57600', 'BAUD,S2,115200', 'LABEL,S2,Kyber Maestro', 'BCAST,OUT,S2,OFF',
                                 'BCAST,IN,S2,OFF', 'MAESTRO,M1:W1S1:57600', 'MAESTRO,M2:W2S1:57600',
                                 'KYBER,LOCAL,S2,M1:W1S1:57600,M2:W2S1:57600']));
  const r = await push(page, 1, { skipReboot: true });
  test.info().annotations.push({ type: 'today', description: JSON.stringify(r.sent) });
  expect(r.sent).toEqual([]);
  expect(r.outcome.reason).toBe('no changes to push');
});

test('wizard.push_fake_relay_reboot a relay push that needs a reboot ends its session with ^?reboot (not with skipReboot), a MAC or channel change asks first and Confirm sends it, and the baseline moves to the pushed config', async ({ page }) => {
  const W2 = chain(board(['LABEL,S5,Holo'], { wcb: 2 }));
  const frags = () => page.evaluate(() => __fake.sent(1).filter((s) => s.includes('MGMT,FRAG,2,')));
  const session = (lines) => {
    const m = lines.map((s) => /^\?MGMT,FRAG,2,([0-9A-F]{4}),(\d+),(\d+),([\s\S]*)$/.exec(s));
    expect(m.every(Boolean), lines.join('\n')).toBe(true);
    expect(new Set(m.map((x) => x[1])).size, 'one session').toBe(1);
    return m.map((x) => x[4]).join('');
  };

  // A hardware-version change reboots (commandStringNeedsReboot): the reboot rides inside the same session.
  await openWizard(page);
  await relayFake(page, 1, { 2: W2 });
  await page.evaluate(() => { __fake.log.length = 0; });
  await edit(page, '#b2-hw-version', '23');
  let r = await page.evaluate(async () => ({ ret: await boardGo(2), outcome: boardPushOutcome[2],
                                            base: boardBaselines[2].hwVersion, cfg: boardConfigs[2].hwVersion }));
  expect(r.outcome).toEqual({ ok: true, aborted: false, reason: '' });
  expect(r.ret, 'boardGoRemote reports the reboot to Push All').toBe(true);
  expect(session(await frags())).toBe('?HW,23^?reboot');
  // Optimistic: the relay path has no ACK to wait for, so the baseline becomes what was sent (app.js boardGoRemote).
  expect([r.base, r.cfg]).toEqual([23, 23]);

  // Push All's stage 1 passes skipConfirm; skipReboot leaves the ^?reboot off.
  await openWizard(page);
  await relayFake(page, 1, { 2: W2 });
  await page.evaluate(() => { __fake.log.length = 0; });
  await edit(page, '#b2-hw-version', '23');
  await page.evaluate(() => boardGo(2, { skipReboot: true }));
  expect(session(await frags())).toBe('?HW,23');

  // A MAC octet or the mesh channel opens the network-group confirm; Confirm sends the push with its reboot.
  for (const [what, id, value, cmd] of [['MAC octet 3', 'g-mac3', '4C', '?MAC,3,4C'], ['mesh channel', 'g-meshch', '6', '?WCBCH,6']]) {
    await openWizard(page);
    await relayFake(page, 1, { 2: W2 });
    await page.evaluate(({ id, value }) => {
      __fake.log.length = 0;
      document.getElementById(id).value = value;
      window.__go = boardGo(2);
    }, { id, value });
    await expect(page.locator('#network-group-change-modal.open'), what).toBeVisible();
    expect(await page.locator('#network-group-change-body').textContent(), what)
      .toContain(id === 'g-mac3' ? 'MAC octets' : 'Mesh channel');
    expect(await frags(), `${what}: nothing before the confirm`).toEqual([]);
    await page.locator('#network-group-change-confirm').click();
    await page.evaluate(() => window.__go);
    expect.soft(session(await frags()), what).toBe(`${cmd}^?reboot`);
  }
  expect(page.wizErrors).toEqual([]);
});

// Three boards for Push All: slot 1 a USB board that is also the relay for slot 3, slot 2 a USB board, slot 3 managed
// through slot 1, and a MgmtRelay card in slot 19 that must never be pushed. Every WCB gets a label (a plain change)
// and hardware version 2.3 (a change that needs a reboot, commandStringNeedsReboot).
async function threeBoards(page, { sharedRelay = false } = {}) {
  await openWizard(page);
  await pullFake(page, 1, board([], { wcb: 1, wcbq: 3 }), { shared: sharedRelay });
  await pullFake(page, 2, board([], { wcb: 2, wcbq: 3 }));
  await page.evaluate(async (w3) => {
    addDiscoveredBoards([3]);
    setRemoteConnected(3, 1);
    const cfg = WCBParser.parseBackupString(w3);
    boardConfigs[3] = cfg;
    boardBaselines[3] = JSON.parse(JSON.stringify(cfg));
    populateUIFromConfig(3, cfg);
    __fake.conn(19, {});
    _relaySlots.add(19);
    await new Promise((r) => setTimeout(r, 1000));   // setRemoteConnected's RTERM,START sends
  }, chain(board([], { wcb: 3, wcbq: 3 })));
  for (const n of [1, 2, 3]) {
    await edit(page, `#b${n}-s5-label`, `HIL${n}`);
    await edit(page, `#b${n}-hw-version`, '23');
  }
  await page.evaluate(() => { __fake.log.length = 0; generalSettingsDirty = true; });
}

// Index of the first log entry matching, or -1.
const at = (log, fn) => log.findIndex(fn);

test('wizard.push_fake_all_staged Push All runs remote boards first with the reboot inside their session, then the USB boards, then the relay without a reboot, and reboots the relay last; a MgmtRelay card is never pushed and the General dirty flag clears', async ({ page }) => {
  await threeBoards(page);
  await page.evaluate(() => boardGoAll());
  await page.waitForTimeout(4500);          // the direct reboots' reconnect + 3 s verify pulls
  const r = await page.evaluate(() => ({ log: __fake.log.map((e) => ({ slot: e.slot, op: e.op, s: e.s })),
                                         dirty: generalSettingsDirty }));
  const log = r.log;
  const frag3 = log.filter((e) => e.slot === 1 && e.s.includes('MGMT,FRAG,3,') && !e.s.includes('RTERM'));
  const m = frag3.map((e) => /^\?MGMT,FRAG,3,[0-9A-F]{4},\d+,\d+,([\s\S]*)$/.exec(e.s)[1]);
  expect(m.join(''), 'stage 1: WCB3\'s whole push and its reboot in one session').toBe('?HW,23^?LABEL,S5,HIL3^?reboot');
  const w2 = log.filter((e) => e.slot === 2 && (e.op === 'await' || e.op === 'send')).map((e) => e.s);
  expect(w2.slice(0, 3), 'stage 2: WCB2 ACK-paced, then its own reboot').toEqual(['?HW,23', '?LABEL,S5,HIL2', '?reboot']);
  const w1 = log.filter((e) => e.slot === 1 && e.op === 'await').map((e) => e.s);
  expect(w1, 'stage 3: the relay\'s own push').toEqual(['?HW,23', '?LABEL,S5,HIL1']);
  const i3  = at(log, (e) => e.slot === 1 && e.s.includes('MGMT,FRAG,3,') && !e.s.includes('RTERM'));
  const i2  = at(log, (e) => e.slot === 2 && e.op === 'await');
  const i2r = at(log, (e) => e.slot === 2 && e.s === '?reboot');
  const i1  = at(log, (e) => e.slot === 1 && e.op === 'await');
  const i1r = at(log, (e) => e.slot === 1 && e.s === '?reboot');
  expect([i3, i2, i2r, i1, i1r].every((i) => i >= 0), JSON.stringify([i3, i2, i2r, i1, i1r])).toBe(true);
  expect(i3 < i2 && i2 < i2r && i2r < i1 && i1 < i1r, `order ${[i3, i2, i2r, i1, i1r]}`).toBe(true);
  expect(log.filter((e) => e.slot === 1 && e.s === '?reboot').length, 'one relay reboot').toBe(1);
  // Both USB boards closed, reconnected and were pulled again after their reboots.
  for (const n of [1, 2]) {
    const ir = at(log, (e) => e.slot === n && e.s === '?reboot');
    expect.soft(log.slice(ir).filter((e) => e.slot === n).map((e) => e.op).filter((o) => o !== 'send' && o !== 'await').slice(0, 3),
                `WCB${n} after its reboot`).toEqual(['close', 'reconnect', 'collect']);
  }
  expect(log.filter((e) => e.slot === 19).map((e) => e.s), 'the MgmtRelay card').toEqual([]);
  expect(r.dirty).toBe(false);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_all_shared_relay (should) Push All with the relay on the shared port reboots it without reporting it lost: no "did not come back", and its card stays connected (W-15)', async ({ page }) => {
  await threeBoards(page, { sharedRelay: true });
  // A card shows its connection by its Connect button (updateConnectionUI; no card has a b<n>-conn-label, as
  // board_more.spec.js notes), and the fake connects without the page's connect path: show WCB1 connected first.
  await page.evaluate(() => updateConnectionUI(1, true));
  await page.evaluate(() => boardGoAll());
  await page.waitForTimeout(4500);
  const r = await page.evaluate(() => ({
    sent: __fake.sent(1), toasts: __fake.toastText(),
    connect: document.getElementById('b1-btn-connect')?.textContent, go: document.getElementById('b1-btn-go')?.disabled,
  }));
  expect(r.sent).toContain('?reboot');
  expect(r.toasts).not.toContain('did not come back');
  expect([r.connect, r.go], 'the shared relay still shown connected, Push enabled').toEqual(['Disconnect', false]);
});

test('wizard.push_fake_reboot_path a USB push that needs a reboot sends ?reboot 1.5 s after the last ACK, closes and reopens the port, and pulls 3 s after the reconnect', async ({ page }) => {
  await openWizard(page);
  await pullFake(page, 1, board());
  await edit(page, '#b1-hw-version', '23');
  await page.evaluate(() => { __fake.log.length = 0; });
  const r = await push(page, 1);
  expect(r.outcome).toEqual({ ok: true, aborted: false, reason: '' });
  expect(r.returned, 'boardGo reports the reboot').toBe(true);
  await page.waitForTimeout(4000);
  const log = await page.evaluate(() => __fake.log.map((e) => ({ op: e.op, s: e.s, at: e.at })));
  const ack = log.find((e) => e.op === 'await' && e.s === '?HW,23');
  const reboot = log.find((e) => e.op === 'send' && e.s === '?reboot');
  const close = log.find((e) => e.op === 'close');
  const reopen = log.find((e) => e.op === 'reconnect');
  const pull = log.find((e) => e.op === 'collect' && e.s === 'WCB_WEBTOOL_CONFIG_PULL');
  expect([ack, reboot, close, reopen, pull].every(Boolean), JSON.stringify(log.map((e) => e.op))).toBe(true);
  expect(reboot.at - ack.at, 'the NVS writes get 1.5 s').toBeGreaterThanOrEqual(1450);
  expect(close.at - reboot.at, 'the port closes right after').toBeLessThan(1000);
  expect(pull.at - reopen.at, 'and is pulled 3 s after it reopens').toBeGreaterThanOrEqual(2950);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_shared_reboot_repull (should) a push that reboots a board on the shared port pulls it again afterwards, as a USB push does (W-14)', async ({ page }) => {
  await openWizard(page);
  await pullFake(page, 1, board(), { shared: true });
  await edit(page, '#b1-hw-version', '23');
  await page.evaluate(() => { __fake.log.length = 0; });
  const r = await push(page, 1);
  expect(r.sent).toEqual(['1:?HW,23', '1:?reboot']);
  await page.waitForTimeout(9000);          // the direct path pulls ~3 s after its reconnect; allow a slow boot
  const pulls = await page.evaluate(() => __fake.log.filter((e) => e.op === 'collect' && e.s === 'WCB_WEBTOOL_CONFIG_PULL').length);
  expect(pulls, 'a verify pull after the reboot').toBeGreaterThan(0);
});

test('wizard.pull_fake_usb_guards the USB pull: a backup cut short changes nothing, the fixed pull command goes first, a ?RELAY,1 device becomes a relay card, a board reporting another number moves slot, a duplicate number warns, and the first pull seeds General', async ({ page }) => {
  // (a) A backup without its end marker keeps the slot's config and baseline, and says so.
  await openWizard(page);
  await pullFake(page, 1, board(['LABEL,S1,Kept']));
  const keep = () => page.evaluate(() => JSON.stringify([boardConfigs[1], boardBaselines[1]]));
  const before = await keep();
  await page.evaluate((cut) => {
    const c = boardConnections[1];
    c.__backup = cut;
    c.__replies['?backup'] = cut;
    return boardPull(1);
  }, backup(board(['LABEL,S1,Changed']), { end: false }));
  expect(await keep(), 'a cut backup must not replace the config or the baseline').toBe(before);
  expect(await page.locator('#b1-status-badge').textContent()).toContain('Pull Failed');
  expect(await page.evaluate(() => __fake.toastText())).toContain('config pull incomplete');

  // (b) WCB_WEBTOOL_CONFIG_PULL first; an empty answer falls back to <funcChar>backup.
  await openWizard(page);
  await install(page);
  await page.evaluate(async (text) => {
    __fake.conn(1, { backup: '', replies: { '?backup': text } });
    await boardPull(1);
  }, backup(board(['LABEL,S1,Fallback'])));
  expect((await page.evaluate(() => __fake.log.filter((e) => e.op === 'collect').map((e) => e.s))).slice(0, 2))
    .toEqual(['WCB_WEBTOOL_CONFIG_PULL', '?backup']);
  expect(await page.evaluate(() => boardBaselines[1]?.serialPorts[0].label)).toBe('Fallback');

  // (c) A MgmtRelay (?RELAY,1) gets a relay card anchored at its number, no board section, and no General baseline.
  await openWizard(page);
  await pullFake(page, 1, board(['RELAY,1'], { wcb: 19 }));
  expect(await page.evaluate(() => ({
    relay: [..._relaySlots], card: !!document.getElementById('relay-card-19'), section: !!document.getElementById('section-board-19'),
    slot1: !!boardConnections[1], conn19: !!boardConnections[19], general: generalBaseline,
  }))).toEqual({ relay: [19], card: true, section: false, slot1: false, conn19: true, general: null });

  // (d) A board that reports WCB 3 in slot 1 moves to slot 3, connection and config together.
  await openWizard(page);
  await pullFake(page, 1, board([], { wcb: 3, wcbq: 3 }));
  expect(await page.evaluate(() => ({
    conn: [!!boardConnections[1], !!boardConnections[3]], cfg: [!!boardConfigs[1], boardConfigs[3]?.wcbNumber],
    base: boardBaselines[3]?.wcbNumber, section: !!document.getElementById('section-board-3'),
  }))).toEqual({ conn: [false, true], cfg: [false, 3], base: 3, section: true });
  expect(await page.evaluate(() => __fake.toastText())).toContain('moved from slot 1');

  // (e) Two slots reporting one number: said once, from the second pull.
  await openWizard(page);
  await pullFake(page, 1, board());
  await pullFake(page, 2, board());
  expect(await page.evaluate(() => __fake.toastText())).toContain('Slot 2 and slot 1 both report WCB 1');

  // (f) The first pull seeds General from the board; a second board that differs opens the keep/use modal.
  await openWizard(page);
  await pullFake(page, 1, board());
  expect(await page.evaluate(() => [document.getElementById('g-mac3').value, generalBaseline?.sourceBoard])).toEqual(['4B', 1]);
  await pullFake(page, 2, board(['MAC,3,4C'], { wcb: 2 }));
  await expect(page.locator('#general-conflict-modal.open')).toBeVisible();
  expect(await page.locator('#general-conflict-body').textContent()).toContain('MAC Octet 3');
  expect(page.wizErrors).toEqual([]);
});

// Two pulled boards whose mesh passwords differ (both dummies). The first seeds General; the second opens the modal.
const OTHER = 'dummy_other_mesh_password';
const conflictRows = (page) => page.$$eval('#general-conflict-body tbody tr td:first-child', (tds) => tds.map((t) => t.textContent));

test('wizard.push_fake_general_fields the General fields reach every board\'s push: a second board whose password differs opens the keep/use modal, Keep makes its push send the first board\'s password (with the network-group confirm through a relay), Use incoming rewrites General', async ({ page }) => {
  // Keep, both on USB: WCB2's next push carries WCB1's password; WCB1 has nothing to send.
  await openWizard(page);
  await pullFake(page, 1, board());
  await pullFake(page, 2, board([`EPASS,${OTHER}`], { wcb: 2 }));
  await expect(page.locator('#general-conflict-modal.open')).toBeVisible();
  expect(await conflictRows(page)).toEqual(['ESP-NOW Password']);
  await page.locator('#general-conflict-keep').click();
  expect(await page.locator('#b2-unsaved-badge').isVisible(), 'WCB2 marked unsaved').toBe(true);
  expect((await push(page, 2, { skipReboot: true })).sent).toEqual([`2:?EPASS,${require('../lib/fake').EPASS}`]);
  expect((await push(page, 1, { skipReboot: true })).outcome.reason).toBe('no changes to push');

  // Use incoming: General takes WCB2's value, and it is WCB1 that now has a change to push.
  await openWizard(page);
  await pullFake(page, 1, board());
  await pullFake(page, 2, board([`EPASS,${OTHER}`], { wcb: 2 }));
  await expect(page.locator('#general-conflict-modal.open')).toBeVisible();
  await page.locator('#general-conflict-use').click();
  expect(await page.evaluate(() => [document.getElementById('g-password').value, generalBaseline.sourceBoard])).toEqual([OTHER, 2]);
  expect((await push(page, 1, { skipReboot: true })).sent).toEqual([`1:?EPASS,${OTHER}`]);
  expect((await push(page, 2, { skipReboot: true })).outcome.reason).toBe('no changes to push');

  // Keep with WCB2 behind a relay: its push is a network-group change, so it asks first.
  await openWizard(page);
  await pullFake(page, 1, board([], { wcbq: 2 }));
  await page.evaluate((w2) => {
    addDiscoveredBoards([2]);
    setRemoteConnected(2, 1);
    _applyRemotePulledConfig(1, 2, w2);           // what remoteBoardPull does with a reply that verified
  }, chain(board([`EPASS,${OTHER}`], { wcb: 2 })));
  await expect(page.locator('#general-conflict-modal.open')).toBeVisible();
  await page.locator('#general-conflict-keep').click();
  await page.evaluate(() => { __fake.log.length = 0; window.__go = boardGo(2); });
  await expect(page.locator('#network-group-change-modal.open')).toBeVisible();
  await page.locator('#network-group-change-confirm').click();
  await page.evaluate(() => window.__go);
  const frag = await page.evaluate(() => __fake.sent(1).filter((s) => s.includes('MGMT,FRAG,2,') && !s.includes('RTERM')));
  expect(frag.map((s) => s.replace(/^\?MGMT,FRAG,2,[0-9A-F]{4},0,1,/, ''))).toEqual([`?EPASS,${require('../lib/fake').EPASS}`]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_planned_verbs the bench specs\' pre-flight (lib/wizard.js plannedVerbs) names the verbs a push then sends, on a USB board and through a relay, sends nothing itself, and would stop a push carrying another board\'s WCB quantity', async ({ page }) => {
  const { plannedVerbs } = require('../lib/wizard');
  const verbs = (sent) => sent.map((s) => s.replace(/^\d+:./, '').split(',')[0]);
  await openWizard(page);
  await pullFake(page, 1, board());
  await edit(page, '#b1-s5-label', 'HIL1');
  await page.evaluate(() => { boardBaselines[1].hwVersion = 0; __fake.log.length = 0; });
  expect(await plannedVerbs(page, 1)).toEqual(['HW', 'LABEL']);
  expect(await page.evaluate(() => __fake.log.length), 'the check itself sends nothing').toBe(0);
  expect(verbs((await push(page, 1, { skipReboot: true })).sent)).toEqual(['HW', 'LABEL']);

  await openWizard(page);
  await relayFake(page, 1, { 2: chain(board(['LABEL,S5,Old'], { wcb: 2 })) });
  await edit(page, '#b2-s5-label', 'HIL2');
  expect(await plannedVerbs(page, 2)).toEqual(['LABEL']);
  await page.evaluate(async () => { __fake.log.length = 0; await boardGo(2); });
  const frag = await page.evaluate(() => __fake.sent(1).filter((s) => s.includes('MGMT,FRAG,2,') && !s.includes('RTERM')));
  expect(frag.map((s) => s.replace(/^\?MGMT,FRAG,2,[0-9A-F]{4},0,1,\?/, '').split(',')[0])).toEqual(['LABEL']);

  // Two USB boards on different quantities: the second board's plan carries WCBQ (W-16), which the bench specs refuse.
  await openWizard(page);
  await pullFake(page, 1, board([], { wcbq: 2 }));
  await pullFake(page, 2, board([], { wcb: 2, wcbq: 3 }));
  await edit(page, '#b2-s5-label', 'HIL2');
  expect(await plannedVerbs(page, 2)).toEqual(['WCBQ', 'LABEL']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_general_wcbq (should) a second board whose WCB quantity differs is named in the keep/use modal, like every other General field that goes into every board\'s push (W-16)', async ({ page }) => {
  await openWizard(page);
  await pullFake(page, 1, board([], { wcbq: 2 }));
  await pullFake(page, 2, board([], { wcb: 2, wcbq: 3 }));
  await page.waitForTimeout(500);                    // showGeneralMismatchModal runs 200 ms after the pull
  // What happens today, for the record: a label edit on WCB2 also sends WCB1's quantity.
  await edit(page, '#b2-s5-label', 'Dome');
  const today = (await push(page, 2, { skipReboot: true })).sent;
  test.info().annotations.push({ type: 'today', description: JSON.stringify(today) });
  await expect(page.locator('#general-conflict-modal.open')).toBeVisible({ timeout: 1000 });
  expect(await conflictRows(page)).toContain('WCB Quantity');
});
