// L2: the emulator held to the real board (docs/hil_plan/NAVICORE.md §5.3, NC-WP13). No page: the read-only request set
// goes to the real NaviCore through the pipe and to the in-Node emulator, and lib/navicore/contract.js compares the
// replies by shape - types, key sets, the CLI markers' order - never by value. The L1 specs are only as good as the
// emulator, so a firmware change it has not caught up with fails here. Only under the harness, inside nc_guard:
// python tests/hil/run.py nctool.emulator_contract. Standalone and in CI it skips.
//
// One check is stronger than shapes: the emulator's config model (lib/navicore/model.js), given the board's own
// GET_CONFIG, must print it back byte for byte, which is what the bench-shaped fixture and every L1 save rely on. The
// board's config stays in memory; a difference is reported by key path and value type only.
const { test, expect } = require('../../lib/navicore/fixtures');
const C = require('../../lib/navicore/contract');
const { NcConfig } = require('../../lib/navicore/model');
const { normEvent } = require('../../lib/navicore/clips');

test.beforeEach(({ hilCtx }) => {
  test.skip(!hilCtx || !hilCtx.pipe || hilCtx.device !== 'navicore',
            'L2 runs under the HIL harness with NaviCore piped: python tests/hil/run.py nctool.emulator_contract');
});

test('nctool.emulator_contract the emulator answers PING, GET_CONFIG, GET_CMDLIB_META, GET_WCB_STATUS, GET_MESH_STATS, ?REC,LS and ?OTALOCAL,STATUS in the real NaviCore\'s shapes, and its config model prints the board\'s own config back byte for byte', async ({ device, emu }) => {
  const real = new C.LineTap(device), sim = new C.LineTap(emu);
  await real.open();
  await sim.open();
  const byId = (id) => C.REQUESTS.find((r) => r.id === id);
  // The board's config first (asked twice at most: an 8 KB TX ring can cut a CONFIG line the host does not drain in
  // time, NaviCore.ino:4500-4530), then the emulator is given it, so both hold the same config.
  let cfgLine;
  for (let i = 0; i < 2 && !cfgLine; i++) cfgLine = (await real.ask(byId('GET_CONFIG'), 15_000).catch(() => null))?.[0];
  expect(cfgLine, 'a whole GET_CONFIG reply from the board').toBeTruthy();
  const at = cfgLine.indexOf('"data":') + 7;
  const text = cfgLine.slice(at, -1);                               // {"type":"CONFIG","data":<text>} (:3879-3881)
  const model = new NcConfig();
  model.fromJSON(JSON.parse(text));
  const reprint = model.text();
  if (reprint !== text) {
    const paths = C.valuePaths(JSON.parse(text), JSON.parse(reprint));
    expect.soft(paths.length ? paths : ['same values, other text (key order or number format)'],
                "where the model's reprint of the board's config differs").toEqual([]);
  }
  emu.config = model;
  const answers = { real: { GET_CONFIG: [cfgLine] }, sim: {} };
  for (const r of C.REQUESTS) {
    if (r.id !== 'GET_CONFIG') answers.real[r.id] = await real.ask(r, 15_000);
  }
  // A clip in the emulator only when the board lists one, so both [CLIPITEM] key sets can be compared.
  if (C.recLsShape(answers.real['?REC,LS']).item) {
    emu.clips.set('HILcontract', { mode: 1, events: [normEvent({ t: 0, k: 1, slot: 4, ch: 0, pos: 6000 })] });
  }
  for (const r of C.REQUESTS) answers.sim[r.id] = await sim.ask(r, 5_000);
  const problems = C.REQUESTS.flatMap((r) => C.compare(r, answers.real[r.id], answers.sim[r.id]));
  expect(problems, 'where the emulator does not answer in the board\'s shape').toEqual([]);
  await device.flush();
  expect(device.sentTypes.filter((t) => !['PING', 'GET_CONFIG', 'GET_CMDLIB_META', 'GET_WCB_STATUS', 'GET_MESH_STATS', 'text'].includes(t)),
         'requests outside the read-only set').toEqual([]);
  expect([...new Set(device.sentCli)].sort()).toEqual(['#L12', '?OTALOCAL,STATUS', '?REC,LS']);
  expect(device.errors, 'bridge errors').toEqual([]);
  device.onClose();
});
