// Wizard/parser.js on its own - no browser, no board. `node --test` (npm run unit), and wizard.parser in the harness.
//
// The identity and network tokens the 2026-10-12 coverage re-scan found unasserted (parseToken, parser.js:482-645):
// the mesh channel clamped to 1-11, PEERSLIVE read and never written back, the USB echo (BCAST,OUT,S0) kept on its own
// flag and pushed only when it changes, SPECIAL as the legacy name of CONTROLLER (with its 1-20 id), and ALIAS keeping
// a comma (and cut at 24). Re-scan row parser.tokens_misc (docs/hil_plan/rescan_2026-10-12/wizard.json).
const test = require('node:test');
const assert = require('node:assert');

const P = require('../../../Wizard/parser.js');

const parse = (...tokens) => P.parseBackupString(tokens.map((t) => `?${t}`).join('^'));
// The verbs of a push's command string, each with its arguments, as boardGo splits it.
const cmds = (s) => (s ? s.split('^?').map((x, i) => (i === 0 ? x.slice(1) : x)) : []);

test('WCBCH: 1-11 kept, anything else (14, 0, 12, a word) read as channel 1', () => {
  for (const ch of [1, 6, 11]) assert.equal(parse('WCB,1', `WCBCH,${ch}`).meshChannel, ch);
  for (const bad of ['14', '0', '12', '-3', 'x', '']) assert.equal(parse('WCB,1', `WCBCH,${bad}`).meshChannel, 1, `WCBCH,${bad}`);
});

test('PEERSLIVE: read as livePeerCount (null when absent or not a number) and never part of a push', () => {
  assert.equal(parse('WCB,1', 'PEERSLIVE,3').livePeerCount, 3);
  assert.equal(parse('WCB,1', 'PEERSLIVE,0').livePeerCount, 0);
  assert.equal(parse('WCB,1', 'PEERSLIVE,lots').livePeerCount, null);
  assert.equal(parse('WCB,1').livePeerCount, null);
  const c = parse('WCB,1', 'WCBQ,2', 'PEERSLIVE,3');
  assert.ok(!cmds(P.buildCommandString(c, null, true)).some((x) => x.startsWith('PEERSLIVE')), 'a full push has no PEERSLIVE');
  const changed = JSON.parse(JSON.stringify(c));
  changed.livePeerCount = 7;
  assert.equal(P.buildCommandString(changed, c, false) || '', '', 'a changed count is no change to push');
});

test('BCAST,OUT,S0: the USB echo flag, apart from the five ports; pushed only when it changes, and in a full push', () => {
  const on = parse('WCB,1', 'BCAST,OUT,S0,ON');
  assert.equal(on.broadcastToS0, true);
  assert.deepEqual(on.serialPorts.map((p) => p.broadcastOut), P.createDefaultBoardConfig().serialPorts.map((p) => p.broadcastOut),
                   'no serial port touched');
  assert.equal(parse('WCB,1', 'BCAST,OUT,S0,ON', 'BCAST,OUT,S0,OFF').broadcastToS0, false);
  assert.equal(parse('WCB,1').broadcastToS0, false);
  // S0 has no input side: BCAST,IN,S0 changes nothing.
  assert.equal(parse('WCB,1', 'BCAST,IN,S0,ON').broadcastToS0, false);
  const off = JSON.parse(JSON.stringify(on));
  off.broadcastToS0 = false;
  assert.deepEqual(cmds(P.buildCommandString(off, on, false)), ['BCAST,OUT,S0,OFF']);
  assert.equal(P.buildCommandString(on, on, false) || '', '');
  assert.ok(cmds(P.buildCommandString(on, null, true)).includes('BCAST,OUT,S0,ON'));
});

test('SPECIAL is CONTROLLER\'s legacy name: ON with an id 1-20 sets the controller and its id, an id outside it keeps the default, OFF clears it', () => {
  const s = parse('WCB,1', 'SPECIAL,ON,5');
  assert.deepEqual([s.specialPeer, s.specialPeerId], [true, 5]);
  const c = parse('WCB,1', 'CONTROLLER,ON,15');
  assert.deepEqual([c.specialPeer, c.specialPeerId], [true, 15]);
  const dflt = P.createDefaultBoardConfig().specialPeerId;
  for (const id of ['21', '0', 'x']) {
    const o = parse('WCB,1', `SPECIAL,ON,${id}`);
    assert.deepEqual([o.specialPeer, o.specialPeerId], [true, dflt], `SPECIAL,ON,${id}`);
  }
  for (const on of ['1', 'true', 'on']) assert.equal(parse('WCB,1', `CONTROLLER,${on}`).specialPeer, true, on);
  const off = parse('WCB,1', 'SPECIAL,ON,5', 'SPECIAL,OFF');
  assert.equal(off.specialPeer, false);
});

test('ALIAS keeps a comma and its case, is cut at 24, CLEAR or an empty value clears it, LIST changes nothing', () => {
  assert.equal(parse('WCB,1', 'ALIAS,Dome, Left').alias, 'Dome, Left');
  assert.equal(parse('WCB,1', 'ALIAS,abcdefghijklmnopqrstuvwxyz0123').alias, 'abcdefghijklmnopqrstuvwx');
  assert.equal(parse('WCB,1', 'ALIAS,Body', 'ALIAS,CLEAR').alias, '');
  assert.equal(parse('WCB,1', 'ALIAS,Body', 'ALIAS,').alias, '');
  assert.equal(parse('WCB,1', 'ALIAS,Body', 'ALIAS,LIST').alias, 'Body');
});

test('WCB is the board\'s number and WCBQ the quantity, never each other', () => {
  const c = parse('WCB,4', 'WCBQ,6');
  assert.deepEqual([c.wcbNumber, c.wcbQuantity], [4, 6]);
  assert.equal(parse('WCB,4', 'WCB,Q9').wcbNumber, 4, 'WCB,Q... is not a number change');
});
