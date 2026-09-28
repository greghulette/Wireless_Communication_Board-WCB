// Needs no board: the PUSH side of the Wizard - boardGo, boardGoRemote and everything that feeds them - driven through
// fake connections in the real page. Runs standalone (`npx playwright test`) and in CI (.github/workflows/
// wizard-tests.yml runs every spec). docs/hil_plan/WCB.md WCB-WP20/WP41; each test names the Wizard defect (§3, W-n)
// it pins.
//
// A pull is the real boardPull against a fake USB connection whose sendAndCollect answers with a ?backup printed the
// way W1 prints it (printBackupConfig), so parseBackupString, syncGeneralFromConfig and populateUIFromConfig run
// exactly as on the bench. A push is the real boardGo; its commands are what the fake's sendAndAwaitIdle records.
// The rule the no-op tests hold the Wizard to: a board pulled and pushed with no edit sends NOTHING. A push that
// rewrites one unedited setting - a label, a broadcast flag, a mapping that reboots the board - is a config change
// the user never made, written to real hardware.
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');

const EPASS = 'dummy_not_a_real_mesh_password';

// The board every fixture starts from, in collectConfigCommands' order (WCB.ino), then the fixture's own lines. Later
// lines win, as they do on the board, so a fixture can override HW, WIFI, KYBER or an ETM value.
function board(extra = []) {
  return ['HW,24', 'MAC,2,05', 'MAC,3,4B', 'WCB,1', 'WCBQ,2', 'WCBCH,1', 'WIFI,OFF', 'PEERSLIVE,2', `EPASS,${EPASS}`,
          'CMDCHAR,;', 'KYBER,CLEAR', 'ETM,ON', 'ETM,TIMEOUT,500', 'ETM,HB,10', 'ETM,MISS,5', 'ETM,BOOT,2',
          'ETM,COUNT,20', 'ETM,DELAY,100', 'ETM,CHKSM,ON', ...extra];
}

// A ?backup as printBackupConfig prints it: one command per line behind the board's CURRENT function identifier.
function backup(tokens, lfi = '?') {
  return ['', '*** ========================================', '*** WCB Configuration Backup',
          '*** Copy and paste these commands to restore', '*** ========================================', '',
          ...tokens.map((t) => lfi + t), '',
          "*** === For Configured Boards (Current Delimiter: '^') ===", '(chain not read by the Wizard)', '',
          "*** === For Factory Reset/Fresh Boards (Uses Default '^' Delimiter) ===", '(chain not read by the Wizard)', '',
          '--------- End of Backup ---------', ''].join('\r\n');
}

// One fixture per card (WP20 wiz.dom.noop_push_rich_config), each a real board's lines.
const CARDS = {
  'MP3 on S2 at 38400, volume 3, ONERR':    board(['BAUD,S2,38400', 'MP3,S2:38400:V3', 'MP3,ONERR,oops']),
  'DFPlayer on S3':                         board(['DFP,S3:9600:V20']),
  'HCR on S4 at 57600, poll 0':             board(['BAUD,S4,57600', 'HCR,PORT,S4:57600', 'HCR,POLL,0']),
  'WLED 3 on S2, broadcasts left on':       board(['BAUD,S2,115200', 'WLED,3:W1S2:115200']),
  'WLED 3 on S2 with its own label':        board(['BAUD,S2,115200', 'LABEL,S2,Dome Lights', 'WLED,3:W1S2:115200']),
  "Maestro on S1 labelled 'Dome Maestro'":  board(['BAUD,S1,57600', 'LABEL,S1,Dome Maestro', 'BCAST,OUT,S1,OFF',
                                                   'BCAST,IN,S1,OFF', 'MAESTRO,M1:W1S1:57600']),
  'Maestro on S1 with no label':            board(['BAUD,S1,57600', 'BCAST,OUT,S1,OFF', 'BCAST,IN,S1,OFF',
                                                   'MAESTRO,M1:W1S1:57600']),
  'Kyber LOCAL on S2, targets, Marcuino':   board(['BAUD,S2,115200', 'LABEL,S2,Kyber Maestro', 'LABEL,S3,Kyber Marcuino',
                                                   'BCAST,OUT,S2,OFF', 'BCAST,IN,S2,OFF',
                                                   'KYBER,LOCAL,S2,M1:W2S1:57600,M2:W2S2:57600']),
  'serial and PWM mappings, PWM output':    board(['MAP,PWM,OUT,S5', 'MAP,SERIAL,S2,R,S3,W2S4', 'MAP,PWM,S1,S4']),
  'WDP and auto-join off':                  board(['WDP,OFF', 'WDP,AUTOJOIN,OFF']),
  'WiFi JOIN, a comma in the passphrase':   board(['WIFI,JOIN,NaviCore-20,pass,word 1']),
  'HW 3.2, LED on GPIO47':                  board(['HW,32', 'LED,PIN,47']),
  'variables':                              board(['VAR,SET,lights,1', 'VAR,SET,count,-5']),
  'sequences':                              board(['SEQ,SAVE,hello,;S1hi^;t500^;S2there',
                                                   'SEQ,SAVE,gate,IF,lights=1^;t200^;M11']),
  'alias, disabled controller id 15':       board(['ALIAS,Dome', 'CONTROLLER,ON,15', 'CONTROLLER,OFF']),
  'ETM message delay 0':                    board(['ETM,DELAY,0']),
};
// W-2: a board whose delimiter is ',' (older firmware accepted it), captured under ?FUNCCHAR,! and ?CMDCHAR,/.
const COMMA_BOARD = board(['DELIM,,', 'FUNCCHAR,!', 'CMDCHAR,/']);

// A fake USB board in slot 1, pulled through the real boardPull. Every send is recorded in window.__sent.
async function pullFake(page, tokens, lfi = '?') {
  await page.evaluate(async (text) => {
    window._wdpMeshConn = () => null;   // the 12 s mesh poll would send ?WDP,DUMP through the fake
    window.__backup = text;
    window.__sent = [];
    if (!boardConnections[1]) {
      const conn = new BoardConnection(1);
      conn._connected = true;
      conn.send = async (s) => { __sent.push(String(s).replace(/\r$/, '')); };
      conn.sendAndAwaitIdle = async (s) => { __sent.push(s); return { ok: true }; };
      conn.sendAndCollect = async (s) => (s === 'WCB_WEBTOOL_CONFIG_PULL' ? window.__backup : '');
      boardConnections[1] = conn;
    }
    await boardPull(1);
    __sent.length = 0;
  }, backup(tokens, lfi));
  expect(await page.evaluate(() => !!boardBaselines[1])).toBe(true);
}

// boardGo(1) as the Push button runs it; what it sent and how it ended. skipReboot keeps a reboot-needing push off
// the close/reconnect path (the fake has no port to reopen) without changing the commands.
async function push(page, opts = {}) {
  return page.evaluate(async (opts) => {
    __sent.length = 0;
    await boardGo(1, opts);
    return { outcome: boardPushOutcome[1], sent: [...__sent] };
  }, opts);
}

// Set a field the way a user would, and fire the handlers the page listens on.
function edit(page, selector, value) {
  return page.evaluate(({ selector, value }) => {
    const el = document.querySelector(selector);
    if (!el) throw new Error(`no element ${selector}`);
    if (el.type === 'checkbox' || el.type === 'radio') el.checked = value;
    else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, { selector, value });
}

const NO_CHANGES = { outcome: { ok: true, aborted: false, reason: 'no changes to push' }, sent: [] };

test('wizard.push_fake_noop every card of a pulled board, pushed with no edit, sends nothing (W-4, W-1, W-2)', async ({ page }) => {
  const cases = { ...CARDS, "delimiter ',' under function id '!'": COMMA_BOARD };
  for (const [name, tokens] of Object.entries(cases)) {
    await openWizard(page);
    await pullFake(page, tokens, tokens === COMMA_BOARD ? '!' : '?');
    expect.soft(await push(page), name).toEqual(NO_CHANGES);
  }
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_one_edit one edit on a pulled card sends that edit, and a new Maestro or WLED still gets its defaults (W-4)', async ({ page }) => {
  const cases = [
    // A label the board reports on a Maestro's port is kept: a Maestro edit must not rename it to 'Maestro 1'.
    ["Maestro on S1 labelled 'Dome Maestro'", '#b1-maestro-tbody tr [id$="-baud"]', '115200',
      ['?BAUD,S1,115200', '?MAESTRO,M1:W1S1:115200']],
    // An empty or automatic label follows the Maestro it names.
    ['Maestro on S1 with no label', '#b1-maestro-tbody tr [id$="-id"]', '2',
      ['?LABEL,S1,Maestro 2', '?MAESTRO,CLEAR,M1:W1S1', '?MAESTRO,M2:W1S1:57600']],
    // A WLED line re-reserves its port on the board (wledReserveLocalPort: label, both broadcasts off, baud), so the
    // push says so too, and the next pull agrees with what the Wizard shows.
    ['WLED 3 on S2 with its own label', '#b1-wled-tbody tr [id$="-baud"]', '57600',
      ['?BAUD,S2,57600', '?LABEL,S2,WLED 3', '?BCAST,OUT,S2,OFF', '?BCAST,IN,S2,OFF', '?WLED,3:W1S2:57600']],
    ['MP3 on S2 at 38400, volume 3, ONERR', '#b1-mp3-vol', '5', ['?MP3,S2:38400:V5', '?MP3,ONERR,oops']],
    ['HCR on S4 at 57600, poll 0', '#b1-hcr-poll', '30', ['?HCR,PORT,S4:57600', '?HCR,POLL,30']],
    ['HW 3.2, LED on GPIO47', '#b1-led-pin', '38', ['?LED,PIN,38']],
    ['WiFi JOIN, a comma in the passphrase', '#b1-wifi-join-pass', 'new,pass 2', ['?WIFI,JOIN,NaviCore-20,new,pass 2']],
    ['variables', '#b1-var-tbody tr .var-value-input', '7', ['?VAR,SET,lights,7']],
  ];
  for (const [card, selector, value, want] of cases) {
    await openWizard(page);
    await pullFake(page, CARDS[card]);
    await edit(page, selector, value);
    const r = await push(page, { skipReboot: true });
    expect.soft(r.sent, `${card}: ${selector} = ${value}`).toEqual(want);
  }

  // A Maestro or WLED the user ADDS gets the defaults it always did: an automatic label, its baud on the port, and
  // (WLED) broadcasts off, as the board itself applies them.
  await openWizard(page);
  await pullFake(page, board());
  await page.evaluate(() => { addMaestroRow(1); });
  await edit(page, '#b1-maestro-tbody tr [id$="-port"]', '2');
  expect.soft((await push(page, { skipReboot: true })).sent, 'new Maestro')
    .toEqual(['?BAUD,S2,115200', '?LABEL,S2,Maestro 1', '?MAESTRO,M1:W1S2:115200']);

  await openWizard(page);
  await pullFake(page, board());
  await page.evaluate(() => { addWLEDRow(1); });
  await edit(page, '#b1-wled-tbody tr [id$="-port"]', '2');
  expect.soft((await push(page, { skipReboot: true })).sent, 'new WLED')
    .toEqual(['?BAUD,S2,115200', '?LABEL,S2,WLED 1', '?BCAST,OUT,S2,OFF', '?BCAST,IN,S2,OFF', '?WLED,1:W1S2:115200']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_etm_delay the General ETM delay keeps 0: shown, kept when another ETM field changes, and pushed when typed (W-1)', async ({ page }) => {
  await openWizard(page);
  await pullFake(page, CARDS['ETM message delay 0']);
  expect(await page.locator('#g-etm-delay').inputValue()).toBe('0');
  await edit(page, '#g-etm-timeout', '600');
  expect((await push(page)).sent).toEqual(['?ETM,TIMEOUT,600']);

  await openWizard(page);
  await pullFake(page, board());
  await edit(page, '#g-etm-delay', '0');
  expect(await page.evaluate(() => [systemConfig.general.etm.messageDelayMs, boardConfigs[1].etm.messageDelayMs]))
    .toEqual([0, 0]);
  expect((await push(page)).sent).toEqual(['?ETM,DELAY,0']);
  // Out of the firmware's 0-5000 range: clamped, so the board takes it and the next pull agrees.
  await edit(page, '#g-etm-delay', '9000');
  expect(await page.evaluate(() => boardConfigs[1].etm.messageDelayMs)).toBe(5000);
  expect(page.wizErrors).toEqual([]);
});

test("wizard.push_fake_delimiter a ',' board keeps its delimiter, and the General inputs refuse a delimiter the firmware refuses (W-2)", async ({ page }) => {
  await openWizard(page);
  await pullFake(page, COMMA_BOARD, '!');
  expect(await page.evaluate(() => [document.getElementById('g-delimiter').value, boardBaselines[1].delimiter,
                                    boardBaselines[1].funcChar, boardBaselines[1].cmdChar])).toEqual([',', ',', '!', '/']);
  // An edit elsewhere goes out in the board's own characters, with no ?DELIM bootstrap to revert it to '^'.
  await edit(page, '#b1-alias', 'Dome');
  const r = await push(page);
  expect(r.sent).toEqual(['!ALIAS,Dome']);

  // Typed into the General delimiter: ',' (every command has commas), a letter, a digit, the function identifier or
  // the command character are put back to the last good value, with a toast saying why.
  await openWizard(page);
  await pullFake(page, board());
  for (const bad of [',', 'a', '7', '?', ';']) {
    await edit(page, '#g-delimiter', bad);
    expect.soft(await page.evaluate(() => [document.getElementById('g-delimiter').value,
                                           systemConfig.general.delimiter, boardConfigs[1].delimiter]), bad)
      .toEqual(['^', '^', '^']);
  }
  expect(await page.locator('#toast-container').textContent()).toContain('Command Delimiter');
  // ... and the prefixes cannot take the delimiter or each other.
  await edit(page, '#g-funcchar', '^');
  await edit(page, '#g-cmdchar', '?');
  expect(await page.evaluate(() => [document.getElementById('g-funcchar').value, document.getElementById('g-cmdchar').value]))
    .toEqual(['?', ';']);
  // A good one is taken, pushed after the bootstrap in the board's current function identifier.
  await edit(page, '#g-delimiter', '|');
  expect(await page.evaluate(() => [systemConfig.general.delimiter, boardConfigs[1].delimiter])).toEqual(['|', '|']);
  expect((await push(page)).sent).toEqual(['?DELIM,|', '?DELIM,|']);

  // A value that reached the DOM some other way (a loaded system file) is still never pushed as a change.
  await openWizard(page);
  await pullFake(page, board());
  await page.evaluate(() => { document.getElementById('g-delimiter').value = ','; });
  const refused = await push(page);
  expect(refused.sent).toEqual([]);
  expect(refused.outcome.ok).toBe(false);
  expect(refused.outcome.reason).toMatch(/delimiter/i);

  // The setup wizard's Network step holds its three characters to the same rules before it moves on (its inputs,
  // stood in for here: the step's own markup needs a running wizard).
  const step = await page.evaluate(() => {
    const host = document.createElement('div');
    host.innerHTML = ['wiz-password', 'wiz-mac2', 'wiz-mac3', 'wiz-delim', 'wiz-funcchar', 'wiz-cmdchar']
      .map((id) => `<input id="${id}">`).join('');
    document.body.appendChild(host);
    const set = (v) => Object.entries(v).forEach(([id, x]) => { document.getElementById(id).value = x; });
    set({ 'wiz-password': 'long_enough_pw', 'wiz-mac2': '05', 'wiz-mac3': '4B',
          'wiz-delim': ',', 'wiz-funcchar': '?', 'wiz-cmdchar': ';' });
    const comma = wizardValidateStep('network');
    set({ 'wiz-delim': '|', 'wiz-funcchar': '|' });
    const clash = wizardValidateStep('network');
    set({ 'wiz-funcchar': '!' });
    const good = wizardValidateStep('network');
    host.remove();
    return { comma, clash, good };
  });
  expect(step.comma).toMatch(/cannot be the Command Delimiter/);
  expect(step.clash).toMatch(/'\|' is already the Local Function Identifier/);
  expect(step.good).toBe(null);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_bidir a mapping row touched but not changed re-sends no mapping, so a PWM input does not reboot the board (W-5)', async ({ page }) => {
  await openWizard(page);
  await pullFake(page, CARDS['serial and PWM mappings, PWM output']);
  // What a user does to a row without changing it: flip Raw off and back on. Each flip runs onMappingChange ->
  // syncMappingsToConfig, which rebuilds config.mappings from the rows.
  const raw = '#b1-mappings-container [id$="-raw"]';
  await edit(page, raw, false);
  await edit(page, raw, true);
  expect(await push(page)).toEqual(NO_CHANGES);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_reboot_matchers the reboot and network-group checks read each command, not a substring of a label or a sequence (W-8)', async ({ page }) => {
  await openWizard(page);
  const r = await page.evaluate(() => {
    const reboot = (s, fc, d) => commandStringNeedsReboot(s, fc, d);
    const group  = (s, fc, d) => commandStringChangesNetworkGroup(s, fc, d);
    return {
      rebootYes: ['?WCB,3', '?HW,32', '?MAC,2,05', '?MAC,3,4B', '?WCBCH,6', '?WIFI,AP,Dome,password1', '?WIFI,OFF',
                  '?KYBER,LOCAL,S2', '?KYBER,CLEAR', '?MAESTRO,REMOTE', '?MAP,PWM,S1,S4', '?LABEL,S1,x^?HW,32',
                  '?hw,32'].map((s) => [s, reboot(s)]),
      rebootNo:  ['?LABEL,S1,My WCB, dome', '?LABEL,S1,SHOW, x', '?SEQ,SAVE,k,;S2HW,1', '?SEQ,SAVE,k,;S2WIFI,x^;S3KYBER,y',
                  '?ALIAS,MAC, the dome', '?MAP,PWM,OUT,S5', '?MAESTRO,M1:W1S1:57600', '?LABEL,S2,KYBER, left',
                  '?WCBQ,3', '?EPASS,x'].map((s) => [s, reboot(s)]),
      groupYes:  ['?MAC,2,05', '?MAC,3,4B', '?EPASS,new_one', '?WCBCH,3', '?LABEL,S1,x^?EPASS,y'].map((s) => [s, group(s)]),
      groupNo:   ['?LABEL,S1,EPASS, x', '?SEQ,SAVE,k,;S1MAC,2,05', '?ALIAS,WCBCH,1', '?HW,32'].map((s) => [s, group(s)]),
      // The chain is split exactly as the push splits it: on the delimiter + function identifier it was built with.
      customYes: [reboot('!LABEL,S1,x|!HW,32', '!', '|'), group('!LABEL,S1,x|!EPASS,y', '!', '|')],
      customNo:  [reboot('!LABEL,S1,a|b ?HW,32', '!', '|'), reboot('!SEQ,SAVE,k,;S1x|!LABEL,S1,y', '!', '|')],
    };
  });
  for (const [s, v] of r.rebootYes) expect.soft(v, `needs a reboot: ${s}`).toBe(true);
  for (const [s, v] of r.rebootNo) expect.soft(v, `no reboot: ${s}`).toBe(false);
  for (const [s, v] of r.groupYes) expect.soft(v, `network group: ${s}`).toBe(true);
  for (const [s, v] of r.groupNo) expect.soft(v, `not the network group: ${s}`).toBe(false);
  expect(r.customYes).toEqual([true, true]);
  expect(r.customNo).toEqual([false, false]);
  expect(page.wizErrors).toEqual([]);
});

// A fake relay in slot 1 (a real BoardConnection whose send() only records) managing WCB 2, and WCB 2's pulled
// config seeded as its baseline - the state a relay pull leaves. The General inputs are synced from it, as the first
// pull does, so only what a test edits differs.
async function relayBoard(page, chain) {
  await openWizard(page);
  await page.evaluate(async (chain) => {
    window._wdpMeshConn = () => null;
    const relay = new BoardConnection(1);
    relay._connected = true;
    window.__sent = [];
    relay.send = async (s) => { __sent.push(String(s).replace(/\r$/, '')); };
    boardConnections[1] = relay;
    addDiscoveredBoards([2]);
    setRemoteConnected(2, 1);
    await new Promise((r) => setTimeout(r, 1000));   // setRemoteConnected's RTERM,START sends (3 x, 250 ms apart)
    const cfg = WCBParser.parseBackupString(chain);
    boardConfigs[2] = cfg;
    boardBaselines[2] = JSON.parse(JSON.stringify(cfg));
    syncGeneralFromConfig(cfg);
    populateUIFromConfig(2, cfg);
    __sent.length = 0;
  }, chain);
}
const RELAY_CHAIN = `?HW,32^?WCB,2^?WCBQ,2^?WCBCH,1^?MAC,2,05^?MAC,3,4B^?EPASS,${EPASS}^?CMDCHAR,;`;
const frags = (sent) => sent.filter((s) => s.includes('MGMT,FRAG,'));

test('wizard.push_fake_relay_cap a relay push over 16 chunks is refused before the network-group confirm, the bootstrap or any send (W-9, F14)', async ({ page }) => {
  // Twenty long sequences (~3.9 KB of changes, over 16 x 179), a new mesh password and a new function identifier.
  await relayBoard(page, RELAY_CHAIN);
  await page.evaluate(() => {
    const cfg = boardConfigs[2];
    cfg.sequences = Array.from({ length: 20 }, (_, i) => ({ key: `s${i}`, value: `;S1${'x'.repeat(180)}` }));
    populateUIFromConfig(2, cfg);
    document.getElementById('g-password').value = 'a_new_mesh_password';
    document.getElementById('g-funcchar').value = '!';
    window.__go = boardGo(2);
  });
  await page.waitForTimeout(1500);
  const r = await page.evaluate(() => ({
    modal: document.getElementById('network-group-change-modal').classList.contains('open'),
    sent: [...__sent], outcome: boardPushOutcome[2],
    btn: [document.getElementById('b2-btn-go').disabled, document.getElementById('b2-btn-go').textContent],
  }));
  expect(r.modal, 'the confirm must not open for a push that cannot be sent').toBe(false);
  expect(r.sent, 'nothing reaches the relay: no bootstrap, no chunk').toEqual([]);
  expect(r.outcome.ok).toBe(false);
  expect(r.outcome.reason).toMatch(/too large/);
  expect(r.btn).toEqual([false, 'Push Config']);
  expect(await page.locator('#toast-container').textContent()).toContain('too large to push via a relay');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_relay_push a relay push still asks before a network-group change, bootstraps a new function id first, and frames the chain in 179-char chunks', async ({ page }) => {
  // An EPASS change opens the confirm; Cancel sends nothing at all.
  await relayBoard(page, RELAY_CHAIN);
  await page.evaluate(() => {
    document.getElementById('g-password').value = 'a_new_mesh_password';
    window.__go = boardGo(2);
  });
  await expect(page.locator('#network-group-change-modal.open')).toBeVisible();
  await page.locator('#network-group-change-cancel').click();
  await page.evaluate(() => window.__go);
  expect(await page.evaluate(() => [...__sent])).toEqual([]);

  // A label edit and a new function identifier: the FUNCCHAR bootstrap goes first, in the target's current '?', then
  // one FRAG session whose chunks join back into the diff.
  await relayBoard(page, RELAY_CHAIN);
  const r = await page.evaluate(async () => {
    const cfg = boardConfigs[2];
    cfg.sequences = Array.from({ length: 3 }, (_, i) => ({ key: `s${i}`, value: `;S1${'y'.repeat(150)}` }));
    populateUIFromConfig(2, cfg);
    document.getElementById('g-funcchar').value = '!';
    await boardGo(2);
    return { sent: [...__sent], outcome: boardPushOutcome[2] };
  });
  expect(r.outcome.ok).toBe(true);
  const boot = r.sent.filter((s) => s.endsWith(',0,1,?FUNCCHAR,!'));
  expect(boot.length).toBe(3);                                   // sent 3x: the hop is not ACKed
  expect(r.sent.indexOf(boot[0])).toBe(0);
  const chunks = frags(r.sent).filter((s) => !s.endsWith('?FUNCCHAR,!'));
  const m = chunks.map((s) => /^\?MGMT,FRAG,2,([0-9A-F]{4}),(\d+),(\d+),([\s\S]*)$/.exec(s));
  expect(m.every(Boolean)).toBe(true);
  expect(new Set(m.map((x) => x[1])).size).toBe(1);             // one session
  expect(m.map((x) => Number(x[2]))).toEqual(m.map((_, i) => i));
  expect(m.every((x) => Number(x[3]) === m.length && x[4].length <= 179)).toBe(true);
  const chain = m.map((x) => x[4]).join('');
  expect(chain.startsWith('!FUNCCHAR,!')).toBe(true);
  expect(chain).toContain(`!SEQ,SAVE,s2,;S1${'y'.repeat(150)}`);
  expect(chain).not.toContain('reboot');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_seq_roundtrip an untouched sequence row gives back its stored value exactly; an edited row is sent as before (W-10)', async ({ page }) => {
  await openWizard(page);
  await pullFake(page, board());
  // Firmware-legal values the editor used to rewrite once: a space before ***, ^^, spaces around ^, an inline ^***,
  // plus forms it always kept (IF groups with ;t, own-line comments, commas, a leading comment).
  const corpus = [';w2 ***note', 'a^^b', 'x ^ y', 'x^***inline', 'IF,flag=1^;t500^;M11', ';S1x^^***own line^;S2y',
                  '***lead^;S1x', ';S1a,b,c^;S2d', 'IF,a=1^;t100^;t200^;S1go ***why', ';S1x ^^ ***spaced'];
  const r = await page.evaluate((corpus) => {
    const cfg = boardConfigs[1];
    cfg.sequences = corpus.map((value, i) => ({ key: `k${i}`, value }));
    boardBaselines[1] = JSON.parse(JSON.stringify(cfg));
    populateUIFromConfig(1, cfg);
    const untouched = getSequencesFromUI(1);
    const built = WCBParser.buildCommandString({ ...cfg, sequences: untouched }, boardBaselines[1], false);
    // Now edit row k1 the way a user would, and leave the rest alone.
    const ta = document.querySelectorAll('#b1-seq-tbody .seq-val-textarea')[1];
    ta.value = 'a\nb\nc';
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    const edited = getSequencesFromUI(1);
    return { untouched, built, edited, expectEdited: seqTextareaToValue('a\nb\nc', '^') };
  }, corpus);
  expect(r.untouched.map((s) => s.value)).toEqual(corpus);
  expect(r.built).toBe('');
  expect(r.edited[1].value).toBe(r.expectEdited);
  expect(r.edited.filter((_, i) => i !== 1).map((s) => s.value)).toEqual(corpus.filter((_, i) => i !== 1));
  expect(page.wizErrors).toEqual([]);
});
