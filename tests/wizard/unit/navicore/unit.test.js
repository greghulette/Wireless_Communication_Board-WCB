// nctool.unit — the NaviCore config tool's pure functions, lifted out of the page and run in node
// (docs/hil_plan/NAVICORE.md §5.4, L0 item 7). The config round trip's node half: the save diff, the bridge
// fragmenter, the command-library codec, the sequence renderer's parity with the Wizard, the CSV parser and the
// import sniffer. The DOM-bound half (applyConfig, readActionFromFid, saveConfigToBoard) is the Playwright specs'.
//
//   node --test unit/navicore/unit.test.js        (tests/wizard; the harness test nctool.unit runs it with rig.test.js)
//
// Skips when the NaviCore repo is not beside this one. A lifted name that is gone fails, naming it.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { navicoreRoot, ROOT } = require('../../lib/navicore/paths');
const { NcConfig } = require('../../lib/navicore/model');
const { sandbox } = require('./extract');

const NC = navicoreRoot();
const skip = NC ? false : 'the NaviCore repo is not beside this one (clone it, or set NAVICORE_REPO)';
const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));
const bytes = (s) => Buffer.byteLength(s, 'utf8');

// Objects built inside the vm sandbox have that realm's prototypes, which assert.deepStrictEqual refuses to equate
// with this realm's: results are compared as plain JSON.
const diffFns = () => {
  const s = sandbox(['_deepClone', '_stringifyStable', '_diffConfigBranches', '_diffMappings']);
  return { ...s, _diffConfigBranches: (a, b) => clone(s._diffConfigBranches(a, b)) };
};

test('nctool.unit: _stringifyStable ignores object key order and keeps array order', { skip }, () => {
  const { _stringifyStable: st } = diffFns();
  assert.equal(st({ b: 1, a: { d: [3, 1], c: null } }), st({ a: { c: null, d: [3, 1] }, b: 1 }));
  assert.notEqual(st({ a: [1, 2] }), st({ a: [2, 1] }));
  assert.notEqual(st({ a: 1 }), st({ a: '1' }));           // a type change is a change (applyConfig coerces for this)
});

test('nctool.unit: the save diff against the bench config — nothing, one branch, one button, a deleted button', { skip }, () => {
  const { _deepClone, _diffConfigBranches: diff } = diffFns();
  const base = _deepClone(BENCH);
  assert.deepEqual(diff(_deepClone(BENCH), base), {}, 'a config diffed with its own clone ships nothing');

  const one = _deepClone(BENCH); one.tapWindowMs = 600;
  assert.deepEqual(diff(one, base), { tapWindowMs: 600 });

  // Mappings are sub-diffed per button: one action edit ships that button, not all 34.
  const btn = _deepClone(BENCH); btn.mappings['101'].t1[0].cmd = ';S2HILEDIT';
  const d = diff(btn, base);
  assert.deepEqual(Object.keys(d), ['mappings']);
  assert.deepEqual(Object.keys(d.mappings), ['101']);
  assert.deepEqual(d.mappings['101'], btn.mappings['101']);

  // A deleted button ships {} — the firmware memsets that slot (rc_config.h:1580-1603), an absent key it leaves alone.
  const del = _deepClone(BENCH); delete del.mappings['102'];
  assert.deepEqual(diff(del, base), { mappings: { 102: {} } });
  const m = new NcConfig(); m.fromJSON(clone(BENCH)); m.fromJSON({ mappings: { 102: {} } });
  assert.equal(m.toJSON().mappings['102'], undefined, 'the model (firmware) drops the cleared mapping from GET_CONFIG');

  // No baseline (Save before any Load): the whole config.
  assert.deepEqual(diff(_deepClone(BENCH), null), BENCH);
});

test('nctool.unit: a baseline-only branch ships null — and what the firmware makes of that (D-NC22)', { skip }, () => {
  const { _deepClone, _diffConfigBranches: diff } = diffFns();
  // Pinned: the tool never deletes a top-level branch in normal flow, but if one goes missing the diff sends null.
  for (const key of ['hcrDest', 'mp3Dest', 'dfpDest', 'statsReport', 'tapWindowMs']) {
    const cur = _deepClone(BENCH); delete cur[key];
    assert.deepEqual(diff(cur, _deepClone(BENCH)), { [key]: null }, key);
  }
  // rcConfigFromJSON reads a null branch as present-but-empty, so each field takes its `|` default: a disabled or
  // WCB-routed audio destination becomes LOCAL SERIAL S3 (D-NC22), and a null tapWindowMs becomes 500.
  const m = new NcConfig(); m.fromJSON(clone(BENCH));
  m.fromJSON({ hcrDest: null, dfpDest: null, tapWindowMs: null });
  const out = m.toJSON();
  assert.deepEqual(out.hcrDest, { transport: 'serial', port: 'S3' }, 'hcrDest: null enables the HCR on S3');
  assert.deepEqual(out.dfpDest, { transport: 'serial', port: 'S3' }, 'dfpDest: null enables the DFPlayer on S3');
  assert.equal(out.tapWindowMs, 500);
  m.fromJSON({ mp3Dest: null });
  assert.deepEqual(m.toJSON().mp3Dest, { transport: 'wcb', target: '2' }, 'mp3Dest: null routes the MP3 Trigger to WCB 2');
});

test('nctool.unit: _fragChunks — every envelope fits 187 B and the parts join back exactly, over a hostile corpus', { skip }, () => {
  const f = sandbox(['FRAG_MAX_ENV_BYTES', 'FRAG_ENV_TARGET_BYTES', 'FRAG_ENV_OVERHEAD', '_fragEscBytes', '_fragChunks']);
  assert.equal(f.FRAG_MAX_ENV_BYTES, 187);
  const setConfig = JSON.stringify({ sys: 1, type: 'SET_CONFIG', data: BENCH, saveId: 1 });
  const corpus = {
    'the bench SET_CONFIG': setConfig,
    quotes: '"'.repeat(1000),
    backslashes: '\\'.repeat(1000),
    'quotes and backslashes': '\\"'.repeat(500),
    'escaped config (a note full of quotes)': JSON.stringify({ note: '"a" \\b\\ "c"'.repeat(60) }),
    CJK: '机器人配置保存测试'.repeat(80),
    'emoji (surrogate pairs)': '🤖🔊🎛️'.repeat(120),
    'control characters': Array.from({ length: 32 }, (_, i) => String.fromCharCode(i)).join('').repeat(20),
    mixed: ('a"é\\\n\u0001机🤖').repeat(90),
    'a note with an escaped lone surrogate (what JSON.stringify makes of one)': JSON.stringify({ note: 'ab\uD800'.repeat(60) }),
    'one char': 'x',
  };
  for (const [name, s] of Object.entries(corpus)) {
    const chunks = f._fragChunks(s);
    assert.equal(chunks.join(''), s, `${name}: the parts do not join back to the input`);
    chunks.forEach((c, i) => {
      assert.ok(c.length > 0, `${name}: empty chunk ${i}`);
      const env = JSON.stringify({ f: 999, of: 999, sid: 99999, s: c });   // worst-case header, as sendJSON checks
      assert.ok(bytes(env) <= 187, `${name}: chunk ${i} escapes to ${bytes(env)} B`);
      assert.ok(!/[\uD800-\uDBFF]$/.test(c) || !/^[\uDC00-\uDFFF]/.test(chunks[i + 1] || ''), `${name}: chunk ${i} splits a surrogate pair`);
    });
  }
  // The fill is adaptive (FRAG_ENV_TARGET_BYTES): plain config text packs well past the old fixed 80 B per part.
  const n = f._fragChunks(setConfig).length;
  assert.ok(n < Math.ceil(bytes(setConfig) / 80), `the bench SET_CONFIG takes ${n} parts — no better than fixed 80 B chunks`);
});

// _fragChunks's own comment says a lone surrogate (JSON escapes it to 6 bytes, _fragEscBytes counts 3) is carried
// into the next chunk, "never dropped". Its last flush() used to carry the overflow into `cur` and return, so the
// input's tail was lost (NaviCore 2316d28 flushes it). Latent even then: its callers (sendJSON, _pushBudgetInfo) only
// pass JSON.stringify output, which escapes every lone surrogate (the next test pins that).
test('nctool.unit: _fragChunks keeps the tail of an input that holds lone surrogates', { skip }, () => {
  const { _fragChunks } = sandbox(['FRAG_MAX_ENV_BYTES', 'FRAG_ENV_TARGET_BYTES', 'FRAG_ENV_OVERHEAD', '_fragEscBytes', '_fragChunks']);
  for (const s of ['\uD800x'.repeat(200), 'y\uDC00'.repeat(200), '\uD800𐀀'.repeat(80)]) {
    assert.equal(_fragChunks(s).join('').length, s.length, 'the parts do not join back to the input');
  }
});

test('nctool.unit: what sendJSON fragments never holds a lone surrogate (JSON.stringify escapes them)', { skip }, () => {
  // Every cut the tool itself makes to a text field (.slice(0, 19) of a note, .slice(0, 23) of a label) can split an
  // emoji and leave a lone surrogate in the config. sendJSON fragments JSON.stringify({sys, type, data, saveId}), and
  // well-formed JSON.stringify (ES2019) writes a lone surrogate as a \uXXXX escape: plain ASCII to _fragChunks.
  const note = 'Screams 😱😱😱😱😱😱😱'.slice(0, 19);   // 8 characters, then emoji: the cut lands mid-pair
  assert.ok(/[\uD800-\uDBFF]$/.test(note), 'the 19-character cut should leave a lone high surrogate');
  const inner = JSON.stringify({ sys: 1, type: 'SET_CONFIG', data: { mappings: { 101: { t1note: note } } }, saveId: 1 });
  assert.ok(!/[\uD800-\uDFFF]/.test(inner.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g, '')), 'a lone surrogate survives JSON.stringify');
  const { _fragChunks } = sandbox(['FRAG_MAX_ENV_BYTES', 'FRAG_ENV_TARGET_BYTES', 'FRAG_ENV_OVERHEAD', '_fragEscBytes', '_fragChunks']);
  assert.equal(_fragChunks(inner).join(''), inner);
});

test('nctool.unit: every command-library command encodes and decodes back to its own wire string', { skip }, () => {
  const { buildLibrary } = require('./cmdlib');
  const L = buildLibrary();
  const ids = L.lib.boards.map((b) => b.id);
  assert.equal(ids[0], 'wcb-sequences');
  assert.ok(!ids.includes('wcb-native') && !ids.includes('maestro') && !ids.includes('maestro-native'), 'hidden/superseded boards are gone');
  const valuesFor = (cmd) => {
    const v = {};
    for (const p of cmd.params || []) {
      if (p.enum) {
        const codes = (L._cmdlibEnum(p.enum) || {}).values || [];
        v[p.name] = codes.length ? String(codes[codes.length - 1].code) : 'x';
      } else if (p.type === 'int') {
        let n = p.default !== undefined && p.default !== '' ? +p.default : (p.min != null ? +p.min : 1);
        if (p.max != null && n > +p.max) n = +p.max;
        v[p.name] = p.width ? String(n).padStart(+p.width, '0').slice(-(+p.width)) : String(n);
      } else v[p.name] = p.default !== undefined && p.default !== '' ? String(p.default) : 'abc';
    }
    return v;
  };
  let checked = 0;
  const templateless = [], collided = [];
  for (const b of L.lib.boards) {
    for (const c of b.commands || []) {
      const lk = c.local || b.local;
      if (lk && lk !== 'maestro') continue;              // record/play/stop: never decoded (ncDecodeCommand)
      if (!c.template) { templateless.push(`${b.id}/${c.id}`); continue; }
      const wire = L.ncEncodeCommand(c, valuesFor(c));
      const d = L.ncDecodeCommand(wire);
      assert.ok(d, `${b.id}/${c.id}: "${wire}" does not decode`);
      assert.equal(L.ncEncodeCommand(d.cmd, d.values), wire, `${b.id}/${c.id}: re-encoding the decode changes the wire`);
      if (d.cmd !== c) {
        // Only the same template skeleton earlier in picker order may win (one wire format on two boards; the param
        // names may differ, e.g. astropixels-logics '@{address}M{text}' and rseries-logic '@{addr}M{text}').
        const skel = (t) => String(t).replace(/\{[A-Za-z][A-Za-z0-9]*\}/g, '{}');
        assert.equal(skel(d.cmd.template), skel(c.template), `${b.id}/${c.id}: "${wire}" decodes as ${d.board.id}/${d.cmd.id}`);
        collided.push(`${b.id}/${c.id}`);
      }
      checked++;
    }
  }
  assert.ok(checked > 300, `only ${checked} commands checked`);
  // A command with no template (a custom `encoder`) encodes to '' — _cmdlibUse refuses it (index.html:14129-14141).
  for (const id of templateless) {
    const [bid, cid] = id.split('/');
    const cmd = L.lib.boards.find((b) => b.id === bid).commands.find((c) => c.id === cid);
    assert.ok(cmd.encoder, `${id} has neither a template nor an encoder`);
    assert.equal(L.ncEncodeCommand(cmd, valuesFor(cmd)), '', `${id}`);
  }
  assert.ok(collided.length <= 5, `${collided.length} commands share a template with another board: ${collided.join(', ')}`);
});

test("nctool.unit: _seqValueToLines is the Wizard's seqValueToLines, line for line", { skip }, () => {
  const { _seqValueToLines } = sandbox(['_seqValueToLines']);
  const app = fs.readFileSync(path.join(ROOT, 'Wizard', 'app.js'), 'utf8');
  const { seqValueToLines } = sandbox(['seqValueToLines'], {}, app);
  const corpus = [
    '', ';M11', ';M11^;t500^;M12', ';M11^^;M12', ';M11^*** open the pie', ';M11 ***open', '*** leading note^;M11',
    ';M11^^*** own line^;M12', 'IF,V1,=,1^;M11', 'IF,V1,=,1^;t500^;t200^;M11^;M12', 'IF,V1,>,2^^;M11',
    'IF,V2,<,3***cmt^;M11', ';S1HELLO^;S2WORLD^^^;S3!', '  ;M11  ^  ;M12  ', 'IF,V1,=,1^***note^;M11', ';T500^;M11',
    ';M1,1***a^;S5,<CA1021>***b', 'if,v1,=,1^;t10^;m11', '^^^', ';M11^;t500', 'IF,V1,=,1',
  ];
  for (const v of corpus) assert.equal(_seqValueToLines(v).join('\n'), seqValueToLines(v, '^'), JSON.stringify(v));
});

test('nctool.unit: parseCsv reads quoted fields, doubled quotes, CRLF and a last line with no newline', { skip }, () => {
  const s = sandbox(['parseCsv']);
  const parseCsv = (t) => clone(s.parseCsv(t));
  assert.deepEqual(parseCsv('a,b,c\n1,2,3\n'), [['a', 'b', 'c'], ['1', '2', '3']]);
  assert.deepEqual(parseCsv('x,"a, b","say ""hi"""\r\ny,,z'), [['x', 'a, b', 'say "hi"'], ['y', '', 'z']]);
  assert.deepEqual(parseCsv('"multi\nline",2\n'), [['multi\nline', '2']]);
  assert.deepEqual(parseCsv(''), []);
  assert.deepEqual(parseCsv('only'), [['only']]);
  assert.deepEqual(parseCsv('a,\n'), [['a', '']]);
});

test('nctool.unit: _cfgExtractConfig takes an export wrapper or a bare config and refuses anything else', { skip }, () => {
  const { _cfgExtractConfig: x } = sandbox(['CFG_KNOWN_KEYS', '_cfgExtractConfig']);
  const bare = clone(BENCH);
  assert.equal(x({ navicore: 'config/v1', savedAt: 'now', note: '', config: bare }), bare);
  assert.equal(x(bare), bare);
  assert.throws(() => x({}), /not a NaviCore config/);
  assert.throws(() => x([]), /no config/);
  assert.throws(() => x(null), /no config/);
  // A WCB Wizard system file (its boards, not a controller config) is refused before it can reach applyConfig.
  assert.throws(() => x({ version: 1, boards: { 1: { wcbNumber: 1 } }, globals: {} }), /not a NaviCore config/);
  assert.throws(() => x({ config: { hello: 1 } }), /not a NaviCore config/);
});
