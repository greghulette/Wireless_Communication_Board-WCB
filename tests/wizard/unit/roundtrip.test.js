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

// ── W-2 follow-up: the order a push changes the characters in ───────────────────────────────────────────────────────
// A push sends DELIM, then CMDCHAR, then FUNCCHAR, each on its own line behind the function identifier the board still
// has, and the board checks each against its LIVE characters (WCB.ino delimCharOk, prefixCharOk). The model below is
// those two checks plus the line split every line goes through first (on the live delimiter).
function boardTakes(live, cmd) {
  if (cmd.includes(live.delimiter)) return null;                       // split before any setter reads it
  if (!cmd.startsWith(live.funcChar)) return null;                     // not a local command at all
  const body = cmd.slice(1);
  let field, c;
  if (/^D.$/.test(body)) [field, c] = ['delimiter', body[1]];         // the legacy two-character ?D<x>
  else if (body.startsWith('DELIM,') && body.length === 7) [field, c] = ['delimiter', body[6]];
  else if (body.startsWith('CMDCHAR,') && body.length === 9) [field, c] = ['cmdChar', body[8]];
  else if (body.startsWith('FUNCCHAR,') && body.length === 10) [field, c] = ['funcChar', body[9]];
  else return null;
  if (c <= ' ' || c >= '\x7f') return null;
  if (field === 'delimiter') {
    if (c === live.funcChar || c === live.cmdChar || c === ',' || /[A-Za-z0-9]/.test(c)) return null;
  } else if (c === (field === 'funcChar' ? live.cmdChar : live.funcChar) || c === live.delimiter) {
    return null;
  }
  return { ...live, [field]: c };
}

test('W-2: planCommandCharChange replays DELIM, CMDCHAR, FUNCCHAR from the board\'s characters, and refuses what needs two pushes', () => {
  const D = { delimiter: '^', funcChar: '?', cmdChar: ';' };
  const plan = (cur, change) => P.planCommandCharChange(cur, { ...cur, ...change });
  assert.deepEqual(plan(D, {}), { problem: '', commands: [] });
  assert.deepEqual(plan(D, { delimiter: '|' }), { problem: '', commands: ['?DELIM,|'] });
  assert.deepEqual(plan(D, { delimiter: '|', funcChar: '!', cmdChar: '/' }),
                   { problem: '', commands: ['?DELIM,|', '?CMDCHAR,/', '?FUNCCHAR,!'] });
  // A character freed earlier in the same push can be taken later in it.
  assert.deepEqual(plan(D, { delimiter: '|', funcChar: '^' }), { problem: '', commands: ['?DELIM,|', '?FUNCCHAR,^'] });
  assert.deepEqual(plan(D, { funcChar: ';', cmdChar: '/' }), { problem: '', commands: ['?CMDCHAR,/', '?FUNCCHAR,;'] });
  // Each of these ends somewhere legal, but a step on the way is one the board refuses.
  for (const [what, change, how] of [
    ['function identifier ! then delimiter ? (the review\'s case)', { funcChar: '!', delimiter: '?' }, /first/],
    ['a delimiter that is still the command character', { delimiter: ';', cmdChar: '/' }, /first/],
    ['a command character that is still the function identifier', { funcChar: '!', cmdChar: '?' }, /first/],
    ['swapping the function identifier and the command character', { funcChar: ';', cmdChar: '?' }, /unused character/],
    ['swapping the delimiter and the function identifier', { delimiter: '?', funcChar: '^' }, /unused character/],
    ['rotating all three', { delimiter: '?', funcChar: ';', cmdChar: '^' }, /unused character/],
  ]) {
    const r = plan(D, change);
    assert.match(r.problem, how, what);
    assert.deepEqual(r.commands, [], what);
  }
  // A character the firmware refuses anywhere is named as that, not as an ordering problem.
  assert.match(plan(D, { delimiter: ',' }).problem, /comma/);
  assert.match(plan(D, { funcChar: ';' }).problem, /already the Command Character/);
  // A board still on ',' (an older firmware took it) splits every line at its commas: nothing reaches it until the
  // delimiter changes, and that change only gets through as the two-character <func>D<x>.
  const comma = { delimiter: ',', funcChar: '!', cmdChar: '/' };
  assert.match(plan(comma, {}).problem, /','/);
  assert.match(plan(comma, { funcChar: '#' }).problem, /','/);
  assert.deepEqual(plan(comma, { delimiter: '^' }), { problem: '', commands: ['!D^'] });
  assert.deepEqual(plan(comma, { delimiter: '^', funcChar: '#' }), { problem: '', commands: ['!D^', '!FUNCCHAR,#'] });
  // No baseline: a fresh board is on the defaults.
  assert.deepEqual(P.planCommandCharChange(null, { delimiter: '|', funcChar: '?', cmdChar: ';' }),
                   { problem: '', commands: ['?DELIM,|'] });
});

test('W-2: every plan the board would take ends where it was asked to, and every refusal is one the board would make', () => {
  const A = ['^', '|', '?', '!', ';', '/', ','];
  const triples = [];
  for (const delimiter of A) for (const funcChar of A) for (const cmdChar of A) triples.push({ delimiter, funcChar, cmdChar });
  const legal = (t) => ['delimiter', 'funcChar', 'cmdChar'].every((f) => P.commandCharProblem(f, t) === '');
  // Boards: every legal set, and the legacy ',' delimiter with two distinct prefixes that are not ','.
  const boards = triples.filter((t) => legal(t) ||
    (t.delimiter === ',' && t.funcChar !== ',' && t.cmdChar !== ',' && t.funcChar !== t.cmdChar));
  const targets = triples.filter(legal);
  const naive = (cur, tgt) => {   // the fixed order, in the verb form, with no plan
    let live = cur;
    for (const [f, verb] of [['delimiter', 'DELIM'], ['cmdChar', 'CMDCHAR'], ['funcChar', 'FUNCCHAR']]) {
      if (tgt[f] === cur[f]) continue;
      live = live && boardTakes(live, `${live.funcChar}${verb},${tgt[f]}`);
    }
    return live;
  };
  let taken = 0, refused = 0;
  for (const cur of boards) {
    for (const tgt of targets) {
      const r = P.planCommandCharChange(cur, tgt);
      if (!r.problem) {
        let live = cur;
        for (const cmd of r.commands) {
          live = boardTakes(live, cmd);
          assert.ok(live, `${JSON.stringify(cur)} -> ${JSON.stringify(tgt)}: the board refuses ${cmd}`);
        }
        assert.deepEqual(live, tgt);
        taken++;
      } else {
        // Refused (every target here is legal): sent in the push's order, the board really would refuse a step...
        const where = `${JSON.stringify(cur)} -> ${JSON.stringify(tgt)}`;
        assert.ok(!naive(cur, tgt), `${where} refused needlessly: ${r.problem}`);
        // ... and the two pushes the refusal suggests both go through.
        const FIELD = { 'Command Delimiter': 'delimiter', 'Local Function Identifier': 'funcChar', 'Command Character': 'cmdChar' };
        const first = /Push the new (.+?) first/.exec(r.problem);
        const spare = /Set (the .+?) to unused characters? and push/.exec(r.problem);
        assert.ok(first || spare, `${where}: no advice in "${r.problem}"`);
        const mid = { ...cur };
        if (first) {
          mid[FIELD[first[1]]] = tgt[FIELD[first[1]]];
        } else {
          const unused = ['#', '~', '@', '$'].filter((c) => !Object.values(cur).includes(c) && !Object.values(tgt).includes(c));
          spare[1].split(/, | and /).forEach((name, i) => { mid[FIELD[name.replace(/^the /, '')]] = unused[i]; });
        }
        assert.equal(P.planCommandCharChange(cur, mid).problem, '', `${where}: first push to ${JSON.stringify(mid)}`);
        assert.equal(P.planCommandCharChange(mid, tgt).problem, '', `${where}: second push from ${JSON.stringify(mid)}`);
        refused++;
      }
    }
  }
  assert.ok(taken > 1000 && refused > 100, `${taken} taken, ${refused} refused`);
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

test('W-4: a Maestro routing table with the same content as the board\'s Maestros is not a change', () => {
  // After any Maestro edit, rebuildMaestroRoutingTables (app.js) gives every board a maestroTable built as
  // { id, wcb, port, baud }; the builder derives the baseline's as { ...maestro, wcb } = { id, port, baud, wcb }. The
  // same table in another key order re-sent ?MAESTRO after an edit the user had taken back.
  const base = P.parseBackupString('?WCB,1^?MAESTRO,M1:W1S1:57600,M2:W1S2:115200');
  const cfg = clone(base);
  cfg.maestroTable = base.maestros.map((m) => ({ id: m.id, wcb: 1, port: m.port, baud: m.baud }));
  assert.deepEqual(commands(cfg, base, false), []);
  cfg.maestroTable[0].baud = 115200;   // a real change still goes out
  assert.deepEqual(commands(cfg, base, false), ['?MAESTRO,M1:W1S1:115200,M2:W1S2:115200']);
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
