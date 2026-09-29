// The NaviCore rig checked on its own, in node: the config model (lib/navicore/model.js) against the firmware rules
// it copies, and the emulator (lib/navicore/emulator.js) against the protocol. The L1 specs are only as good as the
// emulator, so a rule it gets wrong must fail here first. (The emulator is also compared with the real board by
// nctool.emulator_contract, L2.) Needs no NaviCore checkout; runs with nctool.unit.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { NcConfig, FACTORY_PW } = require('../../lib/navicore/model');
const { NaviEmulator, fnv1a, fragSlices, extractData } = require('../../lib/navicore/emulator');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));
const model = (cfg = BENCH) => { const m = new NcConfig(); m.fromJSON(clone(cfg)); return m; };

test('rig: the bench fixture holds no real credential and round-trips through the model byte for byte', () => {
  const text = JSON.stringify(BENCH);
  assert.equal(model().text(), text);
  for (const p of [BENCH.wcbNetwork.password, BENCH.wifiPassword, BENCH.wifiSsid, ...BENCH.wcbProfiles.map((w) => w.password)]) {
    assert.match(p, /^HIL/, 'every credential in the fixture is a HIL... placeholder');
  }
});

test("rig: the model reads JSON the way ArduinoJson 7's `|` does (type, not truthiness)", () => {
  const m = model();
  m.fromJSON({ mappings: { 136: { t1: [
    { type: 'wcb_unicast', target: '2', cmd: ';M11', skipRunning: 1 },     // the tool's form (readActionFromFid)
    { type: 'wcb_unicast', target: '2', cmd: ';M12', skipRunning: true },
    { type: 'wcb_unicast', target: 3, cmd: ';S1X' },                        // a number is not a const char*
    { type: 'smooth', cmd: 'x' },                                           // retired type: dropped, takes no slot
    { type: 'serial', port: 'S4', cmd: ':A', delay: '100' },                // a string delay reads 0
  ] } } });
  assert.deepEqual(m.toJSON().mappings['136'].t1, [
    { type: 'wcb_unicast', target: '2', cmd: ';M11' },
    { type: 'wcb_unicast', target: '2', cmd: ';M12', skipRunning: true },
    { type: 'wcb_unicast', target: '', cmd: ';S1X' },
    { type: 'serial', port: 'S4', cmd: ':A' },
  ]);
});

test('rig: the model keeps what a SET_CONFIG omits and rebuilds what it names (the nc_guard trap)', () => {
  const m = model();
  m.fromJSON({ tapWindowMs: 50, holdMs: 520, wcbNetwork: { channel: 13 } });
  let c = m.toJSON();
  assert.equal(c.tapWindowMs, 500, 'a sub-100 ms window reads as unset');
  assert.equal(c.holdMs, 750, 'a hold at or under tapWindow + 100 becomes tapWindow + 250');
  assert.equal(c.wcbNetwork.channel, 1, 'an out-of-range mesh channel falls back to 1');
  assert.equal(c.wcbNetwork.password, BENCH.wcbNetwork.password, 'an omitted password is kept');
  assert.deepEqual(Object.keys(c.mappings), Object.keys(BENCH.mappings), 'an absent mappings key changes nothing');
  m.fromJSON({ mappings: { 101: { t2: [{ type: 'stop' }] } } });
  c = m.toJSON();
  assert.deepEqual(c.mappings['101'], { exclusive: false, t2: [{ type: 'stop' }] }, 'a named mapping is rebuilt from scratch');
  m.fromJSON({ maestros: [{ type: 1, device: 1 }] });
  assert.equal(m.toJSON().maestros[0].channels.length, 24, 'a slot sent without "channels" keeps its channels');
  m.fromJSON({ maestros: [{ type: 1, device: 1, channels: [] }] });
  assert.equal(m.toJSON().maestros[0].channels, undefined, 'an empty "channels" clears them and the key is not printed');
  m.fromJSON({ serialLabels: { S3: 'HCR', bogus: 'x' } });
  assert.deepEqual(m.toJSON().serialLabels, { S3: 'HCR' });
  m.fromJSON({ serialLabels: {} });
  assert.equal(m.toJSON().serialLabels, undefined);
  m.loadDefaults();
  assert.equal(m.toJSON().wcbNetwork.password, FACTORY_PW, 'RESET_DEFAULTS reloads the compile-time password (a placeholder here)');
  assert.equal(Object.keys(m.toJSON().mappings).length, 0);
});

test('rig: the emulator slices a download the way _startFragSend does (<= 187 B envelopes, exact join)', () => {
  const corpus = [JSON.stringify({ type: 'CONFIG', id: 20, data: BENCH }), '"'.repeat(600), '机🤖'.repeat(300), 'x'];
  for (const s of corpus) {
    const parts = fragSlices(s);
    assert.equal(parts.join(''), s);
    parts.forEach((p, i) => assert.ok(Buffer.byteLength(JSON.stringify({ f: 512, of: 512, sid: 65535, s: p })) <= 187, `part ${i}`));
  }
  assert.equal(fnv1a(''), 2166136261);
  assert.equal(fnv1a('a'), 0xe40c292c);
  assert.equal(extractData('{"sys":1,"type":"SET_CMDLIB","data":{"boards":[{"id":"x}"}]},"sys2":1}'), '{"boards":[{"id":"x}"}]}');
});

function run(emu, lines) {
  const out = [];
  emu.attach((b) => out.push(...b.toString('utf8').split('\r\n').filter(Boolean)));
  emu.onOpen();
  for (const l of lines) emu.onWrite(Buffer.from(l + '\n'));
  return new Promise((r) => setTimeout(() => r(out), 60));
}

test('rig: the emulator answers direct USB lines like NaviCore.ino processInputLine', async () => {
  const emu = new NaviEmulator({ config: BENCH });
  const out = await run(emu, [
    '{"sys":1,"type":"PING"}', ';w20,{"sys":1,"type":"PING"}', 'PING', '{"sys":1,"type":"SET_CONFIG","data":{"tapWindowMs":600},"saveId":7}',
    '{"sys":1,"type":"NOPE"}', '{broken', '?FOO',
  ]);
  assert.deepEqual(out, [
    `{"type":"PONG","version":"${emu.version}"}`,
    '{"type":"ACK","of":"SET_CONFIG","ok":true,"saveId":7}',
    '{"type":"ERROR","msg":"unknown type"}',
    '{"type":"ERROR","msg":"JSON parse failed (InvalidInput)","rxLen":7}',
    'Unknown command: ?FOO',
  ]);
  assert.equal(emu.config.toJSON().tapWindowMs, 600);
  emu.stop();
});

test('rig: via WCB, the emulator reassembles a fragmented SET_CONFIG, strips the transport fields and ACKs with sys', async () => {
  const emu = new NaviEmulator({ mode: 'via-wcb', config: BENCH });
  const inner = JSON.stringify({ sys: 1, type: 'SET_CONFIG', data: { wcbNetwork: { deviceId: 7, password: 'x', channel: 3 }, tapWindowMs: 650 }, saveId: 9 });
  const parts = fragSlices(inner);
  const lines = parts.map((s, i) => ';w20,' + JSON.stringify({ f: i + 1, of: parts.length, sid: 4, s }));
  const out = await run(emu, ['{"sys":1,"type":"PING"}', ';w20,{"sys":1,"type":"PING"}', ...lines.reverse()]);
  assert.equal(out[0], `{"sys":1,"type":"PONG","id":20,"version":"${emu.version}","model":0,"mode":1}`, 'only the wrapped PING is answered');
  assert.equal(out[1], '{"sys":1,"type":"ACK","of":"SET_CONFIG","id":20,"ok":true,"saveId":9}');
  const c = emu.config.toJSON();
  assert.equal(c.tapWindowMs, 650);
  assert.equal(c.wcbNetwork.deviceId, 20, 'deviceId stripped (_applyReassembled)');
  assert.equal(c.wcbNetwork.password, BENCH.wcbNetwork.password, 'password stripped');
  assert.equal(c.wcbNetwork.channel, 3, 'channel is NOT stripped by the firmware (D-NC18)');
  emu.stop();
});

// nctool.emulator_contract's helpers (lib/navicore/contract.js), on the emulator and on lines put together from the
// firmware's own printf formats, so the L2 contract fails on a real drift and on nothing else.
const C = require('../../lib/navicore/contract');
const { normEvent } = require('../../lib/navicore/clips');

async function answers(emu) {
  const tap = new C.LineTap(emu);
  await tap.open();
  const out = {};
  for (const r of C.REQUESTS) out[r.id] = await tap.ask(r, 2000);
  return out;
}

test('rig: the contract finds no drift between two emulators, with and without clips', async () => {
  const one = new NaviEmulator({ config: BENCH, clips: { HILc: { mode: 1, events: [normEvent({ t: 0, k: 1, slot: 4, ch: 0, pos: 6000 })] } } });
  const two = new NaviEmulator({ config: BENCH, roster: { known: [1, 1], online: [1, 0] } });
  const a = await answers(one), b = await answers(two);
  for (const r of C.REQUESTS) assert.deepEqual(C.compare(r, a[r.id], b[r.id]), [], r.id);
  assert.deepEqual(C.recLsShape(a['?REC,LS']).markers, ['[CLIPFS]', '[REC] clips:', '[CLIPLIST:BEGIN]', '[CLIPITEM]*', '[CLIPLIST:END]']);
  one.stop(); two.stop();
});

test("rig: the contract accepts the firmware's own formats and names each drift by path and type only", async () => {
  const emu = new NaviEmulator({ config: BENCH });
  const sim = await answers(emu);
  const byId = (id) => C.REQUESTS.find((r) => r.id === id);
  // buildMeshStatsPage (rc_telemetry.h:1373-1431), a USB page: aggregate under "agg", positional rows, "last":1.
  const mesh = ['{"type":"MESH_STATS","pg":0,"self":20,"upMs":98765,"agg":{"sent":5,"ackd":5,"rty":0,"fail":0,"ung":1,' +
                '"bcast":3,"recv":9},"peers":[[1,5,5,0,0,1,9],[2,0,0,0,0,0,0]],"last":1}'];
  assert.deepEqual(C.compare(byId('GET_MESH_STATS'), mesh, sim.GET_MESH_STATS), []);
  // The dense USB WCB_STATUS (NaviCore.ino:4149-4234), two boards.
  const st = ['{"type":"WCB_STATUS","quantity":1,"self":20,"online":[1,1],"known":[1,1],"clients":[0,0],"temporary":[0,0],' +
              '"aliases":["body",""],"portLabels":[["","","","",""],["","HCR","","",""]],"seqHash":[123,0]}'];
  assert.deepEqual(C.compare(byId('GET_WCB_STATUS'), st, sim.GET_WCB_STATUS), []);
  // otaPrintStatus (navicore_ota.h:250-263) and ?REC,LS with a clip (NaviCore.ino:3485-3491, navicore_record.h:960-982).
  const ota = ['---------- OTA Status ----------', 'Chip:        ESP32-S3 (family 1)', 'Firmware:    v0.2.0_281426QSEP26',
               'App SHA256:  529503cd35f1e5e5', "Running:     'app1' @0x1f0000 (1966080 B)",
               "Next (OTA):  'app0' @0x010000 (1966080 B)", 'Session:     idle', '--------------------------------'];
  assert.deepEqual(C.compare(byId('?OTALOCAL,STATUS'), ota, sim['?OTALOCAL,STATUS']), []);
  const ls = ['[CLIPFS]{"total":12582912,"used":8192}', '[REC] clips:', '[CLIPLIST:BEGIN]',
              '[CLIPITEM]{"name":"wave","bytes":4216,"dur":2760,"n":30}', '[CLIPLIST:END]'];
  assert.deepEqual(C.compare(byId('?REC,LS'), ls, sim['?REC,LS']), []);
  // Drifts are reported, without a value: the old flat MESH_STATS keys, a STATUS without its App SHA256 line.
  const flat = ['{"type":"MESH_STATS","pg":0,"self":20,"upMs":1,"sent":0,"ackd":0,"retries":0,"failed":0,"unguaranteed":0,' +
                '"bcast":0,"recv":0,"peers":[],"last":1}'];
  const d = C.compare(byId('GET_MESH_STATS'), mesh, flat);
  assert.ok(d.includes('GET_MESH_STATS: agg: only on the board') && d.includes('GET_MESH_STATS: retries: only in the emulator'), d.join('; '));
  assert.equal(C.compare(byId('?OTALOCAL,STATUS'), ota, ota.filter((l) => !l.startsWith('App SHA256'))).length, 1);
  assert.deepEqual(C.compare(byId('PING'), ['{"type":"PONG","version":"v1"}'], ['{"type":"PONG","version":2}']),
                   ['PING: version: string vs number']);
  emu.stop();
});

test("rig: the contract's value diff names paths and types, never a value", () => {
  const a = clone(BENCH), b = clone(BENCH);
  b.wcbNetwork.password = 'HILotherSecret';
  b.tapWindowMs = 610;
  delete b.statsReport;
  const d = C.valuePaths(a, b);
  assert.deepEqual(d.sort(), ['statsReport: only on the board', 'tapWindowMs: number vs number', 'wcbNetwork.password: string vs string']);
  assert.ok(!d.join(' ').includes('HILotherSecret') && !d.join(' ').includes(BENCH.wcbNetwork.password));
});
