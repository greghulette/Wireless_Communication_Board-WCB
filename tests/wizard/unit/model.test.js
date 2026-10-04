// Wizard/parser.js: the config model's remaining edges (docs/hil_plan/WCB.md WCB-WP40) - diffConfigs covering every
// field the model has, the serial and PWM mappings, the port claims each device makes, WiFi and WDP, and a local
// Kyber. No browser, no board (`npm run unit`, wizard.parser in the harness, CI). The app.js halves of the same rows
// run in the page (specs/*_fake.spec.js).
//
// A (should) test here is a node `todo`: it runs and reports, and a failure does not fail the run (the harness notes it
// by name, hil/wizard.py run_unit_tests). Each names its W-row in docs/hil_plan/WCB.md §3.
//
// Every WiFi SSID and passphrase below is an obvious fake.
const test = require('node:test');
const assert = require('node:assert');

const P = require('../../../Wizard/parser.js');

const clone = (o) => JSON.parse(JSON.stringify(o));
const chain = (...tokens) => P.parseBackupString(['?HW,24', '?WCB,1', ...tokens].join('^'));
// A push's commands, split where the board splits them ("^?"): a sequence value keeps its own ^.
const commands = (cfg, baseline = null, fullPush = false) => {
  const s = P.buildCommandString(cfg, baseline, fullPush);
  return s ? s.split(/\^(?=\?)/) : [];
};
const only = (cmds, ...prefixes) => cmds.filter((c) => prefixes.some((p) => c.startsWith(p)));

// ── WP40 row 3: diffConfigs sees every field the model has ──────────────────────────────────────────────────────────
// W-11 added six fields diffConfigs missed. This walks the whole default config instead, so a field added to the model
// later without a line in diffConfigs fails here, not in a round trip that quietly passes. The fields left out are
// the ones no push writes: read-only telemetry, where a config came from, the firmware version, the MgmtRelay flag,
// the proxies a board learned (never pushed), the port claims (derived), and the Wizard-only slot type and client alias
// (a system file carries those in its ?CLIENT token, pinned by roundtrip.test.js W-6).
const NOT_CONFIG = new Set(['livePeerCount', 'source', 'fwVersion', 'isRelay', 'wledRemotes', 'type', 'clientAlias']);

function mutated(v) {
  if (typeof v === 'boolean') return !v;
  if (typeof v === 'number') return v + 1;
  if (typeof v === 'string') return `${v}x`;
  if (v === null) return 1;
  if (Array.isArray(v)) return [...v, { id: 9, port: 5, baud: 9600, key: 'k', value: 1, name: 'n' }];
  return null;
}

test('diffConfigs reports a change to every field of the model a push writes (WP40 row 3)', () => {
  const base = P.createDefaultBoardConfig();
  const missed = [];
  for (const [key, value] of Object.entries(base)) {
    if (NOT_CONFIG.has(key)) continue;
    if (key === 'serialPorts') {
      for (const f of ['baud', 'broadcastIn', 'broadcastOut', 'label']) {
        const b = clone(base);
        b.serialPorts[2][f] = mutated(b.serialPorts[2][f]);
        if (!P.diffConfigs(base, b).some((d) => d.path === `serialPorts[2].${f}`)) missed.push(`serialPorts[].${f}`);
      }
      continue;
    }
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      for (const f of Object.keys(value)) {
        const b = clone(base);
        b[key][f] = mutated(b[key][f]);
        if (!P.diffConfigs(base, b).some((d) => d.path === key)) missed.push(`${key}.${f}`);
      }
      continue;
    }
    const b = clone(base);
    b[key] = mutated(value);
    if (key === 'mappings') b.mappings = [{ type: 'Serial', sourcePort: 2, rawMode: false, destinations: [{ wcbNumber: 0, port: 3 }] }];
    if (!P.diffConfigs(base, b).some((d) => d.path === key)) missed.push(key);
  }
  assert.deepEqual(missed, [], `diffConfigs does not see: ${missed.join(', ')}`);
});

// ── WP40 row 4: serial and PWM mappings, and PWM output ports ────────────────────────────────────────────────────────
const MAPS = ['?MAP,SERIAL,S2,R,S3,W2S4', '?MAP,PWM,OUT,S5', '?MAP,PWM,S1,S4,W2S3'];

test('mappings: serial (raw flag, local and W<n>S<n> destinations), a PWM input and a PWM output port parse and round-trip through a full push (WP40 row 4)', () => {
  const c = chain(...MAPS);
  assert.deepEqual(c.mappings, [
    { type: 'Serial', sourcePort: 2, rawMode: true, destinations: [{ wcbNumber: 0, port: 3 }, { wcbNumber: 2, port: 4 }] },
    { type: 'PWM', sourcePort: 1, rawMode: false, destinations: [{ wcbNumber: 0, port: 4 }, { wcbNumber: 2, port: 3 }] },
  ]);
  assert.deepEqual(c.pwmOutputPorts, [5]);
  const full = commands(c, null, true);
  // The mapping block is the push's last (buildCommandString: applying a PWM input reboots the board, and nothing sent
  // after it would land), PWM output ports just before it.
  assert.deepEqual(full.slice(-3), ['?MAP,PWM,OUT,S5', '?MAP,SERIAL,S2,R,S3,W2S4', '?MAP,PWM,S1,S4,W2S3']);
  assert.deepEqual(P.diffConfigs(c, P.parseBackupString(full.join('^'))), []);
  assert.deepEqual(commands(c, clone(c)), [], 'unchanged: nothing');
});

test('mappings: an added or edited mapping re-sends the whole table; a removed mapping or PWM output port builds nothing - no push clears it (WP40 row 4, left as found)', () => {
  const base = chain(...MAPS);
  const add = clone(base);
  add.mappings.push({ type: 'Serial', sourcePort: 3, rawMode: false, destinations: [{ wcbNumber: 0, port: 1 }] });
  assert.deepEqual(only(commands(add, base), '?MAP'), ['?MAP,SERIAL,S2,R,S3,W2S4', '?MAP,PWM,S1,S4,W2S3', '?MAP,SERIAL,S3,S1']);
  const edit = clone(base);
  edit.mappings[0].rawMode = false;
  assert.deepEqual(only(commands(edit, base), '?MAP'), ['?MAP,SERIAL,S2,S3,W2S4', '?MAP,PWM,S1,S4,W2S3']);
  const removeOne = clone(base);
  removeOne.mappings.splice(1, 1);                                      // the PWM input goes
  assert.deepEqual(only(commands(removeOne, base), '?MAP'), ['?MAP,SERIAL,S2,R,S3,W2S4'], 'no ?MAP,PWM,CLEAR for it');
  const removeAll = clone(base);
  removeAll.mappings = [];
  assert.deepEqual(commands(removeAll, base), [], 'every mapping removed: nothing at all');
  const noOut = clone(base);
  noOut.pwmOutputPorts = [];
  assert.deepEqual(commands(noOut, base), [], 'a removed PWM output port: nothing - no ?MAP,PWM,CLEAR,OUT');
  const moreOut = clone(base);
  moreOut.pwmOutputPorts.push(4);
  assert.deepEqual(only(commands(moreOut, base), '?MAP'), ['?MAP,PWM,OUT,S5', '?MAP,PWM,OUT,S4']);
});

// ── WP40 row 8: the port each device claims ─────────────────────────────────────────────────────────────────────────
const claims = (c) => c.serialPorts.map((p) => (p.claimedBy ? p.claimedBy.type + (p.claimedBy.id ? `:${p.claimedBy.id}` : '') : '-'));

test('evaluatePortClaims: the port each device, mapping and Kyber mode takes, what a soft serial-map claim carries, and which ports stay free (WP40 row 8)', () => {
  const cases = [
    ['MP3 Trigger on S2', ['?MP3,S2:9600:V0'], ['-', 'mp3', '-', '-', '-']],
    ['HCR on S4', ['?HCR,PORT,S4:57600'], ['-', '-', '-', 'hcr', '-']],
    ['DFPlayer on S3', ['?DFP,S3:9600:V15'], ['-', '-', 'dfp', '-', '-']],
    ['WLED 3 on S5', ['?WLED,3:W1S5:115200'], ['-', '-', '-', '-', 'wled:3']],
    ['Maestro 2 on S1', ['?MAESTRO,M2:W1S1:57600'], ['maestro:2', '-', '-', '-', '-']],
    ['PWM input S1 to S4 and W2S3', ['?MAP,PWM,S1,S4,W2S3'], ['pwm', '-', '-', 'pwm', '-']],
    ['PWM output port S5', ['?MAP,PWM,OUT,S5'], ['-', '-', '-', '-', 'pwm']],
    ['serial map from S2 (soft)', ['?MAP,SERIAL,S2,R,S3'], ['-', 'serial-map', '-', '-', '-']],
    ['Kyber local S2, Marcuino S3', ['?LABEL,S3,Kyber Marcuino', '?KYBER,LOCAL,S2'], ['-', 'kyber', 'kyber-marc', '-', '-']],
    ['a bare Kyber local is S2', ['?KYBER,LOCAL'], ['-', 'kyber', '-', '-', '-']],
    ['Kyber local on S1 leaves S2 free', ['?KYBER,LOCAL,S1'], ['kyber', '-', '-', '-', '-']],
    ['Maestro remote keeps S1', ['?MAESTRO,REMOTE'], ['kyber-reserved', '-', '-', '-', '-']],
    ['Maestro remote with its Maestro on S1', ['?MAESTRO,REMOTE', '?MAESTRO,M1:W1S1:57600'], ['maestro:1', '-', '-', '-', '-']],
    // A mapping from a port a device holds does not take the claim over: the device's is more specific.
    ['serial map from the MP3 port', ['?MP3,S2:9600:V0', '?MAP,SERIAL,S2,S3'], ['-', 'mp3', '-', '-', '-']],
  ];
  for (const [what, tokens, want] of cases) assert.deepEqual(claims(chain(...tokens)), want, what);
  const soft = chain('?MAP,SERIAL,S2,R,S3,W2S4').serialPorts[1].claimedBy;
  assert.deepEqual(soft, { type: 'serial-map', rawMode: true, destinations: [{ wcbNumber: 0, port: 3 }, { wcbNumber: 2, port: 4 }] });
  assert.deepEqual(P.getAvailablePorts(chain('?MP3,S2:9600:V0', '?HCR,PORT,S4:57600')), [1, 3, 5]);
});

// ── WP40 row 9: WiFi and WDP ─────────────────────────────────────────────────────────────────────────────────────────
test('WiFi: JOIN with a comma in the passphrase, AP and OFF parse and round-trip; one line carries mode and credentials, sent only on a change (WP40 row 9)', () => {
  const join = chain('?WIFI,JOIN,HIL-FAKE-SSID,fake,pass word 1');
  assert.deepEqual(join.wifi, { mode: 'join', apSsid: '', apPass: '', joinSsid: 'HIL-FAKE-SSID', joinPass: 'fake,pass word 1' });
  assert.ok(commands(join, null, true).includes('?WIFI,JOIN,HIL-FAKE-SSID,fake,pass word 1'));
  assert.deepEqual(P.parseBackupString(commands(join, null, true).join('^')).wifi, join.wifi);
  const ap = chain('?WIFI,AP,HIL-FAKE-AP,fakepass1');
  assert.deepEqual([ap.wifi.mode, ap.wifi.apSsid, ap.wifi.apPass], ['ap', 'HIL-FAKE-AP', 'fakepass1']);
  const off = chain('?WIFI,OFF');
  assert.equal(off.wifi.mode, 'off');
  assert.ok(commands(off, null, true).includes('?WIFI,OFF'));
  assert.deepEqual(only(commands(join, clone(join)), '?WIFI'), [], 'unchanged: no WIFI line (it needs a reboot)');
  const toOff = clone(join);
  toOff.wifi.mode = 'off';
  assert.deepEqual(only(commands(toOff, join), '?WIFI'), ['?WIFI,OFF']);
  const newPass = clone(join);
  newPass.wifi.joinPass = 'other,fake';
  assert.deepEqual(only(commands(newPass, join), '?WIFI'), ['?WIFI,JOIN,HIL-FAKE-SSID,other,fake']);
});

test('WDP: the OFF forms the board prints parse, and turning either back on pushes the ON form (the board prints only OFF) (WP40 row 9)', () => {
  const off = chain('?WDP,OFF', '?WDP,AUTOJOIN,OFF');
  assert.deepEqual([off.wdpEnabled, off.wdpAutoJoin], [false, false]);
  const on = clone(off);
  on.wdpEnabled = true;
  on.wdpAutoJoin = true;
  assert.deepEqual(only(commands(on, off), '?WDP'), ['?WDP,ON', '?WDP,AUTOJOIN,ON']);
  const again = clone(on);
  again.wdpAutoJoin = false;
  assert.deepEqual(only(commands(again, on), '?WDP'), ['?WDP,AUTOJOIN,OFF']);
  assert.deepEqual(only(commands(P.createDefaultBoardConfig(), null, true), '?WDP'), ['?WDP,ON', '?WDP,AUTOJOIN,ON']);
  assert.deepEqual([chain('?WDP,ON').wdpEnabled, chain('?WDP,AUTOJOIN,ON').wdpAutoJoin, chain('?WDP,DUMP').wdpEnabled], [true, true, true]);
});

// ── WP40 row 10: a local Kyber ──────────────────────────────────────────────────────────────────────────────────────
test('Kyber local: the port, the targets, and the Marcuino port read from its label (any case); a full push round-trips all three (WP40 row 10)', () => {
  const c = chain('?LABEL,S3,Kyber Marcuino', '?KYBER,LOCAL,S2,M1:W1S1:57600,M2:W2S1:115200');
  assert.deepEqual(c.kyber, { mode: 'local', port: 2, baud: 115200, marcduinoPort: 3,
                              targets: [{ id: 1, wcb: 1, port: 1, baud: 57600 }, { id: 2, wcb: 2, port: 1, baud: 115200 }] });
  assert.equal(chain('?LABEL,S4,KYBER MARCUINO', '?KYBER,LOCAL,S2').kyber.marcduinoPort, 4);
  assert.equal(chain('?KYBER,LOCAL,S2').kyber.marcduinoPort, null);
  // A repeated target (same id, board and port) updates its baud instead of adding a second.
  assert.deepEqual(chain('?KYBER,LOCAL,S2,M1:W2S1:9600,M1:W2S1:57600').kyber.targets, [{ id: 1, wcb: 2, port: 1, baud: 57600 }]);
  const full = commands(c, null, true);
  assert.ok(full.includes('?KYBER,LOCAL,S2,M1:W1S1:57600,M2:W2S1:115200'), full.join(' | '));
  assert.ok(full.indexOf('?KYBER,CLEAR') < full.indexOf('?BAUD,S1,9600'), 'the release goes before the port rows');
  const back = P.parseBackupString(full.join('^'));
  assert.deepEqual(back.kyber, c.kyber);
  // A bare ?KYBER,LOCAL (older firmware, no port) builds as S2, the firmware's own fallback.
  assert.ok(commands(chain('?KYBER,LOCAL'), null, true).includes('?KYBER,LOCAL,S2'));
  assert.deepEqual([chain('?KYBER,REMOTE').kyber.mode, chain('?MAESTRO,REMOTE').kyber.port], ['remote', null]);
});

// ── (should) the parser halves of W-17 and W-20 ─────────────────────────────────────────────────────────────────────
test('W-17 (should): a system file keeps the WCB quantity it was saved with when it holds a board above it or a client slot', { todo: 'W-17: parseSystemFile raises general.wcbQuantity to the number of [WCB] sections (parser.js:1257-1260)' }, () => {
  const sys = P.createDefaultSystemConfig();
  sys.general.wcbQuantity = 2;
  const b = (n, extra = {}) => Object.assign(P.createDefaultBoardConfig(), { wcbNumber: n, hwVersion: 24 }, extra);
  sys.boards = [b(1), b(2), b(5), b(20, { type: 'client', clientAlias: 'NaviCore' })];
  const back = P.parseSystemFile(P.buildSystemFile(sys));
  assert.equal(back.general.wcbQuantity, 2);
  assert.deepEqual(back.boards.map((x) => x.wcbQuantity), [2, 2, 2, 2]);
});

test('W-20 (should): the same Kyber targets in another order are not a change', () => {
  const base = chain('?MAESTRO,M1:W1S1:57600', '?MAESTRO,M2:W2S1:57600', '?KYBER,LOCAL,S2,M1:W1S1:57600,M2:W2S1:57600');
  assert.deepEqual(base.kyber.targets.map((t) => t.id), [2, 1], 'the parse order the backup gives: the table\'s proxy first');
  const cfg = clone(base);
  cfg.kyber.targets = [...cfg.kyber.targets].reverse();          // autoComputeKyberTargets' order: live boards first
  assert.deepEqual(commands(cfg, base), []);
});
