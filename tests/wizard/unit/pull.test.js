// Wizard/parser.js: the remote config pull pieces (F13) - crc32, the line splitter both serial transports use, the
// part / error / reply parsers and the pull collector remoteBoardPull feeds every relay line to. No browser, no board
// (`npm run unit`, wizard.parser in the harness, CI). remoteBoardPull itself runs against a fake relay in
// specs/remote_pull_fake.spec.js.
//
// The wire forms (docs in parser.js above crc32):
//   [MGMT:CONFIG,<n>]<reply>                  a whole reply of 2912 characters or less - what every firmware sends
//   [MGMT:CFGPART,<n>]P<id>,<k>,<K>:<data>~   part k of K of a longer reply
//   [MGMT:CFGERR,<n>]<CODE>,<detail>          the target could not send it (the relay strips the leading 'E')
// <reply> = [VER:<fw>] + chain + ^?CHK<8 hex>, the CRC-32 of the chain. The target cuts parts on a grid of D bytes and
// backs each cut off a UTF-8 continuation byte (at most 3 steps), so every part decodes whole.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const PARSER = path.join(__dirname, '..', '..', '..', 'Wizard', 'parser.js');
const P = require(PARSER);

const hex8 = (n) => n.toString(16).toUpperCase().padStart(8, '0');
const utf8 = (s) => Buffer.from(s, 'utf8');

// A reply exactly as the config pull (configPullWalk, WCB.ino) makes it.
const reply = (chain, ver = '6.2.1_TEST') => `[VER:${ver}]${chain}^?CHK${hex8(P.crc32(chain))}`;

// The relay lines for a reply sent in parts, split the way the target does: split_k = back(k*D), where back(x) steps
// x down while the byte at x is a UTF-8 continuation byte, at most 3 steps (none found: x unchanged).
function partLines(text, src, { D = 64, id = 'A1B2' } = {}) {
  const b = utf8(text);
  const K = Math.ceil(b.length / D);
  const cuts = [0];
  for (let k = 1; k < K; k++) {
    let x = k * D;
    for (let i = 0; i < 3 && (b[x] & 0xC0) === 0x80; i++) x--;
    if ((b[x] & 0xC0) === 0x80) x = k * D;
    cuts.push(x);
  }
  cuts.push(b.length);
  const lines = [];
  for (let k = 1; k <= K; k++) {
    lines.push(`[MGMT:CFGPART,${src}]P${id},${k},${K}:${b.subarray(cuts[k - 1], cuts[k]).toString('utf8')}~`);
  }
  return lines;
}

// A chain with 2-, 3- and 4-byte characters, a value with a run of spaces, and an earlier "^?CHK" inside a sequence
// value (legal: WCB.ino finds the real checksum with lastIndexOf). The EPASS here is a dummy, not any board's.
const CHAIN = [
  '?HW,32', '?WCB,2', '?ALIAS,Dôme€', '?WCBQ,3', '?EPASS,dummy_not_a_secret',
  '?LABEL,S1,Périscope €', '?LABEL,S2,Clef 𝄞 G', '?LABEL,S3,Teeces', '?BAUD,S1,57600',
  '?SEQ,SAVE,spaced,a      b', '?SEQ,SAVE,chk,;w2 keep ^?CHK00000000 literal', '?SEQ,SAVE,Leia,;w2;s3test',
].join('^');

function collect(want, lines) {
  const c = P.createPullCollector(want);
  return lines.map((l) => c.feed(l));
}

// ── API ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

test('parser.js exports ONE api: the Node and browser objects are the same object with the same keys', () => {
  // Load the file as the page does, but with both a window and a module in scope: two independent ifs must fill both.
  const sandbox = { window: {}, module: { exports: {} }, TextEncoder, TextDecoder, console };
  vm.runInNewContext(fs.readFileSync(PARSER, 'utf8'), sandbox, { filename: 'parser.js' });
  assert.strictEqual(sandbox.window.WCBParser, sandbox.module.exports);
  assert.deepEqual(Object.keys(sandbox.window.WCBParser).sort(), Object.keys(P).sort());
  for (const name of ['crc32', 'makeLineSplitter', 'parseConfigPart', 'parseConfigError', 'verifyConfigReply',
                      'createPullCollector', 'parseBackupString', 'buildCommandString']) {
    assert.equal(typeof P[name], 'function', name);
  }
});

// ── crc32 ────────────────────────────────────────────────────────────────────────────────────────────────────────────

test('crc32 is the firmware / zlib CRC-32, over UTF-8 bytes', () => {
  assert.equal(P.crc32('123456789'), 0xCBF43926);                // the CRC-32 check value
  assert.equal(P.crc32(''), 0);
  assert.equal(P.crc32(new Uint8Array(0)), 0);
  assert.equal(P.crc32('Dôme €𝄞'), 0x38D63D51);                  // zlib.crc32('Dôme €𝄞'.encode()) in Python
  assert.equal(P.crc32(utf8('Dôme €𝄞')), P.crc32('Dôme €𝄞'));   // bytes and string agree
  assert.ok(P.crc32('\u00e9') !== P.crc32('\u00c3\u00a9'));        // not the Latin-1 bytes of the UTF-8 encoding
});

// ── makeLineSplitter ─────────────────────────────────────────────────────────────────────────────────────────────────

const SPLIT_TEXT = 'first é line\r\n\r\n  €uro 𝄞 clef  \r\n[MGMT:CFGPART,2]P0001,1,2:ab  ~\r\nlast ßß\n';
const SPLIT_WANT = ['first é line', '€uro 𝄞 clef', '[MGMT:CFGPART,2]P0001,1,2:ab  ~', 'last ßß'];

function splitAll(chunks) {
  const out = [];
  const feed = P.makeLineSplitter((l) => out.push(l));
  for (const c of chunks) feed(c);
  return out;
}

test('makeLineSplitter: CRLF and LF, blank lines dropped, lines trimmed, a trailing partial line held back', () => {
  assert.deepEqual(splitAll([utf8(SPLIT_TEXT)]), SPLIT_WANT);
  assert.deepEqual(splitAll([utf8('no newline yet')]), []);
  assert.deepEqual(splitAll([utf8('held'), utf8(' back\n')]), ['held back']);
});

test('makeLineSplitter: a read boundary at EVERY byte offset, inside 2-, 3- and 4-byte characters, loses nothing', () => {
  const b = utf8(SPLIT_TEXT);
  for (let i = 0; i <= b.length; i++) {
    assert.deepEqual(splitAll([b.subarray(0, i), b.subarray(i)]), SPLIT_WANT, `cut at byte ${i}`);
  }
  // Two cuts, every pair: a 4-byte character can arrive in three reads.
  for (let i = 0; i <= b.length; i++) {
    for (let j = i; j <= b.length; j++) {
      const got = splitAll([b.subarray(0, i), b.subarray(i, j), b.subarray(j)]);
      assert.deepEqual(got, SPLIT_WANT, `cuts at ${i}, ${j}`);
    }
  }
  // One byte per read, the worst Intellex forwards ('in_waiting or 1').
  assert.deepEqual(splitAll([...b].map((x) => Uint8Array.of(x))), SPLIT_WANT);
  // The per-read decoder this replaces turned a character cut like this into U+FFFD; the splitter does not.
  const cut = 7;   // inside the 2-byte character of 'first \u00e9 line'
  const perRead = new TextDecoder().decode(b.subarray(0, cut)) + new TextDecoder().decode(b.subarray(cut));
  assert.ok(perRead.includes('\uFFFD'));
  assert.ok(!splitAll([b.subarray(0, cut), b.subarray(cut)]).join('').includes('\uFFFD'));
});

test('makeLineSplitter: two splitters keep separate state, and a throwing onLine never gets the same line twice', () => {
  const a = [], c = [];
  const fa = P.makeLineSplitter((l) => a.push(l));
  const fc = P.makeLineSplitter((l) => c.push(l));
  const e = utf8('€\n');
  fa(e.subarray(0, 1)); fc(utf8('x\n')); fa(e.subarray(1));
  assert.deepEqual(a, ['€']);
  assert.deepEqual(c, ['x']);

  const seen = [];
  const feed = P.makeLineSplitter((l) => { seen.push(l); if (l === 'boom') throw new Error('listener bug'); });
  assert.throws(() => feed(utf8('one\nboom\ntwo\n')), /listener bug/);
  feed(utf8('three\n'));
  // 'boom' once; 'two' held until the next read, not lost and not delivered twice.
  assert.deepEqual(seen, ['one', 'boom', 'two', 'three']);
});

// ── parseConfigPart ──────────────────────────────────────────────────────────────────────────────────────────────────

test('parseConfigPart: strict framing, trailing spaces kept by the ~', () => {
  assert.deepEqual(P.parseConfigPart('P1A2B,1,3:?HW,32^?WCB,2~'), { id: '1A2B', k: 1, K: 3, data: '?HW,32^?WCB,2' });
  assert.deepEqual(P.parseConfigPart('P00FF,3,3:tail   ~'), { id: '00FF', k: 3, K: 3, data: 'tail   ' });
  assert.deepEqual(P.parseConfigPart('PABCD,2,2:a~b~'), { id: 'ABCD', k: 2, K: 2, data: 'a~b' });   // ~ inside data
  assert.deepEqual(P.parseConfigPart('PABCD,10,16:x~'), { id: 'ABCD', k: 10, K: 16, data: 'x' });
  for (const bad of [
    'P1A2B,1,3:no tilde',                       // cut short, or a print landed on it
    'P1A2B,1,3:data~[ETM] WCB3 came ONLINE',    // another task's print after the ~
    'P1a2b,1,3:x~',                             // lower-case id
    'P1A2,1,3:x~',                              // 3-digit id
    'P1A2B,0,3:x~',                             // k is 1-based
    'P1A2B,4,3:x~',                             // k > K
    'P1A2B,01,3:x~',                            // no leading zeros
    'P1A2B,1,3:~',                              // no data
    ' P1A2B,1,3:x~',
    'E1A2B,1,3:x~', '', null, undefined, 42,
  ]) {
    assert.equal(P.parseConfigPart(bad), null, JSON.stringify(bad));
  }
});

// ── parseConfigError ─────────────────────────────────────────────────────────────────────────────────────────────────

test('parseConfigError: NOMEM, CHANGED and NOPARTS retry, TOOBIG / unknown codes do not', () => {
  const nomem = P.parseConfigError('NOMEM,need 3001 free 2048 largest 1536');
  assert.equal(nomem.code, 'NOMEM');
  assert.equal(nomem.detail, 'need 3001 free 2048 largest 1536');
  assert.equal(nomem.retryable, true);
  assert.match(nomem.reason, /out of memory.*need 3001/);
  assert.equal(P.parseConfigError('CHANGED,restarted 2x').retryable, true);
  const noparts = P.parseConfigError('NOPARTS,L=4100 update the Wizard');
  assert.equal(noparts.retryable, true);
  assert.match(noparts.reason, /L=4100 update the Wizard/);
  assert.equal(P.parseConfigError('TOOBIG,L=50000 max 46080').retryable, false);
  const unknown = P.parseConfigError('SOMETHINGNEW,why');
  assert.deepEqual([unknown.code, unknown.retryable], ['SOMETHINGNEW', false]);
  assert.match(unknown.reason, /SOMETHINGNEW.*why/);
  assert.deepEqual([P.parseConfigError('NOMEM').code, P.parseConfigError('NOMEM').detail], ['NOMEM', '']);
  const junk = P.parseConfigError('');
  assert.deepEqual([junk.code, junk.retryable], ['UNKNOWN', false]);
  assert.equal(P.parseConfigError('toString').code, 'UNKNOWN');       // lower case: not a code at all
  assert.equal(P.parseConfigError('CONSTRUCTOR').retryable, false);    // no prototype lookups
});

// ── verifyConfigReply ────────────────────────────────────────────────────────────────────────────────────────────────

test('verifyConfigReply: [VER:] head, ^?CHK tail taken from the END, CRC over the chain between them', () => {
  const r = reply(CHAIN);
  assert.deepEqual(P.verifyConfigReply(r), { ok: true, reason: '', ver: '6.2.1_TEST' });
  assert.equal(P.verifyConfigReply(r.replace(/CHK(\w{8})$/, (m, h) => 'CHK' + h.toLowerCase())).ok, true);
  assert.equal(P.verifyConfigReply(r.replace('Teeces', 'Teeces ')).ok, false);          // one byte more
  assert.match(P.verifyConfigReply(r.replace('Périscope', 'Periscope')).reason, /checksum/);
  assert.equal(P.verifyConfigReply(r.slice(0, -1)).ok, false);                           // tail cut short
  assert.equal(P.verifyConfigReply(r.slice(r.indexOf(']') + 1)).ok, false);              // no head
  assert.equal(P.verifyConfigReply(CHAIN).ok, false);
  assert.equal(P.verifyConfigReply(undefined).ok, false);
});

// ── createPullCollector ──────────────────────────────────────────────────────────────────────────────────────────────

test('collector: a legacy line is taken as it always was; an empty one is its own outcome', () => {
  const [r] = collect(2, [`[MGMT:CONFIG,2]${reply(CHAIN)}  `]);
  assert.deepEqual(r, { kind: 'legacy', src: 2, body: reply(CHAIN) });
  assert.equal(collect(2, ['[MGMT:CONFIG,2]'])[0].kind, 'empty');
  assert.equal(collect(2, ['[MGMT:CONFIG,2]   '])[0].kind, 'empty');
  // Both orders a new target can produce for an out-of-memory legacy build: CFGERR then the empty reply, or (an old
  // relay, which drops the CFGERR) the empty reply alone.
  assert.deepEqual(collect(2, ['[MGMT:CFGERR,2]NOMEM,x', '[MGMT:CONFIG,2]']).map((x) => x.kind), ['error', 'empty']);
});

test('collector: the tag is matched strictly and other boards are ignored', () => {
  for (const line of [
    `[MGMT:CONFIG,3]${reply(CHAIN)}`,                // another board
    `[MGMT:CONFIG,2,1/3]${reply(CHAIN)}`,            // parseInt used to read this as board 2
    `[MGMT:CONFIG,X]${reply(CHAIN)}`,                // ... and this (NaN) as every board
    `[MGMT:CONFIGPART,2]P0001,1,1:x~`,
    `[MGMT:STATS,2]x`, `[MGMT:SEQ,2]x`, `[TERM:2][MGMT:CONFIG,2]x`, `x[MGMT:CONFIG,2]x`,
    `[MGMT:CFGPART,3]P0001,1,1:${reply(CHAIN)}~`,
    `[MGMT:CFGERR,3]TOOBIG,x`,
  ]) {
    assert.equal(collect(2, [line])[0].kind, 'ignored', line.slice(0, 40));
  }
  assert.equal(collect('2', [`[MGMT:CONFIG,2]${reply(CHAIN)}`])[0].kind, 'legacy');   // a slot string still works
});

test('collector: 2 parts in order join to the reply, CRC-checked', () => {
  const text = reply(CHAIN);
  const lines = partLines(text, 2, { D: Math.ceil(utf8(text).length / 2) });
  assert.equal(lines.length, 2);
  const out = collect(2, lines);
  assert.deepEqual(out[0], { kind: 'partial', src: 2, id: 'A1B2', k: 1, K: 2, have: 1, progress: true });
  assert.equal(out[1].kind, 'complete');
  assert.equal(out[1].text, text);
  assert.equal(out[1].ver, '6.2.1_TEST');
});

test('collector: 3+ parts out of order, with other lines and another board\'s parts in between', () => {
  const text = reply(CHAIN);
  const mine = partLines(text, 2, { D: 60, id: 'BEEF' });
  const theirs = partLines(reply('?WCB,3^?ALIAS,Other'), 3, { D: 16, id: 'BEEF' });   // same id, other board
  assert.ok(mine.length >= 3, `${mine.length} parts`);
  const order = mine.map((_, i) => i).reverse();
  const lines = [];
  order.forEach((i, n) => {
    lines.push(mine[i], '[ETM] WCB3 came ONLINE (boot)', theirs[n % theirs.length], '[MGMT:STATS,2]noise');
  });
  const out = collect(2, lines).filter((r) => r.kind !== 'ignored');
  assert.equal(out.length, mine.length);
  assert.ok(out.slice(0, -1).every((r) => r.kind === 'partial' && r.progress));
  assert.equal(out.at(-1).kind, 'complete');
  assert.equal(out.at(-1).text, text);
});

test('collector: a duplicate part is not progress, a missing part never completes', () => {
  const lines = partLines(reply(CHAIN), 2, { D: 100 });
  const out = collect(2, [lines[0], lines[0], ...lines.slice(2)]);
  assert.equal(out[1].progress, false);
  assert.equal(out[1].have, 1);
  assert.ok(out.every((r) => r.kind === 'partial'), 'part 2 is missing: nothing may complete');
});

test('collector: a part of another id starts over - two builds are never joined', () => {
  const a = partLines(reply(CHAIN), 2, { D: 100, id: 'AAAA' });
  const bText = reply(CHAIN.replace('Teeces', 'Teeces2'));
  const b = partLines(bText, 2, { D: 100, id: 'BBBB' });
  assert.ok(a.length >= 3 && b.length === a.length, `${a.length} / ${b.length} parts`);
  const out = collect(2, [a[0], a[1], b[0], ...b.slice(1)]);
  assert.deepEqual([out[2].id, out[2].have], ['BBBB', 1], 'the new id restarted the collection');
  assert.equal(out.at(-1).kind, 'complete');
  assert.equal(out.at(-1).text, bText);
  // Any id change restarts, back to an old one included: a's parts 1..2 and b's 2.. never make one reply.
  const mixed = collect(2, [a[0], a[1], b[0], a[2], ...b.slice(1)]);
  assert.deepEqual([mixed[3].id, mixed[3].have], ['AAAA', 1]);
  assert.ok(mixed.every((r) => r.kind === 'partial'));
});

test('collector: trailing spaces at a part boundary survive the line trim', () => {
  // Put a run of spaces exactly at the end of part 1: the relay line ends "...   ~", and trim() stops at the ~.
  const pre = `[VER:T]?SEQ,SAVE,a,`;
  const D = 48;
  const chain = `?SEQ,SAVE,a,${'x'.repeat(D - pre.length - 4)}    ${'y'.repeat(30)}`;
  const text = `[VER:T]${chain}^?CHK${hex8(P.crc32(chain))}`;
  const lines = partLines(text, 2, { D });
  assert.match(lines[0], / {4}~$/);
  // Through the real splitter, CRLF and all, as the relay prints them.
  const got = [];
  const feed = P.makeLineSplitter((l) => got.push(l));
  feed(utf8(lines.map((l) => `${l}\r\n`).join('')));
  const out = collect(2, got);
  assert.equal(out.at(-1).kind, 'complete');
  assert.equal(out.at(-1).text, text);
});

test('collector: "^?" inside the data, split across a part boundary, joins back', () => {
  const D = 40;
  const pre = '[VER:T]?SEQ,SAVE,a,';
  const chain = `?SEQ,SAVE,a,${'x'.repeat(D - 1 - pre.length)}^?SEQ,SAVE,b,${'z'.repeat(50)}`;
  const text = `[VER:T]${chain}^?CHK${hex8(P.crc32(chain))}`;
  assert.equal(utf8(text)[D - 1], 0x5E);   // '^' ends part 1, '?' starts part 2
  const lines = partLines(text, 2, { D });
  assert.ok(lines[0].endsWith('^~') && lines[1].includes(':?SEQ,SAVE,b,'));
  const out = collect(2, lines);
  assert.equal(out.at(-1).kind, 'complete');
  assert.equal(out.at(-1).text, text);
});

test('collector: multi-byte characters on part boundaries (the target backs each cut off a continuation byte)', () => {
  const text = reply(CHAIN);
  for (let D = 20; D <= 90; D++) {
    const out = collect(2, partLines(text, 2, { D }));
    assert.equal(out.at(-1).kind, 'complete', `D=${D}`);
    assert.equal(out.at(-1).text, text, `D=${D}`);
  }
});

test('collector: a join that fails its CRC is crcFail (retry), and the next id starts clean', () => {
  const text = reply(CHAIN);
  const good = partLines(text, 2, { D: 100, id: 'C0DE' });
  const bad = partLines(text.replace('Teeces', 'TeeceZ'), 2, { D: 100, id: 'BAD0' });
  const c = P.createPullCollector(2);
  const r1 = bad.map((l) => c.feed(l)).at(-1);
  assert.equal(r1.kind, 'crcFail');
  assert.match(r1.reason, /checksum/);
  const r2 = good.map((l) => c.feed(l)).at(-1);
  assert.equal(r2.kind, 'complete');
});

test('collector: U+FFFD in a join that fails its CRC is NOTUTF8 with its job id, retryable (one join cannot tell why)', () => {
  // Stored that way: the target sums raw bytes; a stored 0xE9 (Latin-1 é) decodes to U+FFFD and re-encodes as
  // EF BF BD, so no job ever verifies.
  const rawChain = Buffer.concat([utf8('?WCB,2^?LABEL,S1,Caf'), Buffer.from([0xE9]), utf8('^?BAUD,S1,9600')]);
  const decoded = `[VER:T]${rawChain.toString('utf8')}^?CHK${hex8(P.crc32(rawChain))}`;
  assert.ok(decoded.includes('\uFFFD'));
  const stored = collect(2, partLines(decoded, 2, { D: 16, id: '5707' })).at(-1);
  assert.deepEqual([stored.kind, stored.code, stored.retryable, stored.id], ['error', 'NOTUTF8', true, '5707']);
  // Lost on the way: a reply that is fine on the target, one byte of its 'é' (C3 A9) dropped in transport. The same
  // U+FFFD, and the next job would verify - which is why the first NOTUTF8 of a pull is retried (app.js _pullNotUtf8).
  const good = utf8(reply('?WCB,2^?LABEL,S1,Café^?BAUD,S1,9600'));
  const at = good.indexOf(0xA9);
  const lost = Buffer.concat([good.subarray(0, at), good.subarray(at + 1)]).toString('utf8');
  assert.ok(lost.includes('\uFFFD'));
  const r = collect(2, partLines(lost, 2, { D: 16, id: '1057' })).at(-1);
  assert.deepEqual([r.kind, r.code, r.retryable, r.id], ['error', 'NOTUTF8', true, '1057']);
  assert.equal(collect(2, partLines(reply('?WCB,2^?LABEL,S1,Café^?BAUD,S1,9600'), 2, { D: 16 })).at(-1).kind, 'complete');
});

test('collector: every CFGERR code comes through with its retry verdict', () => {
  const kinds = collect(2, ['[MGMT:CFGERR,2]NOMEM,need 1', '[MGMT:CFGERR,2]CHANGED,x', '[MGMT:CFGERR,2]NOPARTS,L=4000',
                            '[MGMT:CFGERR,2]TOOBIG,L=60000', '[MGMT:CFGERR,2]WHAT,x']);
  assert.deepEqual(kinds.map((r) => [r.kind, r.code, r.retryable]), [
    ['error', 'NOMEM', true], ['error', 'CHANGED', true], ['error', 'NOPARTS', true],
    ['error', 'TOOBIG', false], ['error', 'WHAT', false],
  ]);
});
