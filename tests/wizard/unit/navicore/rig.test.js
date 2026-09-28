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
