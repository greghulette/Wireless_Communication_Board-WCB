// Wizard/parser.js: a relay push planned into ?MGMT,FRAG sessions (planRelayPush, utf8Chunks; docs/MGMT_RELAY.md §10).
// No browser, no board (`npm run unit`, CI). boardGoRemote and updateSequence send what this plans; the no-board specs
// (specs/push_fake.spec.js wizard.push_fake_relay_sessions) drive them through a fake relay.
//
// The limits it plans against: a chunk of at most 179 BYTES (the relay copies it into a 180-byte field and refuses a
// longer one), at most 16 chunks a session (a released target reassembles 16), at most 150 commands a session (the
// target queues a session's commands at once from its WiFi task, into a 200-slot queue that task never waits on).
const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const P = require(path.join(__dirname, '..', '..', '..', 'Wizard', 'parser.js'));
const bytes = (s) => Buffer.byteLength(s, 'utf8');

test('utf8Chunks: at most 179 bytes a chunk, never inside a character, and the chunks join back', () => {
  const pieces = ['a', 'é', '€', '😀', ';S1', '^', ','];
  for (let n = 0; n < 300; n++) {
    let s = '';
    const len = (n * 37) % 1500;
    for (let i = 0; i < len; i++) s += pieces[(i * 7 + n) % pieces.length];
    const chunks = P.utf8Chunks(s, 179);
    assert.equal(chunks.join(''), s);
    for (const c of chunks) {
      assert.ok(bytes(c) <= 179, `a chunk of ${bytes(c)} bytes`);
      assert.ok(c.length > 0);
      assert.ok(!/^[\uDC00-\uDFFF]/.test(c) && !/[\uD800-\uDBFF]$/.test(c), 'a surrogate pair was split');
    }
  }
  // ASCII cuts exactly every 179 characters, as fragmentString always did.
  const ascii = 'x'.repeat(179 * 3 + 5);
  assert.deepEqual(P.utf8Chunks(ascii, 179).map((c) => c.length), [179, 179, 179, 5]);
  // 179 characters with one 'é' are 180 bytes: two chunks (one was refused by the relay).
  assert.equal(P.utf8Chunks('é' + 'x'.repeat(178), 179).length, 2);
  assert.deepEqual(P.utf8Chunks('', 179), []);
});

test('utf8Chunks: no chunk but the last ends in whitespace (the relay trims the end of every line it reads)', () => {
  // A cut just after a space: the space opens the next chunk, which sits in the middle of its relay line.
  const s = 'x'.repeat(178) + ' ' + 'y'.repeat(20);
  const c = P.utf8Chunks(s, 179);
  assert.deepEqual(c.map((x) => x.length), [178, 21]);
  assert.equal(c[1][0], ' ');
  assert.equal(c.join(''), s);
  for (let n = 0; n < 200; n++) {
    let t = '';
    for (let i = 0; i < 600 + n; i++) t += (i * 13 + n) % 5 === 0 ? ' ' : (i % 7 === 0 ? 'é' : 'a');
    const chunks = P.utf8Chunks(t, 179);
    assert.equal(chunks.join(''), t);
    for (const x of chunks.slice(0, -1)) assert.ok(!/\s$/.test(x), `a chunk ends in whitespace: ${JSON.stringify(x.slice(-5))}`);
    for (const x of chunks) assert.ok(bytes(x) <= 179 && x.length > 0);
  }
  // Nothing but whitespace cannot be helped: it is cut as it is, never lost.
  assert.equal(P.utf8Chunks(' '.repeat(400), 179).join(''), ' '.repeat(400));
});

test('capTagChunks / relayLongChunks: the long session needs the target\'s mgmt.rx and the relay\'s mgmt.fwd (WDP CAPTAGS)', () => {
  assert.equal(P.capTagChunks('mgmt.fwd24 mgmt.rx24', 'mgmt.rx'), 24);
  assert.equal(P.capTagChunks('mgmt.fwd24 mgmt.rx24', 'mgmt.fwd'), 24);
  assert.equal(P.capTagChunks('  MGMT.RX32  rc ', 'mgmt.rx'), 32);
  assert.equal(P.capTagChunks('mgmt.fwd24', 'mgmt.rx'), 0);
  assert.equal(P.capTagChunks('mgmt.rxx mgmt.rx', 'mgmt.rx'), 0);
  assert.equal(P.capTagChunks('', 'mgmt.rx'), 0);
  assert.equal(P.capTagChunks(undefined, 'mgmt.rx'), 0);
  const S3 = 'mgmt.fwd24 mgmt.rx24', W = 'mgmt.fwd24';
  assert.equal(P.relayLongChunks(S3, W), 24);                        // an S3 target through any relay with today's firmware
  assert.equal(P.relayLongChunks(S3, S3), 24);
  assert.equal(P.relayLongChunks(W, W), 0);                          // a classic ESP32 target takes 16
  assert.equal(P.relayLongChunks(S3, ''), 0);                        // a relay from before: it forwards 16
  assert.equal(P.relayLongChunks(S3, undefined), 0);                 // a relay whose tags no dump has shown yet
  assert.equal(P.relayLongChunks(undefined, W), 0);                  // a target likewise
  assert.equal(P.relayLongChunks(S3, '', { relayForwardsAny: true }), 24);   // a WcbMgmt relay forwards any count
  assert.equal(P.relayLongChunks('mgmt.rx32', 'mgmt.fwd24'), 24);    // the smaller of the two
  assert.equal(P.relayLongChunks('mgmt.rx16', 'mgmt.fwd24'), 0);     // no more than one session anyway
});

const seqs = (n, len) => Array.from({ length: n }, (_, i) => `?SEQ,SAVE,s${i},;S1${'y'.repeat(len)}`);

test('planRelayPush: what fits one session is one session, in the order given (as before)', () => {
  const chain = ['?HW,23', '?MAC,3,07', '?LABEL,S5,Holo', '?MAP,PWM,S1,S5', '?EPASS,new_pw'].join('^');
  const p = P.planRelayPush(chain, { reboot: true });
  assert.equal(p.problem, '');
  assert.equal(p.sessions.length, 1);
  assert.equal(p.split, false);
  assert.equal(p.sessions[0].text, chain + '^?reboot');
  assert.ok(p.sessions[0].chunks.length <= 16);
  assert.deepEqual(P.planRelayPush('', {}).sessions, []);
});

test('planRelayPush: a push over one session goes as several, every command whole, the network and restart commands in the last', () => {
  const early = ['?HW,24', '?WCB,2', '?ALIAS,Dome', ...seqs(30, 160), '?LABEL,S5,Holo'];
  const late = ['?MAP,PWM,S1,S5', '?MAC,3,07', '?EPASS,a_new_mesh_password', '?PX5'];
  const chain = [early[0], early[1], late[0], early[2], ...early.slice(3, 20), late[1], ...early.slice(20), late[2], late[3]].join('^');
  const p = P.planRelayPush(chain, { reboot: true });
  assert.equal(p.problem, '');
  assert.ok(p.split && p.sessions.length >= 2, `${p.sessions.length} session(s)`);
  for (const s of p.sessions) {
    assert.ok(s.chunks.length <= 16, `a session of ${s.chunks.length} chunks`);
    assert.ok(s.cmds <= 150);
    assert.equal(s.chunks.join(''), s.text);
    assert.equal(s.list.join('^'), s.text);
    for (const c of s.chunks) assert.ok(bytes(c) <= 179);
  }
  // Every command once, the early ones in their order, the late ones (and the reboot) at the end of the last session.
  const all = p.sessions.flatMap((s) => s.list);
  assert.deepEqual(all, [...early, ...late, '?reboot']);
  const last = p.sessions[p.sessions.length - 1].list;
  for (const c of [...late, '?reboot']) assert.ok(last.includes(c), `${c} is not in the last session`);
  // A ?WCB,<n> that repeats the number (a full push sends it) is an ordinary command.
  assert.ok(p.sessions[0].list.includes('?WCB,2'));
});

test('planRelayPush: the command cap splits a push of many short commands', () => {
  const chain = Array.from({ length: 400 }, (_, i) => `?VAR,SET,v${i},${i}`).join('^');
  const p = P.planRelayPush(chain, {});
  assert.equal(p.problem, '');
  assert.ok(p.sessions.every((s) => s.cmds <= 150 && s.chunks.length <= 16));
  assert.equal(p.sessions.flatMap((s) => s.list).length, 400);
});

test('planRelayPush: a board-number change too long for one session goes in two steps: ?WCB,<new> first to the old number, the rest to the new', () => {
  const early = ['?HW,32', ...seqs(30, 160), '?LABEL,S5,Holo'];
  const chain = [early[0], '?WCB,5', ...early.slice(1), '?MAC,3,07', '?EPASS,a_new_mesh_password'].join('^');
  const p = P.planRelayPush(chain, { wcbChange: true, reboot: true });
  assert.equal(p.problem, '');
  assert.ok(p.split && p.sessions.length >= 2, `${p.sessions.length} session(s)`);
  assert.equal(p.sessions[0].list[0], '?WCB,5', 'the number change opens the first session');
  assert.equal(p.sessions[0].to, 'old');
  for (const s of p.sessions.slice(1)) assert.equal(s.to, 'new');
  for (const s of p.sessions) {
    assert.ok(s.chunks.length >= (s.to === 'new' ? 2 : 1) && s.chunks.length <= 16);
    assert.equal(s.chunks.join(''), s.text);
    assert.equal(s.list.join('^'), s.text);
  }
  // Every command once: the number change, the rest in their order, the network and restart commands last.
  assert.deepEqual(p.sessions.flatMap((s) => s.list), ['?WCB,5', ...early, '?MAC,3,07', '?EPASS,a_new_mesh_password', '?reboot']);
  assert.deepEqual(p.sessions[p.sessions.length - 1].list.slice(-3), ['?MAC,3,07', '?EPASS,a_new_mesh_password', '?reboot']);

  // A later session short enough for one chunk still goes as two: a WCB relay sends a one-chunk session as an ETM
  // unicast to the address of the number it names, which the board takes only when it restarts.
  // (Two commands a session here, so the ?reboot is a session of its own.)
  const short = P.planRelayPush('?WCB,5^?LABEL,S1,a^?LABEL,S2,b b^?LABEL,S3,c', { wcbChange: true, reboot: true, maxCmds: 2 });
  assert.equal(short.problem, '');
  assert.deepEqual(short.sessions.map((s) => [s.to, s.list]),
                   [['old', ['?WCB,5', '?LABEL,S1,a']], ['new', ['?LABEL,S2,b b', '?LABEL,S3,c']], ['new', ['?reboot']]]);
  assert.equal(short.sessions[0].chunks.length, 1, 'the first session as it is');
  for (const s of short.sessions.slice(1)) {
    assert.equal(s.chunks.length, 2);
    assert.equal(s.chunks.join(''), s.text);
    assert.ok(!/\s$/.test(s.chunks[0]), 'the first chunk does not end in whitespace');
  }

  // Through a WcbMgmt relay, which sends every chunk to that address: refused, nothing planned.
  const mgmt = P.planRelayPush(chain, { wcbChange: true, relayBroadcasts: false });
  assert.match(mgmt.problem, /board number/);
  assert.deepEqual(mgmt.sessions, []);

  // The same change in a push that fits one session goes as one, to the number the board has now, as before.
  const one = P.planRelayPush('?WCB,5^?LABEL,S1,x', { wcbChange: true, relayBroadcasts: false });
  assert.equal(one.sessions.length, 1);
  assert.equal(one.sessions[0].to, 'old');
  // A ?WCB,<n> that only repeats the number (no wcbChange) is an ordinary command: every session to the board as it is.
  const same = P.planRelayPush(chain, { reboot: true });
  assert.ok(same.sessions.every((s) => s.to === 'old'));
  assert.notEqual(same.sessions[0].list[0], '?WCB,5');
});

test('planRelayPush: one command longer than a session goes as a long session only where the board and relay take one', () => {
  const longSave = `?SEQ,SAVE,abcdefghijklmno,${'v'.repeat(3999)}`;   // 4025 bytes, 23 chunks
  const refused = P.planRelayPush(longSave, {});
  assert.match(refused.problem, /one command is 4025 bytes/);
  assert.deepEqual(refused.sessions, []);
  const ok = P.planRelayPush(longSave, { longChunks: 24 });
  assert.equal(ok.problem, '');
  assert.equal(ok.sessions.length, 1);
  assert.equal(ok.sessions[0].chunks.length, 23);
  assert.equal(ok.sessions[0].chunks.join(''), longSave);
  // Inside a longer push: the long command alone in its session, the rest around it.
  const mixed = P.planRelayPush(['?LABEL,S1,a', longSave, '?LABEL,S2,b'].join('^'), { longChunks: 24 });
  assert.equal(mixed.problem, '');
  assert.deepEqual(mixed.sessions.map((s) => s.list.length), [1, 1, 1]);
  assert.equal(mixed.sessions[1].text, longSave);
  // Longer than even a long session: refused.
  assert.match(P.planRelayPush(`?SEQ,SAVE,k,${'v'.repeat(5000)}`, { longChunks: 24 }).problem, /one command is/);
});

test('planRelayPush: another function identifier and delimiter', () => {
  const chain = Array.from({ length: 40 }, (_, i) => `!SEQ,SAVE,k${i},;S1${'z'.repeat(150)}`).join('#') + '#!MAC,2,05';
  const p = P.planRelayPush(chain, { funcChar: '!', delim: '#', reboot: true });
  assert.equal(p.problem, '');
  assert.ok(p.sessions.length >= 2);
  const last = p.sessions[p.sessions.length - 1].list;
  assert.deepEqual(last.slice(-2), ['!MAC,2,05', '!reboot']);
  assert.ok(p.sessions.every((s) => s.text === s.list.join('#')));
});
