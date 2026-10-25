// Wizard/parser.js on its own - no browser, no board. `node --test` (npm run unit), and wizard.parser in the harness.
//
// The windowed relay OTA (otaRelayStream / otaRelayFinish, docs/WCB_OTA_TECHNICAL.md §4) against a simulated relay and
// target on a virtual clock: the relay's input queue (a WCB's WebSocket queue holds 6 lines, a serial RX ring ~28),
// one line at a time through the relay, the target's 12-deep OTA queue and its write cursor (otaWrite: only the frame
// at the cursor is written, every frame is answered with the cursor - handleOtaDataPacket, WCB_OTA.cpp), and FIFO
// delivery on every hop. Faults are injected per line or per ACK. Every run checks the image the target wrote, byte
// for byte, and every line's CRC-32 with a CRC written independently here.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const P = require('../../../Wizard/parser.js');

// Reflected CRC-32, poly 0xEDB88320, bit by bit - deliberately not parser.js's table, so a slip in either shows.
function crcRef(str) {
  let c = 0xFFFFFFFF;
  for (const b of Buffer.from(str, 'latin1')) {
    c ^= b;
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xEDB88320 & -(c & 1));
  }
  return (~c) >>> 0;
}

const image = (n) => Uint8Array.from({ length: n }, (_, i) => (i * 7 + (i >> 8) * 13) & 0xFF);

// The relay line grammar the firmware parses (processOtaRelayCommand): ?OTA,DATA,<t>,<s>,<offset>[:<crc32>],<b64>.
const DATA_RE = /^\?OTA,DATA,(\d+),(\d+),(\d+):([0-9A-F]{8}),([A-Za-z0-9+/=]+)$/;

// opts: hostMs / ackMs one-way latency to and from the relay; lineMs serialisation per line on a serial link (0 for a
// WebSocket); relayMs the relay's time per line (one per loop() pass); relayDepth its input queue; radioMs relay ->
// target; targetMs per frame; targetDepth the target's OTA queue. dropLine(i, off) / dropAck(j, ack) lose line i / ACK
// j; stall(i) adds a delay before the target processes frame i; extra(j, ack) returns ACKs delivered right after ACK j;
// collapse(i) makes the target lose its session at frame i (status = what it answers: 0 like firmware before
// 2026-08-06, 1 since).
function sim(total, opts = {}) {
  const o = { target: 2, session: 77, hostMs: 2, ackMs: 2, lineMs: 0, relayMs: 1, relayDepth: Infinity, radioMs: 2,
              targetMs: 1, targetDepth: 12, ...opts };
  const want = image(total);
  const written = new Uint8Array(total);
  const t = { now: 0, linkFree: 0, relayFree: 0, targetFree: 0, cursor: 0, alive: true,
              relayStarts: [], targetStarts: [], acks: [], lines: [], ackCount: 0,
              relayOverflows: 0, targetOverflows: 0, crcBad: 0, maxOutstanding: 0, answered: 0 };
  const ack = (at, offset, status = 0, src = o.target, session = o.session) => t.acks.push({ at, a: { src, session, offset, status } });

  const io = {
    now: () => t.now,
    sleep: async (ms) => { t.now += ms; },
    log: () => {},
    async send(line) {
      const i = t.lines.length;
      t.lines.push(line);
      const m = DATA_RE.exec(line);
      if (!m) {
        // END: verified when the whole image is there and right; answered with the offset 0 the firmware gives once
        // the session has ended (otaWrittenOffset after otaEnd).
        const e = /^\?OTA,END,(\d+),(\d+)$/.exec(line);
        if (e) {
          const ok = t.alive && t.cursor === total && Buffer.compare(Buffer.from(written), Buffer.from(want)) === 0;
          ack(Math.max(t.now, t.targetFree) + o.hostMs + o.ackMs + 50, ok ? 0 : t.cursor, ok ? 0 : 1);
        }
        return;
      }
      const [, tgt, ses, off, crc, b64] = m;
      assert.equal(+tgt, o.target);
      assert.equal(+ses, o.session);
      if (crcRef(`${off},${b64}`) !== parseInt(crc, 16)) t.crcBad++;
      const outstanding = i + 1 - t.answered;   // sent, minus answered so far (lost ones stay counted)
      if (outstanding > t.maxOutstanding) t.maxOutstanding = outstanding;
      if (o.dropLine?.(i, +off)) return;
      // Host -> relay.
      let arrive = t.now + o.hostMs;
      if (o.lineMs) { arrive = Math.max(arrive, t.linkFree) + o.lineMs; t.linkFree = arrive; }
      const waiting = t.relayStarts.filter((s) => s > arrive).length;
      if (waiting >= o.relayDepth) { t.relayOverflows++; return; }
      const start = Math.max(arrive, t.relayFree);
      t.relayFree = start + o.relayMs;
      t.relayStarts.push(start);
      // Relay -> target.
      const tArrive = t.relayFree + o.radioMs;
      if (t.targetStarts.filter((s) => s > tArrive).length >= o.targetDepth) { t.targetOverflows++; return; }
      const tStart = Math.max(tArrive, t.targetFree) + (o.stall?.(i) ?? 0);
      t.targetFree = tStart + o.targetMs;
      t.targetStarts.push(tStart);
      const data = Buffer.from(b64, 'base64');
      let status = 0;
      if (o.collapse?.(i)) { t.alive = false; t.cursor = 0; status = o.collapse(i).status; }
      else if (!t.alive) status = 1;
      else if (+off === t.cursor) { written.set(data, +off); t.cursor += data.length; }
      const a = { src: o.target, session: o.session, offset: t.alive ? t.cursor : 0, status };
      const j = t.ackCount++;
      if (o.dropAck?.(j, a)) return;
      t.acks.push({ at: t.targetFree + o.ackMs, a });
      for (const x of o.extra?.(j, a) ?? []) t.acks.push({ at: t.targetFree + o.ackMs, a: x });
    },
    async nextAck(ms) {
      t.acks.sort((x, y) => x.at - y.at);
      const first = t.acks[0];
      if (first && first.at <= t.now + ms) {
        t.acks.shift();
        if (first.at > t.now) t.now = first.at;
        if (first.a.src === o.target && first.a.session === o.session) t.answered++;
        return first.a;
      }
      t.now += ms;
      return null;
    },
  };
  return { io, t, want, written, o };
}

const run = async (total, simOpts, streamOpts) => {
  const s = sim(total, simOpts);
  const r = await P.otaRelayStream(s.want, s.io, { target: s.o.target, session: s.o.session, prefix: '?OTA,', ...streamOpts });
  return { ...s, r };
};
const frames = (total) => Math.ceil(total / 192);
const intact = (s) => assert.deepEqual(Buffer.from(s.written), Buffer.from(s.want), 'the target wrote the image exactly');

test('the CRC suffix: CRC-32 of "<offset>,<b64>" as sent, 8 upper-case hex digits on the offset field', () => {
  assert.equal(P.crc32('123456789'), 0xCBF43926, 'the standard check value');
  const line = P.otaRelayDataLine('?OTA,', 2, 77, 384, Uint8Array.from([0, 1, 2, 250, 255]));
  const m = DATA_RE.exec(line);
  assert.ok(m, line);
  assert.equal(m[3], '384');
  assert.equal(parseInt(m[4], 16), crcRef(`384,${m[5]}`));
  assert.deepEqual([...Buffer.from(m[5], 'base64')], [0, 1, 2, 250, 255]);
  // A relay predating the check reads the offset with String::toInt(), which stops at the ':'.
  assert.equal(parseInt('384:0A1B2C3D', 10), 384);
  // The function identifier rides in the prefix; the CRC never covers it.
  assert.ok(P.otaRelayDataLine('!OTA,', 2, 77, 0, Uint8Array.of(1)).startsWith('!OTA,DATA,2,77,0:'));
});

test('a clean transfer on each link: every frame once, in order, every CRC right, never more in flight than the window', async () => {
  for (const [link, { window, paceMs }] of Object.entries(P.OTA_RELAY_LINKS)) {
    const total = 50 * 192 + 77;
    const s = await run(total, link === 'usb' ? { lineMs: 25.2, relayDepth: 28 } : { hostMs: 35, ackMs: 35, relayDepth: 6 },
                        { window, paceMs });
    intact(s);
    assert.equal(s.r.frames, frames(total), `${link}: no frame sent twice`);
    assert.deepEqual([s.r.rewinds, s.r.timeouts, s.t.crcBad, s.t.relayOverflows, s.t.targetOverflows], [0, 0, 0, 0, 0], link);
    assert.ok(s.t.maxOutstanding <= window, `${link}: ${s.t.maxOutstanding} in flight`);
    const offs = s.t.lines.map((l) => +DATA_RE.exec(l)[3]);
    assert.deepEqual(offs, Array.from({ length: frames(total) }, (_, i) => i * 192));
  }
});

test('the windows fit the relays: a WCB\'s WebSocket queue (WS_QUEUE_DEPTH) and the target\'s OTA queue hold a whole window', () => {
  const src = (f) => fs.readFileSync(path.join(__dirname, '../../../Code/WCB', f), 'utf8');
  const wsDepth = +/WS_QUEUE_DEPTH\s*=\s*(\d+)/.exec(src('WCB_WS.cpp'))[1];
  const otaDepth = +/OTA_PKT_QUEUE_DEPTH\s*=\s*(\d+)/.exec(src('WCB_OTA.cpp'))[1];
  // One line may still sit in the queue after a rewind, and an ABORT must find room.
  assert.ok(P.OTA_RELAY_LINKS.ws.window <= wsDepth - 2, `ws window ${P.OTA_RELAY_LINKS.ws.window}, WS_QUEUE_DEPTH ${wsDepth}`);
  for (const [k, v] of Object.entries(P.OTA_RELAY_LINKS)) assert.ok(v.window < otaDepth, `${k} window under the OTA queue (${otaDepth})`);
  // A serial DATA line (~290 B) at 115200 8N1 takes 25.2 ms: the USB pace is the wire's.
  assert.ok(P.OTA_RELAY_LINKS.usb.paceMs >= 25);
});

test('a slow relay (20 ms a line, slower than the pace) never overflows a 6-line queue with the WebSocket window; a 12-frame window would', async () => {
  const slow = { hostMs: 35, ackMs: 35, relayDepth: 6, relayMs: 20 };
  const ok = await run(40 * 192, slow, P.OTA_RELAY_LINKS.ws);
  intact(ok);
  assert.equal(ok.t.relayOverflows, 0);
  // The simulation does catch an overflow: the same relay with an oversized window and no pace.
  const over = await run(40 * 192, slow, { window: 12, paceMs: 0 });
  intact(over);
  assert.ok(over.t.relayOverflows > 0, 'an oversized window overflows the queue (and still recovers)');
});

test('a lost DATA line: one rewind to the cursor once every frame behind it has answered, and no more than a window resent', async () => {
  for (const at of [0, 7, 20, 49]) {   // first frame, mid-window, later, the very last frame
    const total = 50 * 192;
    let lost = false;
    const s = await run(total, { dropLine: (i, off) => (off === at * 192 && !lost ? (lost = true) : false) }, P.OTA_RELAY_LINKS.usb);
    intact(s);
    assert.ok(s.r.frames - frames(total) <= P.OTA_RELAY_LINKS.usb.window, `lost ${at}: ${s.r.frames - frames(total)} resent`);
    // The last frame has nothing behind it to answer: that one is found by the ACK timeout and a probe.
    if (at < 49) assert.deepEqual([s.r.rewinds, s.r.timeouts], [1, 0], `lost ${at}`);
    else assert.deepEqual([s.r.timeouts, s.r.frames - frames(total)], [1, 1], 'lost last frame: one probe');
  }
});

test('a line the relay drops for a bad CRC (a serial RX overflow) is an ordinary loss: resent, image intact', async () => {
  // The simulated relay counts CRC mismatches; here it drops the line as the firmware does (WCB_OTA.cpp: DROPPED).
  let n = 0;
  const s = await run(30 * 192, { dropLine: (i, off) => off === 10 * 192 && n++ === 0 }, P.OTA_RELAY_LINKS.ws);
  intact(s);
  assert.equal(s.r.rewinds, 1);
});

test('a lost ACK costs nothing: the next ACK carries the cursor past it', async () => {
  const total = 40 * 192;
  const s = await run(total, { dropAck: (j) => j === 5 || j === 6 || j === 30 }, P.OTA_RELAY_LINKS.usb);
  intact(s);
  assert.deepEqual([s.r.frames, s.r.rewinds, s.r.timeouts], [frames(total), 0, 0]);
});

test('duplicate, stale and foreign ACKs are skipped: another session, another target, a repeated and an older cursor', async () => {
  const total = 40 * 192;
  const s = await run(total, {
    extra: (j, a) => (j % 5 === 0 ? [
      { ...a, session: 78 },                       // another transfer through the same relay
      { ...a, src: 3, offset: 0, status: 1 },      // another target, even with an error status
      { ...a },                                    // the same ACK twice
      { ...a, offset: Math.max(0, a.offset - 384) },   // an older cursor
    ] : []),
  }, P.OTA_RELAY_LINKS.usb);
  intact(s);
  assert.equal(s.r.timeouts, 0);
  assert.ok(s.r.frames - frames(total) <= P.OTA_RELAY_LINKS.usb.window, `${s.r.frames - frames(total)} resent`);
});

test('a target busy past the ACK timeout gets ONE probe, not a window of duplicates that would echo through the rest of the image', async () => {
  for (const link of ['usb', 'ws']) {
    const total = 60 * 192;
    const s = await run(total, { stall: (i) => (i === 12 ? 3500 : 0), hostMs: 20, ackMs: 20 }, P.OTA_RELAY_LINKS[link]);
    intact(s);
    assert.equal(s.r.timeouts, 1, link);
    assert.equal(s.r.rewinds, 0, `${link}: late frames are not lost frames`);
    assert.equal(s.r.frames, frames(total) + 1, `${link}: the probe is the only duplicate`);
  }
});

test('a whole window lost: the probe, one short wait, and the window resent from the cursor', async () => {
  const total = 40 * 192;
  const s = await run(total, { dropLine: (i) => i >= 16 && i < 24 }, P.OTA_RELAY_LINKS.usb);   // a burst: 8 lines in a row
  intact(s);
  assert.equal(s.r.timeouts, 1);
  assert.ok(s.r.frames - frames(total) <= P.OTA_RELAY_LINKS.usb.window + 1, `${s.r.frames - frames(total)} resent`);
});

test('the fast-fails: a non-zero status stops at once; a cursor back at 0 after progress stops with "lost the session"', async () => {
  const total = 40 * 192;
  // Firmware since 2026-08-06: the frame whose write ends the session answers OTA_ST_ERR.
  await assert.rejects(run(total, { collapse: (i) => (i === 10 ? { status: 1 } : null) }, P.OTA_RELAY_LINKS.usb),
                       /target lost the OTA session at \d+ bytes|target rejected OTA data near \d+ bytes/);
  // Status 1 while the cursor has not moved yet: the status message.
  await assert.rejects(run(total, { collapse: (i) => (i === 0 ? { status: 1 } : null) }, P.OTA_RELAY_LINKS.usb),
                       /^Error: target rejected OTA data near 0 bytes \(write error or lost session\) — retry the OTA$/);
  // Firmware before it: OK with the cursor at 0 - the collapse check, which also wins over the status.
  await assert.rejects(run(total, { collapse: (i) => (i === 10 ? { status: 0 } : null) }, P.OTA_RELAY_LINKS.usb),
                       /target lost the OTA session at \d+ bytes \(it likely timed out or rebooted\) — retry the OTA/);
});

test('silence: a relay or target that answers nothing stops after the idle cap, inside the target\'s 30 s session timeout', async () => {
  const s = sim(20 * 192, { dropLine: () => true });
  await assert.rejects(P.otaRelayStream(s.want, s.io, { target: 2, session: 77, ...P.OTA_RELAY_LINKS.ws }),
                       /OTA stalled at 0 \(no progress after retries\)/);
  assert.ok(s.t.now < 30000, `gave up after ${Math.round(s.t.now)} ms`);
});

test('ACKs that keep coming without moving the cursor (every resend of one frame lost) stop at the rewind cap', async () => {
  const s = sim(20 * 192, { dropLine: (i, off) => off === 5 * 192 });
  await assert.rejects(P.otaRelayStream(s.want, s.io, { target: 2, session: 77, ...P.OTA_RELAY_LINKS.usb }),
                       /OTA stalled at 960/);
});

test('END: stale DATA cursor ACKs still in flight are skipped; the verdict is status != 0, or status 0 with offset 0', async () => {
  const total = 10 * 192;
  const s = await run(total, {}, P.OTA_RELAY_LINKS.usb);
  // Leftover cursor ACKs (a duplicate's answer) queued ahead of the verdict.
  s.t.acks.push({ at: s.t.now, a: { src: 2, session: 77, offset: total, status: 0 } },
                { at: s.t.now, a: { src: 2, session: 99, offset: 0, status: 0 } });
  const v = await P.otaRelayFinish(s.io, { target: 2, session: 77, prefix: '?OTA,', timeoutMs: 12000 });
  assert.deepEqual(v, { src: 2, session: 77, offset: 0, status: 0 });
  assert.equal(s.t.lines.at(-1), '?OTA,END,2,77');
  // A failed verify (here: nothing written) answers ERR; no answer at all is null.
  const bad = sim(total);
  const failed = await P.otaRelayFinish(bad.io, { target: 2, session: 77, timeoutMs: 12000 });
  assert.equal(failed.status, 1);
  const quiet = sim(total);
  quiet.io.send = async () => {};
  assert.equal(await P.otaRelayFinish(quiet.io, { target: 2, session: 77, timeoutMs: 3000 }), null);
});
