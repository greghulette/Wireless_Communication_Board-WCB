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

test('capTagChunks / relayLongChunks: the long session needs the target\'s mgmt.rx and the relay\'s mgmt.fwd or mgmt.rx (WDP CAPTAGS)', () => {
  assert.equal(P.capTagChunks('mgmt.fwd24 mgmt.rx24', 'mgmt.rx'), 24);
  assert.equal(P.capTagChunks('mgmt.fwd24 mgmt.rx24', 'mgmt.fwd'), 24);
  assert.equal(P.capTagChunks('  MGMT.RX32  rc ', 'mgmt.rx'), 32);
  assert.equal(P.capTagChunks('mgmt.fwd24', 'mgmt.rx'), 0);
  assert.equal(P.capTagChunks('mgmt.rxx mgmt.rx', 'mgmt.rx'), 0);
  assert.equal(P.capTagChunks('', 'mgmt.rx'), 0);
  assert.equal(P.capTagChunks(undefined, 'mgmt.rx'), 0);
  // What the firmware sends (wcbOwnCapTags): the S3 takes 24 and so forwards 24 (mgmt.rx24); the classic forwards 24.
  const S3 = 'mgmt.rx24', W = 'mgmt.fwd24';
  assert.equal(P.relayLongChunks(S3, W), 24);                        // an S3 target through a classic relay
  assert.equal(P.relayLongChunks(S3, S3), 24);                       // ... through an S3 relay: rx24 says it forwards 24
  assert.equal(P.relayLongChunks(W, W), 0);                          // a classic ESP32 target takes 16
  assert.equal(P.relayLongChunks(W, S3), 0);
  assert.equal(P.relayLongChunks(S3, ''), 0);                        // a relay from before: it forwards 16
  assert.equal(P.relayLongChunks(S3, undefined), 0);                 // a relay whose tags no dump has shown yet
  assert.equal(P.relayLongChunks(undefined, W), 0);                  // a target likewise
  assert.equal(P.relayLongChunks(S3, '', { relayForwardsAny: true }), 24);   // a WcbMgmt relay forwards any count
  assert.equal(P.relayLongChunks('mgmt.rx32', W), 24);               // the smaller of the two
  assert.equal(P.relayLongChunks('mgmt.rx32', 'mgmt.fwd32 mgmt.rx24'), 32);  // both tags: a relay forwarding more
  assert.equal(P.relayLongChunks('mgmt.rx16', W), 0);                // no more than one session anyway
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
  assert.deepEqual(P.planRelayPush('', { reboot: true }).sessions.map((x) => x.text), ['?reboot']);
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

test('planRelayPush: a board-number change too long for one session goes in two steps with a restart between: ?WCB,<new> and ?reboot to the old number, then the rest to the new', () => {
  const early = ['?HW,32', ...seqs(30, 160), '?LABEL,S5,Holo'];
  const late = ['?MAC,3,07', '?EPASS,a_new_mesh_password'];
  const chain = [early[0], '?WCB,5', ...early.slice(1), ...late].join('^');
  const asked = [];
  const p = P.planRelayPush(chain, { wcbChange: true, reboot: true, restReboot: (rest) => { asked.push(rest); return true; } });
  assert.equal(p.problem, '');
  assert.ok(p.split && p.renumber);
  // Step 1: the number change and the restart, one session to the number the board has now.
  assert.deepEqual(p.sessions[0].list, ['?WCB,5', '?reboot']);
  assert.equal(p.sessions[0].to, 'old');
  assert.equal(p.sessions[0].restart, true);
  assert.equal(p.sessions[0].chunks.length, 1);
  // Step 2: the rest as a push of its own, to the new number, two chunks a session at least, the late ones last.
  const rest = p.sessions.slice(1);
  assert.ok(rest.length >= 2, `${rest.length} session(s) after the restart`);
  for (const s of rest) {
    assert.equal(s.to, 'new');
    assert.equal(s.restart, false);
    assert.ok(s.chunks.length >= 2 && s.chunks.length <= 16, `a session of ${s.chunks.length} chunks`);
    assert.equal(s.chunks.join(''), s.text);
    assert.equal(s.list.join('^'), s.text);
  }
  assert.deepEqual(rest.flatMap((s) => s.list), [...early, ...late, '?reboot']);
  assert.deepEqual(rest[rest.length - 1].list.slice(-3), [...late, '?reboot']);
  assert.deepEqual(asked, [[...early, ...late].join('^')], 'restReboot is asked about the rest, without the ?WCB');

  // The rest gets a ?reboot only if it needs one of its own (here it does not); then it fits one session - still two
  // chunks: a relay sends a one-chunk session as a unicast, which needs the new number in its peer table first.
  const small = P.planRelayPush(['?WCB,5', '?LABEL,S1,a b', ...seqs(16, 160)].join('^'),
                                { wcbChange: true, reboot: true, restReboot: () => false });
  assert.equal(small.problem, '');
  assert.equal(small.sessions[0].restart, true);
  assert.ok(small.sessions.slice(1).every((s) => s.to === 'new' && !s.list.includes('?reboot')));
  const tiny = P.planRelayPush('?WCB,5^?LABEL,S1,a b^?LABEL,S2,c', { wcbChange: true, maxCmds: 2 });
  assert.deepEqual(tiny.sessions.map((s) => [s.to, s.restart, s.list]),
                   [['old', true, ['?WCB,5', '?reboot']], ['new', false, ['?LABEL,S1,a b', '?LABEL,S2,c']]]);
  assert.equal(tiny.sessions[1].chunks.length, 2);
  assert.equal(tiny.sessions[1].chunks.join(''), tiny.sessions[1].text);
  assert.ok(!/\s$/.test(tiny.sessions[1].chunks[0]), 'the first chunk does not end in whitespace');

  // The rest has a command no relay session carries: refused before anything, as any push.
  const long = P.planRelayPush(['?WCB,5', ...seqs(17, 160), `?SEQ,SAVE,k,${'v'.repeat(3999)}`].join('^'), { wcbChange: true });
  assert.match(long.problem, /one command is/);
  assert.deepEqual(long.sessions, []);

  // The same change in a push that fits one session goes as one, to the number the board has now, as before.
  const one = P.planRelayPush('?WCB,5^?LABEL,S1,x', { wcbChange: true, reboot: true });
  assert.equal(one.sessions.length, 1);
  assert.equal(one.renumber, false);
  assert.deepEqual([one.sessions[0].to, one.sessions[0].restart], ['old', false]);
  assert.equal(one.sessions[0].text, '?WCB,5^?LABEL,S1,x^?reboot');
  // A ?WCB,<n> that only repeats the number (no wcbChange) is an ordinary command: no restart, every session as is.
  const same = P.planRelayPush(chain, { reboot: true });
  assert.ok(same.sessions.every((s) => s.to === 'old' && !s.restart) && !same.renumber);
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
