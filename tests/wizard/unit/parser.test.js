// Wizard/parser.js on its own — no browser, no board. `node --test` (npm run unit), and wizard.parser in the harness.
//
// This is where a config-loss bug lives: the Wizard pushes the DIFF against the baseline it pulled, so a field the
// parser drops, or a builder that re-emits everything, writes stale settings onto a good board. The fixture is a
// real ?backup from W1 on the bench (harness run 20260922-095852), with its two passwords replaced and its CRCs
// recomputed — a WCB 1 with an alias, five labelled ports, five Maestros, a remote HCR, a WLED and two sequences.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const P = require('../../../Wizard/parser.js');
const BACKUP = fs.readFileSync(path.join(__dirname, '..', 'fixtures', 'w1-backup.txt'), 'utf8');

const clone = (o) => JSON.parse(JSON.stringify(o));
const parsed = () => P.parseBackupString(BACKUP);

// kyber.targets — the Maestros hosted on OTHER boards — is derived, not owned by this board's config: the Wizard
// recomputes it from every connected board before a push (autoComputeKyberTargets, Wizard/app.js), and
// buildCommandString emits only this board's own Maestros unless the caller supplies a whole-system maestroTable.
// So a parser-level round trip legitimately drops it, and these comparisons leave it out.
const diffOwned = (a, b) => {
  const strip = (c) => { const x = clone(c); delete x.kyber.targets; return x; };
  return P.diffConfigs(strip(a), strip(b));
};
// What a full push would send, as the individual commands the board sees.
const commands = (cfg, baseline = null, fullPush = false) => {
  const s = P.buildCommandString(cfg, baseline, fullPush);
  return s ? s.split(cfg.delimiter || '^') : [];
};

test('parseBackupString reads the board identity and ports', () => {
  const c = parsed();
  assert.equal(c.wcbNumber, 1);
  assert.equal(c.alias, 'Body');
  assert.equal(c.wcbQuantity, 2);
  assert.equal(c.serialPorts[0].baud, 57600);
  assert.equal(c.serialPorts[0].label, 'Maestro 1');
  assert.equal(c.serialPorts[1].label, 'Magic Panel(IA)');
  assert.equal(c.serialPorts[0].broadcastOut, false);   // ?BCAST,OUT,S1,OFF
  assert.equal(c.serialPorts[1].broadcastOut, true);
});

test('parseBackupString splits Maestros into local ones and the ones this board proxies', () => {
  const c = parsed();
  // ?MAESTRO,M1:W1S1 is this board's own; M3/M2/M4 on W2 and M1 on W20 (NaviCore) are remote proxies, which the
  // parser files under kyber.targets. M1 appears twice on purpose — the same id on two boards is legal (CLAUDE.md
  // rule 5), so a parser that keyed on the id alone would lose one.
  assert.equal(c.maestros.length, 1, JSON.stringify(c.maestros));
  assert.equal(c.maestros[0].id, 1);
  assert.equal(c.maestros[0].port, 1);
  assert.equal(c.maestros[0].baud, 57600);
  assert.deepEqual(c.kyber.targets.map((t) => `M${t.id}:W${t.wcb}S${t.port}:${t.baud}`),
                   ['M3:W2S1:57600', 'M1:W20S1:57600', 'M2:W2S1:115200', 'M4:W2S1:115200']);
  assert.equal(c.kyber.mode, 'remote');       // ?MAESTRO,REMOTE
  // ?WLED,1:W2S0 is hosted by W2, so it is an auto-learned proxy: display-only, kept apart from the editable
  // local slots and never pushed back (a push that re-sent it would claim another board's device).
  assert.deepEqual(c.wleds, []);
  assert.deepEqual(c.wledRemotes, [{ id: 1, host: 2, baud: 115200 }]);
  assert.equal(c.sequences.length, 2);
  assert.deepEqual(c.sequences.map((s) => s.key).sort(), ['03', 'Leia']);
});

test('a full push round-trips: parse -> build -> parse gives the same config', () => {
  const a = parsed();
  const rebuilt = P.parseBackupString(P.buildCommandString(a, null, true));
  const diffs = diffOwned(a, rebuilt);
  assert.deepEqual(diffs, [], 'round trip changed: ' + JSON.stringify(diffs, null, 2));
});

test('a push with nothing changed sends nothing', () => {
  const a = parsed();
  assert.deepEqual(commands(a, clone(a), false), [],
                   'an unchanged board must produce no commands — otherwise every push rewrites settings');
});

test('a diff push sends only what changed', () => {
  const baseline = parsed();
  const edited = clone(baseline);
  edited.serialPorts[4].label = 'Holo Projector';
  assert.deepEqual(commands(edited, baseline, false), ['?LABEL,S5,Holo Projector']);

  const twoFields = clone(baseline);
  twoFields.serialPorts[1].baud = 19200;
  twoFields.alias = 'Dome';
  assert.deepEqual(commands(twoFields, baseline, false).sort(), ['?ALIAS,Dome', '?BAUD,S2,19200'].sort());
});

test('clearing a field sends its CLEAR form, not an empty value', () => {
  const baseline = parsed();
  const cleared = clone(baseline);
  cleared.alias = '';
  assert.deepEqual(commands(cleared, baseline, false), ['?ALIAS,CLEAR']);
});

test('evaluatePortClaims marks the port its Maestro sits on', () => {
  const c = parsed();
  assert.ok(c.serialPorts[0].claimedBy, 'S1 hosts Maestro 1, so it must read as claimed');
  assert.ok(!P.getAvailablePorts(c).includes(1));
});

test('a system file round-trips through build and parse', () => {
  const board = parsed();
  const system = P.createDefaultSystemConfig();
  system.droidName = 'Bench R2';
  system.general.wcbQuantity = board.wcbQuantity;
  system.general.espnowPassword = board.espnowPassword;
  system.general.macOctet2 = board.macOctet2;
  system.general.macOctet3 = board.macOctet3;
  system.general.meshChannel = board.meshChannel;
  system.general.etm = clone(board.etm);
  system.boards = [board];

  const reread = P.parseSystemFile(P.buildSystemFile(system));
  assert.equal(reread.droidName, 'Bench R2');
  assert.equal(reread.boards.length, 1);
  assert.equal(reread.general.espnowPassword, board.espnowPassword);
  const diffs = diffOwned(board, reread.boards[0]);
  assert.deepEqual(diffs, [], 'a saved and reloaded system file changed: ' + JSON.stringify(diffs, null, 2));
});
