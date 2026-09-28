// Writes fixtures/navicore/config.bench.json: a NaviCore GET_CONFIG shaped like the bench NaviCore's, for the
// config tool's no-board specs. Built from the bench facts in docs/hil_plan/NAVICORE.md §1.4 (themselves read from a
// redacted GET_CONFIG) and pushed through lib/navicore/model.js, so it has the firmware's exact printed shape:
// key order, sparse mappings and Maestro channels, defaults for everything the facts do not name.
//
// Every credential is a fixed placeholder (HIL...): the mesh password, both profiles' passwords, the AP SSID and
// passphrase. When the bench is free, tools/scrub_nc_config.py replaces this file with the REAL config, scrubbed the
// same way; the specs read whatever this file holds and do not depend on the synthetic content below.
//
//   node tools/make_nc_fixture.js            (from tests/wizard)
const fs = require('node:fs');
const path = require('node:path');
const { NcConfig } = require('../lib/navicore/model');

const OUT = path.join(__dirname, '..', 'fixtures', 'navicore', 'config.bench.json');

const uni = (target, cmd, extra = {}) => ({ type: 'wcb_unicast', target: String(target), cmd, ...extra });
const bc = (cmd, extra = {}) => ({ type: 'wcb_broadcast', cmd, ...extra });
const mae = (target, cmd, extra = {}) => ({ type: 'maestro', target: String(target), cmd, ...extra });

// 34 mappings, the bench's keys (101-118, 201, 207-213, 218, 304, 305, 311-315), with every action type the
// firmware stores, notes, delays, skip-if-running, exclusive, and all four tiers somewhere.
const mappings = {
  101: { exclusive: false, t1: [uni(1, ';S2HIL101', { note: 'Dome A' })], t1note: 'Pie open' },
  102: { exclusive: false, t1: [mae(1, 'restartScript,3', { skipRunning: true })], t2: [mae(1, 'goHome')] },
  103: { exclusive: false, t1: [{ type: 'hcr', fn: 14, chan: 0, track: 0 }], t2: [{ type: 'mp3', fn: 1, track: 5 }] },
  104: { exclusive: false, t1: [{ type: 'serial', port: 'S3', cmd: ':PP100' }] },
  105: { exclusive: false, t1: [bc(':SE00')], t3: [bc(':SE01', { delay: 250 })], t3note: 'Scream' },
  106: { exclusive: false, t1: [{ type: 'wled', cmd: ';L1,ON' }], t2: [{ type: 'wled', cmd: ';L1,OFF' }] },
  107: { exclusive: false, t1: [{ type: 'play', cmd: 'intro', fn: 0 }], t4: [{ type: 'record', cmd: 'take1' }], t4note: 'Record' },
  108: { exclusive: true, t1: [{ type: 'stop' }], t2: [{ type: 'play', cmd: 'loopA', fn: 1 }] },
  109: { exclusive: false, t1: [{ type: 'dfplayer', fn: 1, chan: 0, track: 3 }] },
  110: { exclusive: false,
         t1: [uni(1, ';S2HIL110A'), uni(2, ';S2HIL110B', { delay: 500 }), bc(';W1;S3HIL110C', { note: 'port S3' })],
         t2: [uni(2, ';S2HIL110D')], t2note: 'Double' },
  111: { exclusive: false, t1: [uni(2, ';M11', { skipRunning: true })] },
  112: { exclusive: false, t1: [uni(2, ';M12')] },
  113: { exclusive: false, t1: [uni(1, ';S2HIL113^;S3HIL113')] },
  114: { exclusive: false, t1: [mae(2, 'setTarget,0,6000')] },
  115: { exclusive: false, t1: [mae(1, 'stopScript')] },
  116: { exclusive: false, t1: [{ type: 'hcr', fn: 1, chan: 1, track: 12 }] },
  117: { exclusive: false, t1: [{ type: 'mp3', fn: 3, track: 0 }] },
  118: { exclusive: false, t1: [uni(1, ';S2HIL118')], t4: [uni(1, ';S2HIL118L')] },
  201: { exclusive: false, t1: [mae(2, 'goHome')] },
  207: { exclusive: false, t1: [mae(3, 'setTarget,4,6000')] },
  208: { exclusive: false, t1: [mae(4, 'setTarget,5,6000')] },
  209: { exclusive: false, t1: [mae(5, 'setSpeed,0,20')] },
  210: { exclusive: false, t1: [mae(6, 'setAccel,0,5')] },
  211: { exclusive: false, t1: [mae(7, 'restartScript,1')] },
  212: { exclusive: false, t1: [mae(8, 'goHome')] },
  213: { exclusive: false, t1: [mae(3, 'stopScript')] },
  218: { exclusive: false, t1: [uni(1, ';S2HIL218^;S3HIL218')] },
  304: { exclusive: false, t1: [{ type: 'hcr', fn: 14, chan: 2, track: 0 }] },
  305: { exclusive: false, t1: [{ type: 'hcr', fn: 2, chan: 0, track: 0 }] },
  311: { exclusive: false, t1: [uni(2, ';M13', { skipRunning: true })] },
  312: { exclusive: false, t1: [uni(2, ';M14')] },
  313: { exclusive: false, t1: [uni(1, ';S2HIL313')] },
  314: { exclusive: false, t1: [uni(1, ';S2HIL314')] },
  315: { exclusive: false, t1: [uni(1, ';S2HIL315')] },
};

const range = (a, b) => Array.from({ length: b - a + 1 }, (_, i) => a + i);
const pass = (target, ch, lo, hi) => ({ target, maestroCh: ch, posMin: lo, posMax: hi, releaseIdleMs: 1500 });

const bench = {
  txModel: 0, boardType: 0, sbusOutEnabled: true,
  wifiEnabled: true, wifiSsid: 'HILssid', wifiPassword: 'HILwifiPass',
  maeGateMs: 250, tapWindowMs: 500, holdMs: 750, switchSettleMs: 80, chRateHz: 5,
  matrixChannel: 7, matrixDebounceFrames: 1, funcBindings: { mode: 4 },
  peerEvent: { alert: true, actions: [] },
  mappings,
  switches: {
    SC: { channel: 10, positions: 3,
          p0: [mae(1, 'setEasing,p0'), mae(2, 'setEasing,p0')],
          p1: [mae(1, 'setEasing,off'), mae(2, 'setEasing,off')],
          p2: [mae(1, 'setEasing,release'), mae(2, 'setEasing,release')], p2note: 'Release' },
    // A 2-position switch reads as position 0 or 2 (NaviCore.ino:555; t[1] unused, rc_config.h:206): Down is p0, Up is p2.
    SI: { channel: 16, positions: 2, p0: [bc(';W1;S3HILSIA')], p2: [bc(';W2;S2HILSIB')] },
    SJ: { channel: 17, positions: 2, p0: [bc(';W1;S1HILSJA')] },
  },
  knobs: {
    S1: { channel: 5, function: 2, reverse: false, modeAware: false, outputs: [{ target: 0, maestroCh: 0, posMin: 0, posMax: 99 }] },
    S2: { channel: 6, function: 2, reverse: false, modeAware: false, outputs: [{ target: 1, maestroCh: 0, posMin: 0, posMax: 99 }] },
    RS: { channel: 19, function: 1, reverse: false, modeAware: false,
          outputs: range(2, 9).map((ch) => ({ target: 1, maestroCh: ch, posMin: 4000, posMax: 8000 })) },
    J2: { channel: 24, function: 1, reverse: false, modeAware: true, smoothProfile: 0,
          outputs: [pass(1, 0, 3968, 8000)], outputs2: [pass(1, 0, 3968, 8000)], outputs3: [pass(1, 0, 4400, 7600)] },
    J4: { channel: 4, function: 1, reverse: true, modeAware: true,
          outputs: range(2, 7).map((t) => pass(t, 0, 4000, 8000)),
          outputs2: range(2, 7).map((t) => pass(t, 0, 4000, 8000)),
          outputs3: range(2, 7).map((t) => pass(t, 1, 4000, 8000)) },
  },
  hcrDest: { transport: 'wcb', target: '2', wcbPort: 1 },
  maestros: [
    { type: 1, device: 1, channels: range(0, 23).map((ch) => ({ ch, name: ch < 12 ? `Pie ${ch + 1}` : `Panel ${ch - 11}`,
                                                              min: 3968 + ch * 4, max: 8000 - ch * 4 })) },
    ...range(2, 8).map((d) => ({ type: 2, device: d })),
  ],
  wcbNetwork: { macOct2: 10, macOct3: 11, password: 'HILmeshPass', quantity: 1, deviceId: 20, channel: 1 },
  wcbProfiles: [
    { name: 'HILdev', macOct2: 10, macOct3: 11, password: 'HILmeshPass', quantity: 1, deviceId: 20, channel: 1 },
    { name: 'HILdroid', macOct2: 12, macOct3: 13, password: 'HILdroidPass', quantity: 4, deviceId: 20, channel: 6 },
  ],
  mp3Dest: { transport: 'off', port: 'S3' },
  dfpDest: { transport: 'off', port: 'S3' },
  wledSlots: [{ id: 1, port: 0, wcb: 2, configured: true }],
  auxBaud: { S3: 115200, S4: 9600, S5: 9600, maestro: 57600 },
  serialBcast: { S3: { out: false, in: false }, S4: { out: false, in: false }, S5: { out: false, in: false } },
  modeReport: { enabled: true, wcb: 2, template: ';V,MODE,{mode}', cmds: ['', '', ''] },
  statsReport: { enabled: true, wcb: 1 },
  smoothProfiles: [
    { name: 'Default', entries: [{ mid: 1, ch: 0, spd: 20, acc: 5 }, { mid: 1, ch: 1, spd: 30, acc: 0 }] },
    { name: 'Slow', entries: [{ mid: 1, ch: 0, spd: 8, acc: 2 }] },
    { name: '', entries: [] }, { name: '', entries: [] }, { name: '', entries: [] }, { name: '', entries: [] },
  ],
};

const model = new NcConfig();
model.fromJSON(bench);
const data = model.toJSON();
const n = Object.keys(data.mappings).length;
if (n !== 34) throw new Error(`expected the bench's 34 mappings, built ${n}`);
fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(OUT, JSON.stringify(data, null, 1) + '\n');
console.log(`wrote ${path.relative(process.cwd(), OUT)}: ${n} mappings, ${Buffer.byteLength(JSON.stringify(data))} bytes as one line`);
