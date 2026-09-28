// Wizard/parser.js: fields that must survive parse -> build -> parse, and diffs that must stay quiet when nothing
// changed. No browser, no board (`npm run unit`, CI). Each test names the defect it pins (docs/hil_plan/WCB.md §3,
// "Wizard defects"); the app.js halves of the same defects run in the page, specs/push_fake.spec.js.
//
// Why these matter: the Wizard pushes the DIFF against the baseline it pulled. A value the parser misreads is pushed
// back as a "change" the user never made (W-1 wrote ETM,DELAY,100 over a legal 0), and a field the diff cannot see
// lets a builder drop it with every round-trip test still passing (W-11).
const test = require('node:test');
const assert = require('node:assert');

const P = require('../../../Wizard/parser.js');

const clone = (o) => JSON.parse(JSON.stringify(o));
const commands = (cfg, baseline = null, fullPush = false) => {
  const s = P.buildCommandString(cfg, baseline, fullPush);
  return s ? s.split(cfg.delimiter || '^') : [];
};
// A ?backup as printBackupConfig prints it: a header, then one command per line behind the board's CURRENT function
// identifier (lfi) - which is what the parser reads from a live board.
const backup = (lfi, tokens) => [
  '*** ========================================',
  '*** WCB Configuration Backup',
  '*** ========================================',
  '',
  ...tokens.map((t) => lfi + t),
  '',
  "*** === For Configured Boards (Current Delimiter: 'x') ===",
  '--------- End of Backup ---------',
].join('\r\n');

// ── W-1: ETM,DELAY 0 ─────────────────────────────────────────────────────────────────────────────────────────────────

test('W-1: ?ETM,DELAY,0 is a legal delay (WCB.ino: 0-5000 ms) - it parses as 0 and a full push writes 0', () => {
  const c = P.parseBackupString('?HW,32^?WCB,1^?ETM,ON^?ETM,DELAY,0');
  assert.equal(c.etm.messageDelayMs, 0);
  assert.ok(commands(c, null, true).includes('?ETM,DELAY,0'), 'a full push must keep the 0');
  assert.deepEqual(commands(c, clone(c), false), [], 'and an unchanged board sends nothing');
  // A value that is not a number still falls back to the firmware default.
  assert.equal(P.parseBackupString('?WCB,1^?ETM,DELAY,x').etm.messageDelayMs, 100);
});

// ── W-2: the command characters, ',' included ───────────────────────────────────────────────────────────────────────

test('W-2: a backup captured under ?FUNCCHAR,! and ?DELIM,, (and ?CMDCHAR,/) reads all three characters back', () => {
  // Per-line form (a live board's ?backup): every line carries the board's own prefix.
  const perLine = P.parseBackupString(backup('!', ['HW,32', 'WCB,1', 'DELIM,,', 'FUNCCHAR,!', 'CMDCHAR,/', 'LABEL,S1,a, b']));
  assert.deepEqual([perLine.delimiter, perLine.funcChar, perLine.cmdChar], [',', '!', '/']);
  assert.equal(perLine.serialPorts[0].label, 'a, b');
  // Chain form (a relay pull's reply, always '?' and '^', WCB.ino configPullWalk).
  const chain = P.parseBackupString('?HW,32^?WCB,1^?DELIM,,^?FUNCCHAR,!^?CMDCHAR,/^?LABEL,S1,x');
  assert.deepEqual([chain.delimiter, chain.funcChar, chain.cmdChar], [',', '!', '/']);
  // Defaults when a line carries no character at all.
  const bare = P.parseBackupString('?WCB,1^?DELIM,^?FUNCCHAR,^?CMDCHAR,');
  assert.deepEqual([bare.delimiter, bare.funcChar, bare.cmdChar], ['^', '?', ';']);
});

test('W-2: the characters round-trip through a saved system file', () => {
  const system = P.createDefaultSystemConfig();
  Object.assign(system.general, { delimiter: ',', funcChar: '!', cmdChar: '/' });
  const board = P.createDefaultBoardConfig();
  Object.assign(board, { hwVersion: 32, delimiter: ',', funcChar: '!', cmdChar: '/' });
  system.boards = [board];
  const back = P.parseSystemFile(P.buildSystemFile(system));
  assert.deepEqual([back.general.delimiter, back.general.funcChar, back.general.cmdChar], [',', '!', '/']);
  assert.deepEqual([back.boards[0].delimiter, back.boards[0].funcChar, back.boards[0].cmdChar], [',', '!', '/']);
});

test('W-2: commandCharProblem refuses what the firmware refuses, and each character against the other two', () => {
  const ok = { delimiter: '^', funcChar: '?', cmdChar: ';' };
  const why = (field, v) => P.commandCharProblem(field, { ...ok, [field]: v });
  for (const d of ['^', '|', '~', '#']) assert.equal(why('delimiter', d), '', `delimiter ${d}`);
  for (const d of [',', 'a', 'Z', '5', ' ', '', '^^', '\t', '€', '?', ';']) {
    assert.notEqual(why('delimiter', d), '', `delimiter ${JSON.stringify(d)} must be refused`);
  }
  assert.match(why('delimiter', ','), /comma/i);
  assert.match(why('delimiter', '?'), /Local Function Identifier/);
  assert.match(why('delimiter', ';'), /Command Character/);
  // Prefixes: any printable non-space ASCII character (letters too, as prefixCharOk allows), but never the other
  // prefix or the delimiter.
  for (const c of ['!', '.', 'x', '#']) assert.equal(why('funcChar', c), '', `funcChar ${c}`);
  for (const c of [';', '^', ' ', '', 'ab', 'é']) assert.notEqual(why('funcChar', c), '', `funcChar ${JSON.stringify(c)}`);
  for (const c of ['/', ':', '@']) assert.equal(why('cmdChar', c), '', `cmdChar ${c}`);
  for (const c of ['?', '^', ' ']) assert.notEqual(why('cmdChar', c), '', `cmdChar ${JSON.stringify(c)}`);
});

// ── W-3: a disabled controller's custom id ───────────────────────────────────────────────────────────────────────────

test('W-3: CONTROLLER,ON,15^CONTROLLER,OFF (how the firmware stores a disabled custom id) survives a full push', () => {
  const c = P.parseBackupString('?HW,32^?WCB,1^?CONTROLLER,ON,15^?CONTROLLER,OFF');
  assert.deepEqual([c.specialPeer, c.specialPeerId], [false, 15]);
  const full = commands(c, null, true);
  const on = full.indexOf('?CONTROLLER,ON,15');
  assert.ok(on >= 0 && full[on + 1] === '?CONTROLLER,OFF', `full push: ${full.filter((x) => x.includes('CONTROLLER')).join(' | ')}`);
  const back = P.parseBackupString(full.join('^'));
  assert.deepEqual([back.specialPeer, back.specialPeerId], [false, 15]);
  assert.deepEqual(commands(c, clone(c), false), []);
});

test('W-3: the controller lines on a delta push: only what moves the board to the config', () => {
  const cfg = (on, id) => Object.assign(P.createDefaultBoardConfig(), { specialPeer: on, specialPeerId: id });
  const ctl = (cur, base) => commands(cur, base, false).filter((x) => x.includes('CONTROLLER'));
  assert.deepEqual(ctl(cfg(true, 15), cfg(false, 20)), ['?CONTROLLER,ON,15']);
  assert.deepEqual(ctl(cfg(false, 15), cfg(true, 15)), ['?CONTROLLER,OFF']);          // OFF keeps the stored id
  assert.deepEqual(ctl(cfg(false, 15), cfg(false, 20)), ['?CONTROLLER,ON,15', '?CONTROLLER,OFF']);
  assert.deepEqual(ctl(cfg(false, 20), cfg(true, 15)), ['?CONTROLLER,ON,20', '?CONTROLLER,OFF']);
  assert.deepEqual(ctl(cfg(false, 15), cfg(false, 15)), []);
  // A default board (disabled, id 20) still writes just OFF on a full push, as before.
  assert.deepEqual(commands(cfg(false, 20), null, true).filter((x) => x.includes('CONTROLLER')), ['?CONTROLLER,OFF']);
});

// ── W-5: a UI-only key on a mapping ──────────────────────────────────────────────────────────────────────────────────

test('W-5: a mapping carrying the UI-only bidir key (and keys in another order) is not a change', () => {
  const base = P.parseBackupString('?WCB,1^?MAP,SERIAL,S2,R,S3,W2S4^?MAP,PWM,S1,S4^?MAP,PWM,OUT,S5');
  assert.equal(base.mappings.length, 2);
  // What syncMappingsToConfig (app.js) builds from the rows: the same mappings plus bidir.
  const ui = clone(base);
  ui.mappings = base.mappings.map((m) => ({ type: m.type, sourcePort: m.sourcePort, rawMode: m.rawMode, bidir: false,
                                             destinations: m.destinations.map((d) => ({ port: d.port, wcbNumber: d.wcbNumber })) }));
  assert.deepEqual(commands(ui, base, false), [], 'an unchanged mapping must not be re-sent - a PWM one reboots the board');
  assert.deepEqual(P.diffConfigs(base, ui), []);
  // A real change is still sent.
  ui.mappings[0].rawMode = false;
  assert.ok(commands(ui, base, false).includes('?MAP,SERIAL,S2,S3,W2S4'));
});

// ── W-6: a client slot in a system file ──────────────────────────────────────────────────────────────────────────────

test('W-6: a client slot keeps its type and alias through a saved system file; a WCB slot stays a WCB', () => {
  const system = P.createDefaultSystemConfig();
  system.general.wcbQuantity = 2;
  const wcb = Object.assign(P.createDefaultBoardConfig(), { wcbNumber: 1, alias: 'Body' });
  const client = Object.assign(P.createDefaultBoardConfig(), { wcbNumber: 20, type: 'client', clientAlias: 'Dome, 50% ^?x' });
  system.boards = [wcb, client];
  const back = P.parseSystemFile(P.buildSystemFile(system));
  const byNum = Object.fromEntries(back.boards.map((b) => [b.wcbNumber, b]));
  assert.deepEqual([byNum[1].type, byNum[1].clientAlias, byNum[1].alias], ['wcb', '', 'Body']);
  assert.deepEqual([byNum[20].type, byNum[20].clientAlias], ['client', 'Dome, 50% ^?x']);
  // An empty alias round-trips too, and the token never reaches a push: a client slot is never pushed, and the
  // builder does not emit it.
  client.clientAlias = '';
  const back2 = P.parseSystemFile(P.buildSystemFile(system));
  assert.deepEqual(back2.boards.map((b) => [b.wcbNumber, b.type, b.clientAlias]), [[1, 'wcb', ''], [20, 'client', '']]);
  assert.ok(!P.buildCommandString(client, null, true).includes('CLIENT'));
});

// ── W-11: diffConfigs sees every field the builder writes ────────────────────────────────────────────────────────────

test('W-11: diffConfigs reports alias, the controller, WDP and the LED pin', () => {
  const a = P.createDefaultBoardConfig();
  const fields = { alias: 'Dome', specialPeer: true, specialPeerId: 15, wdpEnabled: false, wdpAutoJoin: false, statusLedPin: 47 };
  for (const [k, v] of Object.entries(fields)) {
    const b = clone(a);
    b[k] = v;
    assert.deepEqual(P.diffConfigs(a, b).map((d) => d.path), [k], k);
  }
});

test('W-11: a board with all six at non-default values round-trips through a full push', () => {
  const c = Object.assign(P.createDefaultBoardConfig(), {
    hwVersion: 32, statusLedPin: 47, alias: 'Dome', specialPeer: true, specialPeerId: 15, wdpEnabled: false, wdpAutoJoin: false,
  });
  const back = P.parseBackupString(P.buildCommandString(c, null, true));
  assert.deepEqual(P.diffConfigs(c, back), []);
  const off = Object.assign(clone(c), { specialPeer: false });   // and disabled, with its custom id (W-3)
  assert.deepEqual(P.diffConfigs(off, P.parseBackupString(P.buildCommandString(off, null, true))), []);
});
