// Wizard/seq-builder.js: the sequence builder's core - a stored value to a timeline and back, the firmware rules it
// encodes, and command-library decoding against the real cmdlib/ files. No browser, no board (`npm run unit`, CI).
//
// The rules come from the firmware, not from the builder's comments:
// - ;T<ms> starts a group that fires <ms> after the group before it; consecutive delays add up; each is capped at
//   30 minutes; a negative one refuses the whole chain (command_timer.cpp parseCommandGroups).
// - an IF gates the next command, and a gated pure delay is dropped along with it (ifGateConsumeToken,
//   WCB_Variables.cpp), so the builder writes an IF after its delay.
// - a chain carrying ?SEQ,SAVE / ?CS / ?MGMT never reaches the timer engine (chainCarriesValueVerb, WCB.ino).
// - ;W<n>,<cmd> / ;W<d><cmd> route to WCB n; ;S<p> writes port p minus one comma (processWCBMessage,
//   processSerialMessage, WCB.ino).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SB = require('../../../Wizard/seq-builder.js');
const WIZ = path.join(__dirname, '..', '..', '..', 'Wizard');

// The Wizard's own text editor functions, out of app.js, so the round trip is checked against the real code.
function extractFunction(src, name) {
  const start = src.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} not found in app.js`);
  let depth = 0, i = src.indexOf('{', start);
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}' && --depth === 0) break;
  }
  return src.slice(start, i + 1);
}
const APP = fs.readFileSync(path.join(WIZ, 'app.js'), 'utf8');
const editor = {};
vm.runInNewContext(
  extractFunction(APP, 'seqTextareaToValue') + '\n' + extractFunction(APP, 'seqValueToLines') +
  '\nout.toValue = seqTextareaToValue; out.toLines = seqValueToLines;', { out: editor });

function loadLibrary() {
  const lib = SB.sbNewLibrary();
  for (const dir of ['droidnet', 'wcb']) {
    const base = path.join(WIZ, 'cmdlib', dir);
    const manifest = JSON.parse(fs.readFileSync(path.join(base, 'manifest.json'), 'utf8'));
    for (const b of manifest.boards) SB.sbMergeLibrary(lib, JSON.parse(fs.readFileSync(path.join(base, b.file), 'utf8')));
  }
  SB.sbNormalizeLibrary(lib);
  return lib;
}
const LIB = loadLibrary();
const decode = (cmd, chars) => {
  const d = SB.sbDecodeCmd(LIB, cmd, chars);
  return d.hit ? { board: d.hit.board.id, cmd: d.hit.cmd.id, values: d.hit.values, route: d.route } : { route: d.route };
};
const ev = (t, cmd, extra) => Object.assign({ t, cmd, conds: [], note: '' }, extra);
const compile = (events, markers, chars) => SB.sbCompile({ events, markers: markers || [] }, chars);

test('compile: one ;T per distinct time, carrying the gap to it', () => {
  assert.strictEqual(compile([ev(0, ';M11'), ev(0, ';A,PLAY,5'), ev(1250, ';S3A001'), ev(4000, ';L1,PS,2')]),
    ';M11^;A,PLAY,5^;T1250^;S3A001^;T2750^;L1,PS,2');
});

test('compile: steps sort by time; steps at one time keep their order', () => {
  assert.strictEqual(compile([ev(500, 'b'), ev(0, 'a'), ev(500, 'c')]), 'a^;T500^b^c');
});

test('compile: a sequence that starts late opens with a delay', () => {
  assert.strictEqual(compile([ev(300, ';M11')]), ';T300^;M11');
});

test('compile: a gap over 30 minutes becomes several delays, which the board adds up', () => {
  const v = compile([ev(0, 'a'), ev(2000000, 'b')]);
  assert.strictEqual(v, 'a^;T1800000^;T200000^b');
  assert.deepStrictEqual(SB.sbParse(v).events.map(e => e.t), [0, 2000000]);
});

test('compile: an IF goes after its delay, right before its command', () => {
  assert.strictEqual(compile([ev(0, 'a'), ev(500, ';M11', { conds: ['mode=2'] })]), 'a^;T500^IF,mode=2^;M11');
});

test('compile: notes ride inline, markers stand on their own, a marker past the last step goes last', () => {
  const v = compile([ev(0, ';M11', { note: 'wave' }), ev(1000, ';M12')],
    [{ t: 0, text: 'intro' }, { t: 600, text: 'half way' }, { t: 9000, text: 'end' }]);
  assert.strictEqual(v, '***intro^;M11***wave^;T600^^***half way^;T400^;M12^^***end');
});

test('compile: the delimiter inside a note or a marker is not written', () => {
  assert.strictEqual(compile([ev(0, 'a', { note: 'x^y' })], [{ t: 0, text: 'p^q' }]), '***p q^a***x y');
});

test('parse: delays accumulate, consecutive ones add up, ;t and ;T alike', () => {
  const p = SB.sbParse(';M11^;T500^;t300^;S1x^;T200^;S2y');
  assert.deepStrictEqual(p.events.map(e => [e.t, e.cmd]), [[0, ';M11'], [800, ';S1x'], [1000, ';S2y']]);
  assert.strictEqual(p.timed, true);
});

test('parse: ;t<ms>,<cmd> carries its first command inline', () => {
  assert.deepStrictEqual(SB.sbParse(';T5000,;S1:PP100').events.map(e => [e.t, e.cmd]), [[5000, ';S1:PP100']]);
});

test('parse: an IF gates the next command, across a delay, and warns that the board would skip that delay too', () => {
  const p = SB.sbParse('IF,flag=1^;t500^;M11^;M12');
  assert.deepStrictEqual(p.events.map(e => [e.t, e.cmd, e.conds]), [[500, ';M11', ['flag=1']], [500, ';M12', []]]);
  assert.ok(p.warnings.some(w => w.code === 'ifDelay'));
  assert.strictEqual(compile(p.events), ';T500^IF,flag=1^;M11^;M12');
});

test('parse: chained IFs AND together on one step; an IF at the end is reported', () => {
  const p = SB.sbParse('IF,a=1^IF,b=2^;M11^IF,c=3');
  assert.deepStrictEqual(p.events[0].conds, ['a=1', 'b=2']);
  assert.ok(p.warnings.some(w => w.code === 'ifDangling'));
});

test('parse: comments as the text editor reads them', () => {
  const p = SB.sbParse('***intro^;M11^***wave^;T500^^***half^;M12***late^^***end');
  assert.deepStrictEqual(p.markers, [
    { t: 0, text: 'intro', after: false }, { t: 500, text: 'half', after: false }, { t: 500, text: 'end', after: true }]);
  assert.deepStrictEqual(p.events.map(e => [e.t, e.cmd, e.note]), [[0, ';M11', 'wave'], [500, ';M12', 'late']]);
});

test('parse: a negative delay refuses the chain; one over 30 minutes is capped', () => {
  assert.strictEqual(SB.sbParse(';M11^;T-500^;M12').refused, true);
  const p = SB.sbParse(';T2000000^;M11');
  assert.strictEqual(p.events[0].t, SB.SB_DELAY_CAP_MS);
  assert.ok(p.warnings.some(w => w.code === 'capped'));
});

test('parse: a chain carrying a value verb is flagged (the board runs it without delays)', () => {
  assert.strictEqual(SB.sbParse('?SEQ,SAVE,x,;M11^;T500^;M12').valueVerb, true);
  assert.strictEqual(SB.sbParse('?CS,x').valueVerb, true);
  assert.strictEqual(SB.sbParse(';M11^;T500^;M12').valueVerb, false);
  assert.strictEqual(SB.sbHasTimer('?SEQ,SAVE,x,;M11^;T500'), false);
  assert.strictEqual(SB.sbHasTimer(';M11^;T500^;M12'), true);
  assert.strictEqual(SB.sbHasTimer(';S1set;temp'), false);   // a ;t inside a payload is not a timer token
});

test('round trip: compile(parse(v)) gives back what the builder writes', () => {
  for (const v of [
    ';M11^;A,PLAY,5^;T1250^;W2,;S3A001^;T2750^;L1,PS,2',
    '***intro^;M11***wave^;T600^^***half way^;T400^IF,mode=2^;M12^^***end',
    ';T300^;V,armed,TOGGLE^;P11500',
  ]) {
    const p = SB.sbParse(v);
    assert.strictEqual(compile(p.events, p.markers), v);
  }
});

test('round trip: the Wizard text editor gives the builder\'s value back exactly', () => {
  // The builder writes seqValueToLines(value) into the row, and SAVE/UPDATE and a push read seqTextareaToValue(text).
  for (const v of [
    ';M11^;A,PLAY,5^;T1250^;W2,;S3A001^;T2750^;L1,PS,2',
    '***intro^;M11***wave^;T600^^***half way^;T400^IF,mode=2^;M12^^***end',
    ';T500^IF,a=1,AND,b=2^;M11***gated^;M12',
    compile([ev(0, ';M11', { note: 'a***b' }), ev(10, ';S1hello world')], [{ t: 10, text: 'go' }]),
  ]) {
    assert.strictEqual(editor.toValue(editor.toLines(v, '^', ';'), '^'), v, v);
  }
});

test('other characters: delimiter, command character and function identifier come from the board', () => {
  const chars = { delim: '|', cmd: '#', func: '!' };
  const p = SB.sbParse('#M11|#T500|#W2,#S3A001', chars);
  assert.deepStrictEqual(p.events.map(e => [e.t, e.cmd]), [[0, '#M11'], [500, '#W2,#S3A001']]);
  assert.strictEqual(SB.sbCompile({ events: p.events, markers: [] }, chars), '#M11|#T500|#W2,#S3A001');
  assert.strictEqual(decode('#M11', chars).cmd, 'wcbmaestro.run');
  assert.deepStrictEqual(decode('#W2,#S3A001', chars).route, { wcb: 2, port: 3, alias: false });
});

test('routes: ;W comma and one-digit forms, ;S strips one comma, ;SEQ is not a port', () => {
  const ch = { delim: '^', cmd: ';', func: '?' };
  assert.deepStrictEqual(SB.sbSplitRoute(';W13,;S4:PP100', ch), { route: { wcb: 13, port: 4, alias: false }, body: ':PP100' });
  assert.deepStrictEqual(SB.sbSplitRoute(';W2;S1Test', ch), { route: { wcb: 2, port: 1, alias: false }, body: 'Test' });
  assert.deepStrictEqual(SB.sbSplitRoute(';S3,hello', ch), { route: { wcb: 0, port: 3, alias: false }, body: 'hello' });
  assert.deepStrictEqual(SB.sbSplitRoute(';SEQwave', ch), { route: { wcb: 0, port: null, alias: false }, body: ';SEQwave' });
  assert.strictEqual(SB.sbSplitRoute(';wdome,;s2x', ch).route.alias, true);
  assert.strictEqual(SB.sbJoinRoute({ wcb: 2, port: 3 }, 'A001', ch, 1), ';W2,;S3A001');
  assert.strictEqual(SB.sbJoinRoute({ wcb: 1, port: 3 }, 'A001', ch, 1), ';S3A001');      // this board: no ;W
  assert.strictEqual(SB.sbJoinRoute({ wcb: 0, port: 2 }, ',x', ch, 1), ';S2,,x');         // the board strips one comma
});

test('recall keys: ;C and ;SEQ, with ,L; a routed recall runs elsewhere', () => {
  const ch = { delim: '^', cmd: ';', func: '?' };
  assert.strictEqual(SB.sbRecallKey(';Cwave', ch), 'wave');
  assert.strictEqual(SB.sbRecallKey(';SEQwave,L', ch), 'wave');
  assert.strictEqual(SB.sbRecallKey(';seqwave', ch), 'wave');
  assert.strictEqual(SB.sbRecallKey(';W2,;Cwave', ch), null);
  assert.strictEqual(SB.sbRecallKey(';S1x', ch), null);
});

test('analyze: a timed sequence started mid-way stops this one; a self-recall is refused', () => {
  const seqs = { sub: ';M11^;T500^;M12', flat: ';M11^;M12' };
  const run = (events, key) => SB.sbAnalyze({ events, markers: [] },
    { key, value: compile(events), seqValue: (k) => (k in seqs ? seqs[k] : null) });
  assert.ok(run([ev(0, ';Csub', { id: 1 }), ev(100, ';M13', { id: 2 })], 'main').some(w => w.level === 'warn' && /one timed sequence at a time/.test(w.text)));
  assert.ok(!run([ev(0, ';M13', { id: 1 }), ev(100, ';Csub', { id: 2 })], 'main').some(w => /one timed sequence/.test(w.text)));
  assert.ok(!run([ev(0, ';Cflat', { id: 1 }), ev(100, ';M13', { id: 2 })], 'main').some(w => /one timed sequence/.test(w.text)));
  assert.ok(run([ev(0, ';Cmain', { id: 1 })], 'main').some(w => w.level === 'error'));
  assert.ok(run([ev(0, ';Cnope', { id: 1 })], 'main').some(w => /No sequence "nope"/.test(w.text)));
});

test('analyze: the delimiter, a delay or an IF as a command, value verbs in a timed chain, length', () => {
  const run = (events) => SB.sbAnalyze({ events, markers: [] }, { value: compile(events) }).filter(w => w.level === 'error');
  assert.strictEqual(run([ev(0, ';S1a^b', { id: 1 })]).length, 1);
  assert.strictEqual(run([ev(0, ';T500', { id: 1 })]).length, 1);
  assert.strictEqual(run([ev(0, 'IF,a=1', { id: 1 })]).length, 1);
  assert.strictEqual(run([ev(0, '?SEQ,SAVE,x,y', { id: 1 }), ev(500, ';M11', { id: 2 })]).length, 1);
  assert.strictEqual(run([ev(0, ';S1' + 'x'.repeat(4000), { id: 1 })]).length, 1);
  assert.strictEqual(run([ev(0, ';M11', { id: 1 }), ev(500, ';M12', { id: 2 })]).length, 0);
});

test('analyze: past 2,400 characters the heap warning is only for a board that does not take the whole 3,999', () => {
  // A classic ESP32 stopped near 2,400 on firmware before 2026-10-24 (about 7x a save's length in heap); the firmware
  // that saves from the queued line takes 3,999 on both chips and says so with mgmt.rx24 (app.js boardTakesLongSave).
  const events = [ev(0, ';S1' + 'x'.repeat(2600), { id: 1 })];
  const warns = (longSaveOk) => SB.sbAnalyze({ events, markers: [] }, { value: compile(events), longSaveOk })
    .filter(w => w.level === 'warn' && /run out of memory/.test(w.text));
  assert.strictEqual(warns(false).length, 1, 'a board that does not say it takes 3,999');
  assert.strictEqual(warns(true).length, 0, 'a board that does');
});

test('library: the vendored DroidNet snapshot and the WCB boards load, normalized like NaviCore\'s', () => {
  const ids = LIB.boards.map(b => b.id);
  assert.ok(ids.length >= 24, `${ids.length} boards`);
  assert.strictEqual(ids[0], 'wcb-sequences');                                  // the WCB group leads
  assert.strictEqual(LIB.boards.find(b => b.id === 'wcb-native').hidden, true);   // setup, not actions
  assert.strictEqual(LIB.boards.find(b => b.id === 'maestro').hidden, true);      // superseded by wcb-maestro
  assert.strictEqual(LIB.boards.find(b => b.id === 'maestro-native').hidden, true);
  assert.ok(!LIB.boards.find(b => b.id === 'wcb-native').commands.some(c => c.id === 'wcb.runSeq'));   // moved
});

test('library: decoding picks the most specific command, over a hidden board', () => {
  assert.deepStrictEqual(decode(';A,PLAY,5'), { board: 'wcb-mp3', cmd: 'mp3.play', values: { track: '5' }, route: { wcb: 0, port: null, alias: false } });
  assert.strictEqual(decode(';A,PLAY,5,ONFIN,done').cmd, 'mp3.playCb');
  assert.strictEqual(decode(';V,armed,TOGGLE').cmd, 'wcb.varToggle');
  assert.strictEqual(decode(';V,mode,2').cmd, 'wcb.varSet');
  assert.strictEqual(decode(';M11').board, 'wcb-maestro');
  assert.deepStrictEqual(decode(';M1,setTarget,0,6000').values, { maestroId: '1', channel: '0', position: '6000' });
  assert.strictEqual(decode(';Cwave').cmd, 'wcb.runSeq');
  assert.strictEqual(decode(';P11500').cmd, 'wcb.pwmPulse');
  assert.strictEqual(decode(';D,PLAY,3,ONFIN,done').cmd, 'dfp.playCb');
  assert.strictEqual(decode('?ETM,ON').board, 'wcb-native');                      // hidden boards still decode
  const flthy = decode(';W2,;S3A001');
  assert.strictEqual(flthy.cmd, 'flthy.led.leia');
  assert.deepStrictEqual(flthy.route, { wcb: 2, port: 3, alias: false });
  assert.strictEqual(decode(';S3A001|30').values.__dur, '30');                   // FlthyHPs duration suffix
  assert.strictEqual(decode(';S3:PP100').cmd, 'uppity.lifter.seek');              // the Periscope lifter's own verb
  assert.strictEqual(decode(';S3hello there').cmd, undefined);                     // free serial text: no library command
});

test('library: encode is decode\'s inverse for every visible command\'s defaults', () => {
  for (const board of LIB.boards) {
    if (board.hidden) continue;
    for (const cmd of board.commands) {
      if (!cmd.template || SB.SB_HIDDEN_CMDS.has(cmd.id)) continue;
      const values = {};
      for (const p of cmd.params || []) {
        let v = p.default !== undefined ? String(p.default) : '';
        if (v === '' && p.enum) v = String(LIB.enums[p.enum].values[0].code);
        if (v === '') v = p.type === 'int' ? String(p.min != null ? p.min : 1) : 'abc';
        values[p.name] = v;
      }
      const wire = SB.sbEncode(cmd, values);
      const d = SB.sbDecodeBody(LIB, wire);
      if (!d) continue;   // free text alone (serial text) never decodes
      assert.strictEqual(SB.sbEncode(d.cmd, d.values), wire, `${board.id}/${cmd.id}: ${wire}`);
    }
  }
});

test('lanes: by the device each command reaches', () => {
  const ctx = { chars: {}, self: 1, boardName: (w) => 'WCB ' + w, portLabel: (w, p) => (w === 2 && p === 3 ? 'Flthy HP Controller' : '') };
  const lane = (c) => SB.sbLaneOf(c, ctx);
  assert.strictEqual(lane(';M11').label, 'Maestro 1');
  assert.strictEqual(lane(';M01').label, 'All Maestros');
  assert.strictEqual(lane(';A,PLAY,1').fam, 'sound');
  assert.strictEqual(lane(';L3,PS,1').label, 'WLED 3');
  assert.strictEqual(lane(';W2,;S3A001').label, 'WCB 2 · S3 · Flthy HP Controller');
  assert.strictEqual(lane(';S3A001').label, 'This board · S3');
  assert.strictEqual(lane(';W2,;A,PLAY,1').label, 'WCB 2 · MP3 Trigger');
  assert.strictEqual(lane(';SEQwave').fam, 'seq');
  assert.strictEqual(lane('?ETM,ON').fam, 'board');
  assert.strictEqual(lane(':PP100').label, 'Broadcast to every port');
});

test('lanes: a device string the library knows gets its device\'s lane, so two devices on one port are two lanes', () => {
  const ctx = { chars: {}, self: 1, boardName: (w) => 'WCB ' + w, portLabel: () => '' };
  const flthy = SB.sbLaneOf(';S1F001', ctx, { id: 'flthy-hps', name: 'FlthyHPs Holoprojectors' });
  const dome = SB.sbLaneOf(';S1:DPA180', ctx, { id: 'roam-a-dome', name: 'Roam-A-Dome' });
  assert.deepStrictEqual([flthy.key, flthy.name, flthy.sub], ['D:flthy-hps:W1S1', 'FlthyHPs Holoprojectors', 'This board · S1']);
  assert.notStrictEqual(flthy.key, dome.key);
  const hcr = SB.sbLaneOf(';W2,;S4<SE>', ctx, { id: 'hcr-native', name: 'HCR Vocalizer (native <…>)' });
  assert.deepStrictEqual([hcr.key, hcr.name, hcr.sub], ['D:hcr-native:W2S4', 'HCR Vocalizer', 'WCB 2 · S4']);
  assert.strictEqual(SB.sbLaneOf(';S0@AMonsterMash', ctx, { id: 'x', name: 'Maestro via DroidNet' }).sub, 'This board · S0 (USB)');
  assert.strictEqual(SB.sbLaneOf(';S1F001', ctx).key, 'S:1:1');   // no device known: the port's lane
});

test('library: DroidNet\'s Maestro trigger is @A<name> out Serial 0 (USB)', () => {
  const d = decode(';S0@AMonsterMash');
  assert.deepStrictEqual([d.board, d.cmd, d.values, d.route], ['wcb-maestro', 'wcbmaestro.droidnet', { name: 'MonsterMash' }, { wcb: 0, port: 0, alias: false }]);
  assert.strictEqual(decode(';W2,;S0@AMonsterMash').cmd, 'wcbmaestro.droidnet');
  assert.deepStrictEqual(decode(';S0@aMonsterMash').values, { name: 'MonsterMash' });   // either case of @A
});

// The Wizard presents the library NaviCore's config tool does: its two folders are copies of NaviCore's, byte for
// byte (the WCB boards are kept in NaviCore, beside the DroidNet snapshot). Skipped where NaviCore is not checked out
// beside this repo (CI).
test('library: Wizard/cmdlib is the same as NaviCore\'s config_tool/cmdlib (droidnet and wcb)', (t) => {
  const nc = path.join(WIZ, '..', '..', 'NaviCore', 'config_tool', 'cmdlib');
  if (!fs.existsSync(nc)) { t.skip('NaviCore is not beside this repo'); return; }
  const files = (dir) => fs.readdirSync(dir, { recursive: true }).filter((f) => fs.statSync(path.join(dir, f)).isFile())
    .map((f) => f.split(path.sep).join('/')).sort();
  for (const vendor of ['droidnet', 'wcb']) {
    const a = path.join(WIZ, 'cmdlib', vendor), b = path.join(nc, vendor);
    assert.deepStrictEqual(files(a), files(b), `${vendor}: the two folders list different files`);
    for (const f of files(a)) {
      const ab = fs.readFileSync(path.join(a, f), 'utf8').replace(/\r\n/g, '\n');
      const bb = fs.readFileSync(path.join(b, f), 'utf8').replace(/\r\n/g, '\n');
      assert.ok(ab === bb, `${vendor}/${f} differs from NaviCore's: copy NaviCore's config_tool/cmdlib/${vendor} over Wizard/cmdlib/${vendor}`);
    }
  }
});

// A device wears one colour in both tools: NaviCore's timeline lanes (config_tool/index.html, --tl-c1..8 and
// TL_BOARD_SLOT, NaviCore docs/CONFIG_TOOL.md §7) use this builder's eight colours in its order, in both themes, and
// give every board this builder knows the same home slot (NaviCore adds its own boards). Skipped where NaviCore is not
// beside this repo (CI).
test('colour: NaviCore\'s timeline lanes use the builder\'s palette and home slots', (t) => {
  const ncHtml = path.join(WIZ, '..', '..', 'NaviCore', 'config_tool', 'index.html');
  if (!fs.existsSync(ncHtml)) { t.skip('NaviCore is not beside this repo'); return; }
  const html = fs.readFileSync(ncHtml, 'utf8');
  const css = fs.readFileSync(path.join(WIZ, 'seq-builder.css'), 'utf8');
  // The colour tokens a rule block defines: { 'c1': '#..', ..., 'cx': '#..' }.
  const tokens = (text, selector, prefix) => {
    const at = text.indexOf(selector + ' {');
    assert.ok(at >= 0, `${selector} not found`);
    const block = text.slice(at, text.indexOf('}', at));
    const out = {};
    for (const m of block.matchAll(new RegExp(`--${prefix}-(c\\d|cx):\\s*(#[0-9a-fA-F]{6})`, 'g'))) out[m[1]] = m[2].toLowerCase();
    assert.strictEqual(Object.keys(out).length, 9, `${selector}: expected --${prefix}-c1..8 and -cx`);
    return out;
  };
  // NaviCore's dark tokens are the :root block that defines --tl-c1 (its file has other :root blocks).
  const ncDarkAt = html.indexOf('--tl-c1:');
  assert.ok(ncDarkAt >= 0, 'NaviCore: --tl-c1 not found');
  const ncDark = tokens(html.slice(html.lastIndexOf(':root', ncDarkAt)), ':root', 'tl');
  const ncLight = tokens(html.slice(html.indexOf('body.theme-light {', ncDarkAt)), 'body.theme-light', 'tl');
  assert.deepStrictEqual(ncDark, tokens(css, ':root', 'sb'), 'dark theme colours differ');
  assert.deepStrictEqual(ncLight, tokens(css, ':root.light', 'sb'), 'light theme colours differ');
  const table = (text, name) => {
    const m = new RegExp(`const ${name} = (\\{[\\s\\S]*?\\n\\});`).exec(text);
    assert.ok(m, `${name} not found`);
    return vm.runInNewContext('(' + m[1] + ')');
  };
  const sb = table(fs.readFileSync(path.join(WIZ, 'seq-builder.js'), 'utf8'), 'SB_BOARD_SLOT');
  const tl = table(html, 'TL_BOARD_SLOT');
  for (const [id, slot] of Object.entries(sb)) assert.strictEqual(tl[id], slot, `${id}: home slot ${tl[id]} in NaviCore, ${slot} here`);
});
